#!/usr/bin/env python3
"""compxctl.py — set or measure the polling rate of a CompX / Ardor Gaming
mouse and probe (read back) its config memory.

The proprietary control protocol lives below — the packet table and report
order for `set` in POLLING_VARIANTS, the config-memory read framing for
`probe` in the PROBE_* section — this file is the single source of truth.

Architecture — two transports talk to the same config interface
(interface 1), layered under one protocol and one set of commands:

* pyusb (raw USB) — always works, because it needs no kernel driver on the
  interface. `probe` reads through it alone (a SET_REPORT read command,
  the reply picked off the interrupt-IN endpoint); `set` delivers every
  rate packet through it.
* hidapi / raw hidraw ioctls — used by `set` as an extra delivery channel
  per packet, but only while the kernel exposes the config interface as a
  hidraw node (usage pages FF01-FF04). CompX mice may ship with interface 1
  unbound and no such node at all, so nothing here requires it; the pyusb
  path exists precisely for that case.
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

__version__ = "1.0.1"

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
#   3. report 0x08 / sub-report 0x07 (0x08 0x07 ...): EEPROM write of the same
#      interval so the rate survives unplug / re-plug. The write stores the
#      interval as the report-interval code (in ms) and ends in a checksum
#      byte, so it is assembled by _eeprom_packet(), never hardcoded.
# The EEPROM write stores the rate as the report-interval code of packet 2's
# byte 6 (value in ms): 1 ms = 1000 Hz, 2 ms = 500 Hz, 8 ms = 125 Hz.
INTERVAL_CODE_BY_RATE: dict[int, int] = {125: 0x08, 500: 0x02, 1000: 0x01}

# CompX config-memory (EEPROM) write frame, 17 bytes — see
# _build_write_frame() for the generic builder:
#   [0:3]  0x08 0x07 0x00   report 0x08, write opcode 0x07, reserved
#   [3:5]  AH AL            write address (big-endian; 0x0000 for the rate)
#   [5]    LN               payload length (six bytes follow)
#   [6:12] rate byte        interval code: 0x01 = 1000 Hz, 0x02 = 500 Hz,
#                           0x08 = 125 Hz (same convention as packet 2, byte 6)
#          complement       additive complement to 0x55 (0x55 - rate byte)
#          01 54 00 55      observed fields, fixed across rates
#   [12:16] 0x00 pad
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
# what is confirmed):
#   0x0000           polling-rate code + complement (01 54 = 1000 Hz)
#   0x000C..0x002B   DPI slots, 4 bytes each (x, y, mul, per-slot checksum);
#                    slot checksum = 0x55 − x − y − mul (observed 13 13 00 2f)
#   0x0060..0x009F   button matrix, ~0x00A0 LED; past 0x00A0 the memory is
#                    empty 0xFF.
PROBE_BLOCK_BYTES = 8  # bytes read per probe dump line
PROBE_MAX_READ_BYTES = 10  # firmware ceiling for one read command (LN)
PROBE_FRAME_BYTES = 17  # fixed size of the command and reply frames
PROBE_READ_TIMEOUT_MS = 500  # interrupt-IN reply deadline
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


def _parse_read_reply(reply: bytes) -> bytes:
    """Extract the payload of a config-memory read reply.

    A reply is 09 08 00 AH AL LN + LN payload bytes + 0x00 pad + checksum;
    LN is byte 5, so the frame itself says how long the payload is. Callers
    verify the frame first (_verify_frame + header echo) and only then
    trust the slice.
    """
    if len(reply) != PROBE_FRAME_BYTES:
        raise ValueError(
            f"config read reply must be {PROBE_FRAME_BYTES} bytes, "
            f"got {len(reply)}"
        )
    return reply[6 : 6 + reply[5]]


def _eeprom_packet(rate_code: int) -> bytes:
    """Assemble the 17-byte EEPROM persistence packet for `rate_code`.

    `rate_code` is the report-interval code the write stores (see
    INTERVAL_CODE_BY_RATE). The rate byte, its complement and the observed
    fixed fields form the payload of a plain config-memory write at
    0x0000, so the frame is delegated to _build_write_frame() and the tail
    can never drift out of sync with the payload.
    """
    data = bytes((rate_code, 0x55 - rate_code)) + b"\x01\x54\x00\x55"
    return _build_write_frame(0x0000, data)


# Byte-identity reference for _verify_packet_generation(): the packets below
# were captured from a real mouse while compxctl still carried them as
# literals (v1.0.1 and earlier). The builder above must reproduce them exactly.
_KNOWN_GOOD_EEPROM_PACKETS: dict[int, bytes] = {
    125: b"\x08\x07\x00\x00\x00\x06\x08\x4d\x01\x54\x00\x55\x00\x00\x00\x00\x41",
    500: b"\x08\x07\x00\x00\x00\x06\x02\x53\x01\x54\x00\x55\x00\x00\x00\x00\x41",
    1000: b"\x08\x07\x00\x00\x00\x06\x01\x54\x01\x54\x00\x55\x00\x00\x00\x00\x41",
}


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
    one transport path.
    """
    with _usb_config_device() as usb_dev:
        report_id = packet[0]
        result = usb_dev.ctrl_transfer(
            bmRequestType=0x21,
            bRequest=9,  # SET_REPORT
            wValue=(report_type << 8) | report_id,
            wIndex=1,
            data_or_wLength=packet,
            timeout=1000,
        )
        if result != len(packet):
            raise OSError("USB SET_REPORT rejected by the device")


