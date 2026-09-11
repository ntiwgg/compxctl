# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
