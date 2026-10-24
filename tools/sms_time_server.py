#!/usr/bin/env python3
# SMS time server: answers "heure" (or "time") with the current time.
# Runs on the PC connected to the EG800Q-EU and drives it with AT commands.
# Usage: python sms_time_server.py COM18 [--delete]
#   --delete  deletes each answered request from the SIM so storage never fills up
# Needs pyserial: pip install pyserial

import argparse
import datetime
import re
import time

import serial

KEYWORDS = ('heure', 'time')
SCAN_PERIOD = 5  # seconds between two checks for unread SMS
DAYS = ('lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche')


class ModemError(Exception):
	pass


class Modem:
	def __init__(self, port):
		self.ser = serial.Serial(port, 115200, timeout=0.5)
		self.pending = []  # indexes of received SMS not handled yet

	def _readline(self):
		return self.ser.readline().decode('ascii', 'replace').strip()

	def _urc(self, line):
		# +CMTI: "SM",3 -> new SMS stored at index 3
		m = re.match(r'\+CMTI: "\w+",(\d+)', line)
		if m:
			self.pending.append(int(m.group(1)))
			return True
		return False

	def _wait_result(self, cmd, timeout):
		lines = []
		end = time.time() + timeout
		while time.time() < end:
			line = self._readline()
			if not line or line == cmd or self._urc(line):
				continue
			if line == 'OK':
				return lines
			if 'ERROR' in line:
				raise ModemError('%s -> %s' % (cmd, line))
			lines.append(line)
		raise ModemError('%s -> timeout' % cmd)

	def command(self, cmd, timeout=5):
		self.ser.write((cmd + '\r').encode())
		return self._wait_result(cmd, timeout)

	def send_sms(self, number, text):
		cmd = 'AT+CMGS="%s"' % number
		self.ser.write((cmd + '\r').encode())
		buf = b''
		end = time.time() + 5
		while b'>' not in buf:
			if time.time() > end:
				raise ModemError('%s -> no prompt' % cmd)
			buf += self.ser.read(1)
		self.ser.write(text.encode('ascii', 'replace') + b'\x1a')
		return self._wait_result(cmd, 60)

	def poll(self):
		line = self._readline()
		if line:
			self._urc(line)


def log(msg):
	print(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg, flush=True)


def time_text():
	now = datetime.datetime.now()
	return 'Il est %s - %s %s' % (now.strftime('%H:%M'), DAYS[now.weekday()], now.strftime('%d/%m/%Y'))


def handle(modem, index, delete):
	lines = modem.command('AT+CMGR=%d' % index)
	if not lines:
		return
	# +CMGR: "REC UNREAD","+33612345678","","26/10/24,14:32:10+08"
	m = re.match(r'\+CMGR: "[^"]*","([^"]*)"', lines[0])
	if not m:
		log('index %d: unreadable header %r' % (index, lines[0]))
		return
	sender = m.group(1)
	text = ' '.join(lines[1:]).strip()
	log('SMS %d from %s: %r' % (index, sender, text))
	# Only answer real phone numbers, never operator short codes
	if text.lower() not in KEYWORDS or not re.fullmatch(r'\+?\d{10,15}', sender):
		return
	reply = time_text()
	modem.send_sms(sender, reply)
	log('replied to %s: %s' % (sender, reply))
	if delete:
		modem.command('AT+CMGD=%d' % index)


def scan_unread(modem):
	# On firmware EG800QEULCR01A07M04, AT+CMGL="REC UNREAD" always returns an
	# empty list, so all messages are listed and the unread ones kept.
	for line in modem.command('AT+CMGL="ALL"', 10):
		m = re.match(r'\+CMGL: (\d+),"REC UNREAD"', line)
		if m and int(m.group(1)) not in modem.pending:
			modem.pending.append(int(m.group(1)))


def main():
	parser = argparse.ArgumentParser(description='Answers "heure" SMS with the current time.')
	parser.add_argument('port', help='serial port of the module, e.g. COM18 or /dev/cuaU0')
	parser.add_argument('--delete', action='store_true', help='delete answered requests from the SIM')
	args = parser.parse_args()

	modem = Modem(args.port)
	for cmd in ('AT', 'ATE0', 'AT+CMEE=2', 'AT+CMGF=1', 'AT+CSCS="GSM"', 'AT+CNMI=2,1,0,0,0'):
		modem.command(cmd)
	log('ready on %s, waiting for SMS' % args.port)

	last_scan = 0
	while True:
		# +CMTI only reaches the port set by AT+QCFG="urc/port" (USB by default),
		# so unread messages are also listed every few seconds.
		if time.time() - last_scan > SCAN_PERIOD:
			last_scan = time.time()
			try:
				scan_unread(modem)
			except ModemError as e:
				log('error: %s' % e)
		while modem.pending:
			index = modem.pending.pop(0)
			try:
				handle(modem, index, args.delete)
			except ModemError as e:
				log('error: %s' % e)
		modem.poll()


if __name__ == '__main__':
	try:
		main()
	except KeyboardInterrupt:
		pass
