#!/usr/bin/env python3
# Answers every incoming SMS with Claude: each SMS is a question, the reply is
# Claude Code's answer, cut to fit in a single SMS (160 characters).
# Runs on Linux next to the EG800Q-EU and calls the Claude Code CLI (claude -p).
#
# At startup, every SMS already stored in the module is deleted without answer.
# After that, every SMS is deleted once handled, answered or not.
#
# Usage: python3 sms_claude.py /dev/ttyUSB0 [--allow +336...] [--model NAME]
# Auth: export CLAUDE_CODE_OAUTH_TOKEN=... (from "claude setup-token")
#       or ANTHROPIC_API_KEY=...
# Needs pyserial and the claude CLI in PATH.

import argparse
import datetime
import os
import subprocess
import tempfile

from eg800q import Modem, fit_sms, is_phone_number, log

CLAUDE_TIMEOUT = 120  # seconds
ERROR_REPLY = 'Desole, je ne peux pas repondre pour le moment.'

SYSTEM_PROMPT = """You answer questions received by SMS. Your reply is sent back as is in a single SMS.
- At most 150 characters, plain text: no markdown, no lists, no emoji.
- Answer in the language of the question, in one or two sentences.
- You have no tools and no internet access. If you don't know, say so briefly.
Current date and time: {now}."""


def ask_claude(question, model):
	now = datetime.datetime.now().strftime('%A %d %B %Y %H:%M')
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
	# the question goes through stdin so it is never parsed as an option.
	with tempfile.TemporaryDirectory() as cwd:
		result = subprocess.run(cmd, input=question, capture_output=True, encoding='utf-8',
			errors='replace', timeout=CLAUDE_TIMEOUT, cwd=cwd)
	if result.returncode != 0:
		raise RuntimeError('claude exited with %d: %s' % (result.returncode, result.stderr.strip()[:300]))
	return result.stdout.strip()


def main():
	parser = argparse.ArgumentParser(description='Answers incoming SMS questions with Claude Code.')
	parser.add_argument('port', help='serial port of the module, e.g. /dev/ttyUSB0')
	parser.add_argument('--allow', action='append', metavar='NUMBER',
		help='only answer this number (repeatable, e.g. +33612345678); default: any phone number')
	parser.add_argument('--model', help='Claude model alias or name passed to claude --model')
	args = parser.parse_args()

	if not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') and not os.environ.get('ANTHROPIC_API_KEY'):
		log('warning: neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set')

	modem = Modem(args.port)
	log('purged %d SMS already in memory' % modem.purge_sms())

	def on_sms(sender, text):
		if not is_phone_number(sender) or not text:
			return
		if args.allow and sender not in args.allow:
			log('ignored, %s is not in --allow' % sender)
			return
		try:
			reply = fit_sms(ask_claude(text, args.model)) or ERROR_REPLY
		except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
			log('claude error: %s' % e)
			reply = ERROR_REPLY
		log('sending to %s: %s' % (sender, reply))
		modem.send_sms(sender, reply)
		log('sent to %s' % sender)

	log('ready on %s, waiting for SMS' % args.port)
	modem.serve(on_sms)


if __name__ == '__main__':
	try:
		main()
	except KeyboardInterrupt:
		pass
