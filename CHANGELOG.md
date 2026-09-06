# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `pyproject.toml` with the `compxctl` console script (`pip install .`).
- Unit tests (`tests/`) and GitHub Actions CI.
- `install.sh` — one-shot setup of udev rules and fish completions, with an
  optional `--with-venv` Python install and `--uninstall`.
- Fish shell completions (`completions/compxctl.fish`).

## [1.1.0] — 2026-09-06

### Added
- `status` and `battery` commands — read-only device snapshot and battery state.
- `probe` command to read and interpret windows of the config memory.
- `dpi` command to list the DPI slots and write a slot's DPI
  (`dpi list`, `dpi N`, `dpi --slot S N`).
- Computed CompX EEPROM packet checksums (whole frame sums to `0x55`).
- Documented protocol findings: config-memory map and the LED-effect register
  (`0x08` = off).

### Fixed
- `set` no longer clobbers the DPI-level fields — the EEPROM write now stores
  only two bytes at `0x0000`.

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
