# EG800Q_smsResponder

An SMS responder on FreeBSD. Every SMS received by a **Quectel EG800Q-EU** 4G module is answered by **Claude** (through the Claude Code CLI), in French, in a single SMS. It runs as a FreeBSD service, starts at boot, and logs every SMS exchanged.

The folder also holds an SMS time server and small tools to test the module.

## How it works

```
phone ──SMS──▶ EG800Q-EU ──USB──▶ FreeBSD: sms_claude.py ──▶ claude -p (web search only)
phone ◀──SMS── EG800Q-EU ◀──USB── FreeBSD: answer cut to 160 characters ◀──┘
```

1. `sms_claude.py` drives the module with AT commands over its USB port.
2. It lists the SMS stored in the module every 5 seconds, and 1 second after the module announces a new one (`+CMTI`).
3. For each SMS: if it opens a conversation without "Bonjour", it sends back a politeness reminder; otherwise it asks Claude and sends the answer.
4. Then it deletes the SMS from the module, whose memory only holds 10 messages.

## Hardware

| | |
|---|---|
| Module | Quectel EG800Q-EU (USB ID `2c7c:6007`) |
| Chipset | Qualcomm QCX216 |
| Network | LTE Cat 1 bis only, no 2G/3G fallback; LTE FDD bands B1/B3/B5/B7/B8/B20/B28 |
| GNSS | None |
| Firmware tested | `EG800QEULCR01A07M04` |

**Wiring: use the module's own USB-C port.** One cable to the FreeBSD machine carries both power (it charges the battery) and data. FreeBSD sees the module as a USB network card (`cdce0`, `ue0`) and creates no serial port for it, so the script talks to the module's AT interface directly with pyusb (port `usb`).

The module's UART through a USB-serial adapter (FTDI) also works (port `/dev/cuaU0`, or `auto`), but it is not recommended: the UART is probably 1.8 V, so a 3.3 V adapter receives garbled bytes, and nothing readable gets through once the USB-C is also plugged in. Never wire the adapter's VCC pin to the module board.

## Files

| File | Purpose |
|---|---|
| `tools/sms_claude.py` | The SMS responder |
| `tools/eg800q.py` | Shared code: AT commands over USB or UART, GSM text encoding, SMS loop, SMS log |
| `rc.d/sms_claude` | FreeBSD service: runs `sms_claude.py` at boot under `daemon(8)` |
| `devd/eg800q.conf` | devd rule: gives the module's USB device to the `dialer` group when it is plugged in |
| `newsyslog/sms.conf` | Monthly rotation of `/var/log/sms` |
| `tools/sms_time_server.py` | SMS time server: answers `heure` or `time` with the current time |
| `tools/at_check.py` | Prints identity, firmware, SIM, signal, registration and bands over a serial port |
| `usr/check_eg800q.py` | For a QuecPython firmware, if one exists for this module: lists the modules it provides |

## Installation on FreeBSD

In the commands below, `youruser` is the unprivileged account that will run the service: replace it with your own login. Run the commands as root unless noted, from this folder.

### 1. Packages

The `py312-` prefix follows the default Python version of the package repository (`pkg search pyserial` shows the right one).

```
pkg install python3 py312-pyserial py312-pyusb
```

### 2. The user

The service never runs as root. The user must be in the `dialer` group, which gets access to the module:

```
pw groupmod dialer -m youruser
```

Log out and back in for the new group to apply (`id` must list `dialer`).

### 3. USB access

A raw USB device belongs to root. The devd rule gives the module, and only it (`2c7c:6007`), to the `dialer` group each time it is plugged in, including at boot:

```
mkdir -p /usr/local/etc/devd
install -m 444 devd/eg800q.conf /usr/local/etc/devd/
service devd restart
```

Unplug and plug the module back, then check that its device has group `dialer` and mode `crw-rw----`:

```
ls -lL /dev/ugen*
```

### 4. Claude Code

Claude Code must work for `youruser`. On FreeBSD it runs through the Linux binary compatibility. As `youruser`, create a long-lived token (it needs a Claude Pro or Max subscription), then check that `claude` answers:

```
claude setup-token
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
echo "Bonjour" | claude -p --tools "" --output-format text
which claude
```

`which claude` gives the path to pass to `--claude`, for example `/usr/local/bin/claude`. Never commit the token.

### 5. Test by hand

As `youruser`, in the shell where the token is exported:

```
python3 tools/sms_claude.py usb --claude /usr/local/bin/claude --sms-log /tmp/sms.log
```

The log must show `ready on USB interface 2, waiting for SMS`. Send "Test" (you get the politeness reminder), then "Bonjour" (Claude introduces itself), then a question. Stop with Ctrl+C.

### 6. Service

