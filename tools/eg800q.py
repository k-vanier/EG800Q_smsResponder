# Shared code for the EG800Q-EU SMS scripts: AT command link (UART through a
# USB-serial adapter, or the module's own USB port), GSM 7-bit text conversion
# and the loop that picks up incoming SMS.
# Needs pyserial, and pyusb for the "usb" port.

import ctypes.util
import datetime
import re
import subprocess
import sys
import time
import unicodedata

import serial
import serial.tools.list_ports

SCAN_PERIOD = 5  # seconds between two checks for unread SMS
SMS_LENGTH = 160  # GSM 7-bit characters in a single SMS
MAX_TRIES = 3  # attempts to send a reply before the SMS is deleted anyway
COMMAND_TIMEOUT = 20  # seconds; the module can take more than 5 s right after sending an SMS
SEND_TIMEOUT = 120  # seconds; Quectel gives 120 s as the maximum response time of AT+CMGS
DONE_GRACE = 5  # seconds to wait for OK once a reply already proves success
BATTERY_PERIOD = 60  # seconds between two battery voltage readings in the log
# First field of +CBC
CHARGE_STATES = {'0': 'not charging', '1': 'charging', '2': 'charged'}
FTDI_VID = 0x0403  # USB vendor of the FTDI adapter wired to the module's UART
# The module's own USB port: interfaces 1 to 3 are serial ports, 2 and 3 answer
# AT commands. 2 is the AT port, where URCs such as +CMTI go
# (AT+QCFG="urc/port" is "usbat"). FreeBSD has no driver for them, so pyusb
# talks to them directly.
MODULE_USB_ID = (0x2C7C, 0x6007)
USB_AT_INTERFACE = 2

# GSM 03.38 basic character set: the module sends and expects these codes
# in text mode with AT+CSCS="GSM" (for example 0x04 is "è").
GSM = ('@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?'
	'¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà')
# Never sent: 0x00 may be dropped on the serial line, 0x1A ends the message,
# 0x1B starts the extension table.
TO_GSM = {c: i for i, c in enumerate(GSM) if i not in (0x00, 0x1A, 0x1B)}
SUBSTITUTES = {'’': "'", '‘': "'", '«': '"', '»': '"', '“': '"', '”': '"', '–': '-', '—': '-',
	'…': '...', '€': 'EUR', '[': '(', ']': ')', '{': '(', '}': ')', '\\': '/', '|': '/', '~': '-',
	'^': '', '@': ' a ', '°': '', ' ': ' ', '\t': ' ', '\r': ' '}


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


