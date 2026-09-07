#!/usr/bin/env python3
"""compxctl.py — control a CompX / Ardor Gaming mouse: set the polling
rate, list or set its DPI levels (`dpi`), snapshot the device state
(`status`), read the battery (`battery`), measure the real rate (`check`)
and probe (read back) its config memory.

The proprietary control protocol lives below — the packet table and report
order for `set` in POLLING_VARIANTS, the config-memory read framing for
`probe` in the PROBE_* section, the battery request for `status`/`battery`
in _build_battery_request() — this file is the single source of truth.

Architecture — two transports talk to the same config interface
(interface 1), layered under one protocol and one set of commands:

* pyusb (raw USB) — needs no kernel driver on the interface, so it is the
  channel behind `dpi`, `status`, `battery` and `probe` (their reads are a
  SET_REPORT command whose reply comes back on the interrupt-IN endpoint)
  and the always-on delivery channel of every `set` rate packet.
* hidapi / raw hidraw ioctls — `set` and the PID line of `status` require
  hidapi to be installed: `set` enumerates its control paths through it
  and, when the kernel binds interface 1 to usbhid (usage pages FF01-FF04),
  uses each hidraw node as an extra delivery channel per packet. CompX mice
  may ship with interface 1 unbound and no such node at all; then `set`
  falls back to the pyusb-only pass, which is why `dpi`, `battery` and
  `probe` never touch hidapi.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import glob
import os
import sys
import threading
import time
from pathlib import Path

__version__ = "1.1.0"

VENDOR_ID = 0x25A7
PRODUCT_IDS = (0xFA7B, 0xFA7C, 0xFA03, 0xFA93)

# hidraw nodes exposing one of these usage pages carry the control reports.
CONFIG_USAGE_PAGES = frozenset({0xFF01, 0xFF02, 0xFF03, 0xFF04})

# The third packet of every rate variant is the EEPROM persistence write.
EEPROM_PACKET_INDEX = 2
MIN_SENT_FOR_SUCCESS = 2
MEASURE_SECONDS = 1.5

# Bounded retry for the hidraw fallback: the pyusb step detaches/re-attaches the
# kernel driver, and udev needs a moment to re-create the hidraw node.
SEND_ATTEMPTS = 6
SEND_RETRY_DELAY_SEC = 0.1

# Linux hidraw ioctl numbers (uapi/linux/hidraw.h). The request word mirrors
# _IOC(_IOC_READ | _IOC_WRITE, 'H', nr, size) = 0xC0000000 | (size << 16)
# | (ord('H') << 8) | nr; size is the report length (< 2**16).
HIDIOCSFEATURE = 0x06
HIDIOCSOUTPUT = 0x0B

# Fixed by-id names of the mouse input nodes (checked first by `check`).
MOUSE_BY_ID_PATHS = (
    "/dev/input/by-id/usb-Compx_2.4G_Wireless_Receiver-event-mouse",
    "/dev/input/by-id/usb-Compx_2.4G_Dual_Mode_Mouse-event-mouse",
)

HID_PERMISSION_TEXT = (
    "No access to the HID device. Reinstall the udev rules "
    "(99-compx-mouse.rules) and re-plug the mouse."
)
INPUT_PERMISSION_HINT = (
    "No read access to {0}. Add yourself to the input group "
    "(`sudo usermod -aG input $USER`, then re-login) or make sure the udev rules "
    'carry TAG+="uaccess" for active logind sessions.'
)
DEVICE_NOT_FOUND_TEXT = (
    "No CompX mouse found (VID 0x25A7). Check the USB connection: lsusb -d 25a7:"
)
EVENT_NOT_FOUND_TEXT = (
    "No CompX mouse event node found. Connect the mouse or pass the node "
    "explicitly: compxctl.py check --device /dev/input/eventN"
)

# Proprietary CompX protocol — every rate is a sequence of three reports sent
# to the config interface (report ids 0x06 / 0x08, one byte = rate value):
#   1. report 0x06 (0x06 0x11 ...): applies the rate live; byte 3 is the rate
#      code (0x00 -> 125 Hz, 0x01 -> 500 Hz, 0x02 -> 1000 Hz);
#   2. report 0x08 / sub-report 0x11 (0x08 0x11 ...): applies the interval;
#      byte 6 = report interval in ms (0x08 -> 125 Hz, 0x02 -> 500 Hz,
#      0x01 -> 1000 Hz);
#   3. report 0x08 / sub-report 0x07 (0x08 0x07 ...): EEPROM write so the
#      rate survives unplug / re-plug. It writes ONLY the rate pair to config
#      memory at 0x0000 (2-byte payload: interval code + complement), never
#      the header bytes around it, so the DPI-level count (0x0002) and the
#      active level index (0x0004) survive. The frame is assembled by
#      _eeprom_packet(), never hardcoded.
# The EEPROM write stores the rate as the report-interval code of packet 2's
# byte 6 (value in ms): 1 ms = 1000 Hz, 2 ms = 500 Hz, 8 ms = 125 Hz. The
# complement is 0x55 minus the code; a longer write that also stored the four
# bytes at 0x0002..0x0005 was observed to clobber the DPI-level fields, so
# the payload must stay exactly the 2-byte pair.
INTERVAL_CODE_BY_RATE: dict[int, int] = {125: 0x08, 500: 0x02, 1000: 0x01}

# CompX config-memory (EEPROM) write frame, 17 bytes — see
# _build_write_frame() for the generic builder:
#   [0:3]  0x08 0x07 0x00   report 0x08, write opcode 0x07, reserved
#   [3:5]  AH AL            write address (big-endian; 0x0000 for the rate)
#   [5]    LN               payload length (two bytes follow for the rate write;
#                           the frame builder accepts any length 1..10)
#   [6:8]  rate byte        interval code: 0x01 = 1000 Hz, 0x02 = 500 Hz,
#                           0x08 = 125 Hz (same convention as packet 2, byte 6)
#          complement       additive complement to 0x55 (0x55 - rate byte)
#   [8:16] 0x00 pad
#   [16]   checksum         tail byte such that the sum of all 17 bytes is
#                          ≡ 0x55 (mod 256) — computed, never hardcoded.

# Config-memory read probe: the mouse exposes interface 1 without a hidraw
# node, so reading is a SET_REPORT command whose reply the firmware pushes
# onto the interface's interrupt-IN endpoint (see _build_read_frame() and
# _parse_read_reply()):
#   command (17 bytes): 08 08 00 AH AL LN + ten 0x00 bytes + checksum
#     report 0x08, opcode 0x08 (0x07 is the EEPROM-write opcode above);
#     AH/AL = 16-bit register address (big-endian), LN = bytes to read;
#     tail = _compx_checksum over the 16 leading bytes, as with writes.
#   reply (17 bytes, interrupt-IN endpoint of interface 1):
#     09 08 00 AH AL LN + LN payload bytes + 0x00 pad + checksum; AH/AL/LN
#     echo the command and the whole frame must sum to 0x55 again. The
#     firmware refuses LN > 10 (status 0x01 + zeroes), so reads stay ≤ 10.
# Register map of the config memory (probe prints bytes and interprets only
# what is confirmed; status/battery read the same fields as named readouts):
#   0x0000           polling-rate code + complement (01 54 = 1000 Hz)
#   0x0004           active DPI level index + complement at 0x0005
#                    (observed 01..03; the index is 1-based, see
#                    ACTIVE_LEVEL_OFFSET — pending calibration)
#   0x000C..0x002B   DPI slots, 4 bytes each (x, y, mul, per-slot checksum);
#                    slot checksum = 0x55 − x − y − mul (observed 13 13 00 2f);
#                    DPI = (x + 1) × 50 for x ≤ 0x7F and mul = 0 (dpi_decode)
#   0x0060..0x009F   button matrix, ~0x00A0 LED; past 0x00A0 the memory is
#                    empty 0xFF.
PROBE_BLOCK_BYTES = 8  # bytes read per probe dump line
PROBE_MAX_READ_BYTES = 10  # firmware ceiling for one read command (LN)
PROBE_FRAME_BYTES = 17  # fixed size of the command and reply frames
PROBE_READ_TIMEOUT_MS = 500  # interrupt-IN reply deadline
# Reads retry after receiving a frame that fails verification: `set` bursts
# leave the firmware's write acks (09 07 ...) queued on the interrupt-IN
# endpoint, and each failed read drains exactly one such stale frame, so
# the retry usually sees the true reply. Timeouts are NOT retried (a late
# reply must not be doubled), so a hung device still fails fast.
READ_ATTEMPTS = 4
PROBE_MAX_DUMP_BYTES = 0x100  # `probe --length` ceiling
PROBE_POLLING_ADDR = 0x0000
PROBE_POLLING_HZ_BY_CODE: dict[int, int] = {
    0x01: 1000,
    0x02: 500,
    0x04: 250,
    0x08: 125,
}
PROBE_DPI_TABLE_START = 0x000C
PROBE_DPI_TABLE_END = 0x002C  # exclusive
PROBE_DPI_SLOT_BYTES = 4
# An unset DPI slot row reads back as four 0xFF bytes ("empty").
EMPTY_SLOT = (0xFF, 0xFF, 0xFF, 0xFF)

# Status readouts (v1.1.0) — register addresses and battery framing
# confirmed on hardware (a FA7B 2.4G dual-mode mouse).
ACTIVE_DPI_LEVEL_ADDR = 0x0004  # 1 byte level index, complement at +1
# The level register counts DPI levels 1..8 while the slot rows below are
# 0-based, so the active slot is (level − offset); exact firmware meaning
# still pending calibration.
ACTIVE_LEVEL_OFFSET = 1
# DPI write policy: slots 0..5 store the plain encoding (x = y = code,
# mul = 0), so they are writable; slots 6..7 of this mouse hold the
# not-yet-understood extended encoding (see read_dpi_slots) and are listed
# but never overwritten.
DPI_WRITE_SLOT_MAX = 5
DPI_LEVEL_INDEX_MIN = 1
DPI_LEVEL_INDEX_MAX = 8
DPI_VALUE_ERROR_TEXT = "DPI must be a multiple of 50 between 50 and 6400"
DPI_SLOT_ERROR_TEXT = "slot must be 0..5"
DPI_EXTENDED_SLOT_ERROR_TEXT = "cannot write extended slot"
# Battery reply echo: the 08 04 request comes back as 09 04 ... with
# reply[5] = link state, reply[6] = percent, reply[7] = charging flag.
# Observed on hardware: 09 04 00 00 00 02 64 00 -> 100%, not charging.
BATTERY_REPLY_ECHO = b"\x09\x04"
BATTERY_STATE_LABELS: dict[int, str] = {0x02: "2.4G mode"}


# ==========================================================================
# Protocol primitives — pure byte-level CompX config framing. These
# functions never touch the device: checksum, write/read frame builders,
# the frame verifier and the read-reply parser. Every transport and the
# self-check below consume them.
# ==========================================================================


def _compx_checksum(body: bytes) -> int:
    """Checksum tail of a CompX config frame: appended to the 16 leading
    bytes of a frame, the whole 17-byte frame sums to ≡ 0x55 (mod 256)."""
    return (0x55 - sum(body)) & 0xFF


def _verify_frame(frame: bytes) -> bool:
    """True when `frame` sums to ≡ 0x55 (mod 256) — the identity every
    CompX config frame (write, read command and read reply) must satisfy."""
    return (sum(frame) & 0xFF) == 0x55


def dpi_code(dpi: int) -> int | None:
    """Encode a DPI value into the slot code the mouse stores: the stored
    code is (dpi / 50) − 1, so 400 → 0x07 … 6400 → 0x7F. Returns None when
    `dpi` cannot be represented (not a multiple of 50, or beyond 0x7F)."""
    if dpi % 50 != 0:
        return None
    code = dpi // 50 - 1
    if not 0 <= code <= 0x7F:
        return None
    return code


def dpi_decode(code: int, mul: int = 0) -> int | None:
    """Decode one DPI slot code: (code + 1) × 50 for code ≤ 0x7F and
    mul = 0. Extended encoding (code > 0x7F or mul ≠ 0) is not understood
    yet, so it decodes to None and the caller shows the raw bytes."""
    if mul != 0 or not 0 <= code <= 0x7F:
        return None
    return (code + 1) * 50


def _slot_checksum_ok(x: int, y: int, mul: int, crc: int) -> bool:
    """True when a DPI slot row satisfies its checksum identity: the four
    stored bytes (x, y, mul, per-slot checksum) sum to ≡ 0x55 (mod 256)."""
    return (x + y + mul + crc) & 0xFF == 0x55


def _active_slot_for_index(index: int) -> int | None:
    """Map the raw active-level index (register 0x0004, 1-based) to the DPI
    slot row it marks; None when the byte is not a 1..8 level index."""
    if DPI_LEVEL_INDEX_MIN <= index <= DPI_LEVEL_INDEX_MAX:
        return index - ACTIVE_LEVEL_OFFSET
    return None


def _build_write_frame(addr: int, data: bytes) -> bytes:
    """Assemble the 17-byte config-memory write frame for (addr, data).

    Layout: 08 07 00 AH AL LN + data + 0x00 pad + checksum tail, where
    AH/AL is the big-endian 16-bit address and LN the payload length. The
    `set` EEPROM packet is the canonical frame (see _eeprom_packet()).
    """
    if not 1 <= len(data) <= PROBE_MAX_READ_BYTES:
        raise ValueError(
            f"config write payload must be 1..{PROBE_MAX_READ_BYTES} bytes, "
            f"got {len(data)}"
        )
    body = (
        b"\x08\x07\x00"  # report 0x08, write opcode 0x07, reserved 0x00
        + bytes(((addr >> 8) & 0xFF, addr & 0xFF, len(data)))  # AH, AL, LN
        + data
        + b"\x00" * (PROBE_FRAME_BYTES - 7 - len(data))  # pad to 16 leading bytes
    )
    return body + bytes((_compx_checksum(body),))


def _build_read_frame(addr: int, length: int) -> bytes:
    """Assemble the 17-byte config-memory read command for (addr, length).

    Layout: 08 08 00 AH AL LN + ten 0x00 pad bytes + checksum tail. The
    firmware answers on the interrupt-IN endpoint with the reply frame
    parsed by _parse_read_reply().
    """
    if not 1 <= length <= PROBE_MAX_READ_BYTES:
        raise ValueError(
            f"config read length must be 1..{PROBE_MAX_READ_BYTES}, got {length}"
        )
    body = (
        b"\x08\x08\x00"  # report 0x08, read opcode 0x08, reserved 0x00
        + bytes(((addr >> 8) & 0xFF, addr & 0xFF, length))  # AH, AL, LN
        + b"\x00" * 10  # pad to the 16 leading bytes
    )
    return body + bytes((_compx_checksum(body),))


def _build_battery_request() -> bytes:
    """Assemble the 17-byte battery-state request frame.

    Layout: 08 04 + fourteen 0x00 pad bytes + checksum tail (the same
    0x55-identity tail as every config frame). The firmware echoes 09 04
    on the interrupt-IN endpoint; read_battery() verifies and parses that
    reply. Delivered as an output SET_REPORT (report_type 0x02), which the
    FA7B firmware answers — as with the 0x08 read command, it dispatches on
    the payload, not the report type.
    """
    body = b"\x08\x04" + b"\x00" * (PROBE_FRAME_BYTES - 3)  # 16 leading bytes
    return body + bytes((_compx_checksum(body),))


def _parse_read_reply(reply: bytes) -> bytes:
    """Extract the payload of a config-memory read reply.

    A reply is 09 08 00 AH AL LN + LN payload bytes + 0x00 pad + checksum;
    LN is byte 5, so the frame itself says how long the payload is. The
    parser checks the frame is complete and LN is a sane payload length
    (1..PROBE_MAX_READ_BYTES — the firmware ceiling), then returns the
    payload slice. Callers verify the frame first (_verify_frame + header
    echo) and only then trust the slice.
    """
    if len(reply) != PROBE_FRAME_BYTES:
        raise ValueError(
            f"config read reply must be {PROBE_FRAME_BYTES} bytes, "
            f"got {len(reply)}"
        )
    length = reply[5]
    if not 1 <= length <= PROBE_MAX_READ_BYTES:
        raise ValueError(
            f"config read reply length byte LN must be 1.."
            f"{PROBE_MAX_READ_BYTES}, got {length}"
        )
    return reply[6 : 6 + length]


def _eeprom_packet(rate_code: int) -> bytes:
    """Assemble the 17-byte EEPROM persistence packet for `rate_code`.

    `rate_code` is the report-interval code the write stores (see
    INTERVAL_CODE_BY_RATE). The rate byte and its complement form the whole
    2-byte payload of a plain config-memory write at 0x0000, so the frame is
    delegated to _build_write_frame() and the tail can never drift out of
    sync with the payload. Keeping the write at 2 bytes (rate pair only)
    matters: a longer payload would overwrite the neighbouring header fields
    at 0x0002..0x0005 (DPI-level count and active level index), which is
    exactly the bug this length avoids.
    """
    data = bytes((rate_code, 0x55 - rate_code))
    return _build_write_frame(0x0000, data)


# Byte-identity reference for _verify_packet_generation(): the packets below
# are the CURRENT len=2 EEPROM writes (rate pair only at 0x0000) verified on
# hardware on 2026-09-06 — the mouse accepts them and they no longer clobber
# the DPI-level fields at 0x0002..0x0005 (0x06 level count, 0x01 active
# index). The builder above must reproduce them exactly.
_KNOWN_GOOD_EEPROM_PACKETS: dict[int, bytes] = {
    125: b"\x08\x07\x00\x00\x00\x02\x08\x4d\x00\x00\x00\x00\x00\x00\x00\x00\xef",
    500: b"\x08\x07\x00\x00\x00\x02\x02\x53\x00\x00\x00\x00\x00\x00\x00\x00\xef",
    1000: b"\x08\x07\x00\x00\x00\x02\x01\x54\x00\x00\x00\x00\x00\x00\x00\x00\xef",
}
# Historical captures (v1.0.1..v1.1.0, len=6): the same writes shipped with a
# constant 01 54 00 55 tail, overwriting registers 0x0002..0x0005 (DPI level
# count and active level index) and breaking the mouse's DPI button. They are
# kept here only for reference — the len=2 packets above replaced them:
#   125: b"\x08\x07\x00\x00\x00\x06\x08\x4d\x01\x54\x00\x55\x00\x00\x00\x00\x41"
#   500: b"\x08\x07\x00\x00\x00\x06\x02\x53\x01\x54\x00\x55\x00\x00\x00\x00\x41"
#   1000: b"\x08\x07\x00\x00\x00\x06\x01\x54\x01\x54\x00\x55\x00\x00\x00\x00\x41"

# Byte-identity reference for _verify_battery_request(): the 08 04 request
# as captured from a real mouse. The builder above must reproduce it exactly.
_KNOWN_GOOD_BATTERY_REQUEST: bytes = (
    b"\x08\x04" + b"\x00" * 14 + b"\x49"
)


def _verify_packet_generation() -> None:
    """Assert the built EEPROM packets match the known-good captures.

    Runs only when COMPX_SELFCHECK=1 (see main()); raises AssertionError on
    any drift so a regression in packet assembly fails fast and loud.
    """
    for rate, known_good in _KNOWN_GOOD_EEPROM_PACKETS.items():
        built = _eeprom_packet(INTERVAL_CODE_BY_RATE[rate])
        if built != known_good:
            raise AssertionError(
                f"EEPROM packet drift for {rate} Hz: built {built.hex(' ')} "
                f"!= known-good {known_good.hex(' ')}"
            )
        if (sum(built) & 0xFF) != 0x55:
            raise AssertionError(
                f"EEPROM packet for {rate} Hz violates the 0x55 checksum identity"
            )


def _verify_battery_request() -> None:
    """Assert the built battery request matches the known-good capture.

    Runs only when COMPX_SELFCHECK=1 (see main()); raises AssertionError on
    any drift in the request framing.
    """
    built = _build_battery_request()
    if built != _KNOWN_GOOD_BATTERY_REQUEST:
        raise AssertionError(
            f"Battery request drift: built {built.hex(' ')} "
            f"!= known-good {_KNOWN_GOOD_BATTERY_REQUEST.hex(' ')}"
        )
    if (sum(built) & 0xFF) != 0x55:
        raise AssertionError(
            "Battery request violates the 0x55 checksum identity"
        )


# DPI-codec round-trips confirmed on hardware: the stored code is
# (dpi / 50) − 1, so 400 → 0x07 … 6400 → 0x7F (see dpi_code/dpi_decode).
_DPI_CODEC_ROUND_TRIPS: tuple[tuple[int, int], ...] = (
    (400, 0x07),
    (800, 0x0F),
    (1600, 0x1F),
    (2400, 0x2F),
    (6400, 0x7F),
)


def _verify_dpi_codec() -> None:
    """Assert dpi_code/dpi_decode round-trip every known-good DPI pair.

    Runs only when COMPX_SELFCHECK=1 (see main()); raises AssertionError on
    any drift, and also asserts the not-yet-understood encodings decode to
    None instead of a wrong number.
    """
    for dpi, code in _DPI_CODEC_ROUND_TRIPS:
        if dpi_code(dpi) != code:
            raise AssertionError(f"dpi_code({dpi}) != 0x{code:02X}")
        if dpi_decode(code) != dpi:
            raise AssertionError(f"dpi_decode(0x{code:02X}) != {dpi}")
    if dpi_decode(0x80) is not None:
        raise AssertionError("dpi_decode accepted a code above 0x7F")
    if dpi_decode(0x7B, mul=0x44) is not None:
        raise AssertionError("dpi_decode accepted an extended (mul ≠ 0) slot")


POLLING_VARIANTS: dict[int, tuple[bytes, ...]] = {
    125: (
        b"\x06\x11\x00\x00\x00\x00\x00\x00",
        b"\x08\x11\x00\x00\x00\x06\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        _eeprom_packet(INTERVAL_CODE_BY_RATE[125]),
    ),
    500: (
        b"\x06\x11\x00\x01\x00\x00\x00\x00",
        b"\x08\x11\x00\x00\x00\x06\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        _eeprom_packet(INTERVAL_CODE_BY_RATE[500]),
    ),
    1000: (
        b"\x06\x11\x00\x02\x00\x00\x00\x00",
        b"\x08\x11\x00\x00\x00\x06\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        _eeprom_packet(INTERVAL_CODE_BY_RATE[1000]),
    ),
}


# ==========================================================================
# USB transport (pyusb) — the raw-USB channel to interface 1. Works whether
# or not the kernel bound the interface, so it is the primary channel of
# `probe` and `set`. Each call owns its claimed session (_usb_config_device),
# which lets callers compose _usb_send_report and _usb_read_reply freely.
# ==========================================================================


def _pyusb():
    """Import pyusb lazily: it is only needed by the raw-USB `set`/`probe` paths."""
    try:
        import usb.core
        import usb.util
    except ImportError as exc:
        raise RuntimeError(
            "The `usb` package (pyusb) is missing — it is required for this "
            "command. Install it with: pip install pyusb"
        ) from exc
    return usb.core, usb.util


@contextlib.contextmanager
def _usb_config_device():
    """Yield the CompX mouse with its config interface (interface 1) claimed.

    Detaches the kernel driver (usbhid) if it owns interface 1 and re-attaches
    it on exit, so raw-USB traffic never leaves the interface in a broken
    state. Shared by `set` (SET_REPORT writes) and `probe` (register reads),
    which both talk to interface 1 through pyusb.
    """
    usb_core, usb_util = _pyusb()

    usb_dev = None
    for product_id in PRODUCT_IDS:
        usb_dev = usb_core.find(idVendor=VENDOR_ID, idProduct=product_id)
        if usb_dev is not None:
            break
    if usb_dev is None:
        raise RuntimeError(DEVICE_NOT_FOUND_TEXT)

    detached = False
    try:
        if usb_dev.is_kernel_driver_active(1):
            usb_dev.detach_kernel_driver(1)
            detached = True
        usb_util.claim_interface(usb_dev, 1)
    except usb_core.USBError as exc:
        raise RuntimeError(
            f"Cannot claim the CompX config interface (interface 1): {exc}"
        ) from exc

    try:
        yield usb_dev
    finally:
        try:
            usb_util.release_interface(usb_dev, 1)
        except usb_core.USBError:
            pass  # the device vanished mid-operation — nothing left to restore
        if detached:
            try:
                usb_dev.attach_kernel_driver(1)
            except usb_core.USBError:
                pass
        usb_util.dispose_resources(usb_dev)


def _usb_send_report(packet: bytes, report_type: int = 0x02) -> None:
    """Deliver one report to interface 1 as a USB SET_REPORT (lazy import).

    `report_type` is the wValue high byte: 0x02 (output report) for the
    `set` rate packets, 0x03 (feature report) for the `probe` read command.
    The firmware dispatches on the report payload, so both forms share this
    one transport path. pyusb failures surface as OSError, the transport
    error type every caller (read_config_register, write_config,
    read_battery) already wraps.
    """
    usb_core, _ = _pyusb()
    with _usb_config_device() as usb_dev:
        report_id = packet[0]
        try:
            result = usb_dev.ctrl_transfer(
                bmRequestType=0x21,
                bRequest=9,  # SET_REPORT
                wValue=(report_type << 8) | report_id,
                wIndex=1,
                data_or_wLength=packet,
                timeout=1000,
            )
        except usb_core.USBError as exc:
            raise OSError(str(exc)) from exc
        if result != len(packet):
            raise OSError("USB SET_REPORT rejected by the device")


def _usb_read_reply(timeout: int = PROBE_READ_TIMEOUT_MS) -> bytes | None:
    """Wait for one reply frame on the interrupt-IN endpoint of interface 1.

    Finds the endpoint exactly like `probe` did historically (first
    interrupt-IN descriptor of the interface) and reads one
    PROBE_FRAME_BYTES frame. Returns None when the device sends no frame
    within `timeout` ms; any other USB error propagates as OSError so the
    caller can attach context to it.
    """
    usb_core, usb_util = _pyusb()
    with _usb_config_device() as usb_dev:
        try:
            configuration = usb_dev.get_active_configuration()
        except usb_core.USBError as exc:
            raise RuntimeError(
                f"Cannot read the active USB configuration: {exc}"
            ) from exc
        try:
            interface = configuration[(1, 0)]
        except KeyError as exc:
            raise RuntimeError(
                "The active USB configuration exposes no interface 1 "
                "(CompX config interface)"
            ) from exc

        endpoint = usb_util.find_descriptor(
            interface,
            custom_match=lambda ep: (
                usb_util.endpoint_direction(ep.bEndpointAddress)
                == usb_util.ENDPOINT_IN
                and usb_util.endpoint_type(ep.bmAttributes)
                == usb_util.ENDPOINT_TYPE_INTR
            ),
        )
        if endpoint is None:
            raise RuntimeError(
                "No interrupt-IN endpoint on the CompX config interface — "
                "cannot read the config-memory reply"
            )

        try:
            return bytes(
                usb_dev.read(
                    endpoint.bEndpointAddress,
                    PROBE_FRAME_BYTES,
                    timeout=timeout,
                )
            )
        except usb_core.USBError as exc:
            if exc.errno == errno.ETIMEDOUT:
                return None  # no frame within `timeout` — the caller decides
            raise OSError(str(exc)) from exc


# ==========================================================================
# hidraw transport (hidapi + raw ioctl) — the second delivery channel of
# `set`. Works only while the kernel bound interface 1 to usbhid and gave it
# a hidraw node with one of the CONFIG_USAGE_PAGES.
# ==========================================================================


def _hidapi():
    """Import hidapi lazily: it is only needed by `set`, not `check`/`--help`."""
    try:
        import hid
    except ImportError as exc:
        raise RuntimeError(
            "The `hid` package (hidapi) is missing — it is required for `set`. "
            "Install it with: pip install hidapi"
        ) from exc
    return hid


def _unique_paths(paths: list[bytes]) -> list[bytes]:
    """Drop duplicates while preserving first-seen order."""
    seen: set[bytes] = set()
    unique: list[bytes] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def find_device_entries() -> list[dict]:
    """Enumerate every CompX product known to hidapi; [] if enumeration fails.

    Raises RuntimeError when the `hid` package is not installed — the error is
    deliberate and must not be swallowed by the enumeration fallback below.
    """
    hid = _hidapi()
    entries: list[dict] = []
    try:
        for product_id in PRODUCT_IDS:
            entries.extend(hid.enumerate(VENDOR_ID, product_id))
    except Exception:  # noqa: BLE001 — deliberate catch-all: enumeration is
        # best-effort; any hidapi failure means "no device visible now".
        return []
    return entries


def _hidraw_ioctl(path: bytes, packet: bytes, *, output: bool = False) -> None:
    """Send `packet` through the raw hidraw ioctl, bypassing hidapi."""
    size = len(packet)
    nr = HIDIOCSOUTPUT if output else HIDIOCSFEATURE
    # The request word mirrors _IOC(_IOC_READ | _IOC_WRITE, 'H', nr, size) from
    # uapi/linux/hidraw.h.
    request = 0xC0000000 | (size << 16) | (ord("H") << 8) | nr
    with open(path, "wb+", buffering=0) as handle:
        if fcntl.ioctl(handle, request, packet) < 0:
            raise OSError("hidraw ioctl rejected the report")


def _send_packet_to_device(dev, packet: bytes) -> None:
    """Try a feature report first, then a plain output write on one hidapi handle."""
    last_error = "hidapi refused the report"
    for sender_name, sender in (
        ("feature", dev.send_feature_report),
        ("output", dev.write),
    ):
        try:
            result = sender(packet)
        except OSError as exc:
            last_error = f"{sender_name}: {exc}"
            continue
        if isinstance(result, int) and result < 0:
            last_error = f"{sender_name}: rejected by the device"
            continue
        return
    raise OSError(last_error)


def _send_packet_to_path(path: bytes, packet: bytes) -> None:
    """Send one packet via hidapi, falling back to raw hidraw ioctls.

    Attempts are retried briefly: after the pyusb SET_REPORT step detaches and
    re-attaches the kernel driver, udev re-creates the hidraw node and it may
    not be openable for a moment. Retries never duplicate a successful send.
    """
    hid = _hidapi()
    errors: list[OSError] = []
    for attempt in range(SEND_ATTEMPTS):
        try:
            dev = hid.device()
            dev.open_path(path)
            try:
                _send_packet_to_device(dev, packet)
                return
            finally:
                dev.close()
        except OSError as exc:
            errors.append(exc)

        packet_bytes = bytes(packet)
        for output in (False, True):
            try:
                _hidraw_ioctl(path, packet_bytes, output=output)
                return
            except OSError as exc:
                errors.append(exc)

        if attempt + 1 < SEND_ATTEMPTS:
            time.sleep(SEND_RETRY_DELAY_SEC)

    if errors:
        raise errors[-1]
    raise OSError("device refused the report")


# ==========================================================================
# Device layer — device-level operations over the two transports:
# control-path discovery, config register reads/writes and the multi-channel
# `set` delivery engine (raw-USB always, hidraw paths when present).
# ==========================================================================


def find_device_paths() -> list[bytes]:
    """Control hidraw paths: config usage pages first, interface #1 as fallback."""
    entries = find_device_entries()
    control_paths = _unique_paths(
        entry["path"]
        for entry in entries
        if entry.get("usage_page") in CONFIG_USAGE_PAGES and entry.get("path")
    )
    if control_paths:
        return control_paths
    return _unique_paths(
        entry["path"]
        for entry in entries
        if entry.get("interface_number") == 1 and entry.get("path")
    )


