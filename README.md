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
| `usr/check_eg800q.py` | Module (QuecPython REPL) | Prints model, firmware and IMEI, and lists the QuecPython modules the firmware provides |

## Usage

Install the [Quectel USB drivers](https://python.quectel.com/wp-content/uploads/2024/09/Quectel_Windows_USB_DriverA_Customer_V1.1.13.zip) on Windows, then find the *AT Port* in Device Manager.

```
pip install pyserial
python tools/at_check.py COM5
```

On FreeBSD the port is usually `/dev/cuaU0` (or `cuaU0.x`).
