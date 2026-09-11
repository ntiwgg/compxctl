#!/usr/bin/env python3
"""compxctl — polling rate and DPI for CompX / Ardor Gaming mice on Linux.

Two mechanisms, deliberately kept apart because they are not equivalent:

* **Host polling interval** — `/sys/module/usbhid/parameters/mousepoll`, read
  back from the endpoint descriptor. One sysfs write, reversible, no device
  traffic, no EEPROM. Reading it needs nothing but the standard library, and
  it answers "what rate am I actually polling at" exactly — no event counting,
  no mouse movement.
* **Device settings** — DPI, and (only on explicit request) the interval the
  device advertises after a re-plug. These live inside the chip, so they still
  need the reverse-engineered vendor protocol over raw USB (pyusb).

That split is the whole point of this rewrite. The previous version changed the
polling rate by writing the vendor EEPROM by default, which is the most
dangerous and least portable way to do it: irreversible, wear-limited, and
reverse engineered from a single unit.

The vendor protocol tables below are kept from the previous version because
they were verified on hardware; the framing is unchanged.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

__version__ = "2.0.0"

VENDOR_ID = 0x25A7
PRODUCT_IDS = (0xFA7B, 0xFA7C, 0xFA03, 0xFA93)

SYSFS_USB_ROOT = Path("/sys/bus/usb/devices")
MOUSEPOLL_PATH = Path("/sys/module/usbhid/parameters/mousepoll")

# HID boot-mouse interface: bInterfaceClass 0x03, SubClass 0x01, Protocol 0x02.
# That is the interface the host polls for motion, so its interrupt-IN interval
# is the polling interval a user means. The config interface is 0x03/0x01/0x01.
MOUSE_INTERFACE_CLASS = (0x03, 0x01, 0x02)

# One host interval per supported rate, in milliseconds. mousepoll takes ms.
HOST_INTERVAL_MS_BY_RATE: dict[int, int] = {1000: 1, 500: 2, 125: 8}


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
    endpoint: MouseEndpoint | None

    @property
    def rate_hz(self) -> int | None:
        return self.endpoint.rate_hz if self.endpoint else None

    @property
    def display_name(self) -> str:
        return self.name or f"CompX mouse {self.vendor:04x}:{self.product:04x}"


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


def find_mouses(root: Path = SYSFS_USB_ROOT) -> list[Mouse]:
    """Every supported mouse currently on the USB bus, from sysfs alone."""
    found: list[Mouse] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found

    for device_dir in entries:
        vendor = _read_hex(device_dir / "idVendor")
        product = _read_hex(device_dir / "idProduct")
        if vendor != VENDOR_ID or product not in PRODUCT_IDS:
            continue

        speed_raw = _read_text(device_dir / "speed")
        try:
            speed_mbps = float(speed_raw) if speed_raw else None
        except ValueError:
            speed_mbps = None

        interface_dir = None
        fallback_dir = None
        for candidate in sorted(root.glob(f"{device_dir.name}:*")):
            triple = (
                _read_hex(candidate / "bInterfaceClass"),
                _read_hex(candidate / "bInterfaceSubClass"),
                _read_hex(candidate / "bInterfaceProtocol"),
            )
            if triple == MOUSE_INTERFACE_CLASS:
                interface_dir = candidate
                break
            if fallback_dir is None and triple[0] == 0x03:
                fallback_dir = candidate
        if interface_dir is None:
            interface_dir = fallback_dir

        found.append(
            Mouse(
                sysfs_dir=device_dir,
                vendor=vendor,
                product=product,
                name=_read_text(device_dir / "product") or "",
                speed_mbps=speed_mbps,
                interface_dir=interface_dir,
                endpoint=_mouse_endpoint(interface_dir, speed_mbps)
                if interface_dir is not None
                else None,
            )
        )
    return found


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
PROBE_MAX_DUMP_BYTES = 0x100
CONFIG_MEMORY_BYTES = 0x10000
READ_ATTEMPTS = 4
READ_TIMEOUT_MS = 500

# Device-side interval codes (the value stored at 0x0000 and sent as the live
# interval byte): 1 ms -> 0x01, 2 ms -> 0x02, 8 ms -> 0x08.
DEVICE_INTERVAL_CODE_BY_RATE: dict[int, int] = {125: 0x08, 500: 0x02, 1000: 0x01}
RATE_BY_DEVICE_INTERVAL_CODE: dict[int, int] = {0x08: 125, 0x04: 250, 0x02: 500, 0x01: 1000}

RATE_BY_CODE: dict[int, int] = {0x00: 125, 0x01: 500, 0x02: 1000}

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
    if not 0 <= addr < CONFIG_MEMORY_BYTES:
        raise ValueError(f"address 0x{addr:X} is outside the config memory window")
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
        raise AssertionError(
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
        raise AssertionError(
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


def slot_checksum_ok(x: int, y: int, mul: int, crc: int) -> bool:
    return (x + y + mul + crc) & 0xFF == 0x55


def active_slot_for_index(index: int) -> int | None:
    """Register 0x0004 counts levels 1..8; slot rows are 0-based."""
    return index - 1 if 1 <= index <= 8 else None


def dpi_slot_addr(slot: int) -> int:
    return DPI_TABLE_START + DPI_SLOT_BYTES * slot


# ==========================================================================
# Raw-USB transport (pyusb) — one claimed session per command, so a command
# pays the kernel-driver detach/re-attach once instead of once per transfer.
# ==========================================================================


def _pyusb():
    try:
        import usb.core
        import usb.util
    except ImportError as exc:
        raise RuntimeError(
            "The `usb` package (pyusb) is missing — it is required for the "
            "device commands (dpi, status, battery, probe, rate device). "
            "Install it with: pip install pyusb"
        ) from exc
    return usb.core, usb.util


class UsbSession:
    """The CompX config interface (interface 1), claimed for one command."""

    def __init__(self, timeout_ms: int = READ_TIMEOUT_MS) -> None:
        self.timeout_ms = timeout_ms
        self._device = None
        self._usb_core = None
        self._usb_util = None
        self._detached = False

    def __enter__(self) -> "UsbSession":
        usb_core, usb_util = _pyusb()
        self._usb_core, self._usb_util = usb_core, usb_util

        device = None
        for product_id in PRODUCT_IDS:
            device = usb_core.find(idVendor=VENDOR_ID, idProduct=product_id)
            if device is not None:
                break
        if device is None:
            raise RuntimeError(
                "No CompX mouse found (VID 0x25A7). Check the USB connection: "
                "lsusb -d 25a7:"
            )
        try:
            if device.is_kernel_driver_active(1):
                device.detach_kernel_driver(1)
                self._detached = True
            usb_util.claim_interface(device, 1)
        except usb_core.USBError as exc:
            raise RuntimeError(
                f"Cannot claim the CompX config interface (interface 1): {exc}. "
                "Install the udev rules or run once with sudo."
            ) from exc
        self._device = device
        return self

    def __exit__(self, *exc_info: object) -> None:
        device = self._device
        if device is None:
            return
        try:
            self._usb_util.release_interface(device, 1)
        except self._usb_core.USBError:
            pass
        if self._detached:
            with contextlib.suppress(self._usb_core.USBError):
                device.attach_kernel_driver(1)
        self._usb_util.dispose_resources(device)
        self._device = None

    # -- transfers ---------------------------------------------------------

    def send(self, packet: bytes, report_type: int = 0x02) -> None:
        """SET_REPORT to interface 1. 0x02 = output report, 0x03 = feature."""
        assert self._device is not None
        try:
            sent = self._device.ctrl_transfer(
                bmRequestType=0x21,
                bRequest=9,  # SET_REPORT
                wValue=(report_type << 8) | packet[0],
                wIndex=1,
                data_or_wLength=packet,
                timeout=1000,
            )
        except self._usb_core.USBError as exc:
            raise OSError(str(exc)) from exc
        if sent != len(packet):
            raise OSError("USB SET_REPORT rejected by the device")

    def _interrupt_in_endpoint(self):
        assert self._device is not None
        configuration = self._device.get_active_configuration()
        try:
            interface = configuration[(1, 0)]
        except KeyError as exc:
            raise RuntimeError(
                "The CompX config interface (interface 1) is not in the active "
                "USB configuration"
            ) from exc
        endpoint = self._usb_util.find_descriptor(
            interface,
            custom_match=lambda ep: (
                self._usb_util.endpoint_direction(ep.bEndpointAddress)
                == self._usb_util.ENDPOINT_IN
                and self._usb_util.endpoint_type(ep.bmAttributes)
                == self._usb_util.ENDPOINT_TYPE_INTR
            ),
        )
        if endpoint is None:
            raise RuntimeError(
                "No interrupt-IN endpoint on the CompX config interface"
            )
        return endpoint

    def read_reply(self) -> bytes | None:
        """One frame from interrupt-IN, or None on timeout (never retried)."""
        assert self._device is not None
        endpoint = self._interrupt_in_endpoint()
        try:
            return bytes(
                self._device.read(
                    endpoint.bEndpointAddress, COMPX_FRAME_BYTES, timeout=self.timeout_ms
                )
            )
        except self._usb_core.USBError as exc:
            if exc.errno == errno.ETIMEDOUT:
                return None
            raise OSError(str(exc)) from exc

    # -- request/response --------------------------------------------------

    def request(
        self,
        packet: bytes,
        *,
        report_type: int = 0x02,
        reject: Callable[[bytes], str | None],
        parse: Callable[[bytes], object],
        timeout_message: str,
    ) -> object:
        """Send `packet`, then take the matching reply off interrupt-IN.

        A frame that fails `reject` is a stale queued frame (the firmware acks
        writes on the same endpoint) and is drained by this read, so the
        command is retried. A timeout is not retried: a late reply must not be
        doubled.
        """
        detail = timeout_message
        for _ in range(READ_ATTEMPTS):
            self.send(packet, report_type=report_type)
            reply = self.read_reply()
            if reply is None:
                raise RuntimeError(timeout_message)
            reason = reject(reply)
            if reason is None:
                return parse(reply)
            detail = reason
        raise RuntimeError(
            f"{detail} (retried {READ_ATTEMPTS} times after stale frames; "
            "is another program talking to the mouse?)"
        )

    # -- config memory -----------------------------------------------------

    def read(self, addr: int, length: int) -> bytes:
        command = build_read_frame(addr, length)
        register = f"0x{addr:04X}"

        def reject(reply: bytes) -> str | None:
            if len(reply) != COMPX_FRAME_BYTES:
                return f"short reply for {register}: {len(reply)} bytes"
            if reply[3:6] != command[3:6]:
                return (
                    f"reply header mismatch for {register}: got "
                    f"{reply[3:6].hex(' ')}, expected {command[3:6].hex(' ')}"
                )
            if not verify_frame(reply):
                return f"reply checksum mismatch for {register}"
            return None

        return self.request(
            command,
            report_type=0x03,
            reject=reject,
            parse=parse_read_reply,
            timeout_message=(
                f"No config-memory reply for {register} (interrupt-IN timed out)"
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
                x, y, mul, crc = block[local : local + DPI_SLOT_BYTES]
                addr = offset + local
                slots.append(
                    {
                        "index": (addr - DPI_TABLE_START) // DPI_SLOT_BYTES,
                        "addr": addr,
                        "x": x,
                        "y": y,
                        "mul": mul,
                        "crc": crc,
                        "crc_ok": slot_checksum_ok(x, y, mul, crc),
                        "dpi": None
                        if bytes((x, y, mul, crc)) == EMPTY_SLOT
                        else dpi_decode(x, mul),
                    }
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

    def write_dpi(self, slot: int, code: int) -> None:
        """Write one DPI slot, then verify every byte reads back unchanged."""
        addr = dpi_slot_addr(slot)
        payload = bytes((code, code, 0x00, (0x55 - 2 * code) & 0xFF))
        self.write(addr, payload)
        back = self.read(addr, DPI_SLOT_BYTES)
        if back != payload:
            raise RuntimeError(
                f"Readback mismatch for slot {slot} at 0x{addr:04X}: wrote "
                f"{payload.hex(' ')}, read {back.hex(' ')}"
            )

    def persist_rate(self, rate: int) -> bool:
        """Write the device's stored interval to EEPROM. False if already set."""
        code = DEVICE_INTERVAL_CODE_BY_RATE[rate]
        current = self.read(CONFIG_ADDR_POLLING, 2)
        if current[0] == code:
            return False
        payload = bytes((code, 0x55 - code))
        # The only call in the program that writes to the mouse's EEPROM, over
        # a reverse-engineered frame. Refuse to send anything that is not
        # byte-identical to the capture verified on hardware: a wrong frame
        # here is exactly what once clobbered the DPI-level fields at
        # 0x0002..0x0005 and broke the mouse's DPI button.
        if build_write_frame(CONFIG_ADDR_POLLING, payload) != eeprom_packet(rate):
            raise AssertionError(
                f"refusing to write an EEPROM frame for {rate} Hz that does not "
                "match the verified capture"
            )
        self.write(CONFIG_ADDR_POLLING, payload)
        back = self.read(CONFIG_ADDR_POLLING, 2)
        if back[0] != code:
            raise RuntimeError(
                f"EEPROM readback mismatch at 0x0000: wrote 0x{code:02X}, "
                f"read 0x{back[0]:02X}"
            )
        return True