def get_active_product_id() -> int | None:
    """Return the first CompX product id visible on the bus, or None."""
    hid = _hidapi()
    for product_id in PRODUCT_IDS:
        if hid.enumerate(VENDOR_ID, product_id):
            return product_id
    return None


def read_config_register(addr: int, length: int) -> bytes:
    """Read `length` config-memory bytes at `addr` via the raw-USB channel.

    Sends the read frame as a feature SET_REPORT and picks the reply off the
    interrupt-IN endpoint. The reply echoes AH/AL/LN and must sum to 0x55,
    so both are verified before the payload is trusted. A received frame
    that fails verification was probably a stale write-ack queued by a
    recent `set` burst — it is drained by this read, so the read is retried
    (READ_ATTEMPTS times) before raising RuntimeError with the failing
    address attached. Timeouts and transport errors are not retried.
    """
    command = _build_read_frame(addr, length)
    last_detail = f"No config-memory reply from the device for 0x{addr:04X}"
    for _ in range(READ_ATTEMPTS):
        try:
            _usb_send_report(command, report_type=0x03)  # feature report 0x0308
        except OSError as exc:
            raise RuntimeError(
                f"USB SET_REPORT failed while reading config memory at "
                f"0x{addr:04X}: {exc}"
            ) from exc

        try:
            reply = _usb_read_reply()
        except OSError as exc:
            raise RuntimeError(
                f"No config-memory reply from the device for 0x{addr:04X} "
                f"(interrupt-IN read: {exc})"
            ) from exc
        if reply is None:
            raise RuntimeError(
                f"No config-memory reply from the device for 0x{addr:04X} "
                f"(interrupt-IN read timed out)"
            )
        if len(reply) != PROBE_FRAME_BYTES:
            last_detail = (
                f"Short config-memory reply for 0x{addr:04X}: got "
                f"{len(reply)} bytes, expected {PROBE_FRAME_BYTES}"
            )
            continue
        if reply[3:6] != command[3:6]:
            last_detail = (
                f"Config-memory reply header mismatch for 0x{addr:04X}: "
                f"echo AH/AL/LN = {reply[3:6].hex(' ')} "
                f"(expected {command[3:6].hex(' ')}) — stale reply?"
            )
            continue
        if not _verify_frame(reply):
            last_detail = (
                f"Config-memory reply checksum mismatch for 0x{addr:04X}: "
                "frame does not sum to 0x55"
            )
            continue
        return _parse_read_reply(reply)
    raise RuntimeError(
        f"{last_detail} (retried {READ_ATTEMPTS} times after stale frames; "
        "is another program writing to the mouse?)"
    )


