# compxctl

**Polling rate and DPI for CompX / Ardor Gaming mice on Linux — without the
proprietary Windows utility.**

`compxctl` is a single-file Python CLI. It does two things that live in two
very different places, and it is careful not to blur them:

| What | Where it lives | How compxctl does it | Packages needed |
|---|---|---|---|
| Polling rate | the host's USB polling loop | `usbhid.mousepoll` + the endpoint descriptor in sysfs | **none** |
| DPI, battery, config memory | inside the mouse chip | the vendor HID protocol over `/dev/hidraw*` | **none** |

There are no dependencies at all: both sysfs and hidraw are reachable from the
Python standard library. A test enforces that by reading `compxctl.py`'s own AST.

```console
$ compxctl rate
Device: 2.4G Dual Mode Mouse (25a7:fa7b)
Device advertises: 1000 Hz (1ms via interval, ep_81)
Host override: none (usbhid.mousepoll = 0)
Effective: 1000 Hz (from device descriptor)
```

Русский: [README.md](README.md)

## Why this exists (and why it is a rewrite)

The first version of compxctl changed the polling rate by writing the mouse's
EEPROM through a reverse-engineered vendor protocol. That works — it was
verified on hardware — but it is the most fragile and least portable way to do
it: irreversible, wear-limited, unusable on any other mouse, and dependent on
one captured firmware.

Linux already exposes the polling rate properly. The host polls an interrupt
endpoint at the interval the device advertises, and `usbhid` can override that
interval for all mice. So:

* **Reading the rate** is a sysfs read — exact, instant, no mouse movement, no
  event counting, no dependencies. The previous `check` command counted input
  events for 1.5 s and reported roughly double the real rate whenever the mouse
  moved diagonally (one mouse report carries `REL_X` *and* `REL_Y`, and the
  counter counted both). That command is gone; `rate` replaces it.
* **Setting the rate on the host** is one sysfs write to
  `/sys/module/usbhid/parameters/mousepoll`. Reversible, no EEPROM, no vendor
  protocol.
* **Writing the device's own setting** still exists, because it is the only way
  to change the mouse itself, but it is an explicit opt-in behind `--persist`.

In 3.0 the device is no longer opened through libusb: the config interface is a
HID interface, `usbhid` owns it, and the kernel hands it to user space as a
`/dev/hidrawN` node. Two consequences: `pyusb` is gone, and `status`/`battery`
stopped disturbing the mouse — libusb had to detach the kernel driver for the
duration of a command, which made the pointer freeze for a moment.

## How it works

The report descriptor of the config interface (189 bytes, read from the
verified unit) explains the whole protocol:

| Report | Type | Length | What it is |
|---|---|---|---|
| id 6 | FEATURE | 7 bytes | the first live interval packet |
| id 8 | FEATURE | 16 bytes | config-memory read/write, battery request |
| id 9 | INPUT | 16 bytes | every reply the firmware sends |

So a request goes out as one `fcntl.ioctl` with `HIDIOCSFEATURE` (which *is*
`SET_REPORT` of the declared type), and the answer is read from the input
report queue. `HIDIOCGFEATURE` (`GET_REPORT`) returns 17 zero bytes on this
firmware — replies only ever arrive as an input report, and that is a
measurement, not a guess.

The interface also carries traffic of its own: an acknowledge for every live
interval packet (`09 11 …`) and for every config write (`09 07 …`, echoing the
bytes that were written). All of it is filtered out by opcode, address and
checksum instead of being mistaken for an answer — and it is not taken for a sign
that somebody else is on the node: an acknowledge is the device talking about
itself, not an answer to a question. The difference shows in the diagnosis: only
acknowledgements coming back means "the mouse is asleep" and stops the
questions, while an answer to a *different* question means "something else is
talking to this node".

The node is opened non-blocking (`O_NONBLOCK`): a blocking hidraw read has no
timeout of its own, so it could sleep indefinitely if the report were taken
between the readiness check and the read. Every deadline here is the tool's own,
never the kernel's.

