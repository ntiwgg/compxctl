# compxctl

**Switch the polling rate of CompX / Ardor Gaming mice from Linux — 125, 500 or 1000 Hz — by speaking the proprietary HID protocol that the vendor's Windows-only software uses.**

CompX / Ardor Gaming mice keep their polling rate in on-chip firmware, but the vendor ships configuration software for Windows only. There is no official Linux tool. `compxctl` is the result of reverse-engineering that protocol: a single-file Python CLI that replays the captured control reports to switch the rate on the device itself — including an EEPROM write so the setting survives unplugging. A built-in `check` command then proves the result by measuring the real rate from input events.

```bash
python3 compxctl.py set 1000   # apply 1000 Hz now; the rate survives re-plug
python3 compxctl.py check      # verify: move the mouse for ~1.5 s while it measures
```

No daemon, no GUI, no kernel module. Root is needed once, to install the udev rules.

---

## Why this exists

Gaming mice from this family are configured through a proprietary USB protocol that only the vendor's Windows tool speaks. On Linux you were left with bad options:

- keep whatever rate the mouse came with or was last set to under Windows;
- boot into Windows (or a VM with USB passthrough) every time you want to switch;
- just accept 125 Hz if the mouse defaults to it — a real handicap for competitive play.

The protocol itself is small and stable: every rate is a sequence of three HID reports sent to the mouse's configuration interface. Once those reports were identified, switching rates became a question of replaying bytes, not of emulating a driver. `compxctl` wraps that into two commands — `set` to apply a rate, `check` to measure the actual one.

## Hardware support

Targets the CompX / Ardor Gaming family on USB vendor ID `0x25A7`:

| PID     | Hardware | How it shows up on the bus |
|---------|----------|----------------------------|
| `0xFA7B` | Wired mouse | Identified as a *Dual Mode Mouse* in `lsusb` |
| `0xFA7C` | 2.4 GHz receiver dongle | `Areson Technology Corp 2.4G Wireless Receiver` |
| `0xFA03`, `0xFA93` | Other units of the same family | Recognized by the same VID/PID list |

```console
$ lsusb -d 25a7:
Bus 001 Device 003: ID 25a7:fa7c Areson Technology Corp 2.4G Wireless Receiver
```

The protocol was captured on the **Ardor Gaming Ulta** (Compx brand). The remaining PIDs belong to the same family and are handled by the same code path, but were not individually verified.

For `check`, the mouse input nodes are found by their fixed by-id names:

- `/dev/input/by-id/usb-Compx_2.4G_Wireless_Receiver-event-mouse`
- `/dev/input/by-id/usb-Compx_2.4G_Dual_Mode_Mouse-event-mouse`

with a sysfs scan of `/dev/input/event*` by vendor/product as a fallback.

## How it works

### The protocol: three reports per rate

Switching is done on the chip level through the mouse's configuration HID interface (vendor usage pages `0xFF01`–`0xFF04`, with interface #1 as a fallback). Each rate is exactly three reports: the first two apply the rate live, the third writes it to EEPROM so it survives unplug / re-plug.

| Rate    | ① live rate — report `0x06`    | ② live interval — report `0x08` / `0x11` | ③ persist — report `0x08` / `0x07` |
|---------|--------------------------------|------------------------------------------|--------------------------------------|
| 125 Hz  | `06 11 00 00 00 00 00 00`      | `08 11 00 00 00 06 08 …`                 | `08 07 00 00 00 06 08 …`             |
| 500 Hz  | `06 11 00 01 00 00 00 00`      | `08 11 00 00 00 06 02 …`                 | `08 07 00 00 00 06 02 …`             |
| 1000 Hz | `06 11 00 02 00 00 00 00`      | `08 11 00 00 00 06 01 …`                 | `08 07 00 00 00 06 01 …`             |

(`…` marks the fixed vendor payload, replayed byte-for-byte as captured.)

What the variable bytes mean:

- ① byte 3 of the report (0-based, report id included) is the **rate code**: `0x00` → 125 Hz, `0x01` → 500 Hz, `0x02` → 1000 Hz;
- ② byte 6 is the **report interval in milliseconds**: `0x08` → 8 ms → 125 Hz, `0x02` → 2 ms → 500 Hz, `0x01` → 1 ms → 1000 Hz;
- ③ carries the same interval byte but is the **EEPROM write** — this is what makes the rate stick across unplugging. Its tail bytes change with the rate and are not recomputed; they are replayed exactly as captured.