def write_config(addr: int, data: bytes) -> None:
    """Write `data` to config memory at `addr` via the raw-USB channel.

    The generic counterpart of read_config_register(): builds the write
    frame and delivers it as an output SET_REPORT. `set` uses the same frame
    shape for its EEPROM packet, but delivers it through the full per-packet
    pipeline in _apply_rate_packets().
    """
    frame = _build_write_frame(addr, data)
    try:
        _usb_send_report(frame)
    except OSError as exc:
        raise RuntimeError(
            f"USB SET_REPORT failed while writing config memory at "
            f"0x{addr:04X}: {exc}"
        ) from exc


def read_polling_rate_hz() -> int | None:
    """Read the polling-rate code at 0x0000 and map it to Hz.

    None means the code is not in the known map (PROBE_POLLING_HZ_BY_CODE);
    transport failures still raise RuntimeError with the failing register
    attached, as read_config_register() does.
    """
    code = read_config_register(PROBE_POLLING_ADDR, 1)[0]
    return PROBE_POLLING_HZ_BY_CODE.get(code)


def read_active_dpi_level() -> tuple[int, int] | None:
    """Read the active DPI level marker: (raw index, raw complement).

    The index byte lives at 0x0004, its complement at 0x0005. Returns None
    only when the pair is unset (0xFF 0xFF). No offset interpretation
    happens here — the caller maps the index to a slot row (see
    ACTIVE_LEVEL_OFFSET), so the raw bytes stay available for display.
    """
    data = read_config_register(ACTIVE_DPI_LEVEL_ADDR, 2)
    if data == b"\xff\xff":
        return None
    return data[0], data[1]


