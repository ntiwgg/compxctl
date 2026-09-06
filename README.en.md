# compxctl

**One command for CompX / Ardor Gaming mice on Linux: switch the polling rate (125 / 500 / 1000 Hz) and the DPI levels right on the device — through the proprietary HID protocol that only the vendor's Windows software speaks.**

`compxctl` is a single-file Python CLI. It talks to the mouse's configuration interface (USB VID `0x25A7`), changes the polling rate and the DPI on the chip level, and writes the setting to EEPROM so it survives unplugging. A built-in `check` command honestly shows what rate you actually got. No daemon, no GUI, no kernel module. Root is needed once, to install the udev rules.

```bash
python3 compxctl.py set 1000   # apply 1000 Hz now; the rate survives re-plug
python3 compxctl.py check      # verify: move the mouse for ~1.5 s while it measures
```

## History: why this exists

I play CS and moved to Linux. My CompX / Ardor Gaming mouse worked, but its default polling rate was uncomfortable, and the vendor ships its configuration utility for Windows only. There was no ready Linux solution: the options boiled down to living with it or booting Windows for a single setting.

So I reverse-engineered the mouse's proprietary HID protocol and built `compxctl` — a small CLI that switches the polling rate and the DPI levels right on the device, including an EEPROM write so the setting survives unplugging.

It was a two-person job: I set the direction, worked out the protocol and verified every step on the real mouse. The code was written and refactored together with an AI assistant.

Along the way we found a good bug: an early version of the rate write clobbered the mouse's DPI fields (registers `0x0002..0x0005`) and broke the DPI-cycle button. Fixed — the EEPROM write now stores only two bytes at `0x0000` and leaves neighbouring registers alone. The fix is recorded in git as the commit "Preserve DPI level fields…".

Over time the tool grew from a one-setting switcher into a small diagnostic utility: it shows device state, DPI levels and battery.

## Features

- `set 125|500|1000` — switch the polling rate and write it to the mouse's EEPROM (the setting survives re-plug);
- `check` — measure the actual polling rate: keep the mouse moving for ~1.5 s while events are counted;
- `dpi list` — show the table of all eight DPI slots and the active level;
- `dpi N` — write a DPI value into the active slot (the write is verified by a readback);
- `dpi --slot S N` — same, but into an explicit slot `0..5`;
- `status` — a device snapshot: PID, polling rate, active DPI level, slots, battery;
- `battery` — charge level and charging state;
- `probe --start ADDR --length LEN` — dump a window of the config memory and interpret the known fields;
- `--version` — print the version (`compxctl 1.1.0`).

Running without a subcommand (`python3 compxctl.py`) is equivalent to `status`.

## Quick start

### Requirements

- Linux and Python ≥ 3.10;
- the packages from `requirements.txt`;
- access to the mouse's USB/HID nodes (the udev rules from this repository, installed once).

### Installing dependencies

```bash
python3 -m venv .venv && source .venv/bin/activate   # optional, isolated
pip install -r requirements.txt
```

A note: on Linux `hidapi` often has no ready-made wheel and is built from source — you need a compiler and headers. On Debian/Ubuntu that is roughly `sudo apt install build-essential python3-dev libudev-dev libusb-1.0-0-dev` (pyusb additionally needs libusb).

Dependency map by command:

| Command | Dependencies |
|---------|--------------|
| `set` | `hidapi` — hidraw path discovery and packet delivery; `pyusb` — the raw-USB delivery channel (recommended; without it `set` only works when the config interface has a hidraw node, and prints a warning) |
| `check` | `evdev` |
| `dpi`, `battery`, `probe` | `pyusb` |
| `status` | `pyusb` (all reads) + `hidapi` (the PID line) |

### Access rights (udev)

Running as root works without any udev rules. For regular users, install the access rules once:

