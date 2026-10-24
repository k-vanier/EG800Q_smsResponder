#!/usr/bin/env python3
# Answers every incoming SMS with Claude, in French: each SMS is a message in
# a conversation with its sender, the reply is Claude's answer, cut to fit in
# a single SMS (160 characters).
# Runs on FreeBSD (or Linux) next to the EG800Q-EU and calls the Claude API
# with the anthropic Python SDK. Claude can search the web (web_search runs on
# Anthropic's side) and has no other tool.
#
# A conversation is all the SMS from one number until it stays silent for
# --conversation-timeout minutes. Its first SMS must start with "Bonjour" or
# "Bonsoir": otherwise the sender only gets POLITENESS_REPLY, without Claude.
#
# At startup, every SMS already stored in the module is deleted without answer.
# After that, every SMS is deleted once handled, answered or not.
#
# Usage: python3 sms_claude.py PORT [--allow +336...] [--model NAME]
#                              [--conversation-timeout MINUTES] [--trace]
#   PORT: /dev/cuaU0 on FreeBSD, /dev/ttyUSB0 on Linux
# Auth: export ANTHROPIC_API_KEY=... (from console.anthropic.com)
# Needs pyserial and anthropic.

import argparse
import datetime
import os
import re
import time
import unicodedata

import anthropic

from eg800q import Modem, fit_sms, is_phone_number, log

MODEL = 'claude-opus-5-5'
EFFORT = 'low'  # short chat answers: low effort is enough and costs less
CLAUDE_TIMEOUT = 120  # seconds per API request
MAX_TOKENS = 16000  # thinking and searches included; the answer itself is short
MAX_SEARCHES = 3  # web searches Claude may run for one SMS
MAX_CONTINUATIONS = 3  # pause_turn: the server paused a long search loop
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
- Une ou deux phrases, sans lien internet.
- Tu peux faire une recherche sur internet quand la question porte sur l'actualité, la météo,
  des horaires ou d'autres faits récents. Tu ne peux rien faire d'autre. Si tu ne sais pas, dis-le brièvement.
- Les messages précédents sont ceux de la conversation en cours avec ce correspondant.
Date et heure actuelles : {now}."""


def is_greeting(text):
	plain = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower()
	return re.match(r'\W*(%s)\b' % '|'.join(GREETINGS), plain) is not None


def build_messages(text, history):
	messages = []
	for question, answer in history:
		messages += [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer}]
	return messages + [{'role': 'user', 'content': text}]


def final_text(content):
	# With web search the reply interleaves text, server_tool_use and
	# web_search_tool_result blocks: the answer is the text after the last search.
	parts = []
	for block in content:
		if block.type == 'text':
			parts.append(block.text)
		elif block.type != 'fallback':
			parts = []
	return ''.join(parts).strip()


def ask_claude(client, text, history, model):
	now = datetime.datetime.now()
	now = '%s %d %s %d, %s' % (DAYS[now.weekday()], now.day, MONTHS[now.month - 1], now.year, now.strftime('%H:%M'))
	messages = build_messages(text, history)
	for _ in range(MAX_CONTINUATIONS + 1):
		response = client.with_options(timeout=CLAUDE_TIMEOUT).beta.messages.create(
			model=model,
			max_tokens=MAX_TOKENS,
			system=SYSTEM_PROMPT.format(now=now),
			messages=messages,
			output_config={'effort': EFFORT},
			# Only server-side web search: anyone who knows the number can send a
			# prompt, and nothing runs or is fetched from this machine.
			tools=[{'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': MAX_SEARCHES}],
			# A safety decline is re-run on the model Anthropic recommends for it
			betas=['server-side-fallback-2026-07-01'],
			fallbacks='default',
		)
		if response.stop_reason != 'pause_turn':
			break
		# The server paused its search loop: send the turn back as is to resume
		messages = messages + [{'role': 'assistant', 'content': response.content}]
	if response.stop_reason == 'refusal':
		category = response.stop_details.category if response.stop_details else None
		raise RuntimeError('refused (%s)' % category)
	if response.stop_reason == 'pause_turn':
		raise RuntimeError('web search still paused after %d continuations' % MAX_CONTINUATIONS)
	return final_text(response.content)


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

	def _answer(self, text, history):
		try:
			return fit_sms(self.ask(text, history))
		except anthropic.AuthenticationError:
			log('claude error: ANTHROPIC_API_KEY is missing or invalid')
		except anthropic.RateLimitError:
			log('claude error: rate limited, or no credit left on the API account')
		except anthropic.APIStatusError as e:
			log('claude error: HTTP %d %s' % (e.status_code, e.message))
		except anthropic.APIConnectionError as e:
			log('claude error: cannot reach the API (%s)' % e)
		except RuntimeError as e:
			log('claude error: %s' % e)
		return ''

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
		answer = self._answer(text, history)
		self._send(sender, answer or ERROR_REPLY)
		if conversation is None:
			log('conversation with %s started' % sender)
			conversation = self.conversations[sender] = {'history': []}
		conversation['last'] = time.time()
		if answer:
			conversation['history'] = (conversation['history'] + [(text, answer)])[-MAX_HISTORY:]


def main():
	parser = argparse.ArgumentParser(description='Answers incoming SMS with Claude, in French.')
	parser.add_argument('port', help='serial port of the module: /dev/cuaU0 on FreeBSD, /dev/ttyUSB0 on Linux')
	parser.add_argument('--allow', action='append', metavar='NUMBER',
		help='only answer this number (repeatable, e.g. +33612345678); default: any phone number')
	parser.add_argument('--model', default=MODEL, help='Claude model (default: %s)' % MODEL)
	parser.add_argument('--conversation-timeout', type=float, default=30, metavar='MINUTES',
		help='a conversation ends after this many minutes without SMS from its number (default: 30)')
	parser.add_argument('--trace', action='store_true', help='log every byte exchanged with the module')
	args = parser.parse_args()

	if not os.environ.get('ANTHROPIC_API_KEY'):
		log('warning: ANTHROPIC_API_KEY is not set')
	client = anthropic.Anthropic()

	modem = Modem(args.port, args.trace)
	log('purged %d SMS already in memory' % modem.purge_sms())
	responder = Responder(modem.send_sms, args.allow, args.conversation_timeout,
		lambda text, history: ask_claude(client, text, history, args.model))
	log('ready on %s, waiting for SMS' % args.port)
	modem.serve(responder.on_sms)


if __name__ == '__main__':
	try:
		main()
	except KeyboardInterrupt:
		pass