def apply_live_rate(session: UsbSession, rate: int) -> None:
    """Send the two live interval packets (no EEPROM write)."""
    for packet in LIVE_PACKETS[rate]:
        session.send(packet)


# ==========================================================================
# Commands
# ==========================================================================

BATTERY_STATE_LABELS: dict[int, str] = {0x02: "2.4G mode"}


def _slot_text(slot: dict) -> str:
    if bytes((slot["x"], slot["y"], slot["mul"], slot["crc"])) == EMPTY_SLOT:
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


def _require_mouse() -> Mouse:
    mouses = find_mouses()
    if not mouses:
        raise RuntimeError(
            "No CompX mouse found on the USB bus (VID 0x25A7, PIDs "
            f"{', '.join(f'0x{p:04X}' for p in PRODUCT_IDS)}). "
            "Check the connection: lsusb -d 25a7:"
        )
    return mouses[0]


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


def cmd_rate(args: argparse.Namespace) -> int:
    """Show the polling rate, or change it on the host and/or the device."""
    mouse = _require_mouse()
    target = getattr(args, "target", None)

    if target is None:
        for line in _describe_rate(mouse, read_mousepoll()):
            print(line)
        return 0

    rate = args.hz
    ms = HOST_INTERVAL_MS_BY_RATE[rate]
    did_something = False

    if target in ("host", "both"):
        write_mousepoll(ms)
        print(f"Host polling interval set to {ms} ms ({rate} Hz) for all USB mice.")
        print("  Reversible: echo 0 | sudo tee /sys/module/usbhid/parameters/mousepoll")
        for line in _persistence_advice(ms):
            print(line)
        did_something = True

    if target in ("device", "both"):
        if not args.persist:
            raise RuntimeError(
                "Refusing to write the device's EEPROM without confirmation. "
                f"Re-run with: compxctl rate device {rate} --persist"
            )
        with UsbSession() as session:
            apply_live_rate(session, rate)
            changed = session.persist_rate(rate)
        if changed:
            print(f"Device EEPROM updated to {rate} Hz ({DEVICE_INTERVAL_CODE_BY_RATE[rate]:#04x}).")
        else:
            print(f"Device EEPROM already stored {rate} Hz — nothing written.")
        print("  Re-plug the mouse (or replug the receiver) for the host to re-read it.")
        did_something = True

    if not did_something:  # pragma: no cover - argparse blocks this
        raise RuntimeError("nothing to do")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Read-only snapshot: sysfs rate plus the device's own readouts."""
    mouse = _require_mouse()
    mousepoll_ms = read_mousepoll()
    for line in _describe_rate(mouse, mousepoll_ms):
        print(line)

    warnings: list[str] = []
    with UsbSession() as session:
        stored = _attempt(warnings, "stored rate", session.stored_rate_hz)
        active = _attempt(warnings, "active DPI level", session.active_dpi_level)
        slots = _attempt(warnings, "DPI slots", session.dpi_slots)
        battery = _attempt(warnings, "battery", session.battery)

    if stored is None:
        print("Stored rate: n/a")
    else:
        print(f"Stored rate: {stored} Hz (config memory 0x0000)")

    active_slot = active_slot_for_index(active[0]) if active else None
    if active is None:
        print("Active DPI level: n/a")
    else:
        text = f"Active DPI level: index 0x{active[0]:02X} (0x0004)"
        if active_slot is not None and slots and active_slot < len(slots):
            text += f" -> slot [{active_slot}]: {_slot_text(slots[active_slot])}"
        print(text)

    if slots:
        entries = [f"[{s['index']}] {_slot_text(s)}" for s in slots]
        if active_slot is not None and active_slot < len(entries):
            entries[active_slot] += " <- active"
        print("DPI slots: " + " ".join(entries))
    else:
        print("DPI slots: n/a")

    print(_battery_text(*battery) if battery else "Battery: n/a")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _attempt(warnings: list[str], label: str, reader: Callable[[], object]) -> object:
    try:
        return reader()
    except (RuntimeError, OSError) as exc:
        warnings.append(f"{label}: {exc}")
        return None