The whole table lives in one place in the code — `POLLING_VARIANTS` — so the protocol definition is a single source of truth.

### Delivery with redundancy

The mouse never confirms a report, and the config interface is finicky about kernel-driver ownership. `compxctl` therefore does not trust a single transport. Each of the three reports is sent over up to three independent channels:

1. **USB control transfer** (pyusb/libusb): a feature `SET_REPORT` to interface 1, with the kernel driver detached for the transfer and re-attached right after;
2. **hidapi**: `send_feature_report` first, then a plain output `write` on the same handle;
3. **raw hidraw ioctl**: `HIDIOCSFEATURE` / `HIDIOCSOUTPUT` as a last-resort bypass of hidapi.

Sends are retried briefly — after the pyusb step detaches and re-attaches the kernel driver, udev re-creates the hidraw node and it may not be openable for a moment.

Success is counted per **distinct** packet, not per delivery: a packet delivered by more than one channel (USB `SET_REPORT`, hidapi, raw hidraw ioctl) still counts once. A rate change counts as successful once **at least 2 of the 3 distinct packets** were delivered (`Packets delivered: N/3 distinct (minimum for success: 2)`). If several supported devices are connected, every control path found gets the new rate.

Honest caveat: the device sends no acknowledgement. "Success" means the packets were delivered, not that the chip confirmed them — that is exactly what `check` is for.

### What `check` measures

`check` does not talk to the chip. It opens the mouse's input node and counts relative-motion events (`EV_REL`) for ~1.5 seconds while you move the mouse:

```text
Event device: /dev/input/eventN
Move the mouse… measuring for ~1.5 s
Measured rate: ~1000 Hz
```

The rate is the event count divided by the window — a practical, honest verification, not a lab instrument.

## Requirements and installation

Requirements:

- Linux with Python 3;
- `hidapi` — device discovery and the primary report transport. Needed only for `set`: `check`, `--help`, and `--version` work without it (imported lazily);
- `pyusb` — optional direct USB `SET_REPORT` fallback channel (recommended; without it `set` still works via hidapi and prints a warning);
- `evdev` — event counting for `check` only.

```bash
pip install -r requirements.txt
```

Running as root works without any udev rules. For regular users, install the access rules once:

