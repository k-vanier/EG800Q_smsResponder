#!/usr/bin/env python3
# Answers every incoming SMS with Claude, in French: each SMS is a message in
# a conversation with its sender, the reply is Claude Code's answer, cut to
# fit in a single SMS (160 characters).
# Runs on Linux or FreeBSD next to the EG800Q-EU and calls the Claude Code CLI
# (claude -p). Claude Code has no FreeBSD build: there, --ssh runs it on a
# Linux machine, or --claude points to a binary that runs through Linux
# binary compatibility.
#
# A conversation is all the SMS from one number until it stays silent for
# --conversation-timeout minutes. Its first SMS must start with "Bonjour" or
# "Bonsoir": otherwise the sender only gets POLITENESS_REPLY, without Claude.
#
# At startup, every SMS already stored in the module is deleted without answer.
# After that, every SMS is deleted once handled, answered or not.
#
# Usage: python3 sms_claude.py PORT [--allow +336...] [--model NAME]
#                              [--conversation-timeout MINUTES]
#                              [--claude PATH | --ssh HOST] [--trace]
#   PORT: /dev/ttyUSB0 on Linux, /dev/cuaU0 on FreeBSD
# Auth: export CLAUDE_CODE_OAUTH_TOKEN=... (from "claude setup-token")
#       or ANTHROPIC_API_KEY=... With --ssh, the token is set on the remote
#       machine, where claude runs.
# Needs pyserial, and the claude CLI in PATH (or --claude, or --ssh).

import argparse
import datetime
import os
import re
import shlex
import subprocess
import tempfile
import time
import unicodedata

from eg800q import Modem, fit_sms, is_phone_number, log

CLAUDE_TIMEOUT = 120  # seconds
MAX_TURNS = 5  # enough for a few web searches before the answer
MAX_HISTORY = 10  # previous exchanges of the conversation given to Claude
# With --ssh: shell file on the remote machine that exports CLAUDE_CODE_OAUTH_TOKEN
REMOTE_ENV = '"$HOME/.config/sms_claude.env"'
GREETINGS = ('bonjour', 'bonsoir')
ERROR_REPLY = 'Désolé, je ne peux pas répondre pour le moment.'
POLITENESS_REPLY = ('La politesse avant tout : commencez par « Bonjour » '
	'et je répondrai avec plaisir à votre message.')
DAYS = ('lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche')
MONTHS = ('janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août',
	'septembre', 'octobre', 'novembre', 'décembre')

SYSTEM_PROMPT = """Tu réponds à des SMS. Ta réponse est renvoyée telle quelle dans un seul SMS.
- Réponds toujours en français, quelle que soit la langue du message.
- 150 caractères au maximum, en texte brut : pas de markdown, pas de liste, pas d'émoji.
- Une ou deux phrases, sans lien internet.
- Tu peux faire une recherche sur internet quand la question porte sur l'actualité, la météo,
  des horaires ou d'autres faits récents. Tu ne peux rien faire d'autre. Si tu ne sais pas, dis-le brièvement.
- Si la conversation a déjà commencé, ses échanges précédents sont donnés avant le nouveau message.
Date et heure actuelles : {now}."""


def is_greeting(text):
	plain = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower()
	return re.match(r'\W*(%s)\b' % '|'.join(GREETINGS), plain) is not None


def build_prompt(text, history):
	if not history:
		return text
	lines = ['Échanges précédents de la conversation :']
	for question, answer in history:
		lines += ['Correspondant : ' + question, 'Toi : ' + answer]
	lines += ['', 'Nouveau message :', text]
	return '\n'.join(lines)