```
mkdir -p /usr/local/libexec/eg800q
install -m 444 tools/eg800q.py tools/sms_claude.py /usr/local/libexec/eg800q/
install -m 555 rc.d/sms_claude /usr/local/etc/rc.d/sms_claude
install -m 600 /dev/null /usr/local/etc/sms_claude.env
echo 'CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...' > /usr/local/etc/sms_claude.env
mkdir -p /usr/local/etc/newsyslog.conf.d
install -m 444 newsyslog/sms.conf /usr/local/etc/newsyslog.conf.d/
sysrc sms_claude_enable=YES sms_claude_user=youruser sms_claude_port=usb
sysrc sms_claude_args="--claude /usr/local/bin/claude"
sysrc sms_claude_env_file=/usr/local/etc/sms_claude.env
service sms_claude start
```

The token file stays readable by root only: rc.d reads it before starting the script as `youruser`. Then reboot and check that everything comes back by itself:

```
service sms_claude status
tail -f /var/log/messages /var/log/sms
```

### Updating

After `git pull`, copy the scripts again and restart:

```
install -m 444 tools/eg800q.py tools/sms_claude.py /usr/local/libexec/eg800q/
install -m 555 rc.d/sms_claude /usr/local/etc/rc.d/sms_claude
service sms_claude restart
```

## Settings

### Service (`sysrc`)

