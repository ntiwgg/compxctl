# compxctl

**Polling rate and DPI for CompX / Ardor Gaming mice on Linux — without the
proprietary Windows utility.**

`compxctl` is a single-file Python CLI. It does two things that live in two
very different places, and it is careful not to blur them:

| What | Where it lives | How compxctl does it | Packages needed |
|---|---|---|---|
| Polling rate | the host's USB polling loop | `usbhid.mousepoll` + the endpoint descriptor in sysfs | **none** (stdlib only) |
| DPI, battery, config memory | inside the mouse chip | the vendor HID protocol over raw USB | `pyusb` |

```console
$ compxctl rate
Device: 2.4G Dual Mode Mouse (25a7:fa7b)
Device advertises: 1000 Hz (1ms via interval, ep_81)
Host override: none (usbhid.mousepoll = 0)
Effective: 1000 Hz (from device descriptor)
```

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
* **Writing the device's stored interval** still exists, because it is the only
  way to change what the mouse advertises to *any* host (including Windows), but
  it is now an explicit opt-in behind `--persist`.

## Install

```console
$ pip install pyusb          # only for dpi / status / battery / probe / rate device
```

That is the whole dependency list. `rate` and `--version` work with a bare
Python 3.10+ and nothing else.

Or install it as a package:

```console
$ pip install .            # CLI only, no dependencies
$ pip install ".[device]"  # + pyusb for the device commands
```

Русский: [README.md](README.md)

## Usage

### Polling rate

```console
$ compxctl rate                 # show: device, host override, effective rate
$ sudo compxctl rate host 1000  # host-side, all USB mice, immediate, reversible
$ sudo compxctl rate both 1000 --persist   # also rewrite the device's EEPROM
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

`rate device HZ --persist` places two live interval packets and one EEPROM
write, then reads the value back:

```console
$ compxctl rate device 1000 --persist
Device EEPROM updated to 1000 Hz (0x01).
  Re-plug the mouse (or replug the receiver) for the host to re-read it.
```

It refuses to run without `--persist`, and it skips the write entirely when the
stored value already matches.

Which one do you want?

* **"I want 1000 Hz in Linux right now"** → `rate host 1000`. Nothing is
  written to the mouse.
* **"The setting should travel with the mouse"** → `rate device 1000 --persist`.
* **"I want 125/500 Hz"** → prefer the device path. Forcing the host down to
  8 ms slows *all* your mice, including a 1000 Hz one.

### DPI

```console
$ compxctl dpi list
  slot  addr    x    y    mul  crc   value
  *0    0x000C  13   13   00   2F    1000
   1    0x0010  1F   1F   00   17    1600
   ...
$ compxctl dpi 2400              # writes the active level's slot
$ compxctl dpi --slot 3 2400     # writes an explicit slot 0..5
```

The stored code is `DPI / 50 - 1`; DPI must be a multiple of 50 in 50..6400.
Every write is verified by reading **all four bytes** back.

Slots 6 and 7 on the verified unit hold an undecoded extended encoding. They are
listed as raw bytes and never overwritten. If the active-level register (0x0004)
does not hold a valid 1..8 index, `dpi N` refuses and asks for `--slot` instead
of guessing — the previous version silently wrote slot 0 in that case.

### Everything else

```console
$ compxctl status                       # sysfs rate + stored rate + DPI + battery
$ compxctl battery                      # percent, charging, link mode
$ compxctl probe --start 0x0000 --length 0x20   # read-only config memory dump
```

## Permissions

* `rate` (reading) needs nothing.
* `rate host` needs root — it writes a kernel module parameter.
* The device commands need access to the USB device. Install the rule shipped in
  the repository, or just run those commands with `sudo`:

```console
$ sudo cp 99-compx-mouse.rules /etc/udev/rules.d/
$ sudo udevadm control --reload-rules && sudo udevadm trigger
```

```udev
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTRS{idVendor}=="25a7", TAG+="uaccess"
```

2.x talks to the config interface through libusb, so the `hidraw` and `input`
rules the 1.x release needed are gone. `TAG+="uaccess"` covers an active logind
session. Note that a `GROUP="users"` fallback is a portability trap: that group
does not exist on Fedora or Arch, so the rule silently grants nothing there.

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
| `130` | interrupted |

## Tests

```console
$ pip install pytest && pytest
```

The suite needs no hardware, no root and no pyusb: the device layer is driven
through a fake pyusb object, and the sysfs layer through a synthetic tree. It
also locks down two real bugs found during review of the previous version — the
hex-vs-decimal parsing of USB descriptor attributes, and a partial DPI readback
check that accepted a wrong `y` byte.

## Limitations

* The EEPROM path is still reverse-engineered and still wear-limited. `--persist`
  is the only command that writes it, and it verifies by readback.
* `rate host` cannot express "only this mouse" — `mousepoll` is global.
* Slots 6-7 use an undecoded encoding and are never written.
* `status`, `battery` and `probe` need `pyusb`; there is no hidraw fallback
  because this mouse's config interface is claimed by `usbhid`.

## License

MIT. Unofficial reverse engineering — not affiliated with, or endorsed by,
CompX, Ardor Gaming or Areson Technology. Use at your own risk.