def _usb_read_reply(timeout: int = PROBE_READ_TIMEOUT_MS) -> bytes | None:
    """Wait for one reply frame on the interrupt-IN endpoint of interface 1.

    Finds the endpoint exactly like `probe` did historically (first
    interrupt-IN descriptor of the interface) and reads one
    PROBE_FRAME_BYTES frame. Returns None when the device sends no frame
    within `timeout` ms; any other USB error propagates so the caller can
    attach context to it.
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
            raise


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
    except Exception:
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
    so both are verified before the payload is trusted. Errors surface as
    RuntimeError with the failing address attached.
    """
    command = _build_read_frame(addr, length)
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
        raise RuntimeError(
            f"Short config-memory reply for 0x{addr:04X}: got "
            f"{len(reply)} bytes, expected {PROBE_FRAME_BYTES}"
        )
    if reply[3:6] != command[3:6]:
        raise RuntimeError(
            f"Config-memory reply header mismatch for 0x{addr:04X}: "
            f"echo AH/AL/LN = {reply[3:6].hex(' ')} "
            f"(expected {command[3:6].hex(' ')}) — stale reply?"
        )
    if not _verify_frame(reply):
        raise RuntimeError(
            f"Config-memory reply checksum mismatch for 0x{addr:04X}: "
            "frame does not sum to 0x55"
        )
    return _parse_read_reply(reply)


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
        except Exception as exc:
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
        except Exception as exc:
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
        except Exception as exc:
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
# Commands — CLI entry points; behaviour and output are frozen by v1.0.1.
# ==========================================================================


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
    """`check` entry point: measure the actual polling rate via input events."""
    try:
        event_path = find_event_device(args.device)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Event device: {event_path}")
    print(f"Move the mouse… measuring for ~{MEASURE_SECONDS:g} s", flush=True)
    try:
        measured = measure_event_rate_hz(event_path)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

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
        data.extend(read_config_register(start + offset, chunk_length))

    end = start + length
    for offset in range(0, length, PROBE_BLOCK_BYTES):
        chunk = data[offset : offset + PROBE_BLOCK_BYTES]
        print(f"0x{start + offset:04X}: " + chunk.hex(" "))

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
        if (x, y, mul, crc) == (0xFF, 0xFF, 0xFF, 0xFF):
            dpi_notes.append(f"slot {slot_number}: (empty)")
            continue
        checksum_ok = (x + y + mul + crc) & 0xFF == 0x55
        dpi_notes.append(
            f"slot {slot_number}: x=0x{x:02X} y=0x{y:02X} mul=0x{mul:02X} "
            f"crc=0x{crc:02X} (checksum {'OK' if checksum_ok else 'FAIL'})"
        )
    if dpi_notes:
        notes.append(
            "DPI table 0x000C-0x002B: x/y/mul/crc per slot look like encoded "
            "DPI (value formula unknown)"
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
        description="Set or measure the polling rate of a CompX / Ardor Gaming "
                    "mouse (VID 0x25A7).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

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
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