| Variable | Default | Meaning |
|---|---|---|
| `sms_claude_enable` | `NO` | `YES` to start at boot |
| `sms_claude_user` | none, required | Unprivileged user for whom `claude` works, member of `dialer`. The service refuses to start without it or with root |
| `sms_claude_port` | `usb` | `usb` (module's USB port), `usb:3` (its other AT interface), `auto` (FTDI adapter found by its driver) or a device such as `/dev/cuaU0` |
| `sms_claude_args` | empty | Options of `sms_claude.py`, at least `--claude PATH` |
| `sms_claude_env_file` | none | Root-only file with `CLAUDE_CODE_OAUTH_TOKEN=...` |
| `sms_claude_sms_log` | `/var/log/sms` | SMS log, `root:dialer`, mode 660; empty for no SMS log |
| `sms_claude_dir` | `/usr/local/libexec/eg800q` | Folder of `sms_claude.py` and `eg800q.py` |
| `sms_claude_python` | `/usr/local/bin/python3` | Python interpreter |

The service runs `daemon(8)`, which restarts the script 5 s after any exit (module unplugged, error) and sends its output to syslog with the tag `sms_claude`. `HOME` is set to the user's home directory so that `claude` finds its settings.

### Script (`sms_claude.py`)

| Option | Meaning |
|---|---|
| `PORT` | Same values as `sms_claude_port` |
| `--claude PATH` | `claude` executable (default: `claude` from `PATH`; a service has a short `PATH`) |
| `--model NAME` | Passed to `claude --model` |
| `--allow NUMBER` | Only answer this number (repeatable). Default: every phone number |
| `--conversation-timeout MINUTES` | Silence that ends a conversation (default 30) |
| `--sms-log FILE` | Append one line per SMS received or sent |
| `--trace` | Log every byte exchanged with the module |

## Behaviour

### Conversations and politeness

- A conversation is all the SMS from one number. It ends after 30 minutes without SMS from it, or when the script restarts.
- Its first SMS must start with "Bonjour" or "Bonsoir" (any case, accents ignored). Otherwise the sender gets `La politesse avant tout : commencez par « Bonjour » et je répondrai avec plaisir à votre message.`, Claude is not called, and the conversation does not start.
- The answer to that first SMS starts with Claude introducing itself as an AI by Anthropic that answers in SMS format, then answers the question if the greeting holds one.
- Within a conversation, Claude gets the last 10 exchanges with that number, so follow-up questions keep their context.
- Claude always answers in French, in at most 150 characters, plain text, without links.

### Claude and security

Anyone who knows the number can send a prompt, so Claude is locked down:

- Its only tool is **WebSearch** (`--tools "WebSearch"`, no MCP), for weather, news or schedules. The search runs on Anthropic's side. WebFetch is left out on purpose: it would load pages from this machine, so an SMS could make it read the local network.
- It runs in an empty temporary directory, and the SMS text goes through stdin, never on the command line.
- It gets 5 turns (`--max-turns 5`) and 2 minutes. If it fails, the log shows `claude error: ...` and the sender gets `Désolé, je ne peux pas répondre pour le moment.`
- Operator short codes never get an answer. Every SMS from a phone number uses your Claude quota; `--allow` restricts the numbers.

### SMS text

- Answers are sent in the GSM 7-bit alphabet, 160 characters at most: é, è, à, ù and Ç are kept, `ç` becomes `c`, `€` becomes `EUR`, `°` is dropped, other characters become `?`. Longer answers are cut and end with `...`.
- An SMS holding a character outside that alphabet (`ç`, `’`, an emoji) is sent by the phone in UCS2, which the module shows as hex. The script decodes it, emoji included. A single emoji alone without variation selector (`2764`) cannot be told from a number and stays as hex.
- A question longer than 160 characters arrives as several SMS, answered one by one.

### Module memory and errors

- Every SMS is deleted once handled, answered or not: the module memory (`ME`) holds 10 SMS, and no new SMS arrives once it is full. At startup, SMS already in memory are logged then deleted without answer.
- An SMS listed with no text yet (read while the module was still storing it) is read again, up to 3 scans, before it counts as empty.
- If a reply cannot be sent, the SMS is kept and tried again, 3 times at most. An SMS already answered whose deletion failed is only deleted again, never answered twice.
- `+CMGS: n` counts as sent even if the OK that follows is lost or garbled. A listing with a byte above 0x7F (impossible in GSM text mode) or a malformed `+CMGL` header is ignored and read again.
- Over USB, a reader thread keeps a USB read pending with no timeout: a timed-out bulk read would drop the bytes it had already received.

## Logs

| Where | What |
|---|---|
| `/var/log/messages`, tag `sms_claude` | Script log: start, each SMS, Claude errors, battery voltage and charge state every minute (`battery: 4.05 V, not charging`) |
| `/var/log/sms` | One line per SMS: `2026-10-24 17:40:12 IN  +33612345678 Bonjour` and `... OUT +33612345678 ...`. Received SMS are logged once, sent SMS only once sent. Rotated monthly, 12 compressed archives |
| `--trace` | Every byte to (`>>`) and from (`<<`) the module, in the script log |

`/var/log/sms` holds the text of every SMS: it is readable by root and the `dialer` group only.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Access denied` or `module not found on USB` | The module device is not in group `dialer` | `ls -lL /dev/ugen*`; check the devd rule (step 3), or `chgrp dialer /dev/ugenX.Y && chmod 660 /dev/ugenX.Y` |
| `claude error: ... Not logged in` | No token for the service | Check `/usr/local/etc/sms_claude.env` and `sms_claude_env_file`, or run `claude setup-token` again |
| `claude error: ... No such file or directory` | `claude` not found | Pass the path from `which claude` with `--claude` |
| `daemon: ppidfile ... Permission denied` or `initgroups ... Operation not permitted` | Old version of `rc.d/sms_claude` | Install the current one |
| `AT -> timeout` at startup | Module not ready yet (boot, replug) | Nothing: `daemon` restarts the script 5 s later |
| `battery` below 3.5 V | Battery too low for the radio peaks | Charge it; the UART then garbles bytes, USB is unaffected |
| Garbled bytes (`\xf8\xfc...`) on `/dev/cuaU0` | UART voltage mismatch with the adapter | Use the `usb` port |

## Other tools

**SMS time server.** Answers `heure` or `time` with the machine's local time, for example `Il est 14:32 - samedi 24/10/2026`. Same port values and SMS handling as `sms_claude.py`:

```
python3 tools/sms_time_server.py usb --sms-log /tmp/sms.log
```

**AT check.** Prints identity, firmware, SIM, signal, registration and bands over a serial port (UART adapter, or the module's AT port on Windows with the Quectel USB drivers):

```
python3 tools/at_check.py /dev/cuaU0
```

## Tested module

`ATI`: firmware `EG800QEULCR01A07M04`, registered on Free (20815), LTE band 3.

| Feature | Result |
|---|---|
| SMS (text mode) | Works. `AT+CMGL="REC UNREAD"` always returns an empty list, `AT+CMGL="ALL"` works. Right after sending an SMS, the module can take more than 5 s to answer |
| `+CMTI` (new SMS) | Sent to the USB AT port (`AT+QCFG="urc/port"` is `usbat`), not to the UART |
| USB interfaces | 0 diagnostics, 1 to 3 serial (2 and 3 answer AT), 4 and 5 network (CDC ECM) |
| Voice calls, VoLTE/IMS (`AT+CIREG`, `AT+CAVIMS`, `AT+CLIP`, `ATS0`) | Not supported by this firmware |
| Text-to-speech and audio playback (`AT+QTTS`, `AT+QAUDPLAY`, `AT+QPSND`) | Not supported |
| PCM audio interface (`AT+QDAI`) | Present |
| Network time (`AT+CTZU`) | Disabled, clock stays at 1970 |
| NTP (`AT+QNTP`) | Available |
| Battery (`AT+CBC`) | Voltage in mV; charge state stays `not charging` on USB power |