```bash
sudo cp 99-compx-mouse.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Then unplug and re-plug the mouse (a re-login also works). The rule file grants access:

| Node type | Access |
|-----------|--------|
| `hidraw` (the `set` channel) | group `users`, mode `0660`, plus `TAG+="uaccess"` for the active logind session |
| `input` (read by `check`) | group `input`, mode `0660`, plus `TAG+="uaccess"` |
| `usb` device node (pyusb commands) | group `users`, mode `0664` |

`TAG+="uaccess"` covers a normal desktop session (logind) without extra steps. For SSH sessions or setups without logind, add yourself to the groups and re-login:

```bash
sudo usermod -aG users,input $USER
```

The pyusb commands (`dpi`, `status`, `battery`, `probe`) go through the usb device node, which the rule grants to group `users` **without** `uaccess` — in such environments they need group `users` or a one-off run via sudo.

### Verify the install

```console
$ python3 compxctl.py --version
compxctl 1.1.0
```

### Make it one keystroke

The tool is a single self-contained file, so it can live anywhere and be aliased. With fish:

```fish
# ~/.config/fish/config.fish — adjust the path to your copy
alias 1000 'python3 ~/compx-control/compxctl.py set 1000'
alias 500  'python3 ~/compx-control/compxctl.py set 500'
alias 125  'python3 ~/compx-control/compxctl.py set 125'
```

Restart the shell, then typing `1000` switches the mouse. The same trick works in bash/zsh (use single quotes there).

## How it works

### Which mice are supported

Targets the CompX / Ardor Gaming family on USB vendor ID `0x25A7`:

| PID | Hardware | How it shows up on the bus |
|-----|----------|----------------------------|
| `0xFA7B` | Wired mouse | Identified as a *Dual Mode Mouse* in `lsusb` |
| `0xFA7C` | 2.4 GHz receiver dongle | `Areson Technology Corp 2.4G Wireless Receiver` |
| `0xFA03`, `0xFA93` | Other units of the same family | Recognized by the same VID/PID list |

```console
$ lsusb -d 25a7:
Bus 001 Device 003: ID 25a7:fa7c Areson Technology Corp 2.4G Wireless Receiver
```

The protocol was captured and verified on the **Ardor Gaming Ulta** (Compx brand). The remaining PIDs belong to the same family and are handled by the same code path, but were not individually verified.

For `check`, the mouse input nodes are found by their fixed by-id names:

- `/dev/input/by-id/usb-Compx_2.4G_Wireless_Receiver-event-mouse`
- `/dev/input/by-id/usb-Compx_2.4G_Dual_Mode_Mouse-event-mouse`

with a sysfs scan of `/dev/input/event*` by vendor/product as a fallback. A specific node can be passed explicitly: `check --device /dev/input/eventN`.

### The protocol: three reports per rate

Switching is done on the chip level through the mouse's configuration HID interface (vendor usage pages `0xFF01`–`0xFF04`, with interface #1 as a fallback). Each rate is exactly three reports: the first two apply the rate live, the third writes it to EEPROM so it survives unplug / re-plug.

| Rate | ① live rate — report `0x06` | ② live interval — report `0x08` / `0x11` | ③ persist — report `0x08` / `0x07` |
|------|------------------------------|------------------------------------------|--------------------------------------|
| 125 Hz | `06 11 00 00 00 00 00 00` | `08 11 00 00 00 06 08` + zero pad | `08 07 00 00 00 02 08 4D …` |
| 500 Hz | `06 11 00 01 00 00 00 00` | `08 11 00 00 00 06 02` + zero pad | `08 07 00 00 00 02 02 53 …` |
| 1000 Hz | `06 11 00 02 00 00 00 00` | `08 11 00 00 00 06 01` + zero pad | `08 07 00 00 00 02 01 54 …` |

What the variable bytes mean:

- ① byte 3 of the report (0-based, report id included) is the **rate code**: `0x00` → 125 Hz, `0x01` → 500 Hz, `0x02` → 1000 Hz;
- ② byte 6 is the **report interval in milliseconds**: `0x08` → 8 ms → 125 Hz, `0x02` → 2 ms → 500 Hz, `0x01` → 1 ms → 1000 Hz;
- ③ is the **EEPROM write** (see below) — this is what makes the rate stick across unplugging.

The whole table lives in one place in the code — `POLLING_VARIANTS`, with the EEPROM frames built by `_eeprom_packet()` — so the protocol definition has a single source of truth.

### EEPROM write: two bytes at `0x0000`, the frame sums to `0x55`

The third packet is **not** replayed from a capture — it is assembled programmatically as an ordinary 17-byte config-memory write frame. Exactly **2 bytes** are written at address `0x0000`: the interval code (the same as packet ② byte 6) and its additive complement to `0x55`. Then comes zero padding and the final checksum byte: the sum of all 17 frame bytes must be `≡ 0x55 (mod 256)`. The checksum is computed, never taken from a capture.

| Rate | Full EEPROM packet (17 bytes) |
|------|-------------------------------|
| 125 Hz | `08 07 00 00 00 02 08 4D 00 00 00 00 00 00 00 00 EF` |
| 500 Hz | `08 07 00 00 00 02 02 53 00 00 00 00 00 00 00 00 EF` |
| 1000 Hz | `08 07 00 00 00 02 01 54 00 00 00 00 00 00 00 00 EF` |

Here `08 07` is report `0x08` with write opcode `0x07`, then the address `00 00`, length `02`, the code + complement pair, and the `EF` tail. The built frames are checked against known-good captures in the self-check mode (`COMPX_SELFCHECK=1`).

Why exactly 2 bytes — that is the story of a bug. An early version appended a captured "tail" to the rate, and the write spilled into registers `0x0002..0x0005` (the DPI-level count and the active level index): the mouse lost its DPI settings and the DPI-cycle button broke. Now only the code + complement pair is written and the neighbouring fields are left alone. The fix is recorded in git (commit "Preserve DPI level fields…").

### Config-memory register map

| Address | Contents |
|---------|----------|
| `0x0000` | polling-rate code + complement (e.g. `01 54` = 1000 Hz) |
| `0x0002` | number of DPI levels |
| `0x0004` | active DPI level index + complement at `0x0005`; treated as 1-based (see limitations) |
| `0x000C`–`0x002B` | eight DPI slots, 4 bytes each: `x y mul crc` |
| `0x0060`–`0x009F` | button matrix and similar; past `0x00A0` the memory is empty (`0xFF`) |

### The DPI codec

The mouse stores a code, not the DPI value itself: `code = DPI / 50 − 1`. So 400 → `0x07`, 800 → `0x0F`, 1000 → `0x13`, 1600 → `0x1F`, …, 6400 → `0x7F`. A DPI slot is 4 bytes: for the plain encoding `x = y = code`, `mul = 0`, `crc = 0x55 − x − y − mul` (an example slot row: `13 13 00 2F` = 1000 DPI).

Slots `0..5` hold the plain encoding and are writable. Slots `6..7` on the verified mouse hold an extended encoding (`code > 0x7F` or `mul ≠ 0`) that is not understood yet: `dpi list` and `status` show them as raw bytes, and `dpi` never overwrites them.

A DPI write goes to the active slot (per register `0x0004`) or to an explicit slot via `--slot`, and is immediately verified by reading back: without the readback echo the command fails.

### Delivery and what "success" means

The mouse never acknowledges a report, and the config interface is finicky about kernel-driver ownership. So each of the three `set` packets is sent over several independent channels:

1. **raw-USB SET_REPORT** (pyusb/libusb) to interface 1 — with the kernel driver detached for the transfer and re-attached right after;
2. **hidapi** — `send_feature_report` first, then a plain output `write` on the same handle;
3. **raw hidraw ioctl** — `HIDIOCSFEATURE` / `HIDIOCSOUTPUT` as the last-resort path around hidapi.

Success is counted per **distinct** packet, not per delivery: a packet delivered by more than one channel still counts once. A rate change counts as successful once **at least 2 of the 3 distinct packets** were delivered (the `Packets delivered: N/3 distinct (minimum for success: 2)` line). If several supported devices are connected, every control path found gets the new rate.

Honest caveat: the device sends no acknowledgement. "Success" means the packets were delivered, not that the chip confirmed them — that is exactly what `check` is for.

### What `check` measures

`check` does not talk to the chip. It opens the mouse's input node and counts relative-motion events (`EV_REL`) for ~1.5 seconds while you move the mouse. The rate is the event count divided by the window — a practical, honest verification, not a lab instrument. If the mouse stays still you get an honest `~0 Hz` (and that still counts as a successful run, exit code `0`).

## Example output

The program prints in English; the snippets below are its real lines. Slot, PID and battery values in the examples are illustrative.

```console
$ python3 compxctl.py --version
compxctl 1.1.0

