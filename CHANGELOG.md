# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.0.0] — 2026-10-15

### Changed
- **Breaking:** `rate device HZ --persist` no longer resets the port. The reset
  existed to make the host re-read the descriptor, and the measurement says the
  descriptor does not follow the stored interval: `bInterval` stays `01` with
  either code stored, on both sides of a reset the kernel logged as real. What it
  cost was a second udev rule granting access to the whole USB device node, and a
  second with the entire receiver — keyboard included — off the bus. The rate is
  changed by the live interval packets, which are sent either way; the stored code
  is what the mouse boots with. `--reset-port` asks for the re-enumeration when
  someone wants it (verified: the kernel's reset counter stays put without the
  flag and goes up by one with it).
- **Breaking:** `--no-reset` is gone, because it asked for what is now the
  default. Anyone passing it gets a usage error rather than a silent surprise.
- The udev `usb_device` rule is now optional: only `--reset-port` needs it, so a
  system that never resets the port can install the `hidraw` rule alone.
- `--timeout-ms` and `--tries` on every command that talks to the device. This is
  the status-bar story: an awake mouse answers in single-digit milliseconds, so
  `--tries 1 --timeout-ms 150` reaches "the mouse is asleep" instead of waiting
  out the write-grade default of four attempts. Measured: `status --json` costs
  62 ms end to end, 34 ms of which is interpreter startup.

## [3.0.0] — 2026-10-15

The device commands stopped using pyusb. They reach the config interface
through `/dev/hidrawN` with `fcntl.ioctl`, so the tool is standard library only
from end to end, and no kernel driver is detached any more.

### Changed
- **Breaking:** the `device` extra is gone — `pip install ".[device]"` becomes
  `pip install .`, because there is nothing left to install.
- **Breaking:** the udev rules now cover `SUBSYSTEM=="hidraw"` as well.
  A system still running the 2.x rules has no access to the hidraw node, so
  device commands fail with a permission error until they are reinstalled.
- Device commands no longer detach `usbhid`, so `status`, `battery`, `dpi` and
  `probe` no longer make the pointer stutter while they run.
- Every frame now goes out as `SET_REPORT(FEATURE)` — report ids 6 and 8 are
  FEATURE reports in the device's own report descriptor. The pyusb path sent the
  writes as OUTPUT reports; the firmware accepts both, and hidraw offers the
  feature route.
- `rate device --persist` resets the port itself (`USBDEVFS_RESET`) instead of
  telling you to re-plug the receiver. `--no-reset` keeps the re-plug a human
  step, and the command still says so.
- READMEs rewritten around the hidraw transport; the JSON output and the
  measured behaviour of the stored interval are documented.

### Fixed
- A readback after a config write could return a reply that belonged to an
  earlier command of the same address, reporting a failed write that had in fact
  succeeded. Two causes: the reply filter compared the address and length but
  not the opcode, so a write's acknowledge (`09 07 …`, an echo of the written
  bytes) passed as an answer; and every retry re-sent the command, leaving one
  extra reply in the queue per stale frame. The filter now matches the opcode
  too, and a command is sent once per attempt while the queue is drained.
- `find_mouses` could return a hidraw node belonging to the motion interface
  instead of the config interface, because the lookup matched any interface of
  the device. Writing a feature report to the motion node fails with `EPIPE`.
- `rate both HZ` without `--persist` refused *after* writing `usbhid.mousepoll`,
  so a rejected command had already changed the host. The refusal is now checked
  before the first side effect.
- The reset was reported as `"ok"` from the ioctl returning, even if the device
  never came back. `"ok"` now means it is on the bus again with a readable
  endpoint; otherwise the result is `"failed"` with the reason.
- A short read (`b""`) or a device that never stops talking could spin the
  queue drain forever; it now ends on a short read and after a frame ceiling.
- `build_write_frame` did not check that the payload stays inside the config
  memory, which the read path has always done.
- `cmd_dpi --json` printed its document before the session was closed, so a
  failure to close could have put two documents on stdout.
- The EEPROM readback compared only the interval code at `0x0000`. The
  complement byte written beside it is verified too, and a stored pair that no
  longer satisfies the `0x55` checksum is rewritten instead of being taken for
  "already set".
- The protocol-drift guards (a frame that no longer matches the capture verified
  on hardware) raised `AssertionError`, which no caller rendered: they now raise
  `RuntimeError` like every other refusal, so the user gets one error line or one
  JSON document instead of a traceback.