class UsbPort:
	"""The module's USB serial interface, with the part of the pyserial API
	Modem uses (write, read, readline, in_waiting) and the same 0.5 s timeout."""

	TIMEOUT = 0.5

	def __init__(self, interface=USB_AT_INTERFACE):
		import usb.backend.libusb1
		import usb.core
		import usb.util
		self.usb = usb
		# The library is libusb-1.0 on Linux and libusb in FreeBSD's base system
		backend = usb.backend.libusb1.get_backend(
			find_library=lambda name: ctypes.util.find_library('usb-1.0') or ctypes.util.find_library('usb'))
		self.dev = usb.core.find(idVendor=MODULE_USB_ID[0], idProduct=MODULE_USB_ID[1], backend=backend)
		if self.dev is None:
			raise ModemError('module not found on USB (%04x:%04x)' % MODULE_USB_ID)
		intf = self.dev.get_active_configuration()[(interface, 0)]
		def bulk(direction):
			return usb.util.find_descriptor(intf, custom_match=lambda e:
				usb.util.endpoint_direction(e.bEndpointAddress) == direction
				and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
		self.ep_out = bulk(usb.util.ENDPOINT_OUT)
		self.ep_in = bulk(usb.util.ENDPOINT_IN)
		try:
			# Linux binds its option driver to the interface; FreeBSD binds none
			if self.dev.is_kernel_driver_active(interface):
				self.dev.detach_kernel_driver(interface)
		except (NotImplementedError, usb.core.USBError):
			pass
		usb.util.claim_interface(self.dev, interface)
		# SET_CONTROL_LINE_STATE with DTR and RTS on, as when a serial port opens
		self.dev.ctrl_transfer(0x21, 0x22, 0x03, interface)
		self.buf = b''

	def _fill(self, seconds):
		try:
			self.buf += bytes(self.ep_in.read(512, timeout=max(1, int(seconds * 1000))))
		except self.usb.core.USBTimeoutError:
			pass

	@property
	def in_waiting(self):
		if not self.buf:
			self._fill(0.001)
		return len(self.buf)

	def write(self, data):
		self.ep_out.write(data, timeout=5000)

	def read(self, size=1):
		if not self.buf:
			self._fill(self.TIMEOUT)
		data, self.buf = self.buf[:size], self.buf[size:]
		return data

	def readline(self):
		end = time.time() + self.TIMEOUT
		while b'\n' not in self.buf and time.time() < end:
			self._fill(end - time.time())
		if b'\n' in self.buf:
			line, self.buf = self.buf.split(b'\n', 1)
			return line + b'\n'
		# Like pyserial on timeout: whatever arrived, maybe nothing
		data, self.buf = self.buf, b''
		return data


def find_ftdi_port():
	"""Returns the serial port of the FTDI adapter wired to the module's UART.

	Its number can change from one boot to the next (another USB serial
	device, or the module's own USB port when it is plugged in to charge),
	so "auto" finds it by its driver instead."""
	if sys.platform.startswith('freebsd'):
		# pyserial cannot tell USB devices apart on FreeBSD: the uftdi(4) driver
		# gives the tty name of each adapter, e.g. dev.uftdi.0.ttyname: U1
		for unit in range(32):
			result = subprocess.run(['sysctl', '-n', 'dev.uftdi.%d.ttyname' % unit],
				capture_output=True, text=True)
			if result.returncode == 0 and result.stdout.strip():
				return '/dev/cua' + result.stdout.strip()
	else:
		for port in serial.tools.list_ports.comports():
			if port.vid == FTDI_VID:
				return port.device
	raise ModemError('no FTDI serial adapter found')


def is_phone_number(sender):
	# Real phone numbers only, never operator short codes
	return re.fullmatch(r'\+?\d{10,15}', sender) is not None


class Modem:
	def __init__(self, port, trace=False):
		if port == 'auto':
			port = find_ftdi_port()
			log('FTDI adapter found on %s' % port)
		self.port = port
		if port == 'usb' or port.startswith('usb:'):
			# usb, or usb:N for interface N of the module's USB port
			interface = int(port[4:]) if port.startswith('usb:') else USB_AT_INTERFACE
			self.ser = UsbPort(interface)
			self.port = 'USB interface %d' % interface
		else:
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

	def _wait_result(self, cmd, timeout, done=None):
		"""Returns the lines the module sends before OK.

		done is a reply prefix that already proves success ("+CMGS:" for a sent
		SMS): once seen, a lost or garbled OK only costs DONE_GRACE seconds.
		Bytes do get garbled on the UART right after the module transmits."""
		lines = []
		done_seen = False
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
			if done and status.startswith(done) and not done_seen:
				done_seen = True
				end = min(end, time.time() + DONE_GRACE)
			lines.append(line)
		if done_seen:
			log('%s: no OK after %s, taken as done' % (cmd, done))
			return lines
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
		return self._wait_result(cmd, SEND_TIMEOUT, done='+CMGS:')

	def list_sms(self):
		"""Returns [(index, sender, stamp, text)] for every SMS stored in the
		module; stamp is the time the network gives, "26/10/24,14:32:10+08".

		Read and unread messages alike: AT+CMGL="REC UNREAD" always returns an
		empty list on firmware EG800QEULCR01A07M04, and listing marks messages
		as read anyway."""
		messages = []
		for line in self.command('AT+CMGL="ALL"', 30):
			# In GSM text mode the module only sends 7-bit bytes (UCS2 text comes
			# as hex), so a byte above 0x7F means the line was garbled on the UART.
			# Nothing is handled or deleted then: the next scan reads it again.
			if any(ord(c) > 0x7F for c in line):
				raise ModemError('AT+CMGL="ALL" -> garbled reply %r' % line)
			# +CMGL: 3,"REC UNREAD","+33612345678",,"26/10/24,14:32:10+08"
			m = re.match(r'\s*\+CMGL: (\d+),"(?:REC UNREAD|REC READ)","([^"]*)",[^,]*,'
				r'"(\d\d/\d\d/\d\d,\d\d:\d\d:\d\d[+-]\d\d)"\s*$', line)
			if m:
				messages.append([int(m.group(1)), m.group(2), m.group(3), ''])
			elif '+CMGL:' in line:
				raise ModemError('AT+CMGL="ALL" -> garbled header %r' % line)
			elif messages:
				body = messages[-1][3]
				messages[-1][3] = (body + ' ' if body else '') + gsm_decode(line)
		return [(index, sender, stamp, ucs2_decode(text.strip()).strip())
			for index, sender, stamp, text in messages]

	def delete_sms(self, index):
		self.command('AT+CMGD=%d' % index)

	def battery(self):
		"""Returns (volts, charge state) from AT+CBC."""
		# +CBC: <charge status>,<charge level>,<voltage in mV>
		for line in self.command('AT+CBC'):
			m = re.match(r'\s*\+CBC: (\d+),\d+,(\d+)\s*$', line)
			if m:
				return int(m.group(2)) / 1000, CHARGE_STATES.get(m.group(1), 'state ' + m.group(1))
		raise ModemError('AT+CBC -> no voltage')

	def purge_sms(self):
		"""Logs then deletes every SMS stored in the module."""
		try:
			messages = self.list_sms()
		except ModemError as e:
			log('error: %s; purging without logging the messages' % e)
			messages = []
		for index, sender, stamp, text in messages:
			log('purged SMS %d from %s: %r' % (index, sender, text))
		# Delete flag 4: all messages, whatever their status; the index is ignored
		self.command('AT+CMGD=0,4', 30)
		return len(messages)

	def serve(self, on_sms):
		"""For each SMS stored in the module, forever: calls on_sms(sender, text),
		then deletes the SMS, so the 10-message memory never fills up.

		An SMS is kept for another try only if on_sms raises ModemError (the
		reply could not be sent), at most MAX_TRIES times. An SMS already handled
		whose deletion failed is only deleted again, never handled twice.

		+CMTI only reaches the port set by AT+QCFG="urc/port" (USB by default),
		so the memory is also listed every few seconds. The battery voltage is
		logged every BATTERY_PERIOD seconds."""
		tries = {}
		handled = set()  # (index, sender, stamp) handled but maybe not deleted
		last_scan = 0
		last_battery = 0
		while True:
			if time.time() - last_battery > BATTERY_PERIOD:
				last_battery = time.time()
				try:
					log('battery: %.2f V, %s' % self.battery())
				except ModemError as e:
					log('error: %s' % e)
			if self.new_sms or time.time() - last_scan > SCAN_PERIOD:
				self.new_sms = False
				last_scan = time.time()
				try:
					messages = self.list_sms()
				except ModemError as e:
					log('error: %s' % e)
					messages = None
				if messages is not None:
					# Forget the ones whose deletion worked after all
					handled &= {(index, sender, stamp) for index, sender, stamp, text in messages}
				for index, sender, stamp, text in messages or []:
					key = (index, sender, stamp)
					if key in handled:
						log('SMS %d already handled, deleting it again' % index)
					else:
						log('SMS %d from %s: %r' % (index, sender, text))
						try:
							on_sms(sender, text)
						except ModemError as e:
							tries[key] = tries.get(key, 0) + 1
							log('error: %s (try %d/%d)' % (e, tries[key], MAX_TRIES))
							if tries[key] < MAX_TRIES:
								continue
						tries.pop(key, None)
						handled.add(key)
					try:
						self.delete_sms(index)
						handled.discard(key)
					except ModemError as e:
						log('error: %s' % e)
			line = self._readline().strip()
			if line:
				self._urc(line)