$ python3 compxctl.py set 1000
Rate: 1000 Hz
PID: 0xfa7b
Packets delivered: 3/3 distinct (minimum for success: 2)
EEPROM write: done
```

- `Rate` — what you asked for; `PID` — the product id found on the bus;
- `Packets delivered` — how many distinct packets of the three reached the device (a packet delivered over several channels counts once); success starts at `2/3`;
- `EEPROM write` — `done` when the persistence report went through; `not confirmed (rate may reset after re-plug)` when it did not.

```console
$ python3 compxctl.py check
Event device: /dev/input/event9
Move the mouse… measuring for ~1.5 s
Measured rate: ~1000 Hz
```

If the mouse was idle:

```console
$ python3 compxctl.py check
Event device: /dev/input/event9
Move the mouse… measuring for ~1.5 s
Measured rate: ~0 Hz (no mouse movement detected during the window)
Hint: keep moving the mouse while `check` runs.
```

```console
$ python3 compxctl.py status
Device: CompX mouse (PID 0xfa7b)
Polling rate: 1000 Hz (register 0x0000)
Active DPI level: index 0x01 (register 0x0004) → slot [0]: 1000
DPI slots: [0] 1000 ← active [1] 1600 [2] 3200 [3] 400 [4] 6400 [5] empty [6] empty [7] empty
Battery: 100% (not charging) [2.4G mode]