**The node is chosen by its report descriptor, not by interface number.** The
descriptor is read from sysfs (world-readable) and parsed: the right node is the
one that declares id `0x08` as a FEATURE report and `0x09` as INPUT. On the
verified unit that is `hidraw1`; the motion interface (`hidraw0`) declares no
vendor report at all, and a feature report sent there fails with `EPIPE`. If the
descriptor cannot be read the tool does not guess: it falls back to the interface
class and lets the reply check decide. If the descriptor *is* readable and
contradicts, the command refuses to talk to that node at all — protection for the
three product ids the code supports but nobody has verified, where the
interfaces may be numbered differently while the reports they declare cannot lie.

**Two runs do not trip over each other.** hidraw hands every opener its own copy
of every input report (measured: a process reading the node alongside the tool
saw all the replies, and the tool was unaffected). The flip side is that two
compxctl instances can see each other's frames and the protocol has no sequence
number, so the node is held under `flock` for the duration of a request: a second
run waits up to two seconds and then says the node is busy (measured: 15 out of
15 concurrent runs went through, the second one waiting and then working).

The lock is taken on a descriptor of its own, and the descriptor that gets read
is opened only afterwards. Waiting with the reading descriptor already open
would spoil the queue by itself: an opener is subscribed to report copies at
`open()` time, so it would find the previous session's replies at the head of
its queue — and without a sequence number in the protocol there is nothing to
tell them from its own. The queue is still drained before every command, but
now there is nothing to drain. The kernel drops the
lock when the descriptor closes or the process dies, so nothing goes stale. It
does not constrain other software that opens the same node — it is a warning, not
a fence.

## Install

```console
$ pip install .        # installs the compxctl command; there is nothing else to install
```

Or just copy `compxctl.py` — it runs on a bare Python 3.10+. Device commands
need access to `/dev/hidraw*` (see Permissions); reading sysfs needs nothing.

## Usage

### Polling rate

```console
$ compxctl rate                 # show: device, host override, effective rate
$ sudo compxctl rate host 1000  # host-side, all USB mice, immediate, reversible
$ compxctl rate device 1000 --persist      # also rewrite the device's EEPROM
```

`rate host` writes `usbhid.mousepoll`, which applies to **every USB mouse** on
the machine. It is not persistent, and *how* you make it permanent depends on
how `usbhid` is built — the command prints the right line for your kernel:

* **loadable module** — `options usbhid mousepoll=1` in `/etc/modprobe.d/`
* **built into the kernel** — `usbhid.mousepoll=1` on the kernel command line;
  `/etc/modprobe.d/` is silently ignored for built-ins

The tool detects which case you are in (`usbhid_is_builtin()` reads
`/lib/modules/$(uname -r)/modules.builtin`). This is not hypothetical: on the
machine this was developed on, `usbhid` is built in and a pre-existing
`/etc/modprobe.d/mousepoll.conf` was doing nothing at all.

`rate device HZ --persist` sends two live interval packets, one EEPROM write, and
reads the value back:

```console
$ compxctl rate device 500 --persist
Device EEPROM updated to 500 Hz (0x02).
  EEPROM written and verified by readback; live interval packets sent. The port was not reset (--reset-port does that).
  What the mouse reports at afterwards is not measured here.
```

The port is **not** reset by default, and the measurement is why: the reset was
meant to make the host re-read the descriptor, but the descriptor does not follow
the stored code — `bInterval` stays `01` with either code, on both sides of a
reset the kernel logged as real. Its price was real: a second udev rule granting
access to the *whole* USB device node, and a second during which the entire
receiver leaves the bus — keyboard included, if one shares it. When the re-enumeration is
actually wanted (say, to see what the host reports), ask for it:

```console
$ compxctl rate device 500 --persist --reset-port
Device EEPROM updated to 500 Hz (0x02).
  Port reset: the receiver re-enumerated, and the host re-read the descriptors; the endpoint advertises 1000 Hz (1ms, ep_81).
  The endpoint does not follow the stored interval on this unit, and neither does the report stream — both are measured; the README says what that does and does not mean for `rate device`.
```

It refuses to run without `--persist`; `--reset-port` additionally re-enumerates.
When the stored value already matches, the EEPROM is not written and the port is
left alone.

