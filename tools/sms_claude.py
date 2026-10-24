#!/usr/bin/env python3
# Answers every incoming SMS with Claude, in French: each SMS is a message in
# a conversation with its sender, the reply is Claude Code's answer, cut to
# fit in a single SMS (160 characters).
# Runs on FreeBSD (or Linux) next to the EG800Q-EU and calls the Claude Code
# CLI (claude -p), with web search as its only tool.
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
#                              [--claude PATH] [--sms-log FILE] [--trace]
#   PORT: usb (module's USB port), auto (FTDI adapter), /dev/cuaU0, /dev/ttyUSB0
# Auth: export CLAUDE_CODE_OAUTH_TOKEN=... (from "claude setup-token")
#       or ANTHROPIC_API_KEY=...
# Needs pyserial, and the claude CLI in PATH (or --claude).

import argparse
import datetime
import os
import re
import subprocess
import tempfile
import time
import unicodedata

from eg800q import Modem, fit_sms, is_phone_number, log, split_sms

CLAUDE_TIMEOUT = 120  # seconds
MAX_TURNS = 5  # enough for a few web searches before the answer
MAX_HISTORY = 10  # previous exchanges of the conversation given to Claude
GREETINGS = ('bonjour', 'bonsoir')
ERROR_REPLY = 'Désolé, je ne peux pas répondre pour le moment.'
POLITENESS_REPLY = ('La politesse avant tout : commencez par « Bonjour » '
	'et je répondrai avec plaisir à votre message.')
DAYS = ('lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche')
MONTHS = ('janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août',
	'septembre', 'octobre', 'novembre', 'décembre')

BOT_NAME = 'Robert3000'
# How the bot introduces itself in the first answer of a conversation
INTRO_TEXT = 'yo je suis Robert3000 le bot stylé qui repond à toutes tes interogration'

SYSTEM_PROMPT = """Tu t'appelles """ + BOT_NAME + """. Tu réponds à des SMS. Ta réponse est renvoyée telle quelle par SMS.
- Réponds toujours en français, quelle que soit la langue du message.
- Elle doit tenir dans un SMS : 150 caractères. Si la question l'exige vraiment, deux SMS
  au plus : 300 caractères au maximum, jamais plus. Texte brut : pas de markdown, pas de
  liste, pas d'émoji, pas de lien internet.
- Tu peux faire une recherche sur internet quand la question porte sur l'actualité, la météo,
  des horaires ou d'autres faits récents. Tu ne peux rien faire d'autre. Si tu ne sais pas, dis-le brièvement.
- Si la conversation a déjà commencé, ses échanges précédents sont donnés avant le nouveau message.
Date et heure actuelles : {now}."""
# Added to SYSTEM_PROMPT for the first message of a conversation (the greeting)
INTRO_PROMPT = """
Ce message ouvre la conversation : commence ta réponse exactement par cette phrase, mot pour
mot, sans la modifier ni la corriger : """ + INTRO_TEXT + """
S'il contient une question, réponds-y ensuite, toujours dans la même limite de longueur."""


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


def ask_claude(text, history, model, claude='claude'):
	now = datetime.datetime.now()
	now = '%s %d %s %d, %s' % (DAYS[now.weekday()], now.day, MONTHS[now.month - 1], now.year, now.strftime('%H:%M'))
	cmd = [claude, '-p',
		'--system-prompt', SYSTEM_PROMPT.format(now=now) + ('' if history else INTRO_PROMPT),
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
	# Empty working directory so no project file or CLAUDE.md is picked up;
	# the message goes through stdin so it is never parsed as an option.
	with tempfile.TemporaryDirectory() as cwd:
		result = subprocess.run(cmd, input=build_prompt(text, history), capture_output=True,
			encoding='utf-8', errors='replace', timeout=CLAUDE_TIMEOUT, cwd=cwd)
	if result.returncode != 0:
		# claude -p may print its error on stdout rather than stderr
		output = ' | '.join(o.strip() for o in (result.stderr, result.stdout) if o.strip())
		raise RuntimeError('claude exited with %d: %s' % (result.returncode, output[:300] or '(no output)'))
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
			answer = ' '.join(self.ask(text, history).split())
		except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
			log('claude error: %s' % e)
			answer = ''
		# One SMS, or two at most for a longer answer
		for part in split_sms(answer) if answer else [ERROR_REPLY]:
			self._send(sender, part)
		if conversation is None:
			log('conversation with %s started' % sender)
			conversation = self.conversations[sender] = {'history': []}
		conversation['last'] = time.time()
		if answer:
			conversation['history'] = (conversation['history'] + [(text, answer)])[-MAX_HISTORY:]


def main():
	parser = argparse.ArgumentParser(description='Answers incoming SMS with Claude Code, in French.')
	parser.add_argument('port', help='usb for the own USB port of the module, or the serial port of the FTDI adapter (/dev/cuaU1, /dev/ttyUSB0, COM18), or auto to find it')
	parser.add_argument('--allow', action='append', metavar='NUMBER',
		help='only answer this number (repeatable, e.g. +33612345678); default: any phone number')
	parser.add_argument('--model', help='Claude model alias or name passed to claude --model')
	parser.add_argument('--conversation-timeout', type=float, default=30, metavar='MINUTES',
		help='a conversation ends after this many minutes without SMS from its number (default: 30)')
	parser.add_argument('--trace', action='store_true', help='log every byte exchanged with the module')
	parser.add_argument('--sms-log', metavar='FILE',
		help='append one line per SMS received or sent to this file, e.g. /var/log/sms')
	parser.add_argument('--claude', default='claude', metavar='PATH',
		help='claude executable (default: claude, found in PATH)')
	args = parser.parse_args()

	if not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') and not os.environ.get('ANTHROPIC_API_KEY'):
		log('warning: neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set')

	modem = Modem(args.port, args.trace, args.sms_log)
	log('purged %d SMS already in memory' % modem.purge_sms())
	responder = Responder(modem.send_sms, args.allow, args.conversation_timeout,
		lambda text, history: ask_claude(text, history, args.model, args.claude))
	log('ready on %s, waiting for SMS' % modem.port)
	modem.serve(responder.on_sms)


if __name__ == '__main__':
	try:
		main()
	except KeyboardInterrupt:
		pass
