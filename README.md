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
| `tools/eg800q.py` | — | Shared code: AT commands over the UART, GSM 7-bit text, loop that picks up incoming SMS |
| `tools/sms_time_server.py` | PC (Windows, Linux or FreeBSD) | SMS time server: answers `heure` or `time` with the current time |
| `tools/sms_claude.py` | FreeBSD or Linux | Answers every incoming SMS with Claude Code (web search only), in French, in a single SMS |
| `rc.d/sms_claude` | FreeBSD | rc.d script that runs `sms_claude.py` at boot under `daemon(8)` |
| `usr/check_eg800q.py` | Module (QuecPython REPL) | Prints model, firmware and IMEI, and lists the QuecPython modules the firmware provides |

## Usage

Install the [Quectel USB drivers](https://python.quectel.com/wp-content/uploads/2024/09/Quectel_Windows_USB_DriverA_Customer_V1.1.13.zip) on Windows, then find the *AT Port* in Device Manager.

```
pip install pyserial
python tools/at_check.py COM5
```

On FreeBSD the port is usually `/dev/cuaU0` (or `cuaU0.x`). On Linux, a USB-serial adapter is usually `/dev/ttyUSB0`.

## SMS time server

Send `heure` (or `time`) to the module's number and it replies with the PC's local time, for example `Il est 14:32 - samedi 24/10/2026`.

```
python tools/sms_time_server.py COM18
```

- Only plain phone numbers get an answer, never operator short codes. Other messages are logged and left alone.
Both servers (this one and `sms_claude.py`) work the same way, in `tools/eg800q.py`:

- Every 5 seconds the server lists **every SMS stored** in the module, read or not. For each one it reads it, answers if needed, then **deletes it**, even when it was not answered. The module's memory (`ME`) holds only **10 messages**, and new SMS stop arriving once it is full.
- If the reply cannot be sent, the SMS is kept and tried again, at most 3 times, then deleted.
- At startup, `sms_time_server.py` handles messages already in memory like new ones, so they may get an answer. `sms_claude.py` instead deletes them all without answering (`AT+CMGD=0,4`), after printing each one in the log.
- Right after the module transmits an SMS, bytes it sends on the UART sometimes arrive garbled (`+CMGS: 32` then `
þ` instead of `OK`). The likely cause is the current peak of the radio on a weak power supply, or a UART voltage mismatch with the USB-serial adapter. Reading SMS can be hit too (`+33ÿÿÿÿÿ`). The server copes with it: a listing with a byte above 0x7F (impossible in GSM text mode) or a malformed `+CMGL` header is ignored and read again 5 s later, nothing is answered or deleted; `+CMGS: n` alone counts as sent (it waits 5 s for the OK, not 120 s), and an SMS already answered whose deletion failed is only deleted again, never answered twice.
- `--trace` logs every byte written to and read from the module, to see what it really answers.
- The server does not rely on `+CMTI` alone, because the module sends it to the USB AT port by default (`AT+QCFG="urc/port"` is `usbat`), not to the UART.

## SMS questions answered by Claude

`tools/sms_claude.py` answers every incoming SMS with Claude Code (`claude -p`), **always in French**, in a single SMS of at most 160 characters. It runs on FreeBSD (see below) and on Linux.

Conversations and politeness:

- A **conversation** is all the SMS from one number. It ends after 30 minutes without SMS from that number (`--conversation-timeout MINUTES`), and after a restart of the script.
- The first SMS of a conversation must **start with "Bonjour" or "Bonsoir"** (any case, accents ignored). Otherwise the sender gets `La politesse avant tout : commencez par « Bonjour » et je répondrai avec plaisir à votre message.`, without calling Claude, and the conversation does not start.
- Within a conversation, Claude gets the last 10 exchanges with that number as previous messages, so a follow-up question keeps its context.

Claude:

- **Auth**: whatever already works for `claude` for that user (a login, `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, or `ANTHROPIC_API_KEY`).
- **Web search only**: Claude runs with `--tools "WebSearch"`, no MCP, for weather, news or schedules. The search runs on Anthropic's side. WebFetch is left out on purpose: it would load pages from this machine, so an SMS could make it read the local network. Claude runs in an empty temporary directory and the question goes through stdin: someone who knows the number can only get a text answer, never run anything on the machine.
- A search makes the answer slower (often 10 to 30 s) and uses more of your Claude quota. Claude gets up to 5 turns (`--max-turns 5`) to search then answer.
- `--claude PATH` sets the `claude` executable when it is not in `PATH` (a service does not get `~/.local/bin`). `--model` is passed to `claude --model`.
- `--allow` (repeatable) restricts answers to some numbers. Without it, every phone number gets an answer, and every SMS uses your Claude quota.
- If Claude fails or takes longer than 2 minutes, the log shows `claude error: ...` and the sender gets `Désolé, je ne peux pas répondre pour le moment.`

SMS text:

- The answer is converted to the GSM SMS alphabet: French accents such as é, è, à and ù are kept, `ç` becomes `c`, `€` becomes `EUR`, `°` is dropped, and other characters become `?`. Answers longer than 160 characters are cut and end with `...`.
- A question with a character missing from the GSM alphabet (`ç`, `’`, an emoji…) is sent by the phone in UCS2, and the module prints it as hex (`0045006E…`). The script decodes it back to text, emoji included (😀, ❤️, ✅, flags, skin tones, 👨‍👩‍👧). Two limits: a single emoji alone without variation selector (`2764`, ❤) cannot be told from a number and stays as hex, and replies cannot contain emoji, since they are sent in the GSM alphabet.
- A question longer than 160 characters arrives as several SMS, and each part is answered separately.

## FreeBSD

**Serial port.** A USB-serial adapter shows up as `/dev/cuaU0` (use `cuaU`, not `ttyU`, which waits for the carrier signal). It belongs to `uucp:dialer`, so the user that runs the script must be in the `dialer` group.

**Python.** Only pyserial is needed besides Python (`py311-` follows the default Python version, check with `pkg search pyserial`):

```
pkg install python3 py311-pyserial
```

**Claude Code.** `claude` must already work for the user that runs the script: check with `echo "Bonjour" | claude -p --tools "" --output-format text`, and note its path with `which claude`.

**Try it by hand** (the user must be in the `dialer` group: `pw groupmod dialer -m $USER`, then log in again):

```
python3 tools/sms_claude.py /dev/cuaU0
```

**Service at boot.** `rc.d/sms_claude` runs the script under `daemon(8)` as the user for whom `claude` works, with `HOME` set to that user's home so `claude` finds its login, restarts it 5 s after any exit, and sends its log to syslog (`/var/log/messages`, tag `sms_claude`). As root, from this folder:

```
mkdir -p /usr/local/libexec/eg800q
install -m 444 tools/eg800q.py tools/sms_claude.py /usr/local/libexec/eg800q/
install -m 555 rc.d/sms_claude /usr/local/etc/rc.d/sms_claude
sysrc sms_claude_enable=YES
sysrc sms_claude_user=bsdouille
sysrc sms_claude_args="--claude /home/bsdouille/.local/bin/claude"
service sms_claude start
tail -f /var/log/messages | grep sms_claude
```

Set `sms_claude_args` to the path `which claude` printed. Other settings (`sysrc`): `sms_claude_port` (default `/dev/cuaU0`), `sms_claude_dir`, `sms_claude_python` (default `/usr/local/bin/python3`). If `claude` only works with a token exported in your shell, put `CLAUDE_CODE_OAUTH_TOKEN=...` in a root-only file (`install -m 600 /dev/null /usr/local/etc/sms_claude.env`, then write the line into it) and set `sysrc sms_claude_env_file=/usr/local/etc/sms_claude.env`.

## Tested module

`ATI` on the module used here: firmware `EG800QEULCR01A07M04`, registered on Free (20815), LTE band 3.

| Feature | Result |
|---|---|
| SMS (text mode, `+CMTI` notification) | Works. `AT+CMGL="REC UNREAD"` always returns an empty list, `AT+CMGL="ALL"` works. Right after sending an SMS, the module can take more than 5 s to answer the next command |
| Voice calls, VoLTE/IMS (`AT+CIREG`, `AT+CAVIMS`, `AT+CLIP`, `ATS0`) | Not supported by this firmware |
| Text-to-speech and audio playback (`AT+QTTS`, `AT+QAUDPLAY`, `AT+QPSND`) | Not supported |
| PCM audio interface (`AT+QDAI`) | Present |
| Network time (`AT+CTZU`) | Disabled, clock stays at 1970 |
| NTP (`AT+QNTP`) | Available |