```bash
sudo cp 99-compx-mouse.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Then unplug and re-plug the mouse (a re-login also works). The rule file grants:

| Node type | Access |
|-----------|--------|
| `hidraw` (control channel of `set`) | group `users`, mode `0660`, plus `TAG+="uaccess"` for the active logind session |
| `input` (read by `check`) | group `input`, mode `0660`, plus `TAG+="uaccess"` |
| `usb` device node (pyusb fallback) | group `users`, mode `0664` |

`TAG+="uaccess"` covers a normal desktop session (logind) without extra steps. For SSH sessions or setups without logind, add yourself to both groups and re-login:

```bash
sudo usermod -aG users,input $USER
```

Verify the install:

```console
$ python3 compxctl.py --version
compxctl 1.0.1
```

## Usage

### `set` — apply a polling rate

```console
$ python3 compxctl.py set 1000
Rate: 1000 Hz
PID: 0xfa7b
Packets delivered: 3/3 distinct (minimum for success: 2)
EEPROM write: done
```

What the output means:

- `Rate` — what you asked for;
- `PID` — the product id of the device that was found on the bus;
- `Packets delivered` — how many **distinct** packets out of the three reached the device (`N/3 distinct`; a packet that went through more than one channel counts once). Success is `>= 2` distinct packets;
- `EEPROM write` — `done` when the persistence report went through; `not confirmed (rate may reset after re-plug)` when it did not.

Non-fatal problems are printed to stderr as `warning: …` lines without failing the command — for example `warning: usb: pyusb is not installed (pip install pyusb)` when the fallback channel is missing. If the device rejects the packets, the command fails with `Error: the device rejected the polling-rate packets`, followed by one indented line per failed transport.

### `check` — measure the actual rate

```console
$ python3 compxctl.py check
Event device: /dev/input/event9
Move the mouse… measuring for ~1.5 s
Measured rate: ~1000 Hz
```

Keep the mouse moving for the whole window. If it stays still you get an honest zero:

```console
$ python3 compxctl.py check
Event device: /dev/input/event9
Move the mouse… measuring for ~1.5 s
Measured rate: ~0 Hz (no mouse movement detected during the window)
Hint: keep moving the mouse while `check` runs.
```

Auto-detection uses the by-id names listed above, then a sysfs scan. If you need a specific node, pass it explicitly: `python3 compxctl.py check --device /dev/input/eventN`.

### Exit codes

| Code | Meaning |
|------|---------|
| `0`  | success (including a legitimate `~0 Hz` measurement) |
| `1`  | an error — device not found, permission problem, rejected packets |
| `130` | interrupted with Ctrl+C |

### Make it one keystroke

The tool is a single self-contained file, so it can live anywhere and be aliased. With fish:

```fish
# ~/.config/fish/config.fish — adjust the path to your copy
alias 1000 "python3 ~/compx-control/compxctl.py set 1000"
alias 500  "python3 ~/compx-control/compxctl.py set 500"
alias 125  "python3 ~/compx-control/compxctl.py set 125"
```

Restart the shell, then typing `1000` switches the mouse. The same trick works in bash/zsh with single quotes.

## Troubleshooting

| Symptom (as printed) | Likely cause | Fix |
|----------------------|--------------|-----|
| `Error: No CompX mouse found (VID 0x25A7). Check the USB connection: lsusb -d 25a7:` | Mouse not connected or not enumerated | Run `lsusb -d 25a7:`; re-plug the mouse / reseat the receiver; try another USB port |
| `No access to the HID device. Reinstall the udev rules (99-compx-mouse.rules) and re-plug the mouse.` | Missing/stale udev rules or group membership on the `set` side | Re-run the udev install, re-plug the mouse, make sure you are in `users` and `input`, re-login |
| `No read access to /dev/input/…` on `check` | Missing `input` group / no `uaccess` for the session | `sudo usermod -aG input $USER` + re-login; check the rules carry `TAG+="uaccess"` |
| `Measured rate: ~0 Hz (no mouse movement detected during the window)` | The mouse was idle during the measurement | Re-run `check` and keep moving the mouse for the whole ~1.5 s |
| `Error: the device rejected the polling-rate packets` + indented transport lines | Every transport failed | Read the indented lines — each names its transport (`usb:`, `feature:`, `output:`, …); try `sudo python3 compxctl.py set …` once to rule out permissions; re-plug and retry |
| Exit `0` with `warning: usb: pyusb is not installed …` | Optional `pyusb` fallback missing | Install it (`pip install pyusb`) — or ignore the warning if `EEPROM write: done` |

## Known limitations

Be honest about what this tool is and is not:

- **Vendor-locked by design.** Only VID `0x25A7` with the listed PIDs is ever touched — which also means other mice are completely safe from it.
- **Experimental protocol.** It was captured on one device (Ardor Gaming Ulta, Compx branding). Sibling PIDs are handled identically but were not individually verified; a future firmware revision could change the reports.
- **No acknowledgement from the device.** Success means the packets were delivered; treat `check` as the ground truth.
- **EEPROM write on every switch.** The persistence report rewrites the mouse's EEPROM on each run. Endurance is finite, but for a setting changed a few times a day this is a non-issue.
- **`check` is a practical estimate**, not a lab measurement: ~1.5 s window, human-driven movement.

## Repository layout

```
compxctl/
├── compxctl.py            # the whole CLI: discovery, transports, protocol table
├── 99-compx-mouse.rules   # udev access for hidraw / input / usb nodes
├── requirements.txt       # hidapi, pyusb (optional channel), evdev (check only)
├── README.md              # this file
└── README.ru.md           # русская версия
```

A short design note: this project was consolidated on purpose. The earlier prototypes — a GUI plus supporting scripts — split the logic across `main.py`, `set-rate.py` and `auto-scan.py` (since removed), and each kept its own copy of the packet table; any protocol fix had to be applied in three places. The current version collapses everything into one file with one table: discovery, transport, CLI, and `POLLING_VARIANTS` — the only place in the codebase where protocol bytes live. Adding another rate, if one is ever captured, means adding one row.

## Disclaimer

Unofficial reverse-engineering project. Not affiliated with, endorsed by, or supported by CompX, Ardor Gaming, Areson Technology, or their distributors. The protocol was obtained experimentally and is used at your own risk; the tool comes with no warranty.