def read_dpi_slots() -> list[dict]:
    """Read the eight DPI slot rows (4 bytes each) from 0x000C onward.

    Two rows are fetched per read command (PROBE_BLOCK_BYTES = 8). Every
    row comes back as a dict {index, addr, x, y, mul, crc_ok, dpi}, where
    crc_ok is the slot checksum identity ((x + y + mul + crc) ≡ 0x55) and
    dpi is dpi_decode(x, mul) — or None for empty, bad-checksum or
    extended-encoding rows, which the caller displays as raw bytes.
    """
    table_bytes = PROBE_DPI_TABLE_END - PROBE_DPI_TABLE_START
    slots: list[dict] = []
    for offset in range(0, table_bytes, PROBE_BLOCK_BYTES):
        data = read_config_register(PROBE_DPI_TABLE_START + offset, PROBE_BLOCK_BYTES)
        for local in range(0, PROBE_BLOCK_BYTES, PROBE_DPI_SLOT_BYTES):
            x, y, mul, crc = data[local : local + PROBE_DPI_SLOT_BYTES]
            addr = PROBE_DPI_TABLE_START + offset + local
            crc_ok = _slot_checksum_ok(x, y, mul, crc)
            dpi = dpi_decode(x, mul)
            slots.append(
                {
                    "index": (addr - PROBE_DPI_TABLE_START) // PROBE_DPI_SLOT_BYTES,
                    "addr": addr,
                    "x": x,
                    "y": y,
                    "mul": mul,
                    "crc": crc,
                    "crc_ok": crc_ok,
                    "dpi": dpi,
                }
            )
    return slots