def cmd_dpi(args: argparse.Namespace) -> int:
    value, slot_arg = args.value, args.slot
    with UsbSession() as session:
        if value is None or value == "list":
            return _print_dpi_table(session)
        code = dpi_code(value)
        if code is None:
            raise RuntimeError("DPI must be a multiple of 50 between 50 and 6400")
        if slot_arg is not None:
            if not 0 <= slot_arg <= DPI_WRITE_SLOT_MAX:
                raise RuntimeError(f"slot must be 0..{DPI_WRITE_SLOT_MAX}")
            slot = slot_arg
        else:
            active = session.active_dpi_level()
            slot = active_slot_for_index(active[0]) if active else None
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
        before = _slot_text(session.dpi_slots()[slot])
        session.write_dpi(slot, code)
    print(f"DPI set: slot {slot} -> {value} (was {before})")
    return 0


def _print_dpi_table(session: UsbSession) -> int:
    slots = session.dpi_slots()
    active = session.active_dpi_level()
    active_slot = active_slot_for_index(active[0]) if active else None
    print("  slot  addr    x    y    mul  crc   value")
    for slot in slots:
        star = "*" if slot["index"] == active_slot else " "
        print(
            f"  {star}{slot['index']}   0x{slot['addr']:04X}  "
            f"{slot['x']:02X}   {slot['y']:02X}   {slot['mul']:02X}   "
            f"{slot['crc']:02X}    {_slot_text(slot)}"
        )
    if active is None:
        print("Active DPI level: unknown (0x0004 unset)")
    elif active_slot is None:
        print(f"Active DPI level: unknown (0x0004 holds 0x{active[0]:02X})")
    else:
        print(f"Active DPI level: slot {active_slot} (index 0x{active[0]:02X})")
    return 0


