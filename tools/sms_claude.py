#!/usr/bin/env python3
# Answers every incoming SMS with Claude, in French: each SMS is a message in
# a conversation with its sender, the reply is Claude Code's answer, cut to
# fit in a single SMS (160 characters).
# Runs on Linux next to the EG800Q-EU and calls the Claude Code CLI (claude -p).
#
# A conversation is all the SMS from one number until it stays silent for
# --conversation-timeout minutes. Its first SMS must start with "Bonjour" or
# "Bonsoir": otherwise the sender only gets POLITENESS_REPLY, without Claude.
#
# At startup, every SMS already stored in the module is deleted without answer.
# After that, every SMS is deleted once handled, answered or not.
#
# Usage: python3 sms_claude.py /dev/ttyUSB0 [--allow +336...] [--model NAME]
#                              [--conversation-timeout MINUTES]
# Auth: export CLAUDE_CODE_OAUTH_TOKEN=... (from "claude setup-token")
#       or ANTHROPIC_API_KEY=...
# Needs pyserial and the claude CLI in PATH.

import argparse
import datetime
import os
import re
import subprocess
import tempfile
import time
import unicodedata

from eg800q import Modem, fit_sms, is_phone_number, log

CLAUDE_TIMEOUT = 120  # seconds
MAX_HISTORY = 10  # previous exchanges of the conversation given to Claude
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
- Une ou deux phrases.
- Tu n'as ni outils ni accès à internet. Si tu ne sais pas, dis-le brièvement.
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


def ask_claude(text, history, model):
	now = datetime.datetime.now()
	now = '%s %d %s %d, %s' % (DAYS[now.weekday()], now.day, MONTHS[now.month - 1], now.year, now.strftime('%H:%M'))
	cmd = ['claude', '-p',
		'--system-prompt', SYSTEM_PROMPT.format(now=now),
		# No tools at all: anyone who knows the number can send a prompt
		'--tools', '',
		'--disallowedTools', 'mcp__*',
		'--strict-mcp-config',
		'--no-session-persistence',
		'--max-turns', '1',
		'--output-format', 'text']
	if model:
		cmd += ['--model', model]
	# Empty working directory so no project file or CLAUDE.md is picked up;
	# the message goes through stdin so it is never parsed as an option.
	with tempfile.TemporaryDirectory() as cwd:
		result = subprocess.run(cmd, input=build_prompt(text, history), capture_output=True,
			encoding='utf-8', errors='replace', timeout=CLAUDE_TIMEOUT, cwd=cwd)
	if result.returncode != 0:
		raise RuntimeError('claude exited with %d: %s' % (result.returncode, result.stderr.strip()[:300]))
	return result.stdout.strip()


class Responder:
	def __init__(self, send_sms, allow, model, conversation_timeout):
		self.send_sms = send_sms
		self.allow = allow
		self.model = model
		self.timeout = conversation_timeout  # minutes
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
			answer = fit_sms(ask_claude(text, history, self.model))
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
	parser.add_argument('port', help='serial port of the module, e.g. /dev/ttyUSB0')
	parser.add_argument('--allow', action='append', metavar='NUMBER',
		help='only answer this number (repeatable, e.g. +33612345678); default: any phone number')
	parser.add_argument('--model', help='Claude model alias or name passed to claude --model')
	parser.add_argument('--conversation-timeout', type=float, default=30, metavar='MINUTES',
		help='a conversation ends after this many minutes without SMS from its number (default: 30)')
	args = parser.parse_args()

	if not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') and not os.environ.get('ANTHROPIC_API_KEY'):
		log('warning: neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set')

	modem = Modem(args.port)
	log('purged %d SMS already in memory' % modem.purge_sms())
	responder = Responder(modem.send_sms, args.allow, args.model, args.conversation_timeout)
	log('ready on %s, waiting for SMS' % args.port)
	modem.serve(responder.on_sms)


if __name__ == '__main__':
	try:
		main()
	except KeyboardInterrupt:
		pass
