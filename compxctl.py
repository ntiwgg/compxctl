#!/usr/bin/env python3
"""compxctl.py — set or measure the polling rate of a CompX / Ardor Gaming mouse.

The proprietary control protocol (packet table and report order) lives in
POLLING_VARIANTS below — this file is the single source of truth for it.
"""

from __future__ import annotations

import argparse
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

# CompX config-memory (EEPROM) write packet, 17 bytes:
#   [0:2]  0x08 0x07       report 0x08, config-memory write opcode 0x07
#   [2:4]  0x00 0x00       write address 0x0000
#   [4:6]  0x00 0x06       payload length 0x0006 (six bytes follow)
#   [6]    rate byte       interval code: 0x01 = 1000 Hz, 0x02 = 500 Hz,
#                          0x08 = 125 Hz (same convention as packet 2, byte 6)
#   [7]    complement      additive complement to 0x55 (0x55 - rate byte)
#   [8:16] observed fields, fixed across rates (01 54 00 55 00 00 00 00)
#   [16]   checksum        tail byte such that the sum of all 17 bytes is
#                          ≡ 0x55 (mod 256) — computed, not hardcoded.


def _compx_checksum(packet: bytes) -> int:
    """CompX config-memory write packets: sum of all 17 bytes ≡ 0x55 (mod 256)."""
    return (0x55 - sum(packet[:-1])) & 0xFF


def _eeprom_packet(rate_code: int) -> bytes:
    """Assemble the 17-byte EEPROM write packet for `rate_code`.

    `rate_code` is the report-interval code the write stores (see
    INTERVAL_CODE_BY_RATE); the trailing checksum is derived from the sixteen
    leading bytes so the tail can never drift out of sync with the payload.
    """
    prefix = (
        b"\x08\x07"  # report 0x08, write opcode 0x07
        b"\x00\x00"  # address 0x0000
        b"\x00\x06"  # length 0x0006
        + bytes((rate_code, 0x55 - rate_code))  # rate byte + complement
        + b"\x01\x54\x00\x55\x00\x00\x00\x00"  # observed fields (fixed)
    )
    return prefix + bytes((_compx_checksum(prefix + b"\x00"),))


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


def _usb_set_feature_report(packet: bytes) -> None:
    """Send `packet` as a USB feature SET_REPORT via pyusb (lazy import)."""
    try:
        import usb.core
        import usb.util
    except ImportError as exc:
        raise OSError("pyusb is not installed (pip install pyusb)") from exc

    usb_dev = None
    for product_id in PRODUCT_IDS:
        usb_dev = usb.core.find(idVendor=VENDOR_ID, idProduct=product_id)
        if usb_dev is not None:
            break
    if usb_dev is None:
        raise OSError("USB device not found (lsusb -d 25a7:)")

    report_id = packet[0]
    detached = False
    try:
        if usb_dev.is_kernel_driver_active(1):
            usb_dev.detach_kernel_driver(1)
            detached = True
        usb.util.claim_interface(usb_dev, 1)
        try:
            result = usb_dev.ctrl_transfer(
                bmRequestType=0x21,
                bRequest=9,  # SET_REPORT
                wValue=0x0200 | report_id,  # feature report type
                wIndex=1,
                data_or_wLength=packet,
                timeout=1000,
            )
            if result <= 0:
                raise OSError("USB SET_REPORT rejected by the device")
        finally:
            usb.util.release_interface(usb_dev, 1)
    finally:
        if detached:
            try:
                usb_dev.attach_kernel_driver(1)
            except usb.core.USBError:
                pass
        usb.util.dispose_resources(usb_dev)


def _apply_rate_packets(path: bytes, rate: int) -> tuple[set[int], bool, list[str]]:
    """Send every packet of `rate` to one path: USB first, then hidapi + ioctl.

    Returns (delivered, eeprom_packet_written, errors); `delivered` holds the
    indices of every distinct packet accepted by at least one channel, so a
    packet confirmed twice (USB and hidapi) still counts once.
    """
    delivered: set[int] = set()
    eeprom_written = False
    errors: list[str] = []
    packets = POLLING_VARIANTS[rate]

    for index, packet in enumerate(packets):
        try:
            _usb_set_feature_report(packet)
        except Exception as exc:
            errors.append(f"usb: {exc}")
        else:
            delivered.add(index)
            if index == EEPROM_PACKET_INDEX:
                eeprom_written = True

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


def get_active_product_id() -> int | None:
    """Return the first CompX product id visible on the bus, or None."""
    hid = _hidapi()
    for product_id in PRODUCT_IDS:
        if hid.enumerate(VENDOR_ID, product_id):
            return product_id
    return None


def _format_hid_error(exc: BaseException) -> str:
    if isinstance(exc, PermissionError):
        return HID_PERMISSION_TEXT
    return str(exc)


def send_polling_packet(rate: int) -> tuple[set[int], bool, list[str]]:
    """Apply `rate` on every control path found.

    Returns (delivered, eeprom_packet_written, errors); `delivered` is the
    union of distinct packet indices confirmed on any path. Success means at
    least MIN_SENT_FOR_SUCCESS distinct packets were delivered (see
    POLLING_VARIANTS).
    """
    paths = find_device_paths()
    if not paths:
        raise RuntimeError(DEVICE_NOT_FOUND_TEXT)

    delivered: set[int] = set()
    eeprom_written = False
    errors: list[str] = []
    for path in paths:
        path_delivered, written, path_errors = _apply_rate_packets(path, rate)
        delivered |= path_delivered
        eeprom_written = eeprom_written or written
        errors.extend(path_errors)
    return delivered, eeprom_written, errors


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


def cmd_set(args: argparse.Namespace) -> int:
    """`set` entry point: apply a polling rate and report the outcome."""
    rate = args.rate
    paths = find_device_paths()
    if not paths:
        print(f"Error: {DEVICE_NOT_FOUND_TEXT}", file=sys.stderr)
        return 1

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