def ask_claude(text, history, model, claude='claude', ssh=None):
	now = datetime.datetime.now()
	now = '%s %d %s %d, %s' % (DAYS[now.weekday()], now.day, MONTHS[now.month - 1], now.year, now.strftime('%H:%M'))
	cmd = [claude, '-p',
		'--system-prompt', SYSTEM_PROMPT.format(now=now),
		# Anyone who knows the number can send a prompt, so the only tool is
		# WebSearch: it runs on Anthropic's side. No WebFetch, which would load
		# pages from this machine and could reach the local network.
		'--tools', 'WebSearch',
		'--allowedTools', 'WebSearch',
		'--disallowedTools', 'mcp__*',
		'--strict-mcp-config',
		'--no-session-persistence',
		'--max-turns', str(MAX_TURNS),
		'--output-format', 'text']
	if model:
		cmd += ['--model', model]
	if ssh:
		# A command run by ssh gets neither ~/.local/bin (where the installer
		# puts claude) in PATH nor the token exported in an interactive shell:
		# the token is read from REMOTE_ENV if that file exists. Same
		# empty-directory rule there. ssh joins its arguments into one shell
		# command line, so each one is quoted.
		remote = ('PATH="$HOME/.local/bin:$PATH"; [ -f %s ] && . %s; '
			'd=$(mktemp -d) || exit 1; cd "$d" || exit 1; %s; r=$?; cd /; rmdir "$d"; exit $r'
			% (REMOTE_ENV, REMOTE_ENV, shlex.join(cmd)))
		cmd = ['ssh', '-T', '-o', 'BatchMode=yes', ssh, remote]
	# Empty working directory so no project file or CLAUDE.md is picked up;
	# the message goes through stdin so it is never parsed as an option.
	with tempfile.TemporaryDirectory() as cwd:
		result = subprocess.run(cmd, input=build_prompt(text, history), capture_output=True,
			encoding='utf-8', errors='replace', timeout=CLAUDE_TIMEOUT, cwd=cwd)
	if result.returncode != 0:
		raise RuntimeError('claude exited with %d: %s' % (result.returncode, result.stderr.strip()[:300]))
	return result.stdout.strip()


class Responder:
	def __init__(self, send_sms, allow, conversation_timeout, ask):
		self.send_sms = send_sms
		self.allow = allow
		self.timeout = conversation_timeout  # minutes
		self.ask = ask  # ask(text, history) -> Claude's answer
		# Number -> {'last': time of its last answered SMS, 'history': [(message, reply)]}
		self.conversations = {}

	def _send(self, sender, reply):
		log('sending to %s: %s' % (sender, reply))
		self.send_sms(sender, reply)
		log('sent to %s' % sender)

	def on_sms(self, sender, text):
		if not is_phone_number(sender) or not text:
			return
		if self.allow and sender not in self.allow:
			log('ignored, %s is not in --allow' % sender)
			return
		conversation = self.conversations.get(sender)
		if conversation and time.time() - conversation['last'] > self.timeout * 60:
			log('conversation with %s ended after %g min of silence' % (sender, self.timeout))
			del self.conversations[sender]
			conversation = None
		if conversation is None and not is_greeting(text):
			log('no greeting from %s, asking for politeness' % sender)
			self._send(sender, fit_sms(POLITENESS_REPLY))
			return
		history = conversation['history'] if conversation else []
		try:
			answer = fit_sms(self.ask(text, history))
		except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
			log('claude error: %s' % e)
			answer = ''
		self._send(sender, answer or ERROR_REPLY)
		if conversation is None:
			log('conversation with %s started' % sender)
			conversation = self.conversations[sender] = {'history': []}
		conversation['last'] = time.time()
		if answer:
			conversation['history'] = (conversation['history'] + [(text, answer)])[-MAX_HISTORY:]


def main():
	parser = argparse.ArgumentParser(description='Answers incoming SMS with Claude Code, in French.')
	parser.add_argument('port', help='serial port of the module: /dev/ttyUSB0 on Linux, /dev/cuaU0 on FreeBSD')
	parser.add_argument('--allow', action='append', metavar='NUMBER',
		help='only answer this number (repeatable, e.g. +33612345678); default: any phone number')
	parser.add_argument('--model', help='Claude model alias or name passed to claude --model')
	parser.add_argument('--conversation-timeout', type=float, default=30, metavar='MINUTES',
		help='a conversation ends after this many minutes without SMS from its number (default: 30)')
	parser.add_argument('--trace', action='store_true', help='log every byte exchanged with the module')
	parser.add_argument('--claude', default='claude', metavar='PATH',
		help='claude executable, on the remote machine with --ssh (default: claude, found in PATH)')
	parser.add_argument('--ssh', metavar='HOST',
		help='run claude on this Linux machine through ssh (key login; token in ~/.config/sms_claude.env there)')
	args = parser.parse_args()

	if not args.ssh and not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') and not os.environ.get('ANTHROPIC_API_KEY'):
		log('warning: neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set')

	modem = Modem(args.port, args.trace)
	log('purged %d SMS already in memory' % modem.purge_sms())
	responder = Responder(modem.send_sms, args.allow, args.conversation_timeout,
		lambda text, history: ask_claude(text, history, args.model, args.claude, args.ssh))
	log('ready on %s, waiting for SMS' % args.port)
	modem.serve(responder.on_sms)


if __name__ == '__main__':
	try:
		main()
	except KeyboardInterrupt:
		pass
