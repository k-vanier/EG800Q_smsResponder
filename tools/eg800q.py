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
MAX_TRIES = 3  # attempts to send a reply before the SMS is deleted anyway

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
		self.new_sms = False  # set by +CMTI to scan without waiting
		for cmd in ('AT', 'ATE0', 'AT+CMEE=2', 'AT+CMGF=1', 'AT+CSCS="GSM"', 'AT+CNMI=2,1,0,0,0'):
			self.command(cmd)

	def _readline(self):
		# latin-1 keeps the GSM codes of SMS text as they are
		return self.ser.readline().decode('latin-1').strip()

	def _urc(self, line):
		# +CMTI: "ME",3 -> new SMS stored at index 3
		if line.startswith('+CMTI:'):
			self.new_sms = True
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
		self.ser.write(gsm_encode(text) + b'\x1a')
		return self._wait_result(cmd, 60)

	def list_sms(self):
		"""Returns [(index, sender, text)] for every SMS stored in the module.

		Read and unread messages alike: AT+CMGL="REC UNREAD" always returns an
		empty list on firmware EG800QEULCR01A07M04, and listing marks messages
		as read anyway."""
		messages = []
		for line in self.command('AT+CMGL="ALL"', 10):
			# +CMGL: 3,"REC UNREAD","+33612345678",,"26/10/24,14:32:10+08"
			m = re.match(r'\+CMGL: (\d+),"[^"]*","([^"]*)"', line)
			if m:
				messages.append([int(m.group(1)), m.group(2), ''])
			elif messages:
				body = messages[-1][2]
				messages[-1][2] = (body + ' ' if body else '') + gsm_decode(line)
		return [(index, sender, text.strip()) for index, sender, text in messages]

	def delete_sms(self, index):
		self.command('AT+CMGD=%d' % index)

	def serve(self, on_sms):
		"""For each SMS stored in the module, forever: calls on_sms(sender, text),
		then deletes the SMS, so the 10-message memory never fills up.

		An SMS is kept for another try only if on_sms raises ModemError (the
		reply could not be sent), at most MAX_TRIES times.

		+CMTI only reaches the port set by AT+QCFG="urc/port" (USB by default),
		so the memory is also listed every few seconds."""
		tries = {}
		last_scan = 0
		while True:
			if self.new_sms or time.time() - last_scan > SCAN_PERIOD:
				self.new_sms = False
				last_scan = time.time()
				try:
					messages = self.list_sms()
				except ModemError as e:
					log('error: %s' % e)
					messages = []
				for index, sender, text in messages:
					log('SMS %d from %s: %r' % (index, sender, text))
					try:
						on_sms(sender, text)
					except ModemError as e:
						tries[index] = tries.get(index, 0) + 1
						log('error: %s (try %d/%d)' % (e, tries[index], MAX_TRIES))
						if tries[index] < MAX_TRIES:
							continue
					tries.pop(index, None)
					try:
						self.delete_sms(index)
					except ModemError as e:
						log('error: %s' % e)
			line = self._readline()
			if line:
				self._urc(line)
