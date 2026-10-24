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
COMMAND_TIMEOUT = 20  # seconds; the module can take more than 5 s right after sending an SMS
SEND_TIMEOUT = 120  # seconds; Quectel gives 120 s as the maximum response time of AT+CMGS

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


# UTF-16 code units likely in an SMS: Latin, Greek and Cyrillic letters, then
# punctuation (’ …), currency (€), the zero width joiner 200D and keycap 20E3,
# symbols, arrows, dingbats and the other emoji of the basic plane (❤ ☀ ✅ ⭐),
# CJK emoji (〰 〽 ㊗ ㊙) and the variation selectors (FE0F after ❤ gives ❤️).
# Emoji outside the basic plane (😀, flags, skin tones) are surrogate pairs.
LIKELY_UNITS = ((0x0000, 0x052F), (0x2000, 0x2BFF), (0x3030, 0x3030), (0x303D, 0x303D),
	(0x3297, 0x3297), (0x3299, 0x3299), (0xFE00, 0xFE0F))


def ucs2_decode(text):
	"""Decodes an SMS body the module shows as UCS2 hex, else returns it unchanged.

	A phone sends an SMS in UCS2 when it holds a character missing from the GSM
	alphabet ("ç", "’", an emoji...), and with AT+CSCS="GSM" the module prints
	it as hex: "0045006E" is "En". Plain text made of hex digits ("CAFE1234")
	is kept: decoded, most of its characters must be in LIKELY_UNITS or
	surrogate pairs. Digits only ("20262027") must hold a 00xx character or
	be emoji symbols only (2190-2BFF: "27052705" is ✅✅). A single
	basic-plane emoji alone ("2764" for ❤) is too short to tell from a
	number and stays as hex."""
	if len(text) < 8 or len(text) % 4 or not re.fullmatch(r'[0-9A-Fa-f]+', text):
		return text
	units = [int(text[i:i + 4], 16) for i in range(0, len(text), 4)]
	if text.isdigit() and not any(u < 0x100 for u in units) \
			and not all(0x2190 <= u <= 0x2BFF for u in units):
		return text
	likely = 0
	for i, u in enumerate(units):
		pair_start = 0xD800 <= u <= 0xDBFF and i + 1 < len(units) and 0xDC00 <= units[i + 1] <= 0xDFFF
		pair_end = 0xDC00 <= u <= 0xDFFF and i > 0 and 0xD800 <= units[i - 1] <= 0xDBFF
		if pair_start or pair_end or any(lo <= u <= hi for lo, hi in LIKELY_UNITS):
			likely += 1
	if likely * 2 < len(units):
		return text
	try:
		decoded = bytes.fromhex(text).decode('utf-16-be')
	except UnicodeDecodeError:
		return text
	# Control characters mean it was not text; format characters are fine
	# (zero width joiner in 👨‍👩‍👧, tags in the Scotland flag)
	if any(unicodedata.category(c) == 'Cc' and not c.isspace() for c in decoded):
		return text
	return decoded


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
	def __init__(self, port, trace=False):
		self.ser = serial.Serial(port, 115200, timeout=0.5)
		self.trace = trace  # log every byte written to and read from the module
		self.new_sms = False  # set by +CMTI to scan without waiting
		for cmd in ('AT', 'ATE0', 'AT+CMEE=2', 'AT+CMGF=1', 'AT+CSCS="GSM"', 'AT+CNMI=2,1,0,0,0'):
			self.command(cmd)

	def _write(self, data):
		if self.trace:
			log('>> %r' % data)
		self.ser.write(data)

	def _read(self):
		data = self.ser.read(1)
		if self.trace and data:
			log('<< %r' % data)
		return data

	def _readline(self):
		# latin-1 keeps the GSM codes of SMS text as they are. Only the line end
		# is removed: strip() would also drop GSM letters whose codes Python sees
		# as whitespace, such as 0x09 "Ç" or 0x1F "É".
		data = self.ser.readline()
		if self.trace and data:
			log('<< %r' % data)
		return data.decode('latin-1').rstrip('\r\n')

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
			# The module's own replies are compared fully stripped (a stray "\r"
			# or space must not hide an OK); SMS text lines are kept as they are.
			status = line.strip()
			if not line or status == cmd or self._urc(status):
				continue
			if status == 'OK':
				return lines
			if 'ERROR' in status:
				raise ModemError('%s -> %s' % (cmd, status))
			lines.append(line)
		raise ModemError('%s -> timeout' % cmd)

	def _flush(self):
		# A reply that came after its command timed out (the module can stay busy
		# for several seconds after sending an SMS) would otherwise be taken as
		# the reply to the next command.
		while self.ser.in_waiting:
			line = self._readline()
			if line.strip() and not self._urc(line.strip()):
				log('late reply dropped: %r' % line)

	def command(self, cmd, timeout=COMMAND_TIMEOUT):
		self._flush()
		self._write((cmd + '\r').encode())
		return self._wait_result(cmd, timeout)

	def send_sms(self, number, text):
		cmd = 'AT+CMGS="%s"' % number
		self._flush()
		self._write((cmd + '\r').encode())
		buf = b''
		end = time.time() + 5
		while b'>' not in buf:
			if time.time() > end:
				raise ModemError('%s -> no prompt' % cmd)
			buf += self._read()
		self._write(gsm_encode(text) + b'\x1a')
		return self._wait_result(cmd, SEND_TIMEOUT)

	def list_sms(self):
		"""Returns [(index, sender, text)] for every SMS stored in the module.

		Read and unread messages alike: AT+CMGL="REC UNREAD" always returns an
		empty list on firmware EG800QEULCR01A07M04, and listing marks messages
		as read anyway."""
		messages = []
		for line in self.command('AT+CMGL="ALL"', 30):
			# +CMGL: 3,"REC UNREAD","+33612345678",,"26/10/24,14:32:10+08"
			m = re.match(r'\s*\+CMGL: (\d+),"[^"]*","([^"]*)"', line)
			if m:
				messages.append([int(m.group(1)), m.group(2), ''])
			elif messages:
				body = messages[-1][2]
				messages[-1][2] = (body + ' ' if body else '') + gsm_decode(line)
		return [(index, sender, ucs2_decode(text.strip()).strip()) for index, sender, text in messages]

	def delete_sms(self, index):
		self.command('AT+CMGD=%d' % index)

	def purge_sms(self):
		"""Logs then deletes every SMS stored in the module."""
		messages = self.list_sms()
		for index, sender, text in messages:
			log('purged SMS %d from %s: %r' % (index, sender, text))
		# Delete flag 4: all messages, whatever their status; the index is ignored
		self.command('AT+CMGD=0,4', 30)
		return len(messages)

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
			line = self._readline().strip()
			if line:
				self._urc(line)