def cmd_battery(args: argparse.Namespace) -> int:
    with UsbSession() as session:
        print(_battery_text(*session.battery()))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    start, length = args.start, args.length
    if start < 0 or start + length > CONFIG_MEMORY_BYTES:
        raise RuntimeError(
            f"window 0x{start:04X}+{length} leaves the config memory "
            f"(0x0000..0x{CONFIG_MEMORY_BYTES - 1:04X})"
        )
    data = bytearray()
    with UsbSession() as session:
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
# CLI
# ==========================================================================


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
            "The polling rate comes from sysfs and needs no device write; DPI "
            "uses the vendor protocol and needs pyusb."
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
            "vendor protocol (per-device, persistent, EEPROM wear)."
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
    rate.set_defaults(func=cmd_rate)

    status = subparsers.add_parser("status", help="read-only device snapshot")
    status.set_defaults(func=cmd_status)

    dpi = subparsers.add_parser("dpi", help="list DPI slots or write one")
    dpi.add_argument("value", nargs="?", type=_parse_dpi_value, default=None,
                     help="'list' or a DPI multiple of 50 in 50..6400")
    dpi.add_argument("--slot", type=int, default=None, help="write slot S (0..5)")
    dpi.set_defaults(func=cmd_dpi)

    battery = subparsers.add_parser("battery", help="read battery level and state")
    battery.set_defaults(func=cmd_battery)

    probe = subparsers.add_parser("probe", help="read config memory (read-only)")
    probe.add_argument("--start", type=_parse_int, default=0x0000)
    probe.add_argument("--length", type=_parse_int, default=0x40)
    probe.set_defaults(func=cmd_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "target", None) is not None and getattr(args, "hz", None) is None:
        parser.error("a rate target needs a rate: rate host|device|both 125|500|1000")
    func = getattr(args, "func", None) or cmd_rate
    try:
        return func(args)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