> **What is verified here, and what is not.** The EEPROM write is verified by
> reading it back — that is measured. An *observable effect* of it, or of the live
> interval packets, could not be found on the verified unit:
>
> * the descriptor does not follow the stored code: `bInterval` stays `01` with
>   `0x01` or `0x02` stored;
> * the live packets do not change what the host receives. With `live = 500` and
>   `live = 1000` reports arrive at the same ~1 ms cadence, and every report that
>   carries a delta is followed by another one that does: 0% repeats after a
>   movement across roughly 15 000 pairs. A 500 Hz device behind a 1 ms host poll
>   would repeat every second report, and it does not.
>
> So **`rate host` is the path with a measurable effect**, while `rate device
> --persist` writes a value into the mouse (verified by readback) in the hope that
> it matters after the mouse is switched off. That last part is **not** verified:
> it needs a power cycle of the mouse, which was not done. There is also what
> cannot be seen from outside — if the dongle interpolates movement, the device's
> internal sampling rate could change while the report stream stays the same, and
> telling that apart needs an independent source of motion.

The reset is `USBDEVFS_RESET` on `/dev/bus/usb/BBB/DDD`; the kernel logs it
(`usb 3-2: reset full-speed USB device number 2`), so it is easy to check. If
the user has no access to that node the command does not fail: it says the port
was not reset and suggests re-plugging.

Which one do you want?

* **"I want 1000 Hz in Linux right now"** → `rate host 1000`. Nothing is
  written to the mouse.
* **"The setting should travel with the mouse"** → `rate device 1000 --persist`.
* **"I want 125/500 Hz"** → `rate host` slows *all* your mice, including a
  1000 Hz one, but its effect is measurable. `rate device` writes the value into
  the mouse itself and verifies the write by reading it back, yet its effect could
  not be observed on this model (see the box above).

### DPI

```console
$ compxctl dpi list
  slot  addr    x    y    mul  crc   value
  *0   0x000C  07   07   00   47    400
   1   0x0010  13   13   00   2F    1000
   2   0x0014  1F   1F   00   17    1600
   3   0x0018  2F   2F   00   F7    2400
   4   0x001C  77   77   00   67    6000
   5   0x0020  7F   7F   00   57    6400
   6   0x0024  EF   EF   00   77    raw:0xEF
   7   0x0028  7B   7B   44   1B    raw:0x7B(mul=0x44)
Active DPI level: slot 0 (index 0x01)

$ compxctl dpi 2400              # writes the active level's slot
$ compxctl dpi --slot 3 2400     # writes an explicit slot 0..5
```

The stored code is `DPI / 50 - 1`; DPI must be a multiple of 50 in 50..6400.
Every write is verified by reading **all four bytes** back.

A write reads the target slot alone (one transaction, not the whole table), and
if those four bytes are already there the write is skipped: an unnecessary write
to config memory is wear on a chip with an unknown budget, and reading back a
value that was never written proves nothing. The text output says
`DPI already set: slot 0 -> 400 (nothing written)` and JSON carries
`"changed": false`. All four bytes are compared, so a slot with the right code
and a broken checksum is still rewritten.

Slots 6 and 7 on the verified unit hold an undecoded extended encoding. They are
listed as raw bytes and never overwritten. Slot 6 holds `EF EF 00 77`, and `0xEF`
is exactly `12000 / 50 - 1`, so the scale **may** simply continue past 6400; slot
7 (`7B 7B 44 1B`) also carries an unknown `mul = 0x44` byte. That is a hypothesis,
not a measurement: closing it needs a capture of the vendor utility while setting
8000/12000/maximum, which we do not have. `dpi_decode` therefore leaves them
undecoded — printing "12000" unverified would be inventing a number. If the active-level register (0x0004)
does not hold a valid 1..8 index, `dpi N` refuses and asks for `--slot` instead
of guessing — the previous version silently wrote slot 0 in that case.

### Everything else

```console
$ compxctl status                       # sysfs rate + stored rate + DPI + battery
$ compxctl battery                      # percent, charging, link mode
$ compxctl probe --start 0x0000 --length 0x20   # read-only config memory dump
```

### Interactive screen: `tui`

```console
$ compxctl tui
```

A full-screen `curses` mode (standard library too): the readings of `status`, and
DPI slots edited with the arrow keys.