- `install.sh` overwrote an existing udev rule in place. That path is also where
  a hand-written vendor rule tends to live — the machine this was developed on
  had one, granting access through a group, which also works over SSH where a
  `uaccess` ACL does not exist. A differing file is now kept as
  `99-compx-mouse.rules.bak.<timestamp>`, `--uninstall` says where those copies
  are, and an identical file is not backed up again.

What is inherent rather than fixed: the vendor protocol has no sequence number,
so a duplicate of the read taken *before* an EEPROM write cannot be told from
the answer to the read after it. That can only turn into a loud readback
mismatch on a write that worked — never a silent wrong value — and
`persist_rate`'s docstring says so.

### Added
- `--json` on `rate`, `status`, `dpi` and `battery`: exactly one JSON document
  on stdout (schema 1), errors included, unknown values as `null`, partial
  failures collected in `warnings`. `status --json` is safe to poll from a
  status bar.
- Automatic port reset after `rate device --persist`, with a report of what the
  host sees afterwards.
- A test that reads this module's own AST and fails on any import outside the
  standard library, so "no dependencies" stays a checked property.
- The hidraw node is now chosen by parsing its HID report descriptor: the right
  node is the one declaring id `0x08` as a FEATURE report and `0x09` as INPUT. A
  readable descriptor that contradicts is taken as proof of the wrong interface
  and the command refuses to talk to that node, instead of sending a feature
  report where it does not belong. This matters for the three product ids the
  code supports but nobody has verified.
- Concurrent runs are serialized with `flock` on the node. Measured first: hidraw
  gives every opener its own copy of every input report, so nothing is stolen
  from anyone — but two compxctl processes can each read the other's frames, and
  the protocol has no sequence number to tell them apart. The lock is taken on a
  descriptor of its own and the reading descriptor is opened only once the lock
  is held, because being subscribed to those copies starts at `open()`: a
  process waiting for the lock with its reader already open would queue the
  previous session's replies ahead of its own.
- A device that has gone quiet is asked once, not four times: after the first run
  of timeouts the session marks the device silent, `status` skips the remaining
  readouts and says so in `warnings`. A sleeping mouse now costs ~2 s instead of
  ~14 s, which is what makes polling it from a status bar reasonable.
- `dpi <value>` reads the target slot on its own instead of the whole table, and
  skips the write when those four bytes are already there: an unnecessary write
  to config memory is wear on a chip with an unknown budget, and the readback of
  a value that was never written proves nothing. JSON reports it as
  `write.changed`.
- `HidrawIO.lock` treats only real contention (`EAGAIN`/`EWOULDBLOCK`, `EACCES`)
  as "someone else has it". A descriptor that went bad — the receiver pulled out
  between `open` and `flock` — used to spend the whole two-second wait and then
  be reported as another compxctl talking to the mouse; it now raises the actual
  errno at once.
- `--json` now prints its document when a run is interrupted as well (exit code
  `130`). A status bar that kills a slow call for its own timeout used to get an
  empty stdout, which is the one thing the JSON contract promises not to do.
- `SIGTERM` is now handled like Ctrl+C. Status bars do not send SIGINT: Waybar,
  Polybar and i3status kill a slow child with `SIGTERM`, and Python's default for
  that signal is to die inside the C handler — so a run with `--json` ended 143
  with an empty stdout, which is the one thing that contract promises never to
  do. It now prints the same error document and exits `143` (128 + 15); Ctrl+C
  keeps `130`.
- The hidraw node is opened `O_NONBLOCK` and a read that finds nothing
  (`BlockingIOError`) goes back to waiting for the time it has left. A blocking
  hidraw read has no timeout of its own, so a report taken between the readiness
  check and the read could have parked the call indefinitely.
- A descriptor that is opened for the lock is closed even when the wait for that
  lock is interrupted. The CLI would exit and let the kernel do it; a
  long-lived process using `HidrawSession` would have leaked it.
- The firmware's own acknowledgements (`09 07 …` echo of a write, `09 11 …` for a
  live packet) no longer count as "another program is talking to the mouse". If a
  read answer is lost while an acknowledgement arrives, the diagnosis used to
  blame a second program and — worse — the session did not mark the device
  silent, so `status` paid the full retry budget for every remaining readout.
  Acknowledgements are recognised by the opcode they echo, and only a frame that
  looks like an answer to a *different* question is treated as foreign traffic.
- `probe --length 0` (and a negative length) used to print nothing and exit `0`,
  which reads like "the memory is empty". It is now an error with a message.
- Tests for hidraw node lookup, the write acknowledge, the leftover-reply
  readback, the port reset, the report-descriptor walk (run against both
  descriptors captured from the hardware), the lock, the signal handling, the
  opt-in reset and the JSON documents: 226 in total.

