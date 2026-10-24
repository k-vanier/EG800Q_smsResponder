#!/usr/bin/env python3
# Talks to the EG800Q-EU over its USB AT port from a PC and prints what the
# module reports: identity, firmware, SIM, signal, registration and bands.
# Usage: python at_check.py COM5        (or /dev/cuaU0 on FreeBSD)
# Needs pyserial: pip install pyserial

import sys
import time

import serial

COMMANDS = [
	('ATI', 'Identity'),
	('AT+CGMR', 'Firmware'),
	('AT+CGSN', 'IMEI'),
	('AT+CPIN?', 'SIM'),
	('AT+QCCID', 'ICCID'),
	('AT+CSQ', 'Signal (RSSI, BER)'),
	('AT+CEREG?', 'LTE registration'),
	('AT+COPS?', 'Operator'),
	('AT+QNWINFO', 'Network info'),
	('AT+QCFG="band"', 'Band configuration'),
]


def send(port, cmd, timeout=3):
	port.reset_input_buffer()
	port.write((cmd + '\r').encode())
	end = time.time() + timeout
	lines = []
	while time.time() < end:
		line = port.readline().decode(errors='replace').strip()
		if not line or line == cmd:
			continue
		lines.append(line)
		if line == 'OK' or 'ERROR' in line:
			break
	return lines


def main():
	if len(sys.argv) != 2:
		sys.exit('usage: at_check.py <serial port>')
	with serial.Serial(sys.argv[1], 115200, timeout=0.5) as port:
		for cmd, label in COMMANDS:
			print('%-20s %s' % (label, ' | '.join(send(port, cmd)) or '(no answer)'))


if __name__ == '__main__':
	main()