```
 compxctl — CompX / Ardor Gaming
  Device     : 2.4G Dual Mode Mouse
  Battery    : 100% (2.4G)
  Rate       : 1000 Hz (device descriptor), stored 1000 Hz
  DPI slots — 0..5 editable, 6..7 undecoded and read-only:
  -> Slot 0: 400      [ACTIVE]
     Slot 1: 1000
     …
     Slot 6: raw:0xEF [read-only]
  q quit · ↑/↓ slot · ←/→ ±50 DPI · [ ] ±400 · Enter write · r refresh
```

What matters:

* **No second protocol.** The screen calls the same functions as the commands:
  `collect_readouts` to read and `apply_dpi_slot` to write, so a write is verified
  by readback and four identical bytes are not written at all.
* **Slots 6 and 7 are read-only.** The down arrow stops at slot 5, and 6 and 7
  carry a marker: an undecoded encoding cannot be overwritten, by accident or
  otherwise.
* **A change is proposed first, written second.** An arrow key edits the proposal
  (marked `* not written`), Enter writes it, and only for the selected slot.
* **The terminal is restored** on `q`, `Esc`, `Ctrl+C` and `SIGTERM` (the last one
  exits `143`, as everywhere). Started without a terminal, it explains itself and
  exits `1`.
* `--tries` and `--timeout-ms` apply here too: the screen reads the device with
  the patience the command line asked for.

### Ready-made integrations: `contrib/`

The repository ships panel widgets — all through one adapter,
`contrib/compx-status.sh` (bash + `python3`, no `jq`), so that three widgets
cannot drift apart in formatting:

| File | Where | What it gives |
|---|---|---|
| `contrib/compx-status.sh` | into `PATH` (`~/.local/bin`) | the tool's JSON → one line, Waybar JSON, or the Argos format |
| `contrib/gnome-argos/compx.30s.sh` | `~/.config/argos/` | a GNOME widget (Argos, Guillotine, Executor): battery, DPI, a menu of presets and a way into `tui` |
| `contrib/waybar/config.jsonc` | into the Waybar config | a `custom/compx` module with a tooltip, `charging`/`discharging`/`asleep` classes and `tui` on middle click |
| `contrib/polybar/module.ini` | into the Polybar config | `[module/compx]` with the same text and clicks |
| `contrib/gnome-tray/compx-tray.py` | `~/.local/bin/compx-tray` | a GNOME tray indicator: the battery percentage drawn into the icon, a menu of DPI presets and a way into `tui` |

A sleeping mouse is a state, not an error: the widget keeps its place and says
`🖱️ —` instead of emptying itself or waiting out `4 × 500 ms`. A full-screen `tui`
from a panel needs a terminal (`foot -e compxctl tui`) — the panel itself has
none.

#### The mouse in the GNOME tray

```console
$ ln -s "$PWD/compxctl.py" ~/.local/bin/compxctl     # the tool in PATH, one copy
$ install -m 755 contrib/gnome-tray/compx-tray.py ~/.local/bin/compx-tray
$ cp contrib/gnome-tray/compx-tray.desktop ~/.config/autostart/
$ compx-tray &                                        # or just log out and in
```

Why this way rather than "install Argos":

* **Script-running extensions lag behind the shell.** Executor (the maintained
  Argos fork) supports up to GNOME 50 as of 51, so on 51 it will not install at
  all. A GNOME desktop already ships `appindicatorsupport`, the extension that
  draws any StatusNotifierItem in the panel, and that protocol is stable.