def read_battery() -> tuple[int, bool, int]:
    """Read the battery state: returns (percent, charging, state).

    Sends the 17-byte 08 04 battery request (_build_battery_request()) as
    an output SET_REPORT and picks the 09 04 echo reply off the
    interrupt-IN endpoint (reply[5] = link state, reply[6] = percent,
    reply[7] = charging flag). A received frame that fails verification was
    probably a stale write-ack queued by a recent `set` burst — it is
    drained by this read, so the request is retried (READ_ATTEMPTS times)
    before RuntimeError is raised. Timeouts ("no battery reply") are not
    retried: a late reply must not be doubled.
    """
    last_detail = "no battery reply"
    for _ in range(READ_ATTEMPTS):
        try:
            _usb_send_report(_build_battery_request())
        except OSError as exc:
            raise RuntimeError(
                f"USB SET_REPORT failed while requesting the battery state: {exc}"
            ) from exc

        try:
            reply = _usb_read_reply()
        except OSError as exc:
            raise RuntimeError(f"Battery reply could not be read: {exc}") from exc
        if reply is None:
            raise RuntimeError("no battery reply")
        if len(reply) != PROBE_FRAME_BYTES:
            last_detail = "no battery reply"
            continue
        if reply[:2] != BATTERY_REPLY_ECHO:
            last_detail = (
                f"Battery reply header mismatch: {reply[:2].hex(' ')} != "
                f"{BATTERY_REPLY_ECHO.hex(' ')} — stale reply?"
            )
            continue
        if not _verify_frame(reply):
            last_detail = "Battery reply checksum mismatch: frame does not sum to 0x55"
            continue
        return reply[6], bool(reply[7]), reply[5]
    raise RuntimeError(
        f"{last_detail} (retried {READ_ATTEMPTS} times after stale frames; "
        "is another program writing to the mouse?)"
    )


def _format_hid_error(exc: BaseException) -> str:
    if isinstance(exc, PermissionError):
        return HID_PERMISSION_TEXT
    return str(exc)


