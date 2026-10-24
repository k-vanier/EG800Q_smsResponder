#!/usr/bin/env python3
# SMS time server: answers "heure" (or "time") with the current time.
# Runs on the PC connected to the EG800Q-EU and drives it with AT commands.
# Usage: python sms_time_server.py COM18 [--delete]
#   --delete  deletes each answered request from the SIM so storage never fills up
# Needs pyserial: pip install pyserial

import argparse
import datetime

from eg800q import Modem, is_phone_number, log

KEYWORDS = ('heure', 'time')
DAYS = ('lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche')


def time_text():
	now = datetime.datetime.now()
	return 'Il est %s - %s %s' % (now.strftime('%H:%M'), DAYS[now.weekday()], now.strftime('%d/%m/%Y'))


def main():
	parser = argparse.ArgumentParser(description='Answers "heure" SMS with the current time.')
	parser.add_argument('port', help='serial port of the module, e.g. COM18 or /dev/ttyUSB0')
	parser.add_argument('--delete', action='store_true', help='delete answered requests from the SIM')
	args = parser.parse_args()

	modem = Modem(args.port)

	def on_sms(sender, text):
		if text.lower() not in KEYWORDS or not is_phone_number(sender):
			return False
		reply = time_text()
		modem.send_sms(sender, reply)
		log('replied to %s: %s' % (sender, reply))
		return True

	log('ready on %s, waiting for SMS' % args.port)
	modem.serve(on_sms, args.delete)


if __name__ == '__main__':
	try:
		main()
	except KeyboardInterrupt:
		pass