* **Indicator labels do not work in GNOME.** The extension has a
  [regression with `set_label`](https://bugs.launchpad.net/ubuntu/+source/gnome-shell-extension-appindicator/+bug/2059818):
  the label stopped being drawn. So the percentage is *painted into the icon*,
  with its own background so it reads on a light or a dark panel; the tooltip is
  built from `Title`, and everything else lives in the menu.
* **It needs only `python-gobject`** with `Gtk 3` and `AyatanaAppIndicator3`,
  which any GNOME already has. The tool itself gains no dependency.

The menu holds the battery, the active DPI and the rate (read-only), four DPI
presets with the current one ticked, "Открыть экран (tui)" in a terminal, a
refresh and a quit. One indicator per session (a `flock`), polling the device
every 10 seconds with the same `--tries 1 --timeout-ms 150` as the other widgets:
a sleeping mouse is a `—` badge, not an exception.

### Machine-readable output: `--json`

`rate`, `status`, `dpi` and `battery` accept `--json` — exactly one JSON
document on stdout, ready for Waybar, Polybar, i3status or a script:

```console
$ compxctl battery --json | jq -r '.battery.percent'
95
$ compxctl status --json | jq '{rate: .polling.effective_rate_hz, dpi: .active_dpi.dpi}'
{
  "rate": 1000,
  "dpi": 400
}
$ compxctl status --json | jq '.dpi_slots[] | select(.active) | .text'
"400"
```

What you can rely on:

* a runtime error is **one** JSON document on stdout:
  `{"schema": 2, "command": "status", "error": {"message": "..."}}`, with exit
  code `1`;
* an interrupt produces the same document with `error` instead of an empty
  stdout: `Ctrl+C` exits `130`, `SIGTERM` exits `143`. The second one matters
  more than it sounds: Waybar, Polybar and i3status kill a slow child with
  `SIGTERM`, and Python has no default handler for it, so without catching it the
  bar would read nothing at all;
* a command-line error (exit code `2`) stays text on stderr and produces no
  document: that is a typo in your script, not a state of the device;
* an unknown value is `null`, never the string `"n/a"`;
* `dpi <value>` adds `write.changed` to the document: `false` means the slot
  already held exactly that and nothing was written to the device;
* `status` reads four things at once and does not die because of one: whatever
  could not be read lands in the `warnings` array and the rest is still there.
  `dpi` and `battery` read a single thing and answer with an `error` document
  instead;
* `schema` changes only when the meaning of a key changes; adding keys does not
  bump it.

`status --json` does not touch the kernel driver and does not disturb the mouse,
so a status bar can call it every second — while the device answers, that is a few
milliseconds (measured: 62 ms including interpreter startup, 34 ms of which is
Python itself). For a bar there is also a pair of impatience flags:
`--tries 1 --timeout-ms 150`. An awake mouse answers in single-digit
milliseconds, so waiting 4 × 500 ms to learn that it is asleep is wasted time. If the mouse is switched off or asleep, the tool does not ask four
times over: after the first run of timeouts (4 attempts × 500 ms) it marks the
device as silent, skips the remaining reads and says so in `warnings`. That is
~2 s instead of ~14, and the document honestly carries `null` where a value was
never asked for.

## Permissions

* `rate` (reading) needs nothing.
* `rate host` needs root — it writes a kernel module parameter.
* The device commands need access to the `/dev/hidraw*` node; only
  `--reset-port` also needs the USB device node (the re-enumeration). Install the
  rule shipped in the repository, or just run those commands with `sudo`:

```console
$ sudo cp 99-compx-mouse.rules /etc/udev/rules.d/
$ sudo udevadm control --reload-rules && sudo udevadm trigger
```

```udev
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="25a7", TAG+="uaccess"
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTRS{idVendor}=="25a7", TAG+="uaccess"
```

`./install.sh` does not silently overwrite a rule that is already there: if the
file exists and differs, the previous version is kept beside it as
`99-compx-mouse.rules.bak.<date>`, and `--uninstall` says where those copies are.

`TAG+="uaccess"` covers an active logind session. The `usb_device` rule is needed
**only** for `--reset-port`: everything else works without it, and the reset says
so when it has no permission. If you never want the re-enumeration, the single
`hidraw` line is enough — fewer privileges and less surface. Note that a `GROUP="users"` fallback is a
trap: device access does not go through that group and membership is whatever
the installer decided, so where nobody is a member the rule silently grants
nothing — check with `id -nG`. If you installed the 2.x rules, reinstall them —
they did not cover `hidraw`.

## Supported devices

USB vendor `0x25A7`, product ids `0xFA7B`, `0xFA7C`, `0xFA03`, `0xFA93`.
The protocol was captured and verified on an Ardor Gaming Ulta (PID `0xFA7B`);
the neighbouring ids are served by the same code but were not individually
verified.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | runtime error — no device, no permission, bad reply, refused readback |
| `2` | command-line error (argparse) |
| `130` | interrupted by Ctrl+C (`SIGINT`) |
| `143` | interrupted by `SIGTERM` (what a status bar sends on its timeout): 128 + 15 |

With `--json` a runtime error arrives as a document on stdout; the exit code is
unchanged.

## Tests

```console
$ pip install pytest && pytest
```

247 tests that need no hardware, no root, no `/dev/hidraw` and no third-party
packages: the device layer runs against a fake hidraw IO object, and the sysfs
layer against a synthetic tree, hidraw node lookup included. They also pin down
real bugs found on live hardware:

* the hidraw lookup could return the motion interface's node instead of the
  config interface's — `SET_REPORT` there fails with `EPIPE`;
* the readback accepted a write's acknowledge as its answer, and a retried
  command left an extra reply queued — together they made a successful EEPROM
  write look like a failure;
* hex-vs-decimal parsing of USB descriptor attributes, and a partial DPI
  readback check that accepted a wrong `y` byte (from the previous review).

One test reads `compxctl.py`'s AST and fails if any import outside the standard
library appears.

## What was verified on hardware

All of the following was measured on an Ardor Gaming Ulta (`25a7:fa7b`), kernel
7.2.9 — not taken from documentation:

* **hidraw instead of libusb.** Config-memory, DPI and battery reads and all
  frame writes return byte-identical answers to the old libusb path, without
  detaching the kernel driver.
* **The port reset works without root** (with the udev rule installed):
  `USBDEVFS_RESET` returns `0` and the kernel logs `usb 3-2: reset full-speed
  USB device number 2 using xhci_hcd`.
* **The readback really does catch a failure.** In one run the 500 Hz write was
  reported as failed, and it was right — not because the write failed, but
  because the read had taken a reply to the previous command out of the queue.
  With the filter fixed, write and readback agree and `probe` shows `0x01`/`0x02`
  at `0x0000`.
* **The descriptor does not follow the EEPROM.** With code `0x02` (500 Hz)
  stored, the interface still advertises `bInterval = 01`, i.e. 1 ms; with `0x01`
  it is the same. Checked on both sides of a port reset. So the old wording
  ("re-plug the mouse so the host re-reads the descriptor") explained the effect
  wrongly: nothing in the descriptor changes, and the stored code governs how
  often the mouse itself sends — refuted below.
* **The live packets do not change the report stream.** Measured with a counter on
  the input reports (one report is one poll, unlike the removed `check`, which
  counted evdev events): with `live = 500` and `live = 1000` reports arrive at the
  same ~1 ms cadence, and every report carrying a delta is followed by another
  that does — 0% repeats after a movement across roughly 15 000 pairs. A mouse
  sampling at 500 Hz behind a 1 ms host poll would repeat every second report, and
  it does not. What happens to the sampling rate inside the mouse is invisible
  from outside: a dongle that interpolates movement would keep the stream the
  same.
* **A full config-memory dump taken before and after all of it is identical** —
  the writes never touched the DPI level count (`0x0002`), the active index
  (`0x0004`) or the slots.

## Limitations

* The EEPROM path is still reverse-engineered and still wear-limited. `--persist`
  is the only command that writes it, and it verifies by readback and compares
  the frame with the capture verified on hardware.
* `rate host` cannot express "only this mouse" — `mousepoll` is global.
* Slots 6-7 use an undecoded encoding and are never written.
* The lock serializes `compxctl` runs only. Another program opening the same node
  does not lose frames (every opener gets its own copies), but the mutual
  visibility of foreign replies remains: the protocol carries no sequence number.
* `--reset-port` drops **the whole receiver** off the bus. If the same receiver
  also carries a keyboard, that keyboard disappears for a second — which is why
  the port is not touched by default.
* The tool does not measure the rate the mouse actually reports at: that needs
  mouse movement, and counting input events the way the removed `check` command
  did is a way to get a wrong number.

## License

MIT. Unofficial reverse engineering — not affiliated with, or endorsed by,
CompX, Ardor Gaming or Areson Technology. Use at your own risk.
