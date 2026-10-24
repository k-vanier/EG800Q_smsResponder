#!/usr/bin/env python3
# SMS time server: answers "heure" (or "time") with the current time.
# Runs on the PC connected to the EG800Q-EU and drives it with AT commands.
# Every SMS is deleted from the module once handled, answered or not.
# Usage: python sms_time_server.py COM18
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
	parser.add_argument('port', help='usb for the own USB port of the module, or the serial port of the FTDI adapter (/dev/cuaU1, /dev/ttyUSB0, COM18), or auto to find it')
	parser.add_argument('--trace', action='store_true', help='log every byte exchanged with the module')
	args = parser.parse_args()

	modem = Modem(args.port, args.trace)

	def on_sms(sender, text):
		if text.lower() not in KEYWORDS or not is_phone_number(sender):
			return
		reply = time_text()
		modem.send_sms(sender, reply)
		log('replied to %s: %s' % (sender, reply))

	log('ready on %s, waiting for SMS' % modem.port)
	modem.serve(on_sms)


if __name__ == '__main__':
	try:
		main()
	except KeyboardInterrupt:
		pass
