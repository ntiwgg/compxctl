#!/usr/bin/env python3
"""compxctl — polling rate and DPI for CompX / Ardor Gaming mice on Linux.

Two mechanisms, deliberately kept apart because they are not equivalent:

* **Host polling interval** — `/sys/module/usbhid/parameters/mousepoll`, read
  back from the endpoint descriptor. One sysfs write, reversible, no device
  traffic, no EEPROM. Reading it needs nothing but the standard library, and
  it answers "what rate am I actually polling at" exactly — no event counting,
  no mouse movement.
* **Device settings** — DPI, and (only on explicit request) the interval the
  device keeps for itself. These live inside the chip, so they still need the
  reverse-engineered vendor protocol, but no longer a third-party library to
  carry it.

The whole tool is standard library only. The device commands reach the config
interface through `/dev/hidraw*` rather than through libusb: that interface is
a HID interface, `usbhid` already owns it, and the kernel offers it to user
space as a hidraw node. Feature reports go out with one `fcntl.ioctl`, replies
come back as input reports, and no kernel driver is ever detached — which is
why `status` no longer makes the pointer stutter the way the libusb version
did.

That works because of what the device itself declares. Its report descriptor
for the config interface (189 bytes, read from the verified unit) contains:

* report id 6, FEATURE, 7 bytes — the first live interval packet;
* report id 8, FEATURE, 16 bytes — config-memory read/write, battery request;
* report id 9, INPUT, 16 bytes — every reply the firmware sends.

So SET_REPORT(FEATURE) is the framing the descriptor asks for, and the answer
has to be taken from the input queue: GET_REPORT (`HIDIOCGFEATURE`) on this
firmware returns 17 zero bytes, so a read is a feature-report write followed by
an input-report read, never a plain feature read.

The vendor protocol tables below are kept from the previous version because
they were verified on hardware; the framing is unchanged.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
import select
import signal
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, NamedTuple, Sequence, TypeVar

__version__ = "4.2.0"

# Version of the `--json` documents. Bump it when a key changes meaning or
# disappears, and when the set of values a key can take changes — 2 dropped the
# `reset: "manual"` of 3.x in favour of `"not-requested"`; adding a new key does
# not bump it.
JSON_SCHEMA = 2

# What a device readout returns; `_attempt` keeps that type when it swallows a
# failed read into a warning.
ResultT = TypeVar("ResultT")

VENDOR_ID = 0x25A7
PRODUCT_IDS = (0xFA7B, 0xFA7C, 0xFA03, 0xFA93)

SYSFS_USB_ROOT = Path("/sys/bus/usb/devices")
SYSFS_HIDRAW_ROOT = Path("/sys/class/hidraw")
DEV_ROOT = Path("/dev")
USBFS_ROOT = Path("/dev/bus/usb")
MOUSEPOLL_PATH = Path("/sys/module/usbhid/parameters/mousepoll")

# HID boot-mouse interface: bInterfaceClass 0x03, SubClass 0x01, Protocol 0x02.
# That is the interface the host polls for motion, so its interrupt-IN interval
# is the polling interval a user means.
MOUSE_INTERFACE_CLASS = (0x03, 0x01, 0x02)

# The config interface of the verified unit: same HID class, but boot-keyboard
# protocol 0x01. It carries the vendor report ids 6/8/9, so its hidraw node is
# the one the reverse-engineered protocol talks to.
CONFIG_INTERFACE_CLASS = (0x03, 0x01, 0x01)

# Opcodes the firmware uses to acknowledge rather than to answer: 0x07 echoes a
# config-memory write, 0x11 acknowledges a live interval packet.
DEVICE_ACK_OPCODES = (0x07, 0x11)

# Reply frames are input report 9 (see the module docstring), and the largest
# report on that interface is 17 bytes. Reading more than one report's worth is
# deliberate: a surprise longer report is then skipped instead of truncated
# into something that looks like a frame.
COMPX_REPLY_REPORT_ID = 0x09
HIDRAW_READ_BYTES = 64

# How long to wait for another compxctl to finish with the device. The lock is
# advisory and released by the kernel when a process exits, so this can be
# short: it only has to outlast a command, not a crash.
LOCK_WAIT_MS = 2000
LOCK_POLL_MS = 50

# The vendor report ids the reverse-engineered protocol needs, as declared by
# the config interface's own report descriptor: 0x08 is a FEATURE report (every
# read, write and the battery request) and 0x09 is the INPUT report the firmware
# answers with. 0x06 is the first live interval packet, also FEATURE.
COMPX_VENDOR_REPORT_ID = 0x08
COMPX_LIVE_REPORT_ID = 0x06

# Ceiling on the frames thrown away before a command goes out. The queue holds
# acknowledgements of earlier commands, not a stream, so this is a guard against
# a device that never stops talking rather than a working limit.
HIDRAW_DRAIN_MAX_FRAMES = 64

# One host interval per supported rate, in milliseconds. mousepoll takes ms.
HOST_INTERVAL_MS_BY_RATE: dict[int, int] = {1000: 1, 500: 2, 125: 8}

# How long to wait for the device to re-appear after a port reset, and how
# often to look. The kernel re-enumerates synchronously inside the reset ioctl,
# so this is slack for driver rebinding, not for the reset itself.
RESET_TIMEOUT_S = 5.0
RESET_POLL_S = 0.1


# --------------------------------------------------------------------------
# ioctl numbers, written the way <sys/ioctl.h> computes them. The test suite
# pins them against values compiled from the kernel headers, because a wrong
# number here fails silently (the ioctl simply does something else).
# --------------------------------------------------------------------------

_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, kind: str, number: int, size: int) -> int:
    """The `_IOC()` macro from `<sys/ioctl.h>`."""
    return (
        (direction << _IOC_DIRSHIFT)
        | (ord(kind) << _IOC_TYPESHIFT)
        | (number << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def hidraw_set_feature_ioctl(size: int) -> int:
    """`HIDIOCSFEATURE(size)`: SET_REPORT of a feature report, control transfer.

    The buffer carries the report number in its first byte, which is exactly how
    the vendor frames are laid out — their first byte is the report id.
    """
    return _ioc(_IOC_WRITE | _IOC_READ, "H", 0x06, size)


# `USBDEVFS_RESET`: re-enumerate one device, the programmatic re-plug.
usbdevfs_reset_ioctl = _ioc(0, "U", 20, 0)


# ==========================================================================
# Sysfs layer — host polling interval. Standard library only, no device access,
# no third-party packages. Every reader takes an injectable path so tests can
# drive a fake sysfs tree.
# ==========================================================================


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    """Read a plain decimal sysfs attribute (mousepoll, speed, ...)."""
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text, 10)
    except ValueError:
        return None


def _read_hex(path: Path) -> int | None:
    """Read a USB descriptor attribute from sysfs.

    The USB core prints these as bare hex without a 0x prefix — idVendor is
    "25a7", bInterfaceClass is "03", bEndpointAddress is "81". Parsing them as
    decimal silently turns 0x81 into 81, so every descriptor attribute read
    below has to go through this function.
    """
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def parse_interval_ms(text: str) -> float | None:
    """Parse a sysfs endpoint interval into milliseconds.

    The kernel prints this attribute in human units: "1ms" on a full-speed
    mouse, "125us" on a high-speed one. A bare number is read as milliseconds.
    """
    match = re.fullmatch(r"\s*(\d+)\s*(us|ms|s)?\s*", text or "")
    if match is None:
        return None
    value = int(match.group(1))
    unit = match.group(2) or "ms"
    if unit == "us":
        return value / 1000.0
    if unit == "s":
        return value * 1000.0
    return float(value)


def interval_ms_from_descriptor(b_interval: int, speed_mbps: float | None) -> float:
    """Fallback interval when sysfs carries no computed `interval` attribute.

    Full- and low-speed interrupt endpoints express bInterval in frames (1 ms),
    so bInterval *is* the interval. High-speed endpoints express it as
    2**(bInterval-1) microframes, i.e. 2**(bInterval-1)/8 ms. Prefer the
    kernel's own `interval` attribute whenever it exists — this is only a
    fallback, and the unit rule above is exactly the kind of detail that is
    easy to get wrong.
    """
    if speed_mbps is not None and speed_mbps >= 480:
        return (2 ** max(b_interval - 1, 0)) / 8.0
    return float(b_interval)


def rate_hz_from_interval_ms(interval_ms: float | None) -> int | None:
    """Convert an interval in milliseconds to a polling rate in Hz."""
    if not interval_ms or interval_ms <= 0:
        return None
    return round(1000.0 / interval_ms)


@dataclass(frozen=True)
class MouseEndpoint:
    """The interrupt-IN endpoint of the mouse interface."""

    name: str
    interval_ms: float | None
    interval_raw: str | None
    interval_source: str  # "interval" | "bInterval" | "unknown"

    @property
    def rate_hz(self) -> int | None:
        return rate_hz_from_interval_ms(self.interval_ms)


@dataclass(frozen=True)
class Mouse:
    """A supported mouse as it appears in sysfs."""

    sysfs_dir: Path
    vendor: int
    product: int
    name: str
    speed_mbps: float | None
    interface_dir: Path | None
    config_interface_dir: Path | None
    hidraw_path: Path | None
    hidraw_descriptor: Path | None
    endpoint: MouseEndpoint | None

    @property
    def rate_hz(self) -> int | None:
        return self.endpoint.rate_hz if self.endpoint else None

    @property
    def display_name(self) -> str:
        return self.name or f"CompX mouse {self.vendor:04x}:{self.product:04x}"


InterfaceClasses = tuple[int | None, int | None, int | None]


def pick_interfaces(
    candidates: Sequence[tuple[Path, InterfaceClasses]],
) -> tuple[Path | None, Path | None]:
    """Split a device's HID interfaces into (motion interface, config interface).

    Both are HID interfaces (class 0x03) and they are told apart by
    bInterfaceProtocol: 0x02 is the boot mouse, the interface whose interrupt-IN
    interval the host polls and the user means by "polling rate"; 0x01 is the
    config interface that carries the vendor report ids. A device that exposes
    only one HID interface gets that one for both roles.
    """
    first_hid = next((directory for directory, classes in candidates if classes[0] == 0x03), None)
    return (
        next((d for d, c in candidates if c == MOUSE_INTERFACE_CLASS), None) or first_hid,
        next((d for d, c in candidates if c == CONFIG_INTERFACE_CLASS), None) or first_hid,
    )


def hidraw_node_for(
    interface_dirs: Sequence[Path | None], root: Path = SYSFS_HIDRAW_ROOT
) -> Path | None:
    """The `/dev/hidrawN` node of the first interface that has one.

    `/sys/class/hidraw/hidrawN/device` points at the HID device whose parent is
    the USB interface directory, so the mapping is by ancestry and not by node
    number — numbers are reused and must not be cached across a re-enumeration.

    The order of `interface_dirs` is the preference order and it is checked
    outermost: a device has one hidraw node per HID interface, the motion
    interface is usually the lower-numbered node, and a feature report sent to
    it fails with EPIPE because the vendor report ids live on the config
    interface only.
    """
    nodes = [(entry.name, (entry / "device").resolve()) for entry in sorted(root.glob("hidraw*"))]
    candidates: list[Path] = []
    for directory in interface_dirs:
        if directory is None:
            continue
        resolved = directory.resolve()
        candidates.extend(
            DEV_ROOT / name for name, device in nodes if resolved in device.parents
        )

    for candidate in candidates:
        # The descriptor decides when it can: on an unverified sibling model the
        # interface numbering may differ, and the reports it declares cannot.
        descriptor = _read_bytes(root / candidate.name / "device" / "report_descriptor")
        if declares_vendor_reports(descriptor):
            return candidate
    return candidates[0] if candidates else None


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def report_ids(descriptor: bytes) -> dict[str, set[int]]:
    """Report ids declared by a raw HID report descriptor, per report type.

    A short walk of the item stream is enough for this question: only two things
    matter, the global Report ID item (0x85) and the main items that consume it
    (Input 0x80, Output 0x90, Feature 0xB0). Long items (0xFE) are skipped by
    their own length, so a descriptor that uses them cannot desynchronize the
    walk.
    """
    found: dict[str, set[int]] = {"input": set(), "output": set(), "feature": set()}
    main_items = {0x8: "input", 0x9: "output", 0xB: "feature"}
    current = 0
    index = 0
    while index < len(descriptor):
        prefix = descriptor[index]
        index += 1
        if prefix == 0xFE:  # long item: 0xFE, size, tag, data
            if index + 2 > len(descriptor):
                break
            index += 2 + descriptor[index]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        kind = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        data = descriptor[index : index + size]
        index += size
        if len(data) != size:
            break
        if kind == 0x01 and tag == 0x8 and size == 1:  # global: Report ID
            current = data[0]
        elif kind == 0x00 and tag in main_items:  # main: Input/Output/Feature
            found[main_items[tag]].add(current)
    return found


def declares_vendor_reports(descriptor: bytes) -> bool:
    """Whether an interface declares the reports this protocol talks in."""
    ids = report_ids(descriptor)
    return COMPX_VENDOR_REPORT_ID in ids["feature"] and COMPX_REPLY_REPORT_ID in ids["input"]


def interface_refusal(node: Path, descriptor: bytes) -> str | None:
    """Why this node cannot be the vendor config interface — when we can tell.

    The descriptor is the authority, and it is readable by anyone, so a node
    that declares something else is a mistake worth stopping on before a feature
    report goes to the wrong interface. An unreadable descriptor says nothing:
    the protocol then decides, and a wrong node fails loudly on the first reply
    check instead of being guessed at.
    """
    if not descriptor or declares_vendor_reports(descriptor):
        return None
    ids = report_ids(descriptor)
    return (
        f"{node} is not the vendor config interface: its report descriptor "
        f"declares feature ids {sorted(ids['feature']) or ['none']} and input ids "
        f"{sorted(ids['input']) or ['none']}, while the protocol needs "
        f"0x{COMPX_VENDOR_REPORT_ID:02X} as a feature report. Refusing to talk to it."
    )


def _interface_candidates(device_dir: Path, root: Path) -> list[tuple[Path, InterfaceClasses]]:
    """Every interface of one device with its class triple, in sysfs order."""
    return [
        (
            candidate,
            (
                _read_hex(candidate / "bInterfaceClass"),
                _read_hex(candidate / "bInterfaceSubClass"),
                _read_hex(candidate / "bInterfaceProtocol"),
            ),
        )
        for candidate in sorted(root.glob(f"{device_dir.name}:*"))
    ]


def _speed_mbps(device_dir: Path) -> float | None:
    raw = _read_text(device_dir / "speed")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _mouse_endpoint(interface_dir: Path, speed_mbps: float | None) -> MouseEndpoint | None:
    """Pick the interrupt-IN endpoint of a HID interface, with its interval."""
    candidates = []
    for entry in sorted(interface_dir.glob("ep_*")):
        attributes = _read_hex(entry / "bmAttributes")
        address = _read_hex(entry / "bEndpointAddress")
        if attributes is None or address is None:
            continue
        # bmAttributes bits 0-1: 0b11 = interrupt; address bit 7 = IN.
        if attributes & 0x03 != 0x03 or not address & 0x80:
            continue
        candidates.append(entry)
    if not candidates:
        return None

    endpoint_dir = candidates[0]
    raw = _read_text(endpoint_dir / "interval")
    interval_ms = parse_interval_ms(raw) if raw is not None else None
    source = "interval"
    if interval_ms is None:
        b_interval = _read_hex(endpoint_dir / "bInterval")
        if b_interval is not None:
            interval_ms = interval_ms_from_descriptor(b_interval, speed_mbps)
            source = "bInterval"
        else:
            source = "unknown"
    return MouseEndpoint(
        name=endpoint_dir.name,
        interval_ms=interval_ms,
        interval_raw=raw,
        interval_source=source,
    )


def find_mouses(
    root: Path = SYSFS_USB_ROOT, hidraw_root: Path = SYSFS_HIDRAW_ROOT
) -> list[Mouse]:
    """Every supported mouse currently on the USB bus, from sysfs alone."""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []

    return [
        _mouse_from_device(device_dir, root, hidraw_root)
        for device_dir in entries
        if _read_hex(device_dir / "idVendor") == VENDOR_ID
        and _read_hex(device_dir / "idProduct") in PRODUCT_IDS
    ]


def _mouse_from_device(device_dir: Path, root: Path, hidraw_root: Path) -> Mouse:
    speed_mbps = _speed_mbps(device_dir)
    motion_dir, config_dir = pick_interfaces(_interface_candidates(device_dir, root))
    hidraw_path = hidraw_node_for((config_dir, motion_dir), hidraw_root)
    return Mouse(
        sysfs_dir=device_dir,
        vendor=VENDOR_ID,
        product=_read_hex(device_dir / "idProduct") or 0,
        name=_read_text(device_dir / "product") or "",
        speed_mbps=speed_mbps,
        interface_dir=motion_dir,
        config_interface_dir=config_dir,
        hidraw_path=hidraw_path,
        hidraw_descriptor=(
            hidraw_root / hidraw_path.name / "device" / "report_descriptor"
            if hidraw_path is not None
            else None
        ),
        endpoint=_mouse_endpoint(motion_dir, speed_mbps) if motion_dir is not None else None,
    )


def read_mousepoll(path: Path | None = None) -> int | None:
    """The usbhid host override in milliseconds; 0 means "use the descriptor"."""
    return _read_int(path or MOUSEPOLL_PATH)


def write_mousepoll(interval_ms: int, path: Path | None = None) -> None:
    """Set the host polling interval for every USB mouse on the system."""
    target = path or MOUSEPOLL_PATH
    try:
        target.write_text(f"{interval_ms}\n")
    except PermissionError as exc:
        raise RuntimeError(
            f"No permission to write {target}. It is a kernel module parameter, "
            f"so it needs root: sudo compxctl rate host "
            f"{rate_hz_from_interval_ms(interval_ms) or interval_ms}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot write {target}: {exc}") from exc


def usbhid_is_builtin(
    release: str | None = None, modules_root: Path = Path("/lib/modules")
) -> bool | None:
    """Whether usbhid is built into the kernel rather than a loadable module.

    It matters: `/etc/modprobe.d/` options are read when a module is loaded,
    so for a built-in usbhid the `options usbhid mousepoll=...` line does
    nothing at all and the setting must go on the kernel command line instead.
    None means "cannot tell" (no modules.builtin for this release).
    """
    text = _read_text(modules_root / (release or os.uname().release) / "modules.builtin")
    if text is None:
        return None
    for line in text.splitlines():
        name = line.strip().rsplit("/", 1)[-1]
        if name.split(".")[0] == "usbhid":
            return True
    return False


def _persistence_advice(interval_ms: int) -> list[str]:
    """How to make the host override survive a reboot, per kernel build."""
    builtin = usbhid_is_builtin()
    if builtin is True:
        return [
            "  usbhid is built into this kernel, so /etc/modprobe.d is ignored.",
            "  To keep it across reboots, add to the kernel command line:",
            f"    usbhid.mousepoll={interval_ms}",
        ]
    if builtin is False:
        return [
            "  Not persistent; to keep it across reboots add to /etc/modprobe.d/:",
            f"    options usbhid mousepoll={interval_ms}",
        ]
    return [
        "  Not persistent; how to keep it depends on how usbhid is built:",
        f"    options usbhid mousepoll={interval_ms}   # loadable module",
        f"    usbhid.mousepoll={interval_ms}           # built-in kernel",
    ]


def effective_rate_hz(mouse: Mouse | None, mousepoll_ms: int | None) -> tuple[int | None, str]:
    """Resolve the rate actually in force, and name where it came from.

    mousepoll != 0 is an explicit host override and wins over the descriptor;
    when it is 0 the device's advertised interval is what the host polls.
    """
    if mousepoll_ms:
        return rate_hz_from_interval_ms(float(mousepoll_ms)), "usbhid.mousepoll"
    if mouse is not None and mouse.rate_hz is not None:
        return mouse.rate_hz, "device descriptor"
    return None, "unknown"


# ==========================================================================
# Vendor protocol — pure byte-level framing, no device access. Verified on
# hardware in the previous version; the layout and the checksum identity are
# unchanged, and the known-good captures are kept as regression anchors.
# ==========================================================================

COMPX_FRAME_BYTES = 17
MAX_PAYLOAD_BYTES = 10  # firmware ceiling for one read/write payload
CONFIG_ADDR_POLLING = 0x0000
CONFIG_ADDR_ACTIVE_DPI = 0x0004
DPI_TABLE_START = 0x000C
DPI_TABLE_END = 0x002C
DPI_SLOT_BYTES = 4
DPI_WRITE_SLOT_MAX = 5  # slots 6-7 use an undecoded extended encoding
EMPTY_SLOT = b"\xff\xff\xff\xff"
PROBE_BLOCK_BYTES = 8
CONFIG_MEMORY_BYTES = 0x10000
READ_ATTEMPTS = 4
READ_TIMEOUT_MS = 500

# Device-side interval codes (the value stored at 0x0000 and sent as the live
# interval byte): 1 ms -> 0x01, 2 ms -> 0x02, 8 ms -> 0x08.
DEVICE_INTERVAL_CODE_BY_RATE: dict[int, int] = {125: 0x08, 500: 0x02, 1000: 0x01}
RATE_BY_DEVICE_INTERVAL_CODE: dict[int, int] = {0x08: 125, 0x04: 250, 0x02: 500, 0x01: 1000}

# The two live packets per rate, then the EEPROM persistence packet. Bytes
# verified against captures from a real mouse.
LIVE_PACKETS: dict[int, tuple[bytes, ...]] = {
    125: (
        b"\x06\x11\x00\x00\x00\x00\x00\x00",
        b"\x08\x11\x00\x00\x00\x06\x08" + b"\x00" * 10,
    ),
    500: (
        b"\x06\x11\x00\x01\x00\x00\x00\x00",
        b"\x08\x11\x00\x00\x00\x06\x02" + b"\x00" * 10,
    ),
    1000: (
        b"\x06\x11\x00\x02\x00\x00\x00\x00",
        b"\x08\x11\x00\x00\x00\x06\x01" + b"\x00" * 10,
    ),
}

_KNOWN_GOOD_EEPROM_PACKETS: dict[int, bytes] = {
    125: b"\x08\x07\x00\x00\x00\x02\x08\x4d\x00\x00\x00\x00\x00\x00\x00\x00\xef",
    500: b"\x08\x07\x00\x00\x00\x02\x02\x53\x00\x00\x00\x00\x00\x00\x00\x00\xef",
    1000: b"\x08\x07\x00\x00\x00\x02\x01\x54\x00\x00\x00\x00\x00\x00\x00\x00\xef",
}

_KNOWN_GOOD_BATTERY_REQUEST = b"\x08\x04" + b"\x00" * 14 + b"\x49"


def compx_checksum(body: bytes) -> int:
    """Tail byte making a whole config frame sum to 0x55 (mod 256)."""
    return (0x55 - sum(body)) & 0xFF


def verify_frame(frame: bytes) -> bool:
    return (sum(frame) & 0xFF) == 0x55


def build_write_frame(addr: int, data: bytes) -> bytes:
    """17-byte config-memory write: 08 07 00 AH AL LN + data + pad + checksum."""
    if not 1 <= len(data) <= MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload must be 1..{MAX_PAYLOAD_BYTES} bytes, got {len(data)}")
    if not 0 <= addr or addr + len(data) > CONFIG_MEMORY_BYTES:
        raise ValueError(
            f"write window 0x{addr:04X}+{len(data)} is outside the config memory "
            f"(0x0000..0x{CONFIG_MEMORY_BYTES - 1:04X})"
        )
    body = (
        b"\x08\x07\x00"
        + bytes(((addr >> 8) & 0xFF, addr & 0xFF, len(data)))
        + data
        + b"\x00" * (COMPX_FRAME_BYTES - 7 - len(data))
    )
    return body + bytes((compx_checksum(body),))


def build_read_frame(addr: int, length: int) -> bytes:
    """17-byte config-memory read command: 08 08 00 AH AL LN + pad + checksum."""
    if not 1 <= length <= MAX_PAYLOAD_BYTES:
        raise ValueError(f"read length must be 1..{MAX_PAYLOAD_BYTES}, got {length}")
    if not 0 <= addr < CONFIG_MEMORY_BYTES or addr + length > CONFIG_MEMORY_BYTES:
        raise ValueError(
            f"read window 0x{addr:04X}+{length} leaves the config memory "
            f"(0x0000..0x{CONFIG_MEMORY_BYTES - 1:04X})"
        )
    body = (
        b"\x08\x08\x00"
        + bytes(((addr >> 8) & 0xFF, addr & 0xFF, length))
        + b"\x00" * 10
    )
    return body + bytes((compx_checksum(body),))


def build_battery_request() -> bytes:
    body = b"\x08\x04" + b"\x00" * (COMPX_FRAME_BYTES - 3)
    request = body + bytes((compx_checksum(body),))
    if request != _KNOWN_GOOD_BATTERY_REQUEST:
        raise RuntimeError(
            f"battery request drift: built {request.hex(' ')} != known-good "
            f"{_KNOWN_GOOD_BATTERY_REQUEST.hex(' ')}"
        )
    return request


def parse_read_reply(reply: bytes) -> bytes:
    """Payload slice of a read reply: 09 08 00 AH AL LN + payload + pad + tail."""
    if len(reply) != COMPX_FRAME_BYTES:
        raise ValueError(f"reply must be {COMPX_FRAME_BYTES} bytes, got {len(reply)}")
    length = reply[5]
    if not 1 <= length <= MAX_PAYLOAD_BYTES:
        raise ValueError(f"reply length byte must be 1..{MAX_PAYLOAD_BYTES}, got {length}")
    return reply[6 : 6 + length]


def eeprom_packet(rate: int) -> bytes:
    """The persistence write for `rate`: exactly two bytes at 0x0000.

    A longer payload once overwrote 0x0002..0x0005 (DPI level count and active
    index) and broke the mouse's DPI button. The pair is (interval code,
    complement to 0x55) and nothing else.
    """
    code = DEVICE_INTERVAL_CODE_BY_RATE[rate]
    packet = build_write_frame(CONFIG_ADDR_POLLING, bytes((code, 0x55 - code)))
    known_good = _KNOWN_GOOD_EEPROM_PACKETS[rate]
    if packet != known_good:
        raise RuntimeError(
            f"EEPROM packet drift for {rate} Hz: built {packet.hex(' ')} "
            f"!= known-good {known_good.hex(' ')}"
        )
    return packet


def dpi_code(dpi: int) -> int | None:
    """Stored code is (dpi / 50) - 1, valid for 50..6400."""
    if dpi % 50 != 0:
        return None
    code = dpi // 50 - 1
    return code if 0 <= code <= 0x7F else None


def dpi_decode(code: int, mul: int = 0) -> int | None:
    """Inverse of dpi_code; None for the undecoded extended encoding."""
    if mul != 0 or not 0 <= code <= 0x7F:
        return None
    return (code + 1) * 50


def dpi_payload(code: int) -> bytes:
    """The four bytes a DPI slot holds for `code` (checksum included)."""
    return bytes((code, code, 0x00, (0x55 - 2 * code) & 0xFF))


def slot_dict(index: int, addr: int, x: int, y: int, mul: int, crc: int) -> dict:
    """One DPI slot as everything else reads it: table, single read, JSON."""
    raw = bytes((x, y, mul, crc))
    return {
        "index": index,
        "addr": addr,
        "x": x,
        "y": y,
        "mul": mul,
        "crc": crc,
        "crc_ok": slot_checksum_ok(x, y, mul, crc),
        "dpi": None if raw == EMPTY_SLOT else dpi_decode(x, mul),
    }


def slot_bytes(slot: dict) -> bytes:
    return bytes((slot["x"], slot["y"], slot["mul"], slot["crc"]))


def slot_checksum_ok(x: int, y: int, mul: int, crc: int) -> bool:
    return (x + y + mul + crc) & 0xFF == 0x55


def active_slot_for_index(index: int) -> int | None:
    """Register 0x0004 counts levels 1..8; slot rows are 0-based."""
    return index - 1 if 1 <= index <= 8 else None


def dpi_slot_addr(slot: int) -> int:
    return DPI_TABLE_START + DPI_SLOT_BYTES * slot


# ==========================================================================
# hidraw transport — the config interface through /dev/hidrawN. Standard
# library only, one ioctl per frame, and usbhid keeps the interface: nothing is
# detached, nothing is claimed, and no third-party package is involved.
# ==========================================================================


class AwaitResult(NamedTuple):
    """What waiting for a reply found.

    `foreign` separates a device that is not answering from a node somebody else
    is also talking on: the first is worth giving up on quickly, the second is
    worth naming in the error.
    """

    frame: bytes | None
    reason: str | None
    foreign: bool


class HidrawIO:
    """The syscalls the protocol needs, isolated so tests can replace them."""

    @staticmethod
    def open(path: Path) -> int:
        """Open the node non-blocking.

        A blocking hidraw read sleeps until a report arrives, so a report
        consumed between the readiness check and the read itself would park the
        call with no timeout of its own. Non-blocking, that read raises
        `BlockingIOError` instead, and the wait keeps its own deadline.
        """
        return os.open(path, os.O_RDWR | os.O_NONBLOCK)

    @staticmethod
    def set_feature(fd: int, frame: bytes) -> None:
        """SET_REPORT(feature) of `frame`; its first byte is the report number."""
        buffer = bytearray(frame)
        fcntl.ioctl(fd, hidraw_set_feature_ioctl(len(frame)), buffer, True)

    @staticmethod
    def read_report(fd: int, timeout_ms: int) -> bytes | None:
        """The next input report, or None when the timeout runs out.

        The descriptor is non-blocking, so the deadline here is the only thing
        that decides how long this waits: a read that finds nothing — because
        whoever made the descriptor readable got there first — goes back to
        waiting for whatever time is left instead of sleeping indefinitely.
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                return None
            try:
                return bytes(os.read(fd, HIDRAW_READ_BYTES))
            except BlockingIOError:
                continue

    @staticmethod
    def lock(fd: int, timeout_ms: int) -> bool:
        """Exclusive advisory lock on the node; False if it stays taken.

        hidraw gives every opener its own copy of every input report, so one
        process cannot consume another's answer — measured: a reader running
        beside the tool sees each reply too and the tool is unaffected. What the
        copies do allow is two compxctl runs reading each other's frames, which
        matters because the protocol carries no sequence number. The lock closes
        that window between our own processes; it cannot speak for other
        software, and it does not need to be cleaned up: the kernel drops it
        when the descriptor closes, whatever happens to the process.
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError as exc:
                # "Busy" is EAGAIN/EWOULDBLOCK (BlockingIOError) and, on some
                # systems, EACCES. Anything else — EBADF after the receiver was
                # pulled out, ENODEV — is not contention, and waiting two
                # seconds to call it "another compxctl is talking to the mouse"
                # would be a wrong answer to a real error.
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise RuntimeError(f"Cannot lock the node: {exc}") from exc
                if time.monotonic() >= deadline:
                    return False
                time.sleep(LOCK_POLL_MS / 1000.0)

    @staticmethod
    def close(fd: int) -> None:
        os.close(fd)


def is_device_ack(report: bytes) -> bool:
    """Whether a frame is the firmware acknowledging the write just sent.

    The interface answers a config write with an echo of it (`09 07 …`) and a
    live interval packet with a `09 11 …`. Both carry the same input-report id
    as an answer, so the only thing that tells them apart is the opcode they
    echo. They are the device talking about itself: not an answer to a question,
    and not evidence of another program on the node either.

    The report id and the checksum are checked too: a damaged frame that happens
    to carry 0x07 in its second byte is not this device's acknowledgement, and
    treating it as one would let the session call a live node asleep.
    """
    return (
        len(report) == COMPX_FRAME_BYTES
        and report[0] == COMPX_REPLY_REPORT_ID
        and report[1] in DEVICE_ACK_OPCODES
        and verify_frame(report)
    )


def is_config_reply(report: bytes) -> bool:
    """Whether an input report can be an answer to a config command.

    The config interface carries traffic of its own — report ids 1, 2, 3 and 5,
    plus the 0x11 acknowledge of a live interval packet — and none of it may be
    mistaken for a reply. A reply is input report 9 and 17 bytes long.
    """
    return len(report) == COMPX_FRAME_BYTES and report[0] == COMPX_REPLY_REPORT_ID


class HidrawSession:
    """The CompX config interface, open for the duration of one command."""

    def __init__(
        self,
        mouse: Mouse | None = None,
        *,
        timeout_ms: int = READ_TIMEOUT_MS,
        tries: int = READ_ATTEMPTS,
        io: type[HidrawIO] = HidrawIO,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mouse = mouse
        self.timeout_ms = timeout_ms
        # How many times one command may be sent. The default is tuned for a
        # write that must not be lost; a status bar that would rather have a fast
        # "the mouse is asleep" can lower it, and `--tries 1 --timeout-ms 150`
        # is the pair the README suggests for that.
        self.tries = max(1, tries)
        self._io = io
        self._clock = clock
        self._fd: int | None = None
        self._lock_fd: int | None = None
        self._silent_reason: str | None = None

    @property
    def mouse(self) -> Mouse:
        """The device this session talks to; found once, then remembered."""
        if self._mouse is None:
            self._mouse = _require_mouse()
        return self._mouse

    def __enter__(self) -> "HidrawSession":
        mouse = self.mouse
        path = require_hidraw_path(mouse)
        descriptor = (
            _read_bytes(mouse.hidraw_descriptor) if mouse.hidraw_descriptor is not None else b""
        )
        mistaken = interface_refusal(path, descriptor)
        if mistaken is not None:
            raise RuntimeError(mistaken)
        # Lock on a descriptor of its own, and open the one that will be read
        # only once the lock is held. An opener is subscribed to a copy of every
        # input report from the moment it opens, so a process that waits for the
        # lock with its reading descriptor already open finds replies to the
        # previous session at the head of its queue — and since the protocol has
        # no sequence number, a same-header frame from that session is
        # indistinguishable from its own answer.
        lock_fd = self._open(path, "lock")
        try:
            if not self._io.lock(lock_fd, LOCK_WAIT_MS):
                raise RuntimeError(
                    f"Another compxctl is talking to the mouse: the lock on {path} "
                    f"is held and did not come free within {LOCK_WAIT_MS} ms. Try "
                    "again in a moment."
                )
            self._fd = self._open(path, "read")
        except BaseException:
            # BaseException, not Exception: Ctrl+C or SIGTERM arriving during the
            # wait for the lock must not leave a descriptor behind. The CLI exits
            # and the kernel closes it anyway, but this class is usable from a
            # long-lived process too, where nothing else would.
            self._io.close(lock_fd)
            raise
        self._lock_fd = lock_fd
        return self

    def _open(self, path: Path, what: str) -> int:
        """Open the node, translating the two ways it usually fails."""
        try:
            return self._io.open(path)
        except PermissionError as exc:
            raise RuntimeError(
                f"No permission to open {path} ({what}). The device commands need "
                "access to the hidraw node: install 99-compx-mouse.rules "
                '(TAG+="uaccess") or run once with sudo.'
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Cannot open {path} ({what}): {exc}") from exc

    def __exit__(self, *exc_info: object) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            self._io.close(fd)
        lock_fd, self._lock_fd = self._lock_fd, None
        if lock_fd is not None:
            # Closing it releases the lock: the kernel does that on close or on
            # process death, so a killed compxctl cannot leave the device locked.
            self._io.close(lock_fd)

    # -- transfers ---------------------------------------------------------

    def send(self, packet: bytes) -> None:
        """One vendor frame out, as a feature report addressed by packet[0]."""
        fd = self._open_fd()
        try:
            self._io.set_feature(fd, packet)
        except OSError as exc:
            raise OSError(f"SET_REPORT of {packet[:3].hex(' ')} was rejected: {exc}") from exc

    def _drain_pending(self) -> int:
        """Throw away whatever is already queued, without waiting for more.

        Called before every command: a reply to an earlier read of the same
        register passes the same header and checksum checks as a fresh one, so
        nothing stale may be left in front of the answer being asked for.

        The loop ends on the first read that brings nothing back, which with a
        non-blocking descriptor is the ordinary "queue is empty", and in any
        case after HIDRAW_DRAIN_MAX_FRAMES, so a device that streams faster than
        this drains cannot hold a command here either. A zero-length read is not
        something the kernel returns for a connected hidraw node; the falsy
        check is there so that "no data" can never be mistaken for data.
        """
        fd = self._open_fd()
        drained = 0
        while drained < HIDRAW_DRAIN_MAX_FRAMES:
            report = self._io.read_report(fd, 0)
            if not report:
                break
            drained += 1
        return drained

    def _open_fd(self) -> int:
        """The descriptor, or a sentence instead of an AssertionError.

        Every transfer goes through here: using a session outside its `with`
        block is a programming error, and it should read like one — and stay an
        error under `python -O`, where `assert` disappears.
        """
        if self._fd is None:
            raise RuntimeError("The hidraw session is not open; use it as a context manager.")
        return self._fd

    def _await_reply(self, reject: Callable[[bytes], str | None]) -> AwaitResult:
        """Drain input reports until one passes `reject` or the deadline passes.

        Returns what it found: the frame, why the last frame was refused, and
        whether anything refused looked like somebody else's answer rather than
        the device's own acknowledgement. Draining instead of
        giving up on the first foreign frame is what keeps the queue clean: the
        firmware sends frames of its own — one acknowledge per live interval
        packet and per config write — and a command sent again while those are
        in flight leaves an extra reply behind, which the *next* command then
        reads as its answer. That is exactly how a readback after an EEPROM
        write ends up reporting the value from before the write.
        """
        fd = self._open_fd()
        deadline = self._clock() + self.timeout_ms / 1000.0
        detail: str | None = None
        foreign = False
        while True:
            remaining_ms = round((deadline - self._clock()) * 1000)
            if remaining_ms <= 0:
                return AwaitResult(None, detail, foreign)
            report = self._io.read_report(fd, remaining_ms)
            if report is None:
                return AwaitResult(None, detail, foreign)
            if not is_config_reply(report):
                continue
            reason = reject(report)
            if reason is None:
                return AwaitResult(report, None, foreign)
            detail = reason
            foreign = foreign or not is_device_ack(report)

    # -- request/response --------------------------------------------------

    def request(
        self,
        packet: bytes,
        *,
        reject: Callable[[bytes], str | None],
        parse: Callable[[bytes], object],
        timeout_message: str,
    ) -> object:
        """Send `packet`, then take the matching reply out of the input queue.

        Anything already queued is dropped first, frames that fail `reject` are
        drained rather than counted as a reply, and the command is sent again
        only when nothing matched at all — a lost answer over the radio link,
        not a foreign frame. A reply to an earlier command cannot be told from a
        fresh one by its header alone, so the fewer commands in flight, the
        fewer chances to read the wrong answer.
        """
        if self._silent_reason is not None:
            # Nothing has answered in this session, and the remaining readouts
            # of `status` would each pay the full retry budget to learn it again
            # — fourteen seconds of a status bar waiting on a sleeping mouse.
            raise RuntimeError(
                f"The device stopped answering ({self._silent_reason}); skipping "
                "the remaining questions. Is the mouse switched off or asleep?"
            )

        detail = timeout_message
        foreign = False
        for _ in range(self.tries):
            self._drain_pending()
            self.send(packet)
            result = self._await_reply(reject)
            if result.frame is not None:
                return parse(result.frame)
            detail = result.reason or timeout_message
            foreign = foreign or result.foreign
        if not foreign:
            # Nothing came back, or only the device's own acknowledgements did.
            # Either way this session will not get an answer, and the remaining
            # readouts of `status` should not each pay the retry budget to learn
            # that again: fourteen seconds of a status bar on a mouse that is
            # switched off. A frame that looks like an answer to a *different*
            # question is another matter — that is somebody else on the node.
            silence = (
                "nothing came back at all"
                if detail == timeout_message
                else "only the device's own acknowledgements came back, no answer"
            )
            self._silent_reason = (
                f"no answer within {self.timeout_ms} ms per attempt, "
                f"{self.tries} attempts in a row"
            )
            raise RuntimeError(
                f"{timeout_message} — {silence}. Is the mouse switched off or "
                "asleep? (The rest of this command's readouts are skipped.)"
            )
        raise RuntimeError(
            f"{detail} (tried {self.tries} times; is another program "
            "talking to the mouse?)"
        )

    # -- config memory -----------------------------------------------------

    def read(self, addr: int, length: int) -> bytes:
        command = build_read_frame(addr, length)
        register = f"0x{addr:04X}"

        def reject(reply: bytes) -> str | None:
            if len(reply) != COMPX_FRAME_BYTES:
                return f"short reply for {register}: {len(reply)} bytes"
            # The opcode is part of the match, not just the address. A config
            # write is acknowledged by the firmware with an input report of its
            # own (09 07 ...) that echoes the bytes just written — same address
            # and same length as the readback that follows, so matching on the
            # address alone would accept that echo as the readback and "verify" a
            # write the device had actually refused.
            if reply[1:6] != command[1:6]:
                return (
                    f"reply header mismatch for {register}: got "
                    f"{reply[1:6].hex(' ')}, expected {command[1:6].hex(' ')}"
                )
            if not verify_frame(reply):
                return f"reply checksum mismatch for {register}"
            return None

        return self.request(
            command,
            reject=reject,
            parse=parse_read_reply,
            timeout_message=(
                f"No config-memory reply for {register} (no input report within "
                f"{self.timeout_ms} ms per attempt)"
            ),
        )

    def write(self, addr: int, data: bytes) -> None:
        try:
            self.send(build_write_frame(addr, data))
        except OSError as exc:
            raise RuntimeError(
                f"USB SET_REPORT failed while writing config memory at "
                f"0x{addr:04X}: {exc}"
            ) from exc

    # -- named readouts ----------------------------------------------------

    def stored_rate_hz(self) -> int | None:
        return RATE_BY_DEVICE_INTERVAL_CODE.get(self.read(CONFIG_ADDR_POLLING, 1)[0])

    def active_dpi_level(self) -> tuple[int, int] | None:
        data = self.read(CONFIG_ADDR_ACTIVE_DPI, 2)
        return None if data == b"\xff\xff" else (data[0], data[1])

    def dpi_slots(self) -> list[dict]:
        slots: list[dict] = []
        for offset in range(DPI_TABLE_START, DPI_TABLE_END, PROBE_BLOCK_BYTES):
            block = self.read(offset, PROBE_BLOCK_BYTES)
            for local in range(0, PROBE_BLOCK_BYTES, DPI_SLOT_BYTES):
                addr = offset + local
                slots.append(
                    slot_dict(
                        (addr - DPI_TABLE_START) // DPI_SLOT_BYTES,
                        addr,
                        *block[local : local + DPI_SLOT_BYTES],
                    )
                )
        return slots

    def battery(self) -> tuple[int, bool, int]:
        def reject(reply: bytes) -> str | None:
            if len(reply) != COMPX_FRAME_BYTES or reply[:2] != b"\x09\x04":
                return "no battery reply"
            if not verify_frame(reply):
                return "battery reply checksum mismatch"
            return None

        percent, charging, state = self.request(
            build_battery_request(),
            reject=reject,
            parse=lambda reply: (reply[6], bool(reply[7]), reply[5]),
            timeout_message="no battery reply",
        )
        if not 0 <= percent <= 100:
            raise RuntimeError(
                f"Battery reply carries an impossible percentage ({percent}) — "
                "the device may be answering a different request"
            )
        return percent, charging, state

    def dpi_slot(self, slot: int) -> dict:
        """One DPI slot, read on its own: one transaction instead of the table."""
        addr = dpi_slot_addr(slot)
        block = self.read(addr, DPI_SLOT_BYTES)
        return slot_dict(slot, addr, *block)

    def write_dpi(self, slot: int, code: int) -> None:
        """Write one DPI slot, then verify every byte reads back unchanged."""
        addr = dpi_slot_addr(slot)
        payload = dpi_payload(code)
        self.write(addr, payload)
        back = self.read(addr, DPI_SLOT_BYTES)
        if back != payload:
            raise RuntimeError(
                f"Readback mismatch for slot {slot} at 0x{addr:04X}: wrote "
                f"{payload.hex(' ')}, read {back.hex(' ')}"
            )

    def persist_rate(self, rate: int) -> bool:
        """Write the device's stored interval to EEPROM. False if already set.

        The readback is a plain read of 0x0000 and the protocol has no sequence
        number, so a duplicate of the read taken *before* the write would be
        indistinguishable from the answer to the one after it. What that can
        cost is a loud readback-mismatch on a write that worked — never a silent
        wrong value — which is why the command verifies the frame before sending
        it and refuses to report success without this read.
        """
        code = DEVICE_INTERVAL_CODE_BY_RATE[rate]
        payload = bytes((code, 0x55 - code))
        if self.read(CONFIG_ADDR_POLLING, 2) == payload:
            # Both bytes, not just the code: a pair that no longer satisfies the
            # 0x55 checksum is worth rewriting, and matching on the code alone
            # would leave a corrupt complement in the EEPROM forever.
            return False
        # The only call in the program that writes to the mouse's EEPROM, over
        # a reverse-engineered frame. Refuse to send anything that is not
        # byte-identical to the capture verified on hardware: a wrong frame
        # here is exactly what once clobbered the DPI-level fields at
        # 0x0002..0x0005 and broke the mouse's DPI button.
        if build_write_frame(CONFIG_ADDR_POLLING, payload) != eeprom_packet(rate):
            raise RuntimeError(
                f"refusing to write an EEPROM frame for {rate} Hz that does not "
                "match the verified capture"
            )
        self.write(CONFIG_ADDR_POLLING, payload)
        back = self.read(CONFIG_ADDR_POLLING, 2)
        if back != payload:
            raise RuntimeError(
                f"EEPROM readback mismatch at 0x0000: wrote {payload.hex(' ')}, "
                f"read {back.hex(' ')}"
            )
        return True


def apply_live_rate(session: HidrawSession, rate: int) -> None:
    """Send the two live interval packets (no EEPROM write)."""
    for packet in LIVE_PACKETS[rate]:
        session.send(packet)


# ==========================================================================
# Re-enumeration — `USBDEVFS_RESET` on the usbfs node. The host re-reads the
# descriptors without anyone crawling under the desk to re-plug a receiver,
# which is the step `rate device --persist` used to ask for.
# ==========================================================================


class UsbfsIO:
    """The syscalls a port reset needs, isolated so tests can replace them."""

    @staticmethod
    def open(path: Path) -> int:
        return os.open(path, os.O_RDWR)

    @staticmethod
    def reset(fd: int) -> None:
        fcntl.ioctl(fd, usbdevfs_reset_ioctl, 0)

    @staticmethod
    def close(fd: int) -> None:
        os.close(fd)


def usbfs_node(mouse: Mouse, root: Path = USBFS_ROOT) -> Path | None:
    """`/dev/bus/usb/BBB/DDD` for a device, from the numbers sysfs reports."""
    bus = _read_int(mouse.sysfs_dir / "busnum")
    devnum = _read_int(mouse.sysfs_dir / "devnum")
    if bus is None or devnum is None:
        return None
    return root / f"{bus:03d}" / f"{devnum:03d}"


def reset_usb_device(mouse: Mouse, *, io: type[UsbfsIO] = UsbfsIO, root: Path = USBFS_ROOT) -> Path:
    """Re-enumerate the device and return the node that was reset.

    The reset itself needs no root on a desktop: the uaccess tag in the udev
    rule covers the usbfs node, which is why this stays a normal-user operation
    like the rest of the tool.
    """
    node = usbfs_node(mouse, root)
    if node is None:
        raise RuntimeError(
            f"{mouse.sysfs_dir} does not report busnum/devnum, so there is no "
            "usbfs node to reset"
        )
    try:
        fd = io.open(node)
    except PermissionError as exc:
        raise RuntimeError(
            f"No permission to reset {node}. Install 99-compx-mouse.rules, or "
            "re-plug the mouse by hand."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot open {node}: {exc}") from exc
    try:
        io.reset(fd)
    except OSError as exc:
        raise RuntimeError(f"Reset of {node} failed: {exc}") from exc
    finally:
        io.close(fd)
    return node


def wait_for_mouse(
    sysfs_name: str,
    *,
    timeout_s: float = RESET_TIMEOUT_S,
    poll_s: float = RESET_POLL_S,
    root: Path = SYSFS_USB_ROOT,
    hidraw_root: Path = SYSFS_HIDRAW_ROOT,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Mouse | None:
    """Wait until the device is back after a reset.

    Returns as soon as it is listed again with a known interval. None means it
    did not come back in time — the caller says so rather than pretending the
    reset worked.
    """
    deadline = clock() + timeout_s
    while True:
        mouse = next(
            (m for m in find_mouses(root, hidraw_root) if m.sysfs_dir.name == sysfs_name),
            None,
        )
        if mouse is not None and mouse.endpoint is not None:
            return mouse
        if clock() >= deadline:
            return mouse
        sleep(poll_s)


# ==========================================================================
# Commands
# ==========================================================================

BATTERY_STATE_LABELS: dict[int, str] = {0x02: "2.4G mode"}


def _slot_text(slot: dict) -> str:
    if slot_bytes(slot) == EMPTY_SLOT:
        return "empty"
    if slot["dpi"] is not None:
        return str(slot["dpi"])
    if slot["mul"]:
        return f"raw:0x{slot['x']:02X}(mul=0x{slot['mul']:02X})"
    return f"raw:0x{slot['x']:02X}"


def _battery_text(percent: int, charging: bool, state: int) -> str:
    text = f"Battery: {percent}% ({'charging' if charging else 'not charging'})"
    label = BATTERY_STATE_LABELS.get(state)
    return text + (f" [{label}]" if label else "")


# ==========================================================================
# Reports — pure translations of what was read, for a person or for a status
# bar. `--json` prints exactly one document on stdout, errors included, so a
# consumer can always parse what it gets; unknown values are null, never text.
# ==========================================================================


def document(command: str, **fields: object) -> dict:
    return {"schema": JSON_SCHEMA, "command": command, **fields}


def print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def device_json(mouse: Mouse) -> dict:
    return {
        "name": mouse.display_name,
        "vendor": f"{mouse.vendor:04x}",
        "product": f"{mouse.product:04x}",
        "sysfs": mouse.sysfs_dir.name,
        "hidraw": str(mouse.hidraw_path) if mouse.hidraw_path else None,
    }


def polling_json(mouse: Mouse | None, mousepoll_ms: int | None) -> dict:
    """The rate picture, with null for anything that is not known right now.

    `mouse` is None when the device is not on the bus (for instance straight
    after a reset that it has not returned from yet), and then every value that
    would have to be read from it is null rather than remembered.
    """
    endpoint = mouse.endpoint if mouse else None
    hz, source = effective_rate_hz(mouse, mousepoll_ms)
    return {
        "advertised_rate_hz": mouse.rate_hz if mouse else None,
        "advertised_interval_ms": endpoint.interval_ms if endpoint else None,
        "advertised_interval_raw": endpoint.interval_raw if endpoint else None,
        "advertised_interval_source": endpoint.interval_source if endpoint else None,
        "endpoint": endpoint.name if endpoint else None,
        "host_override_interval_ms": mousepoll_ms,
        "host_override_rate_hz": (
            rate_hz_from_interval_ms(float(mousepoll_ms)) if mousepoll_ms else None
        ),
        "effective_rate_hz": hz,
        "effective_source": source,
    }


def battery_json(percent: int, charging: bool, state: int) -> dict:
    return {
        "percent": percent,
        "charging": charging,
        "state": state,
        "state_label": BATTERY_STATE_LABELS.get(state),
    }


def slot_json(slot: dict, active_slot: int | None) -> dict:
    return {
        "index": slot["index"],
        "addr": f"0x{slot['addr']:04X}",
        "dpi": slot["dpi"],
        "text": _slot_text(slot),
        "empty": slot_bytes(slot) == EMPTY_SLOT,
        "checksum_ok": slot["crc_ok"],
        "active": slot["index"] == active_slot,
        "bytes": slot_bytes(slot).hex(" "),
        "raw": {"x": slot["x"], "y": slot["y"], "mul": slot["mul"], "crc": slot["crc"]},
    }


def active_dpi_json(active: tuple[int, int] | None, slots: list[dict]) -> dict:
    slot = active_slot_for_index(active[0]) if active else None
    known = slot is not None and slot < len(slots)
    return {
        "index": active[0] if active else None,
        "slot": slot,
        "dpi": slots[slot]["dpi"] if known else None,
    }


def no_mouse_message() -> str:
    return (
        "No CompX mouse found on the USB bus (VID 0x25A7, PIDs "
        f"{', '.join(f'0x{p:04X}' for p in PRODUCT_IDS)}). "
        "Check the connection: lsusb -d 25a7:"
    )


def _require_mouse() -> Mouse:
    mouses = find_mouses()
    if not mouses:
        raise RuntimeError(no_mouse_message())
    return mouses[0]


def require_hidraw_path(mouse: Mouse | None = None) -> Path:
    """The hidraw node the vendor protocol talks to, or a reason why not."""
    device = mouse or _require_mouse()
    if device.hidraw_path is None:
        interface = device.config_interface_dir
        raise RuntimeError(
            f"{device.display_name}: its config interface "
            f"({interface.name if interface else 'not found'}) has no /dev/hidraw "
            "node, so the vendor protocol cannot be reached. Check that hidraw is "
            "enabled in the kernel (CONFIG_HIDRAW) and bound by usbhid."
        )
    return device.hidraw_path


def _fresh_mouse(sysfs_name: str) -> Mouse | None:
    """The same device as sysfs sees it now — its node and interval may differ."""
    return next((m for m in find_mouses() if m.sysfs_dir.name == sysfs_name), None)


def _describe_rate(mouse: Mouse, mousepoll_ms: int | None) -> list[str]:
    lines = [f"Device: {mouse.display_name} ({mouse.vendor:04x}:{mouse.product:04x})"]
    if mouse.endpoint is None:
        lines.append("Polling rate: unknown (no interrupt-IN endpoint in sysfs)")
    else:
        device_hz = mouse.rate_hz
        detail = f"{mouse.endpoint.interval_raw or '?'} via {mouse.endpoint.interval_source}"
        lines.append(
            f"Device advertises: {device_hz} Hz ({detail}, "
            f"{mouse.endpoint.name})"
            if device_hz
            else f"Device advertises: unknown ({detail}, {mouse.endpoint.name})"
        )
    if mousepoll_ms:
        lines.append(
            f"Host override: {rate_hz_from_interval_ms(float(mousepoll_ms))} Hz "
            f"(usbhid.mousepoll = {mousepoll_ms} ms, all USB mice)"
        )
    else:
        lines.append("Host override: none (usbhid.mousepoll = 0)")
    hz, source = effective_rate_hz(mouse, mousepoll_ms)
    lines.append(f"Effective: {hz} Hz (from {source})" if hz else "Effective: unknown")
    return lines


def session_for(args: argparse.Namespace, mouse: Mouse | None = None) -> HidrawSession:
    """The session every device command uses, with the flags it was given.

    `--timeout-ms` and `--tries` exist for the caller that would rather hear
    "the mouse is asleep" quickly than wait out the write-grade default: a status
    bar polling every few seconds has no use for four attempts.
    """
    return HidrawSession(
        mouse,
        timeout_ms=getattr(args, "timeout_ms", READ_TIMEOUT_MS),
        tries=getattr(args, "tries", READ_ATTEMPTS),
    )


def cmd_rate(args: argparse.Namespace) -> int:
    """Show the polling rate, or change it on the host and/or the device."""
    mouse = _require_mouse()
    target = getattr(args, "target", None)
    as_json = getattr(args, "json", False)

    if target is None:
        mousepoll_ms = read_mousepoll()
        if as_json:
            print_json(
                document(
                    "rate",
                    device=device_json(mouse),
                    polling=polling_json(mouse, mousepoll_ms),
                )
            )
            return 0
        for line in _describe_rate(mouse, mousepoll_ms):
            print(line)
        return 0

    rate = args.hz
    applied = apply_rate(
        mouse,
        rate,
        target,
        persist=args.persist,
        reenumerate=getattr(args, "reset_port", False),
        timeout_ms=args.timeout_ms,
        tries=args.tries,
    )
    fresh = _fresh_mouse(mouse.sysfs_dir.name)
    mousepoll_ms = read_mousepoll()

    if as_json:
        print_json(
            document(
                "rate",
                device=device_json(fresh or mouse),
                applied=applied_json(applied),
                # null when the device did not come back: a status bar must not
                # be handed the numbers the device had before it disappeared.
                polling=polling_json(fresh, mousepoll_ms),
            )
        )
        return 0

    for line in applied_text(applied, fresh):
        print(line)
    return 0


@dataclass(frozen=True)
class AppliedRate:
    """What `rate host|device|both` did — one set of facts, two renderings."""

    target: str
    rate_hz: int
    host_interval_ms: int | None
    eeprom_changed: bool | None
    reset: str  # "ok" | "failed" | "skipped" | "not-requested"
    reset_error: str | None


def apply_rate(
    mouse: Mouse,
    rate: int,
    target: str,
    *,
    persist: bool,
    reenumerate: bool = False,
    timeout_ms: int = READ_TIMEOUT_MS,
    tries: int = READ_ATTEMPTS,
) -> AppliedRate:
    """Carry out `rate host|device|both`, reporting what actually happened."""
    if target in ("device", "both") and not persist:
        # Checked before the first side effect: refusing must not leave
        # `mousepoll` already written by a command that then failed.
        raise RuntimeError(
            "Refusing to write the device's EEPROM without confirmation. "
            f"Re-run with: compxctl rate device {rate} --persist"
        )

    host_interval_ms = HOST_INTERVAL_MS_BY_RATE[rate] if target in ("host", "both") else None
    if host_interval_ms is not None:
        write_mousepoll(host_interval_ms)

    if target not in ("device", "both"):
        return AppliedRate(target, rate, host_interval_ms, None, "skipped", None)
    with HidrawSession(mouse, timeout_ms=timeout_ms, tries=tries) as session:
        apply_live_rate(session, rate)
        changed = session.persist_rate(rate)

    reset, reset_error = _reenumerate(mouse, changed, enabled=reenumerate)
    return AppliedRate(target, rate, host_interval_ms, changed, reset, reset_error)


def _reenumerate(mouse: Mouse, changed: bool, *, enabled: bool) -> tuple[str, str | None]:
    """Reset the port so the host re-reads the descriptors. (status, error).

    Off by default, and the measurements are why: on the verified unit the
    endpoint interval does not follow the stored interval code, and neither the
    stored code nor the live interval packets change the stream of reports the
    host receives (0% repeated reports with the live code at 500 and at 1000 —
    see the README for the method). Re-reading the descriptors therefore shows the
    same `1ms` as before, while the whole receiver — keyboard included, if one
    shares it — leaves the bus for a second. `--reset-port` asks for it when
    someone actually wants the re-enumeration.

    "skipped" means there was nothing to write in the first place;
    "not-requested" means the write happened and the port was left alone.
    """
    if not changed:
        # Nothing was written: the stored interval and the live packets are
        # already where this command wanted them, and a device that is working
        # does not need to disappear off the bus for the sake of it.
        return "skipped", None
    if not enabled:
        return "not-requested", None
    try:
        reset_usb_device(mouse)
    except RuntimeError as exc:
        return "failed", str(exc)
    back = wait_for_mouse(mouse.sysfs_dir.name)
    if back is None or back.rate_hz is None:
        # The ioctl succeeded, but success is not the ioctl returning: until the
        # device is back with a readable endpoint, "ok" would be a claim the
        # tool cannot support — and a status bar would read it as fact.
        return "failed", "the device did not come back with a readable endpoint after the reset"
    return "ok", None


def applied_json(applied: AppliedRate) -> dict:
    return {
        "target": applied.target,
        "requested_rate_hz": applied.rate_hz,
        "host_interval_ms": applied.host_interval_ms,
        "eeprom_changed": applied.eeprom_changed,
        "eeprom_code": (
            f"0x{DEVICE_INTERVAL_CODE_BY_RATE[applied.rate_hz]:02x}"
            if applied.eeprom_changed is not None
            else None
        ),
        "reset": applied.reset,
        "reset_error": applied.reset_error,
    }


def applied_text(applied: AppliedRate, fresh: Mouse | None) -> list[str]:
    """Human rendering of an applied rate, including whether the host noticed."""
    lines: list[str] = []
    if applied.host_interval_ms is not None:
        hz = rate_hz_from_interval_ms(float(applied.host_interval_ms))
        lines.append(
            f"Host polling interval set to {applied.host_interval_ms} ms ({hz} Hz) "
            "for all USB mice."
        )
        lines.append("  Reversible: echo 0 | sudo tee /sys/module/usbhid/parameters/mousepoll")
        lines.extend(_persistence_advice(applied.host_interval_ms))
    if applied.eeprom_changed is not None:
        code = DEVICE_INTERVAL_CODE_BY_RATE[applied.rate_hz]
        lines.append(
            f"Device EEPROM updated to {applied.rate_hz} Hz ({code:#04x})."
            if applied.eeprom_changed
            else f"Device EEPROM already stored {applied.rate_hz} Hz — nothing written, "
            "no reset needed."
        )
    lines.extend(_reenumeration_text(applied, fresh))
    return lines


def _reenumeration_text(applied: AppliedRate, fresh: Mouse | None) -> list[str]:
    """What the host sees after the reset — or why it needs a hand instead.

    Measured on the verified unit: the endpoint interval does *not* follow the
    stored interval code. With 0x01 stored the interface advertises `1ms` and
    with 0x02 stored it still advertises `1ms` — checked on both sides of a port
    reset the kernel logged as real ("usb 3-2: reset full-speed USB device
    number 2"). So this reports what the descriptor says instead of asserting
    that the rate followed, and says which of the two happened.
    """
    endpoint = fresh.endpoint if fresh else None
    detail = f"{endpoint.interval_raw or '?'}, {endpoint.name}" if endpoint else "no endpoint"
    if applied.reset == "skipped":
        return []
    if applied.reset == "not-requested":
        # What can be said without overclaiming: the write happened and was read
        # back, the live packets went out, and the port was left alone. What the
        # mouse reports at afterwards is not something this tool measures, and it
        # used to say otherwise — see the README.
        return [
            "  EEPROM written and verified by readback; live interval packets "
            "sent. The port was not reset (--reset-port does that).",
            "  What the mouse reports at afterwards is not measured here.",
        ]
    if applied.reset == "failed":
        return [
            f"  Warning: {applied.reset_error}.",
            "  Re-plug the mouse (or the receiver) to make the host re-read it.",
        ]
    if fresh is None or fresh.rate_hz is None:
        return [
            "  Warning: the device has not come back with a readable endpoint "
            "since the reset; re-plug it.",
        ]
    reset_line = (
        f"  Port reset: the receiver re-enumerated, and the host re-read the "
        f"descriptors; the endpoint advertises {fresh.rate_hz} Hz ({detail})."
    )
    if fresh.rate_hz == applied.rate_hz:
        return [reset_line]
    return [
        reset_line,
        "  The endpoint does not follow the stored interval on this unit, and "
        "neither does the report stream — both are measured; the README says what "
        "that does and does not mean for `rate device`.",
    ]


@dataclass(frozen=True)
class DeviceReadouts:
    """What the config interface answered, and what it failed to answer."""

    stored_rate_hz: int | None
    active: tuple[int, int] | None
    slots: list[dict]
    battery: tuple[int, bool, int] | None
    warnings: list[str]


def collect_readouts(session: HidrawSession) -> DeviceReadouts:
    """Every device readout in one session; a failed one becomes a warning."""
    warnings: list[str] = []
    stored = _attempt(warnings, "stored rate", session.stored_rate_hz)
    active = _attempt(warnings, "active DPI level", session.active_dpi_level)
    slots = _attempt(warnings, "DPI slots", session.dpi_slots)
    battery = _attempt(warnings, "battery", session.battery)
    return DeviceReadouts(stored, active, slots or [], battery, warnings)


def status_text(mouse: Mouse, mousepoll_ms: int | None, readouts: DeviceReadouts) -> list[str]:
    lines = _describe_rate(mouse, mousepoll_ms)
    active_slot = active_slot_for_index(readouts.active[0]) if readouts.active else None

    lines.append(
        "Stored rate: n/a"
        if readouts.stored_rate_hz is None
        else f"Stored rate: {readouts.stored_rate_hz} Hz (config memory 0x0000)"
    )

    if readouts.active is None:
        lines.append("Active DPI level: n/a")
    else:
        text = f"Active DPI level: index 0x{readouts.active[0]:02X} (0x0004)"
        if active_slot is not None and active_slot < len(readouts.slots):
            text += f" -> slot [{active_slot}]: {_slot_text(readouts.slots[active_slot])}"
        lines.append(text)

    if readouts.slots:
        entries = [f"[{slot['index']}] {_slot_text(slot)}" for slot in readouts.slots]
        if active_slot is not None and active_slot < len(entries):
            entries[active_slot] += " <- active"
        lines.append("DPI slots: " + " ".join(entries))
    else:
        lines.append("DPI slots: n/a")

    lines.append(_battery_text(*readouts.battery) if readouts.battery else "Battery: n/a")
    return lines


def status_json(mouse: Mouse, mousepoll_ms: int | None, readouts: DeviceReadouts) -> dict:
    active_slot = active_slot_for_index(readouts.active[0]) if readouts.active else None
    return document(
        "status",
        device=device_json(mouse),
        polling=polling_json(mouse, mousepoll_ms),
        stored_rate_hz=readouts.stored_rate_hz,
        active_dpi=active_dpi_json(readouts.active, readouts.slots),
        dpi_slots=[slot_json(slot, active_slot) for slot in readouts.slots],
        battery=battery_json(*readouts.battery) if readouts.battery else None,
        warnings=readouts.warnings,
    )


def cmd_status(args: argparse.Namespace) -> int:
    """Read-only snapshot: sysfs rate plus the device's own readouts."""
    mouse = _require_mouse()
    mousepoll_ms = read_mousepoll()
    with session_for(args, mouse) as session:
        readouts = collect_readouts(session)

    if getattr(args, "json", False):
        print_json(status_json(mouse, mousepoll_ms, readouts))
        return 0
    for line in status_text(mouse, mousepoll_ms, readouts):
        print(line)
    for warning in readouts.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _attempt(warnings: list[str], label: str, reader: Callable[[], ResultT]) -> ResultT | None:
    try:
        return reader()
    except (RuntimeError, OSError) as exc:
        warnings.append(f"{label}: {exc}")
        return None


def active_level_slot(session: HidrawSession) -> int | None:
    """The slot whose value the mouse is currently using, or None if unclear.

    Named so that it cannot be confused with the local `active_slot` the DPI
    listing keeps for `slot_json`, which is the same number under a shorter name.
    """
    active = session.active_dpi_level()
    return active_slot_for_index(active[0]) if active else None


def apply_dpi_slot(session: HidrawSession, slot: int, dpi: int) -> tuple[bool, dict]:
    """Write one DPI slot the way `dpi --slot S N` does. (changed, before).

    The single place that decides this, for the command line and for the
    interactive screen alike: read the target slot, skip the write when those
    four bytes are already there, and let `write_dpi` verify the readback when
    they are not.

    The active level is refused, and that is a measurement rather than a policy:
    the mouse keeps the DPI it is using in RAM and writes that value back into
    the active level's slot. A write there was read back as correct and was gone
    five to ten seconds later, three times out of three — so reporting success
    would be reporting a value the device has already discarded. Levels the mouse
    is not on are not touched by it, and those writes hold.
    """
    code = dpi_code(dpi)
    if code is None:
        raise RuntimeError("DPI must be a multiple of 50 between 50 and 6400")
    if not 0 <= slot <= DPI_WRITE_SLOT_MAX:
        raise RuntimeError(f"slot must be 0..{DPI_WRITE_SLOT_MAX}")
    if active_level_slot(session) == slot:
        raise RuntimeError(
            f"slot {slot} is the level the mouse is using right now, and the mouse "
            "keeps that value in RAM and writes it back over the slot within seconds "
            "— a write here is undone by the device, not by this tool. Change the "
            "level with the DPI button on the mouse, or write a level you are not on."
        )
    before = session.dpi_slot(slot)
    changed = slot_bytes(before) != dpi_payload(code)
    if changed:
        session.write_dpi(slot, code)
    return changed, before


def cmd_dpi(args: argparse.Namespace) -> int:
    value, slot_arg = args.value, args.slot
    as_json = getattr(args, "json", False)
    mouse = _require_mouse()

    if value is None or value == "list":
        with session_for(args, mouse) as session:
            slots = session.dpi_slots()
            active = session.active_dpi_level()
        active_slot = active_slot_for_index(active[0]) if active else None
        if as_json:
            # Printed after the session is closed: if closing raised, this would
            # otherwise be a second document on stdout.
            print_json(
                document(
                    "dpi",
                    device=device_json(mouse),
                    active_dpi=active_dpi_json(active, slots),
                    dpi_slots=[slot_json(slot, active_slot) for slot in slots],
                )
            )
            return 0
        for line in _dpi_table_text(slots, active, active_slot):
            print(line)
        return 0

    with session_for(args, mouse) as session:
        if slot_arg is not None:
            slot = slot_arg
        else:
            slot = active_level_slot(session)
            if slot is None:
                raise RuntimeError(
                    "The active DPI level in register 0x0004 is not a valid "
                    "1..8 index, so there is no slot to write. Pass --slot S "
                    "to name the slot explicitly."
                )
            if slot > DPI_WRITE_SLOT_MAX:
                raise RuntimeError(
                    f"slot {slot} holds an undecoded extended encoding; "
                    "refusing to overwrite it"
                )
        changed, before = apply_dpi_slot(session, slot, value)

    if as_json:
        print_json(
            document(
                "dpi",
                device=device_json(mouse),
                write={
                    "slot": slot,
                    "dpi": value,
                    "previous": slot_json(before, None),
                    "changed": changed,
                    "verified": True,
                },
            )
        )
        return 0
    if changed:
        print(f"DPI set: slot {slot} -> {value} (was {_slot_text(before)})")
    else:
        # Same four bytes already there: a config-memory write nobody asked for
        # is wear on a chip whose budget is unknown, and a readback of a value
        # that was never written proves nothing.
        print(f"DPI already set: slot {slot} -> {value} (nothing written)")
    return 0


def _dpi_table_text(
    slots: list[dict], active: tuple[int, int] | None, active_slot: int | None
) -> list[str]:
    rows = [
        f"  {'*' if slot['index'] == active_slot else ' '}{slot['index']}   "
        f"0x{slot['addr']:04X}  {slot['x']:02X}   {slot['y']:02X}   "
        f"{slot['mul']:02X}   {slot['crc']:02X}    {_slot_text(slot)}"
        for slot in slots
    ]
    if active is None:
        trailer = "Active DPI level: unknown (0x0004 unset)"
    elif active_slot is None:
        trailer = f"Active DPI level: unknown (0x0004 holds 0x{active[0]:02X})"
    else:
        trailer = f"Active DPI level: slot {active_slot} (index 0x{active[0]:02X})"
    return ["  slot  addr    x    y    mul  crc   value", *rows, trailer]


def cmd_battery(args: argparse.Namespace) -> int:
    mouse = _require_mouse()
    with session_for(args, mouse) as session:
        percent, charging, state = session.battery()
    if getattr(args, "json", False):
        print_json(
            document(
                "battery",
                device=device_json(mouse),
                battery=battery_json(percent, charging, state),
            )
        )
        return 0
    print(_battery_text(percent, charging, state))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    start, length = args.start, args.length
    if length <= 0:
        # Zero used to be a silent success: the loop simply did not run and the
        # command printed nothing, which reads like "the memory is empty".
        raise RuntimeError(f"length must be at least 1 byte, got {length}")
    if start < 0 or start + length > CONFIG_MEMORY_BYTES:
        raise RuntimeError(
            f"window 0x{start:04X}+{length} leaves the config memory "
            f"(0x0000..0x{CONFIG_MEMORY_BYTES - 1:04X})"
        )
    data = bytearray()
    with session_for(args) as session:
        for offset in range(0, length, PROBE_BLOCK_BYTES):
            chunk_length = min(PROBE_BLOCK_BYTES, length - offset)
            chunk = session.read(start + offset, chunk_length)
            data.extend(chunk)
            print(f"0x{start + offset:04X}: " + chunk.hex(" "))

    notes: list[str] = []
    if start <= CONFIG_ADDR_POLLING < start + length:
        code = data[CONFIG_ADDR_POLLING - start]
        hz = RATE_BY_DEVICE_INTERVAL_CODE.get(code)
        notes.append(
            f"0x0000 stores the interval code 0x{code:02X}"
            + (f" = {hz} Hz" if hz else " (no known rate)")
        )
    for slot in range((DPI_TABLE_END - DPI_TABLE_START) // DPI_SLOT_BYTES):
        addr = dpi_slot_addr(slot)
        if not (start <= addr and addr + DPI_SLOT_BYTES <= start + length):
            continue
        x, y, mul, crc = data[addr - start : addr - start + DPI_SLOT_BYTES]
        state = (
            "empty"
            if bytes((x, y, mul, crc)) == EMPTY_SLOT
            else f"checksum {'OK' if slot_checksum_ok(x, y, mul, crc) else 'FAIL'}"
        )
        notes.append(f"slot {slot}: {x:02X} {y:02X} {mul:02X} {crc:02X} ({state})")
    if notes:
        print("Interpretation:")
        for note in notes:
            print(f"  {note}")
    return 0


# ==========================================================================
# Interactive screen — the same reads and writes as the commands, drawn instead
# of printed. Nothing here talks to the device on its own: it calls the
# functions above, so readback verification, the skip-when-unchanged economy and
# the refusal to touch the undecoded slots are exactly the ones the CLI applies.
# ==========================================================================

TUI_DPI_MIN = 50
TUI_DPI_MAX = 6400
TUI_DPI_FINE = 50
TUI_DPI_COARSE = 400
TUI_EDITABLE_SLOTS = DPI_WRITE_SLOT_MAX + 1  # slots 0..5; 6 and 7 stay read-only
TUI_HINT = (
    "q выход · ↑/↓ слот · ←/→ ±50 DPI · [ ] ±400 · Enter записать · r обновить"
)


@lru_cache(maxsize=1)
def _curses():
    """The curses module, imported when the screen is actually asked for.

    Lazily on purpose: a Python built without `_curses` (some minimal
    distributions ship one) can still run every other command, and only this one
    has to say why it cannot.
    """
    try:
        import curses
    except ImportError as exc:  # pragma: no cover - depends on the interpreter
        raise RuntimeError(
            "This Python has no `curses` module, so the interactive screen is "
            "unavailable. Every other command works without it."
        ) from exc
    return curses


def tui_state(args: argparse.Namespace) -> dict:
    """The document `status --json` prints, for the screen to draw.

    Same reader, same session, same timing flags: the screen is a view of the
    status command, not a second implementation of it.
    """
    mouse = _require_mouse()
    mousepoll_ms = read_mousepoll()
    with session_for(args, mouse) as session:
        readouts = collect_readouts(session)
    return status_json(mouse, mousepoll_ms, readouts)


def tui_editable(slot: dict) -> bool:
    """Whether a slot may be written at all.

    Slots 0..5 carry a decoded DPI; 6 and 7 carry an extended encoding nobody has
    worked out, and the tool has always refused to guess there. The active level
    is not writable either: the mouse mirrors its RAM value back into that slot
    within seconds, so the screen must not offer what the device undoes.
    """
    return (
        slot.get("dpi") is not None
        and slot["index"] < TUI_EDITABLE_SLOTS
        and not slot.get("active")
    )


def tui_step(dpi: int, delta: int) -> int:
    """One keypress worth of change, kept inside what the protocol can encode."""
    return min(TUI_DPI_MAX, max(TUI_DPI_MIN, dpi + delta))


def tui_pending(state: dict) -> dict[int, int]:
    """Slot index -> DPI as the screen currently proposes it (edits included)."""
    return {
        slot["index"]: slot["dpi"]
        for slot in state.get("dpi_slots", [])
        if tui_editable(slot)
    }


def tui_colours(curses) -> dict[str, int]:
    """Colour pairs when the terminal has colours, plain attributes otherwise."""
    plain = {"ok": curses.A_BOLD, "heading": curses.A_BOLD, "hint": 0}
    if not curses.has_colors():
        return plain
    with contextlib.suppress(curses.error):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
    if curses.COLOR_PAIRS <= 3:  # pragma: no cover - terminal without colour pairs
        return plain
    return {
        "ok": curses.color_pair(1),
        "heading": curses.color_pair(2) | curses.A_BOLD,
        "hint": curses.color_pair(3),
    }


def tui_put(screen, row: int, column: int, text: str, attr: int = 0) -> None:
    """`addstr` that cannot bring the screen down.

    Writing into the bottom-right cell raises, and so does anything past the
    edge: a screen that dies because a device name got long is worse than a
    truncated name.
    """
    with contextlib.suppress(_curses().error):
        height, width = screen.getmaxyx()
        if 0 <= row < height and 0 <= column < width:
            screen.addstr(row, column, text[: max(0, width - column - 1)], attr)


def tui_draw(
    screen, state: dict, selection: int, pending: dict[int, int], message: str, colours: dict
) -> None:
    """Draw the whole screen from the state — no device access, no decisions."""
    curses = _curses()
    screen.erase()
    height, width = screen.getmaxyx()

    title = " compxctl — CompX / Ardor Gaming "
    tui_put(screen, 0, 0, title.ljust(width - 1), curses.A_REVERSE | curses.A_BOLD)

    device = state.get("device") or {}
    polling = state.get("polling") or {}
    battery = state.get("battery") or {}
    percent = battery.get("percent")
    charge = "заряжается" if battery.get("charging") else "2.4G"

    tui_put(screen, 2, 2, f"Устройство : {device.get('name') or '?'}", curses.A_BOLD)
    tui_put(
        screen,
        3,
        2,
        f"Батарея    : {percent}% ({charge})" if percent is not None else "Батарея    : н/д",
        colours["ok"],
    )
    tui_put(
        screen,
        4,
        2,
        f"Частота    : {polling.get('effective_rate_hz') or '?'} Гц "
        f"({polling.get('effective_source') or '?'}), "
        f"в EEPROM {state.get('stored_rate_hz') or 'н/д'} Гц",
    )

    tui_put(
        screen,
        6,
        2,
        "Слоты DPI — 0..5 правятся, 6..7 расшифровке не поддаются и только читаются:",
        colours["heading"],
    )
    slots = state.get("dpi_slots", [])
    for slot in slots:
        row = 7 + slot["index"]
        if row >= height - 3:
            break
        selected = slot["index"] == selection
        proposed = pending.get(slot["index"])
        changed = proposed is not None and proposed != slot["dpi"]
        marks = "".join(
            (
                " [АКТИВЕН — мышь перезапишет]" if slot.get("active") else "",
                "" if tui_editable(slot) else " [только чтение]",
                " * не записано" if changed else "",
            )
        )
        value = str(proposed) if changed else slot.get("text", "?")
        attr = curses.A_REVERSE if selected else curses.A_NORMAL
        if slot.get("active"):
            attr |= colours["ok"]
        tui_put(
            screen,
            row,
            2,
            f"{'-> ' if selected else '   '}Слот {slot['index']}: {value:<8}{marks}",
            attr,
        )

    warnings = state.get("warnings") or []
    if warnings:
        tui_put(screen, height - 3, 2, f"! {warnings[0]}", colours["hint"])
    tui_put(screen, height - 2, 2, message, colours["hint"])
    screen.refresh()


def tui_unsupported(curses) -> str | None:
    """Why a full-screen view has nothing to draw on here, or None when it has.

    `TERM=dumb` (and anything else without cursor addressing) initialises fine and
    then ignores every write: the screen stays blank and the user is left staring
    at nothing with no idea why. Saying it out loud is the whole point.
    """
    if curses.tigetstr("cup") is None:
        return (
            f"this terminal ({os.environ.get('TERM', 'TERM is not set')}) cannot "
            "position the cursor, so a full-screen view has nothing to draw on. "
            "Run it in a normal terminal."
        )
    return None


def tui_handle(key: int, selection: int, pending: dict[int, int]) -> tuple[int, dict[int, int]]:
    """Apply one keypress to the proposal. (new selection, new proposal)."""
    curses = _curses()
    if key in (curses.KEY_UP, ord("k")):
        return max(min(pending, default=0), selection - 1), pending
    if key in (curses.KEY_DOWN, ord("j")):
        # Down stops at the last writable slot, which is not len(pending) - 1:
        # the active level and the undecoded slots are skipped over, not counted.
        return min(max(pending, default=0), selection + 1), pending
    step = {
        curses.KEY_LEFT: -TUI_DPI_FINE,
        curses.KEY_RIGHT: TUI_DPI_FINE,
        ord("["): -TUI_DPI_COARSE,
        ord("]"): TUI_DPI_COARSE,
        ord("h"): -TUI_DPI_FINE,
        ord("l"): TUI_DPI_FINE,
    }.get(key, 0)
    if step and selection in pending:
        return selection, {**pending, selection: tui_step(pending[selection], step)}
    return selection, pending


def run_tui(screen, args: argparse.Namespace) -> int:
    """The event loop. Returns 0 when the user leaves; exceptions pass through."""
    curses = _curses()
    unsupported = tui_unsupported(curses)
    if unsupported:
        raise RuntimeError(unsupported)
    with contextlib.suppress(curses.error):
        curses.curs_set(0)  # a blinking cursor on a status screen is noise
    colours = tui_colours(curses)
    state = tui_state(args)
    pending = tui_pending(state)
    selection = 0
    message = TUI_HINT

    while True:
        tui_draw(screen, state, selection, pending, message, colours)
        key = screen.getch()

        if key in (ord("q"), ord("Q"), 27):
            return 0
        if key in (ord("r"), ord("R")):
            message = "Читаю устройство..."
            tui_draw(screen, state, selection, pending, message, colours)
            state = tui_state(args)
            pending = tui_pending(state)
            message = TUI_HINT
            continue
        if key in (curses.KEY_ENTER, 10, 13):
            if selection not in pending:
                # No session here: the state on screen already says why the slot
                # cannot be written, and a message must not cost a radio round-trip.
                on_screen = next(
                    (slot for slot in state.get("dpi_slots", []) if slot["index"] == selection),
                    None,
                )
                message = (
                    f"Слот {selection} — активный уровень, мышь вернёт своё значение"
                    if on_screen and on_screen.get("active")
                    else f"Слот {selection} только для чтения: кодировка не расшифрована"
                )
                continue
            try:
                with session_for(args) as session:
                    changed, before = apply_dpi_slot(session, selection, pending[selection])
                message = (
                    f"Слот {selection}: записано {pending[selection]} DPI и проверено чтением"
                    if changed
                    else f"Слот {selection}: уже {pending[selection]} DPI, запись не нужна"
                )
                state = tui_state(args)
                pending = tui_pending(state)
            except (RuntimeError, OSError, ValueError) as exc:
                message = f"Не записано: {exc}"
            continue

        selection, pending = tui_handle(key, selection, pending)


def cmd_tui(args: argparse.Namespace) -> int:
    """Run the interactive screen, or explain why it cannot be run."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError(
            "`compxctl tui` needs a terminal, and stdin or stdout is not one. "
            "Run it from a shell (or from a terminal emulator, if a status bar "
            "is starting it)."
        )
    curses = _curses()
    try:
        return curses.wrapper(lambda screen: run_tui(screen, args))
    except curses.error as exc:
        # initscr failing (an unknown TERM, a terminal that went away) is a
        # RuntimeError here like everything else the CLI reports, not a traceback.
        raise RuntimeError(f"the terminal cannot run a full-screen view: {exc}") from exc


# ==========================================================================
# CLI
# ==========================================================================


def _parse_positive(text: str) -> int:
    value = _parse_int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _parse_int(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None


def _parse_dpi_value(text: str) -> int | str:
    if text == "list":
        return "list"
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid DPI value {text!r} (use 'list' or a multiple of 50 in 50..6400)"
        ) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compxctl",
        description=(
            "Polling rate and DPI for CompX / Ardor Gaming mice (VID 0x25A7). "
            "The polling rate comes from sysfs and needs no device write; DPI, "
            "battery and the stored interval use the vendor protocol over "
            "hidraw. Nothing here needs a third-party package or root, apart "
            "from `rate host`."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    rate = subparsers.add_parser(
        "rate",
        help="show the polling rate, or change it (host and/or device)",
        description=(
            "With no arguments, print the rate the device advertises, the host "
            "override and the effective rate. `rate host HZ` writes "
            "usbhid.mousepoll (global, reversible, root). `rate device HZ "
            "--persist` rewrites the device's stored interval through the "
            "vendor protocol (per-device, persistent, EEPROM wear). The write is "
            "verified by readback; what the mouse reports at afterwards is not "
            "measured. --reset-port additionally re-enumerates the receiver so the "
            "host re-reads the descriptors."
        ),
    )
    rate.add_argument(
        "target", nargs="?", choices=("host", "device", "both"), default=None,
        help="where to apply the rate; omit to just print the current state",
    )
    rate.add_argument("hz", nargs="?", type=int, choices=sorted(HOST_INTERVAL_MS_BY_RATE))
    rate.add_argument(
        "--persist", action="store_true",
        help="confirm the EEPROM write required by `rate device`",
    )
    rate.add_argument(
        "--reset-port", action="store_true",
        help=(
            "after --persist, re-enumerate the port so the host re-reads the "
            "descriptors (needs the usb udev rule; on the verified unit the "
            "descriptor does not change)"
        ),
    )
    _add_timing_flags(rate)
    _add_json_flag(rate)
    rate.set_defaults(func=cmd_rate)

    status = subparsers.add_parser("status", help="read-only device snapshot")
    _add_timing_flags(status)
    _add_json_flag(status)
    status.set_defaults(func=cmd_status)

    dpi = subparsers.add_parser("dpi", help="list DPI slots or write one")
    dpi.add_argument("value", nargs="?", type=_parse_dpi_value, default=None,
                     help="'list' or a DPI multiple of 50 in 50..6400")
    dpi.add_argument("--slot", type=int, default=None, help="write slot S (0..5)")
    _add_timing_flags(dpi)
    _add_json_flag(dpi)
    dpi.set_defaults(func=cmd_dpi)

    battery = subparsers.add_parser("battery", help="read battery level and state")
    _add_timing_flags(battery)
    _add_json_flag(battery)
    battery.set_defaults(func=cmd_battery)

    tui = subparsers.add_parser(
        "tui",
        help="interactive screen: look at the device and edit DPI slots",
        description=(
            "Opens a terminal screen with the same readings as `status` and "
            "lets slots 0..5 be changed with the arrow keys. Slots 6 and 7 hold "
            "an undecoded encoding and stay read-only, exactly as on the command "
            "line, and a write is skipped when the slot already holds the value."
        ),
    )
    _add_timing_flags(tui)
    tui.set_defaults(func=cmd_tui)

    probe = subparsers.add_parser("probe", help="read config memory (read-only)")
    probe.add_argument("--start", type=_parse_int, default=0x0000)
    probe.add_argument("--length", type=_parse_positive, default=0x40)
    _add_timing_flags(probe)
    probe.set_defaults(func=cmd_probe)

    return parser


def _add_timing_flags(parser: argparse.ArgumentParser) -> None:
    """How long one answer may take, and how often to ask.

    Defaults are tuned for a write that must not be lost (4 attempts, 500 ms).
    A status bar is better served by `--tries 1 --timeout-ms 150`: an awake mouse
    answers in single-digit milliseconds, so a longer wait only delays the answer
    "it is asleep".
    """
    parser.add_argument(
        "--timeout-ms", type=_parse_positive, default=READ_TIMEOUT_MS,
        help=f"wait this long for one answer (default {READ_TIMEOUT_MS})",
    )
    parser.add_argument(
        "--tries", type=_parse_positive, default=READ_ATTEMPTS,
        help=f"send a command at most this many times (default {READ_ATTEMPTS})",
    )


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true",
        help=(
            f"print one JSON document on stdout (schema {JSON_SCHEMA}), errors "
            "included; unknown values are null"
        ),
    )


class Interrupted(Exception):
    """A signal asked this run to stop; the caller turns it into an exit code."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"interrupted by signal {signum}")
        self.signum = signum

    @property
    def exit_code(self) -> int:
        """The shell convention: 128 + the signal number."""
        return 128 + self.signum


def _raise_interrupted(signum: int, _frame: object) -> None:
    raise Interrupted(signum)


def _install_signal_handlers() -> None:
    """Let SIGTERM end a run the way Ctrl+C does.

    Status bars do not interrupt a slow child with SIGINT: Waybar, Polybar and
    i3status send SIGTERM when their own timeout expires. Python's default for
    SIGTERM is to die inside the C handler, so the process would exit 143 with an
    empty stdout — the one thing the `--json` contract promises never to do.
    """
    with contextlib.suppress(ValueError):  # not the main thread: nothing to do
        signal.signal(signal.SIGTERM, _raise_interrupted)


def _command_name(args: argparse.Namespace) -> str:
    return getattr(args, "command", None) or "rate"


def _fail(args: argparse.Namespace, message: str, code: int) -> int:
    """One error, rendered the way the rest of this run is rendered."""
    if getattr(args, "json", False):
        print_json(document(_command_name(args), error={"message": message}))
    else:
        print(f"Error: {message}", file=sys.stderr)
    return code


def _interrupted(args: argparse.Namespace, code: int) -> int:
    """One document for `--json`, one line for everyone else."""
    if getattr(args, "json", False):
        return _fail(args, "interrupted", code)
    print("\nInterrupted.", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    _install_signal_handlers()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "target", None) is not None and getattr(args, "hz", None) is None:
        parser.error("a rate target needs a rate: rate host|device|both 125|500|1000")
    func = getattr(args, "func", None) or cmd_rate
    try:
        return func(args)
    except (RuntimeError, OSError, ValueError) as exc:
        return _fail(args, str(exc), 1)
    except Interrupted as exc:
        # SIGTERM: 143, the usual shell convention for 128 + 15.
        return _interrupted(args, exc.exit_code)
    except KeyboardInterrupt:
        return _interrupted(args, 130)


if __name__ == "__main__":
    sys.exit(main())