def _apply_rate_packets(
    path: bytes | None, rate: int
) -> tuple[set[int], bool, list[str]]:
    """Send every packet of `rate`: raw-USB SET_REPORT always, then hidraw.

    `path` is the control hidraw node used by the hidapi/ioctl pass; when it
    is None (no hidraw node exists for interface 1) only the raw-USB pass
    runs. Returns (delivered, eeprom_packet_written, errors); `delivered`
    holds the indices of every distinct packet accepted by at least one
    channel, so a packet confirmed twice (USB and hidapi) still counts once.
    """
    delivered: set[int] = set()
    eeprom_written = False
    errors: list[str] = []
    packets = POLLING_VARIANTS[rate]

    for index, packet in enumerate(packets):
        try:
            _usb_send_report(packet)
        except Exception as exc:  # noqa: BLE001 — per-packet USB failures are
            # collected as warnings: pyusb may raise varied exceptions across
            # channels, and one bad packet must not abort the whole `set`.
            errors.append(f"usb: {exc}")
        else:
            delivered.add(index)
            if index == EEPROM_PACKET_INDEX:
                eeprom_written = True

    if path is None:
        return delivered, eeprom_written, errors

    for index, packet in enumerate(packets):
        try:
            _send_packet_to_path(path, packet)
        except (PermissionError, OSError) as exc:
            errors.append(_format_hid_error(exc))
        except Exception as exc:  # noqa: BLE001 — hidapi paths may raise other
            # error types; each is recorded and the remaining channels still run.
            errors.append(str(exc))
        else:
            delivered.add(index)
            if index == EEPROM_PACKET_INDEX:
                eeprom_written = True

    return delivered, eeprom_written, errors


def send_polling_packet(rate: int) -> tuple[set[int], bool, list[str]]:
    """Apply `rate` on every control path found (raw-USB fallback included).

    Returns (delivered, eeprom_packet_written, errors); `delivered` is the
    union of distinct packet indices confirmed on any channel. Success means
    at least MIN_SENT_FOR_SUCCESS distinct packets were delivered (see
    POLLING_VARIANTS).
    """
    paths = find_device_paths()
    # No control hidraw node (interface 1 currently has no kernel driver):
    # the raw-USB SET_REPORT pass can still deliver every packet alone, so
    # fall back to a USB-only pass instead of failing.
    targets: list[bytes | None] = paths if paths else [None]

    delivered: set[int] = set()
    eeprom_written = False
    errors: list[str] = []
    for path in targets:
        path_delivered, written, path_errors = _apply_rate_packets(path, rate)
        delivered |= path_delivered
        eeprom_written = eeprom_written or written
        errors.extend(path_errors)
    return delivered, eeprom_written, errors


# ==========================================================================
# Input measurement (evdev) — `check` support: locate the mouse input node
# and count EV_REL events on it. Unrelated to the config interface above.
# ==========================================================================


def _sysfs_usb_id(event_path: str) -> tuple[str, str]:
    """Return (vendor, product) hex ids of the USB device behind an event node."""
    base = Path("/sys/class/input") / Path(event_path).name / "device" / "id"

    def read(name: str) -> str:
        try:
            return (base / name).read_text().strip().lower()
        except OSError:
            return ""

    return read("vendor"), read("product")


def find_event_device(explicit: str | None) -> str:
    """Locate the mouse input node: explicit path, by-id name, then sysfs scan."""
    if explicit:
        if os.path.exists(explicit):
            return os.path.realpath(explicit)
        raise RuntimeError(f"No such device node: {explicit}")

    for by_id_path in MOUSE_BY_ID_PATHS:
        if os.path.exists(by_id_path):
            return os.path.realpath(by_id_path)

    exact_matches: list[str] = []
    vendor_matches: list[str] = []
    known_products = {f"{product_id:04x}" for product_id in PRODUCT_IDS}
    for event_path in sorted(glob.glob("/dev/input/event*")):
        vendor, product = _sysfs_usb_id(event_path)
        if vendor != f"{VENDOR_ID:04x}":
            continue
        if product in known_products:
            exact_matches.append(event_path)
        else:
            vendor_matches.append(event_path)

    for event_path in exact_matches or vendor_matches:
        return os.path.realpath(event_path)
    raise RuntimeError(EVENT_NOT_FOUND_TEXT)


def measure_event_rate_hz(event_path: str, seconds: float = MEASURE_SECONDS) -> float:
    """Count EV_REL mouse events on `event_path` for `seconds`; return Hz."""
    try:
        from evdev import InputDevice, ecodes
    except ImportError as exc:
        raise RuntimeError(
            "The `evdev` package is missing — it is required for `check`. "
            "Install it with: pip install evdev"
        ) from exc

    try:
        device = InputDevice(event_path)
    except PermissionError as exc:
        raise RuntimeError(INPUT_PERMISSION_HINT.format(event_path)) from exc

    stop = threading.Event()
    errors: list[str] = []
    counted = 0

    def count_events() -> None:
        nonlocal counted
        try:
            for event in device.read_loop():
                if stop.is_set():
                    break
                if event.type == ecodes.EV_REL:
                    counted += 1
        except Exception as exc:  # noqa: BLE001 — the reader thread is stopped
            # by closing the device; whatever evdev raises on that shutdown path
            # is only reported when it was NOT the deliberate stop.
            if not stop.is_set():
                errors.append(str(exc))

    thread = threading.Thread(target=count_events, name="evdev-counter", daemon=True)
    thread.start()
    time.sleep(seconds)
    stop.set()
    try:
        device.close()  # unblocks read_loop
    except OSError:
        pass
    thread.join(timeout=1.0)

    if errors:
        raise RuntimeError(f"Failed while reading events from {event_path}: {errors[0]}")
    return counted / seconds


# ==========================================================================
# Commands — CLI entry points. The behaviour and output of `set` and `check`
# are frozen since v1.0.0; `status`, `battery`, `probe` and `dpi` were added
# in v1.1.0.
# ==========================================================================


def _slot_value_text(slot: dict) -> str:
    """Compact human text for one DPI slot row's stored value.

    Decoded rows print the DPI ("400"), rows in the not-yet-understood
    extended encoding print the raw code (with mul when it is the cause:
    "raw:0x7B(mul=0x44)"), and an unset row prints "empty".
    """
    if (slot["x"], slot["y"], slot["mul"], slot["crc"]) == EMPTY_SLOT:
        return "empty"
    if slot["dpi"] is not None:
        return str(slot["dpi"])
    if slot["mul"]:
        return f"raw:0x{slot['x']:02X}(mul=0x{slot['mul']:02X})"
    return f"raw:0x{slot['x']:02X}"


def _battery_text(percent: int, charging: bool, state: int) -> str:
    """One-line battery readout: "Battery: 100% (not charging) [2.4G mode]".
    The bracketed link label is appended only for known states (see
    BATTERY_STATE_LABELS)."""
    text = f"Battery: {percent}% ({'charging' if charging else 'not charging'})"
    label = BATTERY_STATE_LABELS.get(state)
    if label is not None:
        text += f" [{label}]"
    return text


