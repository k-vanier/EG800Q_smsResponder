# Shared code for the EG800Q-EU SMS scripts: AT command link over the UART,
# GSM 7-bit text conversion and the loop that picks up incoming SMS.
# Needs pyserial: pip install pyserial

import datetime
import re
import time
import unicodedata

import serial

SCAN_PERIOD = 5  # seconds between two checks for unread SMS
SMS_LENGTH = 160  # GSM 7-bit characters in a single SMS

# GSM 03.38 basic character set: the module sends and expects these codes
# in text mode with AT+CSCS="GSM" (for example 0x04 is "è").
GSM = ('@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?'
	'¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà')
# Never sent: 0x00 may be dropped on the serial line, 0x1A ends the message,
# 0x1B starts the extension table.
TO_GSM = {c: i for i, c in enumerate(GSM) if i not in (0x00, 0x1A, 0x1B)}
SUBSTITUTES = {'’': "'", '‘': "'", '«': '"', '»': '"', '“': '"', '”': '"', '–': '-', '—': '-',
	'…': '...', '€': 'EUR', '[': '(', ']': ')', '{': '(', '}': ')', '\\': '/', '|': '/', '~': '-',
	'^': '', '@': ' a ', ' ': ' ', '\t': ' ', '\r': ' '}


class ModemError(Exception):
	pass


def gsm_decode(text):
	return ''.join(GSM[ord(c)] if ord(c) < 128 else c for c in text)


def gsm_encode(text):
	out = bytearray()
	for c in text:
		c = SUBSTITUTES.get(c, c)
		for part in c:
			if part not in TO_GSM:
				# "ê" -> "e", anything else -> "?"
				part = unicodedata.normalize('NFKD', part).encode('ascii', 'ignore').decode() or '?'
			out += bytes(TO_GSM.get(p, TO_GSM['?']) for p in part)
	return bytes(out)


def fit_sms(text):
	text = ' '.join(text.split())
	if len(gsm_encode(text)) <= SMS_LENGTH:
		return text
	while len(gsm_encode(text + '...')) > SMS_LENGTH:
		text = text[:-1]
	return text.rstrip() + '...'


def log(msg):
	print(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg, flush=True)


def is_phone_number(sender):
	# Real phone numbers only, never operator short codes
	return re.fullmatch(r'\+?\d{10,15}', sender) is not None


class Modem:
	def __init__(self, port):
		self.ser = serial.Serial(port, 115200, timeout=0.5)
		self.pending = []  # indexes of received SMS not handled yet
		for cmd in ('AT', 'ATE0', 'AT+CMEE=2', 'AT+CMGF=1', 'AT+CSCS="GSM"', 'AT+CNMI=2,1,0,0,0'):
			self.command(cmd)

	def _readline(self):
		# latin-1 keeps the GSM codes of SMS text as they are
		return self.ser.readline().decode('latin-1').strip()

	def _urc(self, line):
		# +CMTI: "SM",3 -> new SMS stored at index 3
		m = re.match(r'\+CMTI: "\w+",(\d+)', line)
		if m:
			self._add_pending(int(m.group(1)))
			return True
		return False

	def _add_pending(self, index):
		if index not in self.pending:
			self.pending.append(index)

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
		self.ser.write(gsm_encode(text) + b'\x1a')
		return self._wait_result(cmd, 60)

	def read_sms(self, index):
		"""Returns (sender, text) of the SMS stored at index, or None."""
		lines = self.command('AT+CMGR=%d' % index)
		if not lines:
			return None
		# +CMGR: "REC UNREAD","+33612345678","","26/10/24,14:32:10+08"
		m = re.match(r'\+CMGR: "[^"]*","([^"]*)"', lines[0])
		if not m:
			log('index %d: unreadable header %r' % (index, lines[0]))
			return None
		return m.group(1), gsm_decode(' '.join(lines[1:])).strip()

	def scan_unread(self):
		# On firmware EG800QEULCR01A07M04, AT+CMGL="REC UNREAD" always returns an
		# empty list, so all messages are listed and the unread ones kept.
		for line in self.command('AT+CMGL="ALL"', 10):
			m = re.match(r'\+CMGL: (\d+),"REC UNREAD"', line)
			if m:
				self._add_pending(int(m.group(1)))

	def serve(self, on_sms, delete=False):
		"""Calls on_sms(sender, text) for each incoming SMS, forever.

		+CMTI only reaches the port set by AT+QCFG="urc/port" (USB by default),
		so unread messages are also listed every few seconds."""
		last_scan = 0
		while True:
			if time.time() - last_scan > SCAN_PERIOD:
				last_scan = time.time()
				try:
					self.scan_unread()
				except ModemError as e:
					log('error: %s' % e)
			while self.pending:
				index = self.pending.pop(0)
				try:
					sms = self.read_sms(index)
					if sms:
						log('SMS %d from %s: %r' % (index, sms[0], sms[1]))
						if on_sms(*sms) and delete:
							self.command('AT+CMGD=%d' % index)
				except ModemError as e:
					log('error: %s' % e)
			line = self._readline()
			if line:
				self._urc(line)
