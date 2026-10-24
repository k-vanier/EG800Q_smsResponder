import uos
import modem

# Run this from the QuecPython REPL to see which modules the EG800Q-EU firmware
# provides. Only useful if a QuecPython firmware is flashed on the module.

print('Model   :', modem.getDevModel())
print('Firmware:', modem.getDevFwVersion())
print('IMEI    :', modem.getDevImei())
print('uname   :', uos.uname())

for name in ('sim', 'net', 'checkNet', 'dataCall', 'sms', 'ntptime', 'usocket', 'request', 'misc', 'gnss', 'wifiScan'):
	try:
		__import__(name)
		print('[OK]  module', name)
	except ImportError as e:
		print('[ERR] module', name, e)