def cmd_status(_args: argparse.Namespace) -> int:
    """`status` entry point: read-only device snapshot.

    Every field is read in its own short raw-USB session (claim +
    detach/re-attach, a few ms each — acceptable for a one-shot readout).
    A failing field degrades to "n/a" with a warning on stderr instead of
    failing the snapshot; only a missing device — or one from which nothing
    could be read at all — exits non-zero (raised to main() as
    RuntimeError, printed as "Error: ...").
    """
    pid = get_active_product_id()
    if pid is None:
        raise RuntimeError(DEVICE_NOT_FOUND_TEXT)

    warnings: list[str] = []
    ok_fields = 0

    def warn(field: str, exc: BaseException) -> None:
        warnings.append(f"{field}: {exc}")

    battery_text = "Battery: n/a"
    try:
        percent, charging, state = read_battery()
    except RuntimeError as exc:
        warn("battery", exc)
    else:
        battery_text = _battery_text(percent, charging, state)
        ok_fields += 1

    polling_text = "n/a"
    try:
        hz = read_polling_rate_hz()
    except RuntimeError as exc:
        warn("polling rate", exc)
    else:
        ok_fields += 1
        if hz is not None:
            polling_text = f"{hz} Hz (register 0x{PROBE_POLLING_ADDR:04X})"
        else:
            polling_text = f"unknown code (register 0x{PROBE_POLLING_ADDR:04X})"

    dpi_slots: list[dict] | None = None
    try:
        dpi_slots = read_dpi_slots()
    except RuntimeError as exc:
        warn("DPI slots", exc)
    else:
        ok_fields += 1

    active_pair: tuple[int, int] | None = None
    try:
        active_pair = read_active_dpi_level()
    except RuntimeError as exc:
        warn("active DPI level", exc)
    else:
        if active_pair is not None:
            ok_fields += 1

    print(f"Device: CompX mouse (PID 0x{pid:04x})")
    print(f"Polling rate: {polling_text}")
    active_text = "n/a (register 0x0004 unset)"
    if active_pair is not None:
        index = active_pair[0]
        active_text = f"index 0x{index:02X} (register 0x{ACTIVE_DPI_LEVEL_ADDR:04X})"
        if index == 0:
            active_text += " → no level marked (register 0x0004 holds 0x00)"
        else:
            slot_index = _active_slot_for_index(index)
            if (
                slot_index is not None
                and dpi_slots is not None
                and slot_index < len(dpi_slots)
            ):
                active_text += (
                    f" → slot [{slot_index}]: {_slot_value_text(dpi_slots[slot_index])}"
                )
            else:
                active_text += (
                    " → no matching DPI slot (level numbering assumed 1-based, "
                    "ACTIVE_LEVEL_OFFSET — pending calibration)"
                )
    print(f"Active DPI level: {active_text}")

    if dpi_slots is not None:
        entries = [f"[{slot['index']}] {_slot_value_text(slot)}" for slot in dpi_slots]
        if active_pair is not None:
            slot_index = _active_slot_for_index(active_pair[0])
            if slot_index is not None and slot_index < len(entries):
                entries[slot_index] += " ← active"
        print("DPI slots: " + " ".join(entries))
    else:
        print("DPI slots: n/a")

    print(battery_text)

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0 if ok_fields else 1


def cmd_battery(_args: argparse.Namespace) -> int:
    """`battery` entry point: read the battery level and charging state.

    Transport failures raise RuntimeError/OSError, which main() reports as
    "Error: ..." — the battery readout has no field-level degradation.
    """
    print(_battery_text(*read_battery()))
    return 0


def _print_dpi_slots() -> int:
    """Print the DPI slot table and the active-level line (`dpi list`)."""
    slots = read_dpi_slots()
    active_pair = read_active_dpi_level()

    active_slot: int | None = None
    if active_pair is not None:
        active_slot = _active_slot_for_index(active_pair[0])

    print(
        "DPI slots (register 0x0004 = active level index; "
        "'*' marks the active slot):"
    )
    print(
        "  "
        + "slot".ljust(6)
        + "addr".ljust(8)
        + "x".ljust(5)
        + "y".ljust(5)
        + "mul".ljust(5)
        + "crc".ljust(6)
        + "value"
    )
    for slot in slots:
        star = "*" if slot["index"] == active_slot else " "
        print(
            "  "
            + f"{star}{slot['index']}".ljust(6)
            + f"0x{slot['addr']:04X}".ljust(8)
            + f"{slot['x']:02X}".ljust(5)
            + f"{slot['y']:02X}".ljust(5)
            + f"{slot['mul']:02X}".ljust(5)
            + f"{slot['crc']:02X}".ljust(6)
            + _slot_value_text(slot)
        )

    if active_pair is None:
        print("Active DPI level: unknown (register 0x0004 unset, 0xFF 0xFF)")
    elif active_slot is None:
        index = active_pair[0]
        print(
            f"Active DPI level: unknown (register 0x0004 holds 0x{index:02X}, "
            f"not a 1..{DPI_LEVEL_INDEX_MAX} index)"
        )
    else:
        print(
            f"Active DPI level: slot {active_slot} "
            f"(index 0x{active_pair[0]:02X}) → "
            f"{_slot_value_text(slots[active_slot])}"
        )
    return 0


def _resolve_active_slot() -> tuple[int, bool]:
    """Map register 0x0004 to the writable slot: (slot, active_known).

    The level index is 1-based (ACTIVE_LEVEL_OFFSET), so 1..8 maps to slots
    0..7. Any other byte — 0x00 (old `set` runs cleared the marker) or 0xFF
    (unset) — means the active level is unknown: the caller falls back to
    slot 0 and warns before writing.
    """
    pair = read_active_dpi_level()
    if pair is None:
        return 0, False
    slot = _active_slot_for_index(pair[0])
    if slot is None:
        return 0, False
    return slot, True


def _write_dpi_slot(target_slot: int, code: int) -> None:
    """Write `code` into both axes of `target_slot`, then read it back.

    The plain slot payload is (code, code, 0x00, checksum) with the per-slot
    checksum identity 0x55 − x − y − mul, i.e. 0x55 − 2·code. Readback
    verification fails fast when the stored code does not echo.
    """
    addr = PROBE_DPI_TABLE_START + PROBE_DPI_SLOT_BYTES * target_slot
    checksum = (0x55 - 2 * code) & 0xFF
    write_config(addr, bytes((code, code, 0x00, checksum)))
    readback = read_config_register(addr, PROBE_DPI_SLOT_BYTES)
    if readback[0] != code:
        raise RuntimeError(
            f"Readback verification failed for slot {target_slot} at "
            f"0x{addr:04X}: stored x=0x{readback[0]:02X}, expected 0x{code:02X}"
        )


def cmd_dpi(args: argparse.Namespace) -> int:
    """`dpi` entry point: list the slot table or write a DPI value.

    Bare `dpi` and `dpi list` print the eight slot rows with the active one
    marked; `dpi N` writes the plain encoding into the active level's slot
    and `dpi --slot S N` into an explicit slot 0..5.
    """
    value = args.value
    slot_arg = args.slot

    if slot_arg is not None and value in (None, "list"):
        raise RuntimeError("--slot requires a DPI value (`dpi --slot S N`)")
    if value is None or value == "list":
        return _print_dpi_slots()

    code = dpi_code(value)
    if code is None:
        raise RuntimeError(DPI_VALUE_ERROR_TEXT)

    active_known = False
    if slot_arg is not None:
        if not 0 <= slot_arg <= DPI_WRITE_SLOT_MAX:
            raise RuntimeError(DPI_SLOT_ERROR_TEXT)
        target_slot = slot_arg
    else:
        target_slot, active_known = _resolve_active_slot()
        if active_known and target_slot > DPI_WRITE_SLOT_MAX:
            raise RuntimeError(DPI_EXTENDED_SLOT_ERROR_TEXT)

    if slot_arg is None and not active_known:
        print(
            "warning: active DPI level unknown (register 0x0004 not a "
            f"1..{DPI_LEVEL_INDEX_MAX} index) — writing slot {target_slot}",
            file=sys.stderr,
        )

    slots = read_dpi_slots()
    old_text = _slot_value_text(slots[target_slot])

    _write_dpi_slot(target_slot, code)
    subject = "active level" if active_known else f"slot {target_slot}"
    print(f"DPI set: {subject} → {value} (was {old_text})")
    print(f"slot {target_slot} updated, verified")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    """`set` entry point: apply a polling rate and report the outcome."""
    rate = args.rate
    delivered, eeprom_written, errors = send_polling_packet(rate)
    if len(delivered) >= MIN_SENT_FOR_SUCCESS:
        pid = get_active_product_id()
        pid_text = hex(pid) if pid is not None else "unknown"
        total = len(POLLING_VARIANTS[rate])
        print(f"Rate: {rate} Hz")
        print(f"PID: {pid_text}")
        print(
            f"Packets delivered: {len(delivered)}/{total} distinct "
            f"(minimum for success: {MIN_SENT_FOR_SUCCESS})"
        )
        if eeprom_written:
            print("EEPROM write: done")
        else:
            print("EEPROM write: not confirmed (rate may reset after re-plug)")
        for err in errors:
            print(f"warning: {err}", file=sys.stderr)
        return 0

    print("Error: the device rejected the polling-rate packets", file=sys.stderr)
    for err in errors:
        print(f"  {err}", file=sys.stderr)
    return 1