### Removed
- The `pyusb` dependency, the `[device]` extra and every import of it.
- The unused `RATE_BY_CODE` table, which also contradicted the table in use:
  it read `0x01` as 500 Hz where `RATE_BY_DEVICE_INTERVAL_CODE` reads 1000.

## [2.0.0] — 2026-09-11

A rewrite. The polling rate no longer goes through the vendor EEPROM: it is read
from sysfs and set through `usbhid.mousepoll`, and the reverse-engineered
protocol is kept only where it is genuinely required — DPI and the device's
stored interval.

### Changed
- **Breaking:** `set HZ` became `rate host HZ` (host polling interval, global,
  reversible, no EEPROM). Writing the device's stored interval is now
  `rate device HZ --persist`, and it is refused without `--persist`.
- **Breaking:** `check` was removed. Measuring the rate no longer counts input
  events; `rate` reads the endpoint interval from sysfs, which is exact,
  instant, and needs no mouse movement.
- `pyusb` is now an optional extra (`pip install ".[device]"`) instead of a
  mandatory dependency. `hidapi` and `evdev` were dropped entirely.
- `dpi N` refuses to guess when register `0x0004` is not a valid 1..8 level
  index and asks for `--slot`, instead of silently writing slot 0.
- DPI write verification compares all four slot bytes, not only `x`.

### Fixed
- `check` counted every `EV_REL` event, so a mouse report carrying both axes —
  or a scroll — inflated the measured rate by 2-3x depending on how the mouse
  was moved. One report is one poll, but it can be several events.
- `set` exited `0` when the EEPROM packet had failed, so a script could not
  tell "applied" from "applied and persisted".
- The raw-USB pass ran once per discovered hidraw path, re-sending every packet
  — including the EEPROM write — as many times as paths were found.
- `probe --start` silently wrapped addresses beyond 16 bits, so it printed one
  address while reading another.
- USB descriptor attributes were read from sysfs as decimal; they are bare hex
  (`idVendor` is `25a7`, `bEndpointAddress` is `81`).
- The charging flag was inverted in the battery readout.
- `status`/`battery` could print an impossible battery percentage from a
  mismatched reply.

### Added
- `usbhid_is_builtin()`: the persistence advice distinguishes a loadable
  `usbhid` (`/etc/modprobe.d/`) from one built into the kernel (kernel command
  line), because the former is silently ignored in the latter case.
- A runtime guard on the EEPROM write: the frame is compared byte for byte with
  the capture verified on hardware, and a mismatch is refused before sending.
- Tests for the sysfs layer, the DPI readback and the command layer
  (114 tests; no hardware, no root, no pyusb).

### Removed
- `requirements.txt` — dependencies are declared in `pyproject.toml`.
- The `hidraw` and `input` udev rules: 2.x uses neither.

## [1.1.0] — 2026-09-06

### Added
- `status` and `battery` commands — read-only device snapshot and battery state.
- `probe` command to read and interpret windows of the config memory.
- `dpi` command to list the DPI slots and write a slot's DPI
  (`dpi list`, `dpi N`, `dpi --slot S N`).
- Computed CompX EEPROM packet checksums (whole frame sums to `0x55`).
- `pyproject.toml` with the `compxctl` console script (`pip install .`).
- Unit tests (`tests/`) and GitHub Actions CI.
- `install.sh` — one-shot setup of udev rules and fish completions, with an
  optional `--with-venv` Python install and `--uninstall`.
- Fish shell completions (`completions/compxctl.fish`).
- Documented protocol findings: config-memory map; the main-RGB backlight
  write at `0x00A0` (device-specific: `0x08` = off on the verified unit; the
  vendor software sends `0x07`, which this firmware treats as strobe).

### Fixed
- `set` no longer clobbers the DPI-level fields — the EEPROM write now stores
  only two bytes at `0x0000`.
- USB error-handling hardening and stale-comment cleanup (review fixes).

### Changed
- Unified USB/hidraw transport under protocol primitives.
- READMEs rewritten for v1.1.0, Russian primary (`README.md`) and English
  secondary (`README.en.md`); MIT license added.

## [1.0.1] — 2026-09-05

### Fixed
- Lazy `hidapi` import so unrelated commands do not require the package.
- Honest distinct-packet success counting for `check`.
- Named ioctl constants instead of magic numbers.
- CompX EEPROM packet checksum computed instead of a hardcoded tail.

## [1.0.0] — 2026-09-05

### Added
- `compxctl` CLI with `set` and `check` over the proprietary HID protocol.
- udev rules granting access to CompX / Ardor Gaming mice (VID `25a7`).
- English and Russian READMEs.
