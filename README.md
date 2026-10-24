# EG800Q-EU

Tests and tools for the **Quectel EG800Q-EU**, an LTE Cat 1 bis module for Europe.

## Module

| | |
|---|---|
| Module | Quectel EG800Q-EU |
| Chipset | Qualcomm QCX216 |
| Category | LTE Cat 1 bis, LTE only (no 2G fallback) |
| LTE FDD bands | B1 / B3 / B5 / B7 / B8 / B20 / B28 |
| GNSS | None built in |

Still to check: is there a QuecPython firmware for the EG800Q-EU? The standard firmware is driven by AT commands only.

## Files

| File | Runs on | Purpose |
|---|---|---|
| `tools/at_check.py` | PC (Windows or FreeBSD) | Sends AT commands over the USB AT port and prints identity, firmware, SIM, signal, registration and bands |
| `tools/sms_time_server.py` | PC (Windows or FreeBSD) | SMS time server: answers `heure` or `time` with the current time |
| `usr/check_eg800q.py` | Module (QuecPython REPL) | Prints model, firmware and IMEI, and lists the QuecPython modules the firmware provides |

## Usage

Install the [Quectel USB drivers](https://python.quectel.com/wp-content/uploads/2024/09/Quectel_Windows_USB_DriverA_Customer_V1.1.13.zip) on Windows, then find the *AT Port* in Device Manager.

```
pip install pyserial
python tools/at_check.py COM5
```

On FreeBSD the port is usually `/dev/cuaU0` (or `cuaU0.x`).

## SMS time server

Send `heure` (or `time`) to the module's number and it replies with the PC's local time, for example `Il est 14:32 - samedi 24/10/2026`.

```
python tools/sms_time_server.py COM18
```

- Only plain phone numbers get an answer, never operator short codes. Other messages are logged and left alone.
- The server lists the SMS every 5 seconds and answers the unread ones. It does not rely on `+CMTI` alone, because the module sends it to the USB AT port by default (`AT+QCFG="urc/port"` is `usbat`), not to the UART. Messages received while the server was stopped are answered too.
- The module stores SMS in its own memory (`ME`), which holds only **10 messages**. `--delete` removes each answered request. Without it, the memory fills up and new SMS stop arriving.

## Tested module

`ATI` on the module used here: firmware `EG800QEULCR01A07M04`, registered on Free (20815), LTE band 3.

| Feature | Result |
|---|---|
| SMS (text mode, `+CMTI` notification) | Works. `AT+CMGL="REC UNREAD"` always returns an empty list, `AT+CMGL="ALL"` works |
| Voice calls, VoLTE/IMS (`AT+CIREG`, `AT+CAVIMS`, `AT+CLIP`, `ATS0`) | Not supported by this firmware |
| Text-to-speech and audio playback (`AT+QTTS`, `AT+QAUDPLAY`, `AT+QPSND`) | Not supported |
| PCM audio interface (`AT+QDAI`) | Present |
| Network time (`AT+CTZU`) | Disabled, clock stays at 1970 |
| NTP (`AT+QNTP`) | Available |