$ python3 compxctl.py dpi list
DPI slots (register 0x0004 = active level index; '*' marks the active slot):
  slot  addr   x    y    mul  crc   value
  *0    0x000C 13   13   00   2F    1000
   1    0x0010 1F   1F   00   17    1600
   2    0x0014 3F   3F   00   D7    3200
   3    0x0018 07   07   00   47    400
   4    0x001C 7F   7F   00   57    6400
   5    0x0020 FF   FF   FF   FF    empty
   6    0x0024 FF   FF   FF   FF    empty
   7    0x0028 FF   FF   FF   FF    empty
Active DPI level: slot 0 (index 0x01) → 1000

$ python3 compxctl.py dpi 2400
DPI set: active level → 2400 (was 1000)
slot 0 updated, verified

$ python3 compxctl.py battery
Battery: 100% (not charging) [2.4G mode]

$ python3 compxctl.py probe --start 0x0000 --length 0x02
0x0000: 01 54
Interpretation:
  polling rate code at 0x0000: 0x01 = 1000 Hz
```

`dpi 2400` writes to the active slot, `dpi --slot 3 2400` to an explicit slot. The `updated, verified` line means the write was confirmed by reading back.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | success — including a legitimate `~0 Hz` measurement in `check` |
| `1` | a runtime error — device not found, permission problem, rejected packets, no config-memory reply |
| `2` | command-line argument error (argparse): unknown command, invalid value, nonexistent `--slot` |
| `130` | interrupted with Ctrl+C |

## Troubleshooting

| Symptom (as printed) | Likely cause | Fix |
|----------------------|--------------|-----|
| `Error: No CompX mouse found (VID 0x25A7). Check the USB connection: lsusb -d 25a7:` | Mouse not connected or not enumerated | Run `lsusb -d 25a7:`; re-plug the mouse / reseat the receiver; try another USB port |
| `No access to the HID device. Reinstall the udev rules (99-compx-mouse.rules) and re-plug the mouse.` | Missing/stale udev rules or group membership (the `set` side) | Re-run the udev install, re-plug the mouse, check the `users` and `input` groups, re-login |
| `Error: Cannot claim the CompX config interface (interface 1): … Access denied …` | No access to the usb device node (pyusb commands) | Reinstall the udev rules, add yourself to group `users` and re-login; a one-off `sudo python3 compxctl.py …` rules out permissions |
| `Error: The 'usb' package (pyusb) is missing — it is required for this command. Install it with: pip install pyusb` | pyusb is not installed (needed by `dpi`/`status`/`battery`/`probe` and the raw-USB `set` channel) | `pip install pyusb`; make sure libusb is present on the system |
| `Error: The 'hid' package (hidapi) is missing — it is required for 'set'. Install it with: pip install hidapi` | hidapi is not installed (needed by `set` and the PID line of `status`) | `pip install hidapi` |
| `No read access to /dev/input/…` on `check` | Missing `input` group / no `uaccess` for the session | `sudo usermod -aG input $USER` + re-login; check the rules carry `TAG+="uaccess"` |
| `Measured rate: ~0 Hz (no mouse movement detected during the window)` | The mouse was idle during the measurement | Re-run `check` and keep moving the mouse for the whole ~1.5 s |
| `Error: the device rejected the polling-rate packets` + indented transport lines | Every transport failed | Read the indented lines — each names its transport (`usb:`, `feature:`, `output:`, …); try `sudo python3 compxctl.py set …` once to rule out permissions; re-plug and retry |

## Known limitations

- **Vendor-locked by design.** Only VID `0x25A7` with the listed PIDs is ever touched — which also means other mice are completely safe from it.
- **Experimental protocol.** It was captured on one device (Ardor Gaming Ulta, Compx branding). Sibling PIDs are handled identically but were not individually verified; a future firmware revision could change the reports.
- **No acknowledgement from the device.** Success means the packets were delivered; treat `check` as the ground truth.
- **EEPROM write on every `set`.** The persistence report rewrites the mouse's EEPROM on each run. Endurance is finite, but for a setting changed a few times a day this is a non-issue.
- **`check` is a practical estimate**, not a lab measurement: ~1.5 s window, human-driven movement.
- **The extended DPI encoding of slots 6–7 is not understood.** Slots 6 and 7 on the verified mouse hold a non-standard encoding: `dpi list`/`status` show them as raw bytes, and `dpi` never overwrites them.
- **The active-index semantics are "pending calibration".** The index at `0x0004` is treated as 1-based (`ACTIVE_LEVEL_OFFSET`); the exact meaning of the field is not fully confirmed.

## License

There is no LICENSE file yet. This is an unofficial reverse-engineering project. Not affiliated with, endorsed by, or supported by CompX, Ardor Gaming, Areson Technology, or their distributors. The protocol was obtained experimentally and is used at your own risk; the tool comes with no warranty.