def cmd_check(args: argparse.Namespace) -> int:
    """`check` entry point: measure the actual polling rate via input events.

    Errors raise RuntimeError/OSError and are reported by main() as
    "Error: ...", after any partial stdout ("Event device: …") already
    printed above — the location of the failure is visible either way.
    """
    event_path = find_event_device(args.device)

    print(f"Event device: {event_path}")
    print(f"Move the mouse… measuring for ~{MEASURE_SECONDS:g} s", flush=True)
    measured = measure_event_rate_hz(event_path)

    if measured <= 0:
        print("Measured rate: ~0 Hz (no mouse movement detected during the window)")
        print("Hint: keep moving the mouse while `check` runs.")
        return 0
    print(f"Measured rate: ~{measured:.0f} Hz")
    return 0


def _parse_hex_arg(text: str) -> int:
    """Parse a hex CLI value like 0x000C (bare digits are hex too: `0C`)."""
    try:
        return int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid hex value {text!r} (use e.g. 0x000C)"
        ) from None


def _parse_dpi_value(text: str) -> int | str:
    """Parse the `dpi` positional value: the literal 'list' or an integer."""
    if text == "list":
        return "list"
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid DPI value {text!r} (use 'list' or a multiple of 50 "
            "between 50 and 6400)"
        ) from None


def cmd_probe(args: argparse.Namespace) -> int:
    """`probe` entry point: read and interpret a config-memory window."""
    start, length = args.start, args.length
    if length < 1 or length > PROBE_MAX_DUMP_BYTES:
        raise RuntimeError(
            f"--length must be at least 1 byte and at most "
            f"0x{PROBE_MAX_DUMP_BYTES:X} bytes"
        )

    data = bytearray()
    for offset in range(0, length, PROBE_BLOCK_BYTES):
        chunk_length = min(PROBE_BLOCK_BYTES, length - offset)
        chunk = read_config_register(start + offset, chunk_length)
        data.extend(chunk)
        print(f"0x{start + offset:04X}: " + chunk.hex(" "))

    end = start + length
    notes: list[str] = []
    if start <= PROBE_POLLING_ADDR < end:
        code = data[PROBE_POLLING_ADDR - start]
        hz = PROBE_POLLING_HZ_BY_CODE.get(code)
        if hz is None:
            notes.append(f"polling rate code at 0x0000: 0x{code:02X} (no known Hz mapping)")
        else:
            notes.append(f"polling rate code at 0x0000: 0x{code:02X} = {hz} Hz")

    dpi_notes: list[str] = []
    for slot_addr in range(
        PROBE_DPI_TABLE_START, PROBE_DPI_TABLE_END, PROBE_DPI_SLOT_BYTES
    ):
        if not (start <= slot_addr and slot_addr + PROBE_DPI_SLOT_BYTES <= end):
            continue
        slot_number = (slot_addr - PROBE_DPI_TABLE_START) // PROBE_DPI_SLOT_BYTES
        x, y, mul, crc = data[slot_addr - start : slot_addr - start + 4]
        if (x, y, mul, crc) == EMPTY_SLOT:
            dpi_notes.append(f"slot {slot_number}: (empty)")
            continue
        checksum_ok = _slot_checksum_ok(x, y, mul, crc)
        dpi_notes.append(
            f"slot {slot_number}: x=0x{x:02X} y=0x{y:02X} mul=0x{mul:02X} "
            f"crc=0x{crc:02X} (checksum {'OK' if checksum_ok else 'FAIL'})"
        )
    if dpi_notes:
        notes.append(
            "DPI table 0x000C-0x002B: x/y/mul/crc per slot — rows with "
            "x <= 0x7F and mul = 0 decode via dpi_decode (400..6400); "
            "extended rows (code > 0x7F or mul != 0) are shown raw"
        )
        notes.extend(dpi_notes)

    if notes:
        print("Interpretation:")
        for note in notes:
            print(f"  {note}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compxctl",
        description="Set polling rate and DPI, measure and read battery of "
                    "CompX / Ardor Gaming mice (VID 0x25A7).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    # No required subcommand: a bare `compxctl` runs `status` (see main()).
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    status_parser = subparsers.add_parser(
        "status",
        help="print a read-only device snapshot (rate, DPI levels, battery)",
        description="Print a read-only snapshot of the CompX mouse: product id, "
                    "polling rate, active DPI level, the eight DPI slot rows and "
                    "the battery state (raw USB, requires pyusb).",
    )
    status_parser.set_defaults(func=cmd_status)

    battery_parser = subparsers.add_parser(
        "battery",
        help="read the battery level and charging state",
        description="Read the battery state of the CompX mouse over raw USB "
                    "(requires pyusb). Read-only: prints percent, charging flag "
                    "and, when known, the link mode.",
    )
    battery_parser.set_defaults(func=cmd_battery)

    set_parser = subparsers.add_parser(
        "set",
        help="apply a polling rate (125, 500 or 1000 Hz)",
        description="Apply a polling rate to the CompX mouse and print the result "
                    "(rate, PID, distinct packets delivered, EEPROM write status).",
    )
    set_parser.add_argument(
        "rate",
        type=int,
        choices=sorted(POLLING_VARIANTS),
        help="target polling rate in Hz",
    )
    set_parser.set_defaults(func=cmd_set)

    dpi_parser = subparsers.add_parser(
        "dpi",
        help="list DPI slots or set the active/selected level's DPI",
        description="List the eight DPI slot rows and the active level, or "
                    "write a DPI value into the active slot (default) or a "
                    "chosen slot (--slot). Bare `dpi` equals `dpi list`. "
                    "Requires pyusb.",
    )
    dpi_parser.add_argument(
        "value",
        metavar="VALUE",
        type=_parse_dpi_value,
        nargs="?",
        default=None,
        help="'list' or a target DPI multiple of 50 in 50..6400 "
             "(bare `dpi` lists)",
    )
    dpi_parser.add_argument(
        "--slot",
        metavar="S",
        type=int,
        default=None,
        help="write slot S (0..5) instead of the active slot",
    )
    dpi_parser.set_defaults(func=cmd_dpi)

    check_parser = subparsers.add_parser(
        "check",
        help="measure the actual polling rate of the mouse",
        description="Measure the real polling rate by counting input events "
                    "(requires the evdev package).",
    )
    check_parser.add_argument(
        "--device",
        metavar="PATH",
        default=None,
        help="input event node to measure (default: auto-detect)",
    )
    check_parser.set_defaults(func=cmd_check)

    probe_parser = subparsers.add_parser(
        "probe",
        help="read the mouse config memory (raw USB, read-only)",
        description="Read and interpret a window of the CompX config memory "
                    "over raw USB (requires pyusb). Read-only: the mouse keeps "
                    "its current settings.",
    )
    probe_parser.add_argument(
        "--start",
        metavar="ADDR",
        type=_parse_hex_arg,
        default=0x0000,
        help="first config-memory address (hex, default: 0x0000)",
    )
    probe_parser.add_argument(
        "--length",
        metavar="LEN",
        type=_parse_hex_arg,
        default=0x40,
        help="number of bytes to read (hex, max 0x100, default: 0x40)",
    )
    probe_parser.set_defaults(func=cmd_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("COMPX_SELFCHECK") == "1":
        _verify_packet_generation()
        _verify_battery_request()
        _verify_dpi_codec()
    parser = build_parser()
    args = parser.parse_args(argv)
    # A bare `compxctl` (no subcommand) means `status`.
    func = args.func if getattr(args, "func", None) else cmd_status
    try:
        return func(args)
    except (RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
