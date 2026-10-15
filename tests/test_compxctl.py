"""Tests for compxctl 3.0.

Three rules shape this suite:

* Anything that can be a pure function is tested as one.
* The device layer is driven through a fake hidraw IO object injected into
  `HidrawSession`, so the suite needs no hardware, no `/dev/hidraw` node and no
  root. The frames it replays are captures from the verified unit.
* The sysfs layer runs against a synthetic tree, hidraw lookup included, because
  the mapping from a USB interface to `/dev/hidrawN` is where a real bug lived:
  matching *any* interface of the device picked the motion node, and a feature
  report sent to that one comes back as `EPIPE`.

The zero-dependency promise is itself a test: `compxctl.py` may import nothing
outside the standard library, enforced by reading its own AST.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import compxctl


# ==========================================================================
# Fake sysfs tree
# ==========================================================================


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")


def _link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(target, link.parent))


def make_sysfs_tree(
    root: Path,
    *,
    vendor: str = "25a7",
    product: str = "fa7b",
    speed: str = "12",
    interval: str | None = "1ms",
    b_interval: str = "01",
    endpoint_address: str = "81",
    hidraw_nodes: bool = True,
    descriptors: bool = True,
    swapped_descriptors: bool = False,
) -> Path:
    """A minimal stand-in for /sys/bus/usb/devices with one CompX mouse.

    Returns the USB devices root; `hidraw_root(root)` gives the matching
    /sys/class/hidraw tree, whose `hidrawN/device` links point back into this
    one exactly the way the real tree does.
    """
    devices = root / "bus" / "usb" / "devices"
    device = devices / "3-2"
    _write(device / "idVendor", vendor)
    _write(device / "idProduct", product)
    _write(device / "product", "2.4G Dual Mode Mouse")
    _write(device / "speed", speed)
    _write(device / "busnum", "3")
    _write(device / "devnum", "2")

    mouse_interface = devices / "3-2:1.0"
    _write(mouse_interface / "bInterfaceClass", "03")
    _write(mouse_interface / "bInterfaceSubClass", "01")
    _write(mouse_interface / "bInterfaceProtocol", "02")
    endpoint = mouse_interface / "ep_81"
    _write(endpoint / "bmAttributes", "03")
    _write(endpoint / "bEndpointAddress", endpoint_address)
    _write(endpoint / "bInterval", b_interval)
    if interval is not None:
        _write(endpoint / "interval", interval)

    config_interface = devices / "3-2:1.1"
    _write(config_interface / "bInterfaceClass", "03")
    _write(config_interface / "bInterfaceSubClass", "01")
    _write(config_interface / "bInterfaceProtocol", "01")

    if descriptors:
        # As on the verified unit: the motion interface declares no vendor
        # reports, the config interface declares ids 6/8 as FEATURE and 9 as
        # INPUT. `swapped_descriptors` puts them the other way round, which is
        # how the check that the descriptor decides — and not the interface
        # numbering — is tested.
        motion_hid = mouse_interface / "0003:25A7:FA7B.0001"
        config_hid = config_interface / "0003:25A7:FA7B.0002"
        for directory, blob in (
            (motion_hid, MOUSE_DESCRIPTOR),
            (config_hid, CONFIG_DESCRIPTOR),
        ):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "report_descriptor").write_bytes(blob)
    else:
        motion_hid = mouse_interface / "0003:25A7:FA7B.0001"
        config_hid = config_interface / "0003:25A7:FA7B.0002"

    if hidraw_nodes:
        # The motion interface is the lower-numbered node, as it is on the
        # verified unit: the trap this tree exists to catch.
        first, second = (config_hid, motion_hid) if swapped_descriptors else (motion_hid, config_hid)
        _link(hidraw_root(root) / "hidraw0" / "device", first)
        _link(hidraw_root(root) / "hidraw1" / "device", second)

    other = devices / "1-1"
    _write(other / "idVendor", "1234")
    _write(other / "idProduct", "5678")
    if hidraw_nodes:
        _link(
            hidraw_root(root) / "hidraw2" / "device",
            other / "1-1:1.0" / "0003:1234:5678.0003",
        )
    return devices


def hidraw_root(root: Path) -> Path:
    return root / "class" / "hidraw"


def find_mouse(root: Path) -> compxctl.Mouse:
    return compxctl.find_mouses(make_sysfs_tree(root), hidraw_root(root))[0]


# ==========================================================================
# HID report descriptors captured from the verified unit
# ==========================================================================

# The config interface (3-2:1.1 -> /dev/hidraw1): declares report id 8 as a
# FEATURE report and id 9 as INPUT, which is the whole protocol.
CONFIG_DESCRIPTOR = bytes.fromhex(
    "05010906a1018501050719e029e715002501750195088102050719002aff0095"
    "067508150026ff008100c00603ff0900a10185020900150026ff007508950781"
    "02c0050c0901a10185051500263c0219002a3c02751095018100c005010980a1"
    "0185031981298315002501950375018102950175058101c00601ff0900a10185"
    "09090095107508150026ff008102c00604ff0902a10185060902150026ff0075"
    "089507b102c00602ff0902a10185080902150026ff0075089510b102c0"
)

# The motion interface (3-2:1.0 -> /dev/hidraw0): no report ids to speak of,
# which is why a feature report sent there fails with EPIPE.
MOUSE_DESCRIPTOR = bytes.fromhex(
    "05010902a1010901a10005091901290515002501950575018102950175038101"
    "05010930093116008026ff7f751095028106c0a100050109381581257f750895"
    "018106c0a100050c0a3802950175081581257f8106c0c0"
)

# ==========================================================================
# Zero dependencies
# ==========================================================================


def test_the_tool_imports_nothing_outside_the_standard_library() -> None:
    """The promise is "no packages at all", so it is checked, not assumed."""
    source = (Path(compxctl.__file__)).read_text()
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    third_party = sorted(name for name in imported if name not in sys.stdlib_module_names)
    assert third_party == []


# ==========================================================================
# ioctl numbers
# ==========================================================================


def test_hidraw_ioctl_numbers_match_the_kernel_headers() -> None:
    """Values compiled from <linux/hidraw.h> and <linux/usbdevice_fs.h>.

    A wrong number here does not raise: the ioctl quietly does something else,
    which is why these are pinned rather than trusted.
    """
    assert compxctl.hidraw_set_feature_ioctl(17) == 0xC0114806
    assert compxctl.hidraw_set_feature_ioctl(8) == 0xC0084806
    assert compxctl.usbdevfs_reset_ioctl == 0x00005514


def test_is_config_reply_accepts_only_input_report_nine() -> None:
    frame = _reply(b"\x09\x08\x00\x00\x00\x02", b"\x01\x54")
    assert compxctl.is_config_reply(frame)
    assert not compxctl.is_config_reply(frame[:8])  # truncated
    assert not compxctl.is_config_reply(b"\x01" + frame[1:])  # keyboard report
    assert not compxctl.is_config_reply(b"")  # nothing queued


# ==========================================================================
# sysfs layer
# ==========================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1ms", 1.0), ("2ms", 2.0), ("8ms", 8.0), ("125us", 0.125), ("1s", 1000.0), ("4", 4.0)],
)
def test_parse_interval_ms(text: str, expected: float) -> None:
    assert compxctl.parse_interval_ms(text) == expected


@pytest.mark.parametrize("bad", ["", "ms", "fast", "-1ms", "1 ms extra"])
def test_parse_interval_ms_rejects_garbage(bad: str) -> None:
    assert compxctl.parse_interval_ms(bad) is None


def test_interval_ms_from_descriptor_uses_frames_at_full_speed() -> None:
    """Full/low speed: bInterval is already in milliseconds."""
    assert compxctl.interval_ms_from_descriptor(1, 12) == 1.0
    assert compxctl.interval_ms_from_descriptor(8, 1.5) == 8.0


def test_interval_ms_from_descriptor_uses_microframes_at_high_speed() -> None:
    """High speed: bInterval counts microframes, so 1 -> 125 us."""
    assert compxctl.interval_ms_from_descriptor(1, 480) == 0.125
    assert compxctl.interval_ms_from_descriptor(4, 480) == 1.0


@pytest.mark.parametrize(
    ("ms", "hz"), [(1.0, 1000), (2.0, 500), (8.0, 125), (0.125, 8000), (None, None), (0.0, None)]
)
def test_rate_hz_from_interval_ms(ms: float | None, hz: int | None) -> None:
    assert compxctl.rate_hz_from_interval_ms(ms) == hz


def test_find_mouses_reads_hex_attributes(tmp_path: Path) -> None:
    """USB descriptor attributes are hex without a prefix.

    This is the regression test for the parser bug that made `find_mouses`
    return nothing at all: `int("25a7", 0)` raises and `int("81", 0)` is 81,
    not 0x81.
    """
    mouses = compxctl.find_mouses(make_sysfs_tree(tmp_path), hidraw_root(tmp_path))
    assert len(mouses) == 1
    mouse = mouses[0]
    assert mouse.vendor == 0x25A7
    assert mouse.product == 0xFA7B
    assert mouse.endpoint is not None
    assert mouse.endpoint.name == "ep_81"
    assert mouse.endpoint.interval_ms == 1.0
    assert mouse.endpoint.interval_source == "interval"
    assert mouse.rate_hz == 1000
    assert mouse.interface_dir is not None and mouse.interface_dir.name == "3-2:1.0"


def test_find_mouses_ignores_other_vendors(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path)
    assert compxctl.find_mouses(root / "nonexistent", hidraw_root(tmp_path)) == []


def test_find_mouses_prefers_the_boot_mouse_interface(tmp_path: Path) -> None:
    """The config interface (protocol 01) must not be mistaken for the mouse."""
    assert find_mouse(tmp_path).interface_dir.name == "3-2:1.0"


def test_find_mouses_falls_back_to_binterval_when_interval_is_absent(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path, interval=None, b_interval="04", speed="12")
    endpoint = compxctl.find_mouses(root, hidraw_root(tmp_path))[0].endpoint
    assert endpoint is not None
    assert endpoint.interval_source == "bInterval"
    assert endpoint.rate_hz == 250


def test_find_mouses_reports_unknown_interval(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path, interval=None)
    (root / "3-2:1.0" / "ep_81" / "bInterval").unlink()
    endpoint = compxctl.find_mouses(root, hidraw_root(tmp_path))[0].endpoint
    assert endpoint is not None
    assert endpoint.interval_source == "unknown"
    assert endpoint.rate_hz is None


@pytest.mark.parametrize("pid", ["fa7b", "fa7c", "fa03", "fa93"])
def test_find_mouses_accepts_every_supported_pid(tmp_path: Path, pid: str) -> None:
    root = make_sysfs_tree(tmp_path, product=pid)
    assert compxctl.find_mouses(root, hidraw_root(tmp_path))[0].product == int(pid, 16)


def test_find_mouses_skips_interface_without_interrupt_endpoint(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path)
    endpoint = root / "3-2:1.0" / "ep_81"
    _write(endpoint / "bmAttributes", "02")  # bulk, not interrupt
    mouse = compxctl.find_mouses(root, hidraw_root(tmp_path))[0]
    assert mouse.endpoint is None
    assert mouse.rate_hz is None


def test_pick_interfaces_separates_the_mouse_from_the_config_interface() -> None:
    mouse = Path("/sys/bus/usb/devices/3-2:1.0")
    config = Path("/sys/bus/usb/devices/3-2:1.1")
    assert compxctl.pick_interfaces([(mouse, (3, 1, 2)), (config, (3, 1, 1))]) == (mouse, config)
    assert compxctl.pick_interfaces([(config, (3, 1, 1)), (mouse, (3, 1, 2))]) == (mouse, config)


def test_pick_interfaces_falls_back_to_the_only_hid_interface() -> None:
    only = Path("/sys/bus/usb/devices/3-2:1.0")
    assert compxctl.pick_interfaces([(only, (3, 1, 2))]) == (only, only)
    assert compxctl.pick_interfaces([(only, (3, 0, 0))]) == (only, only)
    assert compxctl.pick_interfaces([]) == (None, None)


def test_hidraw_node_prefers_the_config_interface(tmp_path: Path) -> None:
    """The regression test for a bug that only hardware showed.

    Both interfaces belong to the same USB device, so a lookup that matches any
    of them returns the motion node — and a feature report sent to that node
    fails with EPIPE, because the vendor report ids exist on the config
    interface only.
    """
    mouse = find_mouse(tmp_path)
    assert mouse.hidraw_path == Path("/dev/hidraw1")
    assert mouse.config_interface_dir.name == "3-2:1.1"


def test_hidraw_node_falls_back_when_the_config_interface_has_none(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path)
    shutil.rmtree(hidraw_root(tmp_path) / "hidraw1")
    mouse = compxctl.find_mouses(root, hidraw_root(tmp_path))[0]
    assert mouse.hidraw_path == Path("/dev/hidraw0")


def test_hidraw_node_is_none_without_any_node(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path, hidraw_nodes=False)
    mouse = compxctl.find_mouses(root, hidraw_root(tmp_path))[0]
    assert mouse.hidraw_path is None


def test_hidraw_node_never_picks_another_devices_node(tmp_path: Path) -> None:
    """hidraw2 belongs to an unrelated device in the same tree."""
    root = make_sysfs_tree(tmp_path)
    assert compxctl.hidraw_node_for([], hidraw_root(tmp_path)) is None
    assert compxctl.find_mouses(root, hidraw_root(tmp_path))[0].hidraw_path != Path("/dev/hidraw2")


def test_require_hidraw_path_explains_a_missing_node(tmp_path: Path) -> None:
    mouse = compxctl.find_mouses(
        make_sysfs_tree(tmp_path, hidraw_nodes=False), hidraw_root(tmp_path)
    )[0]
    with pytest.raises(RuntimeError, match="has no /dev/hidraw node"):
        compxctl.require_hidraw_path(mouse)


def test_report_ids_reads_the_descriptor_of_the_verified_unit() -> None:
    """The two descriptors captured from the hardware, decoded.

    The config interface is the one that declares id 8 as a FEATURE report and
    id 9 as INPUT; the motion interface declares neither, which is why a feature
    report sent to it comes back as EPIPE.
    """
    config = compxctl.report_ids(CONFIG_DESCRIPTOR)
    assert config["feature"] == {0x06, 0x08}
    assert config["input"] == {0x01, 0x02, 0x03, 0x05, 0x09}
    assert compxctl.declares_vendor_reports(CONFIG_DESCRIPTOR) is True

    mouse = compxctl.report_ids(MOUSE_DESCRIPTOR)
    assert mouse["feature"] == set()
    assert compxctl.declares_vendor_reports(MOUSE_DESCRIPTOR) is False


def test_report_ids_skips_long_items() -> None:
    """A long item carries its own length; mis-reading it desynchronizes the walk."""
    descriptor = bytes(
        [0x06, 0x00, 0xFF]  # Usage Page (Vendor)
        + [0xFE, 0x03, 0x01, 0xAA, 0xBB, 0xCC]  # long item, 3 bytes of payload
        + [0x85, 0x08, 0x09, 0x00, 0xB1, 0x02]  # Report ID 8, Feature, 2 bytes
    )
    assert compxctl.report_ids(descriptor)["feature"] == {0x08}


def test_report_ids_survives_a_truncated_descriptor() -> None:
    """A cut-off descriptor is data from sysfs, not a promise."""
    assert compxctl.report_ids(b"") == {"input": set(), "output": set(), "feature": set()}
    assert compxctl.report_ids(bytes([0x85]))["feature"] == set()
    assert compxctl.report_ids(CONFIG_DESCRIPTOR[:40])["feature"] == set()
    assert compxctl.report_ids(bytes([0xFE, 0x10]))["feature"] == set()


def test_interface_refusal_stops_at_the_wrong_interface() -> None:
    node = Path("/dev/hidraw0")
    refusal = compxctl.interface_refusal(node, MOUSE_DESCRIPTOR)
    assert refusal is not None
    assert "not the vendor config interface" in refusal
    assert "0x08" in refusal


def test_interface_refusal_is_silent_for_the_right_interface_and_for_nothing() -> None:
    assert compxctl.interface_refusal(Path("/dev/hidraw1"), CONFIG_DESCRIPTOR) is None
    # An unreadable descriptor is not evidence: the protocol decides instead.
    assert compxctl.interface_refusal(Path("/dev/hidraw1"), b"") is None


def test_hidraw_node_is_chosen_by_the_descriptor_when_it_disagrees(tmp_path: Path) -> None:
    """The descriptor decides, not the interface numbering.

    On an unverified sibling model the interfaces may be numbered differently;
    the reports an interface declares cannot lie about what it is.
    """
    root = make_sysfs_tree(tmp_path, swapped_descriptors=True)
    mouse = compxctl.find_mouses(root, hidraw_root(tmp_path))[0]
    assert mouse.hidraw_path == Path("/dev/hidraw0")
    assert mouse.hidraw_descriptor == hidraw_root(tmp_path) / "hidraw0" / "device" / "report_descriptor"


def test_hidraw_node_falls_back_to_the_interface_class_without_descriptors(
    tmp_path: Path,
) -> None:
    root = make_sysfs_tree(tmp_path, descriptors=False)
    mouse = compxctl.find_mouses(root, hidraw_root(tmp_path))[0]
    assert mouse.hidraw_path == Path("/dev/hidraw1")


def test_mousepoll_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "mousepoll"
    path.write_text("0\n")
    assert compxctl.read_mousepoll(path) == 0
    compxctl.write_mousepoll(1, path)
    assert compxctl.read_mousepoll(path) == 1


def test_read_mousepoll_missing_file_is_none(tmp_path: Path) -> None:
    assert compxctl.read_mousepoll(tmp_path / "absent") is None


def test_write_mousepoll_permission_error_is_explained(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    directory = tmp_path / "rodir"
    directory.mkdir()
    directory.chmod(0o500)
    try:
        with pytest.raises(RuntimeError, match="needs root"):
            compxctl.write_mousepoll(1, directory / "mousepoll")
    finally:
        directory.chmod(0o700)


def test_usbhid_is_builtin_detects_a_builtin_kernel(tmp_path: Path) -> None:
    """modprobe.d is ignored for built-ins, so the advice has to change."""
    modules = tmp_path / "modules" / "6.0-test"
    modules.mkdir(parents=True)
    (modules / "modules.builtin").write_text(
        "kernel/drivers/hid/hid.ko\nkernel/drivers/hid/usbhid.ko\n"
    )
    assert compxctl.usbhid_is_builtin("6.0-test", tmp_path / "modules") is True


def test_usbhid_is_builtin_detects_a_loadable_module(tmp_path: Path) -> None:
    modules = tmp_path / "modules" / "6.0-test"
    modules.mkdir(parents=True)
    (modules / "modules.builtin").write_text("kernel/drivers/hid/hid.ko\n")
    assert compxctl.usbhid_is_builtin("6.0-test", tmp_path / "modules") is False


def test_usbhid_is_builtin_is_unknown_without_the_index(tmp_path: Path) -> None:
    assert compxctl.usbhid_is_builtin("6.0-test", tmp_path / "modules") is None


def test_persistence_advice_for_a_builtin_kernel(monkeypatch) -> None:
    monkeypatch.setattr(compxctl, "usbhid_is_builtin", lambda *a, **k: True)
    advice = " ".join(compxctl._persistence_advice(1))
    assert "kernel command line" in advice
    assert "/etc/modprobe.d is ignored" in advice


def test_persistence_advice_for_a_loadable_module(monkeypatch) -> None:
    monkeypatch.setattr(compxctl, "usbhid_is_builtin", lambda *a, **k: False)
    assert "/etc/modprobe.d" in " ".join(compxctl._persistence_advice(2))


def test_effective_rate_prefers_the_host_override(tmp_path: Path) -> None:
    mouse = find_mouse(tmp_path)
    assert compxctl.effective_rate_hz(mouse, 0) == (1000, "device descriptor")
    assert compxctl.effective_rate_hz(mouse, 2) == (500, "usbhid.mousepoll")
    assert compxctl.effective_rate_hz(None, None) == (None, "unknown")


# ==========================================================================
# Vendor protocol
# ==========================================================================


def test_checksum_makes_the_frame_sum_to_0x55() -> None:
    for body in (b"", b"\x00", b"\x08\x04" + b"\x00" * 14, bytes(range(16))):
        frame = body + bytes((compxctl.compx_checksum(body),))
        assert compxctl.verify_frame(frame)


def test_verify_frame_detects_a_flipped_bit() -> None:
    frame = bytearray(compxctl.eeprom_packet(1000))
    frame[8] ^= 0xFF
    assert not compxctl.verify_frame(bytes(frame))


@pytest.mark.parametrize("rate", sorted(compxctl.DEVICE_INTERVAL_CODE_BY_RATE))
def test_eeprom_packet_matches_hardware_capture(rate: int) -> None:
    """The persistence frame must stay byte-identical to the verified capture."""
    packet = compxctl.eeprom_packet(rate)
    assert packet == compxctl._KNOWN_GOOD_EEPROM_PACKETS[rate]
    assert len(packet) == compxctl.COMPX_FRAME_BYTES
    assert compxctl.verify_frame(packet)


@pytest.mark.parametrize("rate", sorted(compxctl.DEVICE_INTERVAL_CODE_BY_RATE))
def test_eeprom_packet_writes_only_two_bytes_at_0x0000(rate: int) -> None:
    """The bug this length avoids: clobbering 0x0002..0x0005 (DPI fields)."""
    packet = compxctl.eeprom_packet(rate)
    code = compxctl.DEVICE_INTERVAL_CODE_BY_RATE[rate]
    assert packet[:6] == b"\x08\x07\x00\x00\x00\x02"
    assert packet[6:8] == bytes((code, 0x55 - code))
    assert packet[8:16] == b"\x00" * 8


def test_build_write_frame_layout() -> None:
    frame = compxctl.build_write_frame(0x1234, b"\xab\xcd")
    assert len(frame) == compxctl.COMPX_FRAME_BYTES
    assert frame[:3] == b"\x08\x07\x00"
    assert frame[3:6] == b"\x12\x34\x02"
    assert frame[6:8] == b"\xab\xcd"
    assert compxctl.verify_frame(frame)


@pytest.mark.parametrize("bad_payload", [b"", b"\x00" * 11])
def test_build_write_frame_rejects_bad_payload(bad_payload: bytes) -> None:
    with pytest.raises(ValueError):
        compxctl.build_write_frame(0x0000, bad_payload)


def test_build_write_frame_rejects_address_outside_config_memory() -> None:
    """A silent 16-bit wraparound is worse than an error."""
    with pytest.raises(ValueError, match="outside"):
        compxctl.build_write_frame(0x10000, b"\x01")


def test_build_write_frame_rejects_a_window_that_leaves_the_memory() -> None:
    """The read path has always checked this; the EEPROM path now does too."""
    with pytest.raises(ValueError, match="outside"):
        compxctl.build_write_frame(0xFFFF, b"\x01\x02")
    assert compxctl.build_write_frame(0xFFFE, b"\x01\x02")[:3] == b"\x08\x07\x00"


def test_build_read_frame_layout() -> None:
    frame = compxctl.build_read_frame(0x000C, 8)
    assert frame[:3] == b"\x08\x08\x00"
    assert frame[3:6] == b"\x00\x0c\x08"
    assert frame[6:16] == b"\x00" * 10
    assert compxctl.verify_frame(frame)


def test_build_read_frame_no_longer_wraps_addresses_silently() -> None:
    """Previously 0x10000 produced the same frame as 0x0000."""
    with pytest.raises(ValueError):
        compxctl.build_read_frame(0x10000, 1)
    with pytest.raises(ValueError):
        compxctl.build_read_frame(-1, 1)
    with pytest.raises(ValueError):
        compxctl.build_read_frame(0xFFFF, 2)


def test_battery_request_matches_hardware_capture() -> None:
    assert compxctl.build_battery_request() == compxctl._KNOWN_GOOD_BATTERY_REQUEST
    assert compxctl.verify_frame(compxctl.build_battery_request())


def _reply(header: bytes, payload: bytes = b"") -> bytes:
    leading = header + payload
    body = leading + b"\x00" * (compxctl.COMPX_FRAME_BYTES - 1 - len(leading))
    return body + bytes((compxctl.compx_checksum(body),))


def _read_reply(addr: int, payload: bytes) -> bytes:
    return _reply(b"\x09\x08\x00" + bytes(((addr >> 8) & 0xFF, addr & 0xFF, len(payload))), payload)


def _live_ack(rate_code: int) -> bytes:
    """What the firmware sends back after a live interval packet."""
    return _reply(b"\x09\x11\x01\x00\x00\x06", bytes((rate_code,)))


def _write_ack(addr: int, payload: bytes) -> bytes:
    """What the firmware sends back after a config write: an echo of it.

    Same address and length as a reply to a read of that address, which is why
    matching on the address alone reported a mismatch on writes that worked.
    """
    return _reply(b"\x09\x07\x00" + bytes(((addr >> 8) & 0xFF, addr & 0xFF, len(payload))), payload)


@pytest.mark.parametrize("payload", [b"\x01\x54", b"\x13\x13\x00\x2f", b"\x00" * 10])
def test_parse_read_reply_slices_by_length_byte(payload: bytes) -> None:
    assert compxctl.parse_read_reply(_read_reply(0x0000, payload)) == payload


def test_parse_read_reply_rejects_short_frame() -> None:
    with pytest.raises(ValueError):
        compxctl.parse_read_reply(b"\x09\x08\x00\x00\x00\x02\x01\x54")


@pytest.mark.parametrize("bad_ln", [0x00, 0x0B, 0x40])
def test_parse_read_reply_rejects_bad_length_byte(bad_ln: int) -> None:
    reply = bytearray(_read_reply(0x0000, b"\x01\x54"))
    reply[5] = bad_ln
    with pytest.raises(ValueError):
        compxctl.parse_read_reply(bytes(reply))


@pytest.mark.parametrize(
    ("dpi", "code"), [(50, 0x00), (400, 0x07), (1000, 0x13), (2400, 0x2F), (6400, 0x7F)]
)
def test_dpi_codec_round_trip(dpi: int, code: int) -> None:
    assert compxctl.dpi_code(dpi) == code
    assert compxctl.dpi_decode(code) == dpi


@pytest.mark.parametrize("bad", [0, 25, 6450, 6500, -400, 123])
def test_dpi_code_rejects_unrepresentable_values(bad: int) -> None:
    assert compxctl.dpi_code(bad) is None


def test_dpi_decode_refuses_the_undecoded_extended_encoding() -> None:
    assert compxctl.dpi_decode(0x80) is None
    assert compxctl.dpi_decode(0x7B, mul=0x44) is None


def test_slot_checksum_identity() -> None:
    assert compxctl.slot_checksum_ok(0x13, 0x13, 0x00, 0x2F)
    assert not compxctl.slot_checksum_ok(0x13, 0x13, 0x00, 0x2E)


@pytest.mark.parametrize(("index", "slot"), [(1, 0), (2, 1), (8, 7), (0, None), (9, None), (0xFF, None)])
def test_active_slot_for_index(index: int, slot: int | None) -> None:
    assert compxctl.active_slot_for_index(index) == slot


def test_dpi_slot_addresses() -> None:
    assert compxctl.dpi_slot_addr(0) == 0x000C
    assert compxctl.dpi_slot_addr(5) == 0x0020
    assert compxctl.dpi_slot_addr(7) == 0x0028


# ==========================================================================
# Device layer over fake hidraw IO
# ==========================================================================


class FakeHidrawIO:
    """Records feature reports and replays queued input reports.

    `responses` holds one entry per frame sent (None meaning "no answer"), which
    is how the firmware behaves: it answers the command it just received, while
    a frame queued by hand models traffic that was already on the wire.
    """

    def __init__(
        self, responses: list[bytes | None] | None = None, *, lock_available: bool = True
    ) -> None:
        self.sent: list[bytes] = []
        self.queue: list[bytes] = []
        self.responses = list(responses or [])
        self.opened: list[Path] = []
        self.closed: list[int] = []
        self.timeouts: list[int] = []
        self.locks: list[tuple[int, int]] = []
        self.lock_available = lock_available
        self.fail_send: OSError | None = None

    def lock(self, fd: int, timeout_ms: int) -> bool:
        self.locks.append((fd, timeout_ms))
        return self.lock_available

    def open(self, path: Path) -> int:
        self.opened.append(path)
        return 3

    def close(self, fd: int) -> None:
        self.closed.append(fd)

    def set_feature(self, fd: int, frame: bytes) -> None:
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(frame)
        if self.responses:
            reply = self.responses.pop(0)
            if reply is not None:
                self.queue.append(reply)

    def read_report(self, fd: int, timeout_ms: int) -> bytes | None:
        self.timeouts.append(timeout_ms)
        return self.queue.pop(0) if self.queue else None


class FakeClock:
    """A clock that only moves when someone waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DeadlineIO(FakeHidrawIO):
    """A device that never answers and moves the clock by what it was asked to wait."""

    def __init__(self, clock: FakeClock, *, max_reads: int = 8) -> None:
        super().__init__()
        self.clock = clock
        self.max_reads = max_reads

    def read_report(self, fd: int, timeout_ms: int) -> bytes | None:
        self.timeouts.append(timeout_ms)
        if self.queue:
            return self.queue.pop(0)
        if len(self.timeouts) > self.max_reads:
            raise AssertionError("the deadline never expired")
        self.clock.advance(timeout_ms / 1000.0)
        return None


def make_session(io: FakeHidrawIO, tmp_path: Path, *, mouse: compxctl.Mouse | None = None):
    """A HidrawSession over the fake IO, pointed at the synthetic mouse."""
    return compxctl.HidrawSession(mouse or find_mouse(tmp_path), io=io)


def _mouse_for(monkeypatch, tmp_path: Path) -> compxctl.Mouse:
    mouse = find_mouse(tmp_path)
    monkeypatch.setattr(compxctl, "find_mouses", lambda *a, **k: [mouse])
    return mouse


def _session_factory(io: FakeHidrawIO):
    """Replace HidrawSession's IO while keeping its behaviour.

    The class is captured now, before monkeypatch swaps the module attribute,
    so the replacement does not call itself.
    """
    original = compxctl.HidrawSession
    return lambda mouse=None, **kwargs: original(mouse, io=io, **kwargs)


def test_session_opens_the_config_interface_node(tmp_path: Path) -> None:
    io = FakeHidrawIO()
    with make_session(io, tmp_path):
        pass
    assert io.opened == [Path("/dev/hidraw1")]
    assert io.closed == [3]


def test_session_takes_an_advisory_lock(tmp_path: Path) -> None:
    """Two compxctl runs are serialized: each sees every report, so they may not
    overlap or one can read the other's frame."""
    io = FakeHidrawIO()
    with make_session(io, tmp_path):
        pass
    assert io.locks == [(3, compxctl.LOCK_WAIT_MS)]


def test_session_reports_a_lock_held_by_another_process(tmp_path: Path) -> None:
    io = FakeHidrawIO(lock_available=False)
    with pytest.raises(RuntimeError, match="Another compxctl"):
        with make_session(io, tmp_path):
            pass
    assert io.closed == [3]  # the node is not left open on the way out


def test_session_refuses_a_node_that_is_not_the_vendor_interface(tmp_path: Path) -> None:
    """Only the motion interface has a node: better to stop than to poke it."""
    root = make_sysfs_tree(tmp_path)
    shutil.rmtree(hidraw_root(tmp_path) / "hidraw1")
    mouse = compxctl.find_mouses(root, hidraw_root(tmp_path))[0]
    assert mouse.hidraw_path == Path("/dev/hidraw0")
    with pytest.raises(RuntimeError, match="not the vendor config interface"):
        with compxctl.HidrawSession(mouse, io=FakeHidrawIO()):
            pass


def test_session_stops_asking_once_the_device_has_gone_quiet(tmp_path: Path) -> None:
    """A sleeping mouse costs one retry budget, not one per readout.

    `status` asks four questions; paying four full timeouts for the same silence
    is fourteen seconds of a status bar waiting on a mouse that is switched off.
    """
    io = FakeHidrawIO([None] * compxctl.READ_ATTEMPTS)
    with make_session(io, tmp_path) as session:
        with pytest.raises(RuntimeError, match="nothing came back at all"):
            session.read(0x0000, 2)
        sent_for_the_first = len(io.sent)
        with pytest.raises(RuntimeError, match="stopped answering"):
            session.read(0x0004, 2)
        assert len(io.sent) == sent_for_the_first  # the second read sent nothing


def test_a_wrong_reply_is_not_silence(tmp_path: Path) -> None:
    """Frames arriving but not matching is a different problem: keep asking."""
    io = FakeHidrawIO([_read_reply(0x0004, b"\x01\x54")] * compxctl.READ_ATTEMPTS)
    with make_session(io, tmp_path) as session:
        with pytest.raises(RuntimeError, match="header mismatch"):
            session.read(0x0000, 2)
        io.responses.extend([_read_reply(0x0000, b"\x01\x54")])
        assert session.read(0x0000, 2) == b"\x01\x54"


def test_session_read_returns_payload(tmp_path: Path) -> None:
    io = FakeHidrawIO([_read_reply(0x0000, b"\x01\x54")])
    with make_session(io, tmp_path) as session:
        assert session.read(0x0000, 2) == b"\x01\x54"
    assert io.sent == [compxctl.build_read_frame(0x0000, 2)]


def test_session_read_sends_the_command_once_per_live_frame(tmp_path: Path) -> None:
    """A frame from the interface's own traffic is drained, not answered with a resend.

    Resending on every foreign frame is what left one extra reply in the queue
    per stale frame, and the next command then read that reply as its answer.
    """
    io = FakeHidrawIO([_read_reply(0x0000, b"\x01\x54")])
    io.queue.append(_live_ack(0x01))
    with make_session(io, tmp_path) as session:
        assert session.read(0x0000, 2) == b"\x01\x54"
    assert len(io.sent) == 1


def test_session_read_drops_a_leftover_reply_before_asking(tmp_path: Path) -> None:
    """The readback bug, locked down.

    A reply to an earlier read of the same register is byte-compatible with a
    fresh one, so it has to be dropped before the command goes out: otherwise a
    successful EEPROM write is reported as a readback mismatch.
    """
    io = FakeHidrawIO([_read_reply(0x0000, b"\x02\x53")])
    io.queue.append(_read_reply(0x0000, b"\x01\x54"))
    with make_session(io, tmp_path) as session:
        assert session.read(0x0000, 2) == b"\x02\x53"
    assert len(io.sent) == 1


def test_session_read_ignores_the_acknowledge_of_its_own_write(tmp_path: Path) -> None:
    """The write acknowledge carries the written bytes: accepting it would confirm
    any write, including one the device refused."""
    io = FakeHidrawIO([_read_reply(0x0000, b"\x01\x54")])
    io.queue.append(_write_ack(0x0000, b"\x02\x53"))
    with make_session(io, tmp_path) as session:
        assert session.read(0x0000, 2) == b"\x01\x54"


def test_session_read_retries_when_no_answer_comes(tmp_path: Path) -> None:
    """A lost answer over the radio link is worth another try — a lost answer,
    not a foreign frame."""
    io = FakeHidrawIO([None, _read_reply(0x0000, b"\x01\x54")])
    with make_session(io, tmp_path) as session:
        assert session.read(0x0000, 2) == b"\x01\x54"
    assert len(io.sent) == 2


def test_await_reply_gives_up_when_the_deadline_passes(tmp_path: Path) -> None:
    """The timeout is a deadline, not a hope.

    The fake only moves the clock when it is asked to wait, so a deadline that
    never expired would loop until the fake refuses, and one that ignored the
    session's timeout would ask for the wrong wait.
    """
    clock = FakeClock()
    io = DeadlineIO(clock)
    session = compxctl.HidrawSession(find_mouse(tmp_path), timeout_ms=500, io=io, clock=clock)
    with session:
        assert session._await_reply(lambda report: "never") == (None, None)
    assert io.timeouts == [500]
    assert clock.now == 0.5


def test_session_read_gives_up_after_read_attempts(tmp_path: Path) -> None:
    """Silence is named as silence: the caller knows the device is not there."""
    io = FakeHidrawIO([None] * compxctl.READ_ATTEMPTS)
    with make_session(io, tmp_path) as session:
        with pytest.raises(RuntimeError, match="nothing came back at all"):
            session.read(0x0000, 2)
    assert len(io.sent) == compxctl.READ_ATTEMPTS


def test_session_read_blames_the_other_program_when_frames_do_come(tmp_path: Path) -> None:
    """Frames arriving and never matching means someone else is talking."""
    io = FakeHidrawIO([_read_reply(0x0004, b"\x01\x54")] * compxctl.READ_ATTEMPTS)
    with make_session(io, tmp_path) as session:
        with pytest.raises(RuntimeError, match="tried 4 times"):
            session.read(0x0000, 2)


def test_session_read_reports_a_wrong_reply(tmp_path: Path) -> None:
    """A reply for another address is refused rather than parsed."""
    io = FakeHidrawIO([_read_reply(0x0004, b"\x01\x54")] * compxctl.READ_ATTEMPTS)
    with make_session(io, tmp_path) as session:
        with pytest.raises(RuntimeError, match="header mismatch"):
            session.read(0x0000, 2)


def test_session_send_wraps_a_failed_ioctl(tmp_path: Path) -> None:
    io = FakeHidrawIO()
    io.fail_send = OSError(32, "Broken pipe")
    with make_session(io, tmp_path) as session:
        with pytest.raises(OSError, match="08 08 00"):
            session.send(compxctl.build_read_frame(0x0000, 2))


def test_session_without_a_hidraw_node_explains_itself(tmp_path: Path) -> None:
    mouse = compxctl.find_mouses(
        make_sysfs_tree(tmp_path, hidraw_nodes=False), hidraw_root(tmp_path)
    )[0]
    with pytest.raises(RuntimeError, match="has no /dev/hidraw node"):
        with compxctl.HidrawSession(mouse, io=FakeHidrawIO()):
            pass


def test_session_open_permission_error_mentions_the_udev_rule(tmp_path: Path) -> None:
    class RefusingIO(FakeHidrawIO):
        def open(self, path: Path) -> int:
            raise PermissionError(13, "Permission denied")

    with pytest.raises(RuntimeError, match="99-compx-mouse.rules"):
        with make_session(RefusingIO(), tmp_path):
            pass


def test_session_battery_maps_reply_fields(tmp_path: Path) -> None:
    io = FakeHidrawIO([_reply(b"\x09\x04\x00\x00\x00", b"\x02\x64\x00")])
    with make_session(io, tmp_path) as session:
        assert session.battery() == (100, False, 2)
    assert io.sent == [compxctl.build_battery_request()]


def test_session_battery_rejects_an_impossible_percentage(tmp_path: Path) -> None:
    """A garbage reply must fail loudly instead of printing 250%."""
    io = FakeHidrawIO([_reply(b"\x09\x04\x00\x00\x00", b"\x02\xFA\x00")])
    with make_session(io, tmp_path) as session:
        with pytest.raises(RuntimeError, match="impossible percentage"):
            session.battery()


def test_session_write_dpi_verifies_all_four_bytes(tmp_path: Path) -> None:
    payload = bytes((0x13, 0x13, 0x00, 0x2F))
    io = FakeHidrawIO([None, _read_reply(0x000C, payload)])
    with make_session(io, tmp_path) as session:
        session.write_dpi(0, 0x13)
    # one write, its acknowledge, then one readback of the same four bytes
    assert io.sent == [
        compxctl.build_write_frame(0x000C, payload),
        compxctl.build_read_frame(0x000C, 4),
    ]


def test_session_write_dpi_detects_a_partial_readback(tmp_path: Path) -> None:
    """The old version only compared x; a wrong y slipped through as "verified"."""
    io = FakeHidrawIO([None, _read_reply(0x000C, bytes((0x13, 0x99, 0x00, 0x2F)))])
    with make_session(io, tmp_path) as session:
        with pytest.raises(RuntimeError, match="Readback mismatch"):
            session.write_dpi(0, 0x13)


def test_session_persist_rate_skips_when_already_stored(tmp_path: Path) -> None:
    io = FakeHidrawIO([_read_reply(0x0000, bytes((0x01, 0x54)))])
    with make_session(io, tmp_path) as session:
        assert session.persist_rate(1000) is False
    assert io.sent == [compxctl.build_read_frame(0x0000, 2)]


def test_session_persist_rate_writes_and_reads_back(tmp_path: Path) -> None:
    """The whole sequence as the firmware plays it, acknowledges included."""
    io = FakeHidrawIO(
        [
            _read_reply(0x0000, bytes((0x08, 0x4D))),  # currently 125 Hz
            _write_ack(0x0000, bytes((0x01, 0x54))),  # echo of the write frame
            _read_reply(0x0000, bytes((0x01, 0x54))),  # after the write
        ]
    )
    with make_session(io, tmp_path) as session:
        assert session.persist_rate(1000) is True
    assert io.sent[1] == compxctl.eeprom_packet(1000)


def test_session_persist_rate_verifies_both_bytes(tmp_path: Path) -> None:
    """The complement at 0x0001 is part of the pair that was written."""
    io = FakeHidrawIO(
        [
            _read_reply(0x0000, bytes((0x08, 0x4D))),
            None,
            _read_reply(0x0000, bytes((0x01, 0x00))),  # code right, complement lost
        ]
    )
    with make_session(io, tmp_path) as session:
        with pytest.raises(RuntimeError, match="readback mismatch"):
            session.persist_rate(1000)


def test_session_persist_rate_rewrites_a_corrupt_pair(tmp_path: Path) -> None:
    """A stored pair that is not what would be written is not "already set"."""
    io = FakeHidrawIO(
        [
            _read_reply(0x0000, bytes((0x01, 0x00))),  # code matches, pair is wrong
            _write_ack(0x0000, bytes((0x01, 0x54))),
            _read_reply(0x0000, bytes((0x01, 0x54))),
        ]
    )
    with make_session(io, tmp_path) as session:
        assert session.persist_rate(1000) is True


def test_session_persist_rate_reports_a_failed_readback(tmp_path: Path) -> None:
    io = FakeHidrawIO(
        [
            _read_reply(0x0000, bytes((0x08, 0x4D))),
            None,
            _read_reply(0x0000, bytes((0x08, 0x4D))),  # write did not stick
        ]
    )
    with make_session(io, tmp_path) as session:
        with pytest.raises(RuntimeError, match="EEPROM readback mismatch"):
            session.persist_rate(1000)


def test_persist_rate_refuses_a_frame_that_drifts_from_the_capture(
    tmp_path: Path, monkeypatch
) -> None:
    """The EEPROM write is the one irreversible thing here; it must be guarded."""
    io = FakeHidrawIO([_read_reply(0x0000, bytes((0x08, 0x4D)))])
    session = make_session(io, tmp_path)
    monkeypatch.setattr(compxctl, "eeprom_packet", lambda rate: b"\x00" * 17)
    with session:
        with pytest.raises(RuntimeError, match="verified capture"):
            session.persist_rate(1000)
    assert len(io.sent) == 1  # only the read; nothing was written


def test_apply_live_rate_sends_the_interval_packets_only(tmp_path: Path) -> None:
    io = FakeHidrawIO()
    with make_session(io, tmp_path) as session:
        compxctl.apply_live_rate(session, 500)
    assert io.sent == list(compxctl.LIVE_PACKETS[500])
    assert len(io.sent) == 2  # never the EEPROM packet


def test_session_drain_stops_on_a_short_read(tmp_path: Path) -> None:
    """`os.read` returning nothing is falsy but is not None: it ends the drain."""
    io = FakeHidrawIO()
    io.queue.append(b"")
    with make_session(io, tmp_path) as session:
        assert session._drain_pending() == 0


def test_session_drain_is_capped(tmp_path: Path) -> None:
    """A device that never stops talking cannot hold a command here."""
    io = FakeHidrawIO()
    io.queue.extend([_live_ack(0x01)] * (compxctl.HIDRAW_DRAIN_MAX_FRAMES + 10))
    with make_session(io, tmp_path) as session:
        assert session._drain_pending() == compxctl.HIDRAW_DRAIN_MAX_FRAMES
    assert len(io.queue) == 10


def test_session_methods_outside_the_context_manager_say_so(tmp_path: Path) -> None:
    """Not an AssertionError, and not gone under `python -O` either."""
    session = make_session(FakeHidrawIO(), tmp_path)
    with pytest.raises(RuntimeError, match="not open"):
        session.send(compxctl.build_read_frame(0x0000, 2))


def test_session_drains_only_what_is_queued(tmp_path: Path) -> None:
    """Draining must not wait: a status bar cannot pay a timeout per command."""
    io = FakeHidrawIO()
    io.queue.extend([_live_ack(0x01), _live_ack(0x02)])
    with make_session(io, tmp_path) as session:
        assert session._drain_pending() == 2
        assert session._drain_pending() == 0
    assert io.timeouts == [0] * 4


# ==========================================================================
# Re-enumeration
# ==========================================================================


class FakeUsbfsIO:
    """Records the reset ioctl instead of re-enumerating a real device."""

    def __init__(self, *, fail_open: OSError | None = None, fail_reset: OSError | None = None):
        self.opened: list[Path] = []
        self.resets: list[int] = []
        self.closed: list[int] = []
        self.fail_open = fail_open
        self.fail_reset = fail_reset

    def open(self, path: Path) -> int:
        if self.fail_open is not None:
            raise self.fail_open
        self.opened.append(path)
        return 7

    def reset(self, fd: int) -> None:
        if self.fail_reset is not None:
            raise self.fail_reset
        self.resets.append(fd)

    def close(self, fd: int) -> None:
        self.closed.append(fd)


def test_usbfs_node_is_built_from_the_sysfs_numbers(tmp_path: Path) -> None:
    mouse = find_mouse(tmp_path)
    assert compxctl.usbfs_node(mouse, Path("/dev/bus/usb")) == Path("/dev/bus/usb/003/002")


def test_usbfs_node_is_none_without_the_numbers(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path)
    (root / "3-2" / "busnum").unlink()
    mouse = compxctl.find_mouses(root, hidraw_root(tmp_path))[0]
    assert compxctl.usbfs_node(mouse) is None


def test_reset_usb_device_issues_the_reset_ioctl(tmp_path: Path) -> None:
    io = FakeUsbfsIO()
    node = compxctl.reset_usb_device(find_mouse(tmp_path), io=io, root=Path("/dev/bus/usb"))
    assert node == Path("/dev/bus/usb/003/002")
    assert io.opened == [node]
    assert io.resets == [7]
    assert io.closed == [7]


def test_reset_usb_device_explains_a_permission_error(tmp_path: Path) -> None:
    io = FakeUsbfsIO(fail_open=PermissionError(13, "Permission denied"))
    with pytest.raises(RuntimeError, match="re-plug the mouse by hand"):
        compxctl.reset_usb_device(find_mouse(tmp_path), io=io)


def test_reset_usb_device_explains_a_failed_reset(tmp_path: Path) -> None:
    """-EOPNOTSUPP is what the kernel returns for a device it will not reset."""
    io = FakeUsbfsIO(fail_reset=OSError(95, "Operation not supported"))
    with pytest.raises(RuntimeError, match="Reset of"):
        compxctl.reset_usb_device(find_mouse(tmp_path), io=io)
    assert io.closed == [7]  # the node is closed even when the reset fails


def test_reset_usb_device_needs_a_usbfs_node(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path)
    (root / "3-2" / "devnum").unlink()
    mouse = compxctl.find_mouses(root, hidraw_root(tmp_path))[0]
    with pytest.raises(RuntimeError, match="busnum/devnum"):
        compxctl.reset_usb_device(mouse, io=FakeUsbfsIO())


def test_wait_for_mouse_returns_as_soon_as_it_is_back(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path)
    mouse = compxctl.wait_for_mouse(
        "3-2", root=root, hidraw_root=hidraw_root(tmp_path), sleep=lambda _: None
    )
    assert mouse is not None and mouse.sysfs_dir.name == "3-2"


def test_wait_for_mouse_polls_until_the_device_reappears(tmp_path: Path) -> None:
    """A device that is not there yet is waited for, not reported as missing."""
    root = make_sysfs_tree(tmp_path)
    device = root / "3-2"
    (device / "idVendor").unlink()
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 1:
            _write(device / "idVendor", "25a7")

    mouse = compxctl.wait_for_mouse(
        "3-2", root=root, hidraw_root=hidraw_root(tmp_path), sleep=sleep, timeout_s=1.0
    )
    assert mouse is not None
    assert calls == [compxctl.RESET_POLL_S]


def test_wait_for_mouse_gives_up_and_says_so(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path)
    (root / "3-2" / "idVendor").unlink()
    clock = iter([0.0, 0.5, 1.5]).__next__
    mouse = compxctl.wait_for_mouse(
        "3-2",
        root=root,
        hidraw_root=hidraw_root(tmp_path),
        sleep=lambda _: None,
        clock=clock,
        timeout_s=1.0,
    )
    assert mouse is None


def test_reenumerate_is_skipped_when_nothing_was_written(tmp_path: Path) -> None:
    assert compxctl._reenumerate(find_mouse(tmp_path), False, enabled=True) == ("skipped", None)


def test_reenumerate_can_be_turned_off(tmp_path: Path) -> None:
    """--no-reset keeps the re-plug a human step, and the output says so."""
    mouse = find_mouse(tmp_path)
    assert compxctl._reenumerate(mouse, True, enabled=False) == ("manual", None)
    assert compxctl._reenumerate(mouse, False, enabled=False) == ("skipped", None)


def test_reenumerate_needs_the_device_back_to_call_it_ok(tmp_path: Path, monkeypatch) -> None:
    """The ioctl returning is not success: the device has to be there."""
    monkeypatch.setattr(compxctl, "reset_usb_device", lambda *a, **k: Path("/dev/bus/usb/003/002"))
    monkeypatch.setattr(compxctl, "wait_for_mouse", lambda name, **k: None)
    status, error = compxctl._reenumerate(find_mouse(tmp_path), True, enabled=True)
    assert status == "failed"
    assert "did not come back" in (error or "")


def test_reenumerate_reports_a_failure_instead_of_raising(
    tmp_path: Path, monkeypatch
) -> None:
    def refuse(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(compxctl, "reset_usb_device", refuse)
    status, error = compxctl._reenumerate(find_mouse(tmp_path), True, enabled=True)
    assert (status, error) == ("failed", "nope")


# ==========================================================================
# Reports
# ==========================================================================


def test_polling_json_carries_both_sides_of_the_rate(tmp_path: Path) -> None:
    mouse = find_mouse(tmp_path)
    payload = compxctl.polling_json(mouse, 0)
    assert payload["advertised_rate_hz"] == 1000
    assert payload["advertised_interval_raw"] == "1ms"
    assert payload["endpoint"] == "ep_81"
    assert payload["host_override_interval_ms"] == 0
    assert payload["host_override_rate_hz"] is None
    assert payload["effective_rate_hz"] == 1000
    assert payload["effective_source"] == "device descriptor"


def test_polling_json_reports_a_missing_device_as_nulls() -> None:
    """Straight after a reset the device may not be back: null, not remembered."""
    payload = compxctl.polling_json(None, None)
    assert payload["advertised_rate_hz"] is None
    assert payload["endpoint"] is None
    assert payload["effective_rate_hz"] is None
    assert payload["effective_source"] == "unknown"


def test_polling_json_reports_an_unknown_endpoint_without_inventing_numbers(tmp_path: Path) -> None:
    """No interrupt endpoint in sysfs: null, not a made-up rate."""
    root = make_sysfs_tree(tmp_path)
    _write(root / "3-2:1.0" / "ep_81" / "bmAttributes", "02")  # bulk, not interrupt
    mouse = compxctl.find_mouses(root, hidraw_root(tmp_path))[0]
    payload = compxctl.polling_json(mouse, None)
    assert payload["advertised_rate_hz"] is None
    assert payload["advertised_interval_ms"] is None
    assert payload["endpoint"] is None
    assert payload["effective_rate_hz"] is None


def test_polling_json_marks_the_host_override_as_the_source(tmp_path: Path) -> None:
    payload = compxctl.polling_json(find_mouse(tmp_path), 2)
    assert payload["host_override_rate_hz"] == 500
    assert (payload["effective_rate_hz"], payload["effective_source"]) == (500, "usbhid.mousepoll")


def test_document_carries_the_schema_and_command() -> None:
    doc = compxctl.document("status", battery=None)
    assert doc == {"schema": compxctl.JSON_SCHEMA, "command": "status", "battery": None}


def test_slot_json_keeps_the_raw_bytes_and_the_decoded_value() -> None:
    slot = {
        "index": 7, "addr": 0x28, "x": 0x7B, "y": 0x7B, "mul": 0x44, "crc": 0x1B,
        "crc_ok": True, "dpi": None,
    }
    payload = compxctl.slot_json(slot, 0)
    assert payload["bytes"] == "7b 7b 44 1b"
    assert payload["text"] == "raw:0x7B(mul=0x44)"
    assert payload["dpi"] is None
    assert payload["active"] is False
    assert payload["raw"] == {"x": 0x7B, "y": 0x7B, "mul": 0x44, "crc": 0x1B}


def test_slot_json_marks_the_active_and_the_empty_slot() -> None:
    empty = {"index": 0, "addr": 0x0C, "x": 0xFF, "y": 0xFF, "mul": 0xFF, "crc": 0xFF,
             "crc_ok": False, "dpi": None}
    payload = compxctl.slot_json(empty, 0)
    assert payload["empty"] is True
    assert payload["active"] is True
    assert payload["text"] == "empty"


def test_active_dpi_json_maps_the_register_index_to_a_slot() -> None:
    slots = [compxctl.slot_json(
        {"index": i, "addr": 0x0C + 4 * i, "x": i, "y": i, "mul": 0, "crc": 0,
         "crc_ok": True, "dpi": 50 * (i + 1)}, None)
        for i in range(2)]
    assert compxctl.active_dpi_json((2, 0x53), slots) == {"index": 2, "slot": 1, "dpi": 100}
    assert compxctl.active_dpi_json((0xFF, 0x00), slots) == {"index": 0xFF, "slot": None, "dpi": None}
    assert compxctl.active_dpi_json(None, slots) == {"index": None, "slot": None, "dpi": None}


def test_battery_json_names_the_known_state() -> None:
    assert compxctl.battery_json(100, False, 2) == {
        "percent": 100, "charging": False, "state": 2, "state_label": "2.4G mode",
    }
    assert compxctl.battery_json(7, True, 0x7F)["state_label"] is None


def test_device_json_names_the_node_in_use(tmp_path: Path) -> None:
    payload = compxctl.device_json(find_mouse(tmp_path))
    assert payload == {
        "name": "2.4G Dual Mode Mouse",
        "vendor": "25a7",
        "product": "fa7b",
        "sysfs": "3-2",
        "hidraw": "/dev/hidraw1",
    }


# ==========================================================================
# Applying a rate
# ==========================================================================


def _applied(**overrides) -> compxctl.AppliedRate:
    fields = {
        "target": "device", "rate_hz": 1000, "host_interval_ms": None,
        "eeprom_changed": True, "reset": "ok", "reset_error": None,
    }
    return compxctl.AppliedRate(**{**fields, **overrides})


def test_applied_rate_to_device_writes_and_resets(tmp_path: Path, monkeypatch) -> None:
    mouse = find_mouse(tmp_path)
    io = FakeHidrawIO(
        [
            _live_ack(0x01),  # acknowledge of live packet 1
            None,  # no acknowledge for packet 2
            _read_reply(0x0000, bytes((0x08, 0x4D))),  # currently 125 Hz
            _write_ack(0x0000, bytes((0x01, 0x54))),  # acknowledge of the EEPROM write
            _read_reply(0x0000, bytes((0x01, 0x54))),  # readback
        ]
    )
    resets: list[Path] = []
    monkeypatch.setattr(compxctl, "HidrawSession", _session_factory(io))
    monkeypatch.setattr(compxctl, "reset_usb_device", lambda m, **k: resets.append(m.sysfs_dir))
    monkeypatch.setattr(compxctl, "wait_for_mouse", lambda name, **k: mouse)

    applied = compxctl.apply_rate(mouse, 1000, "device", persist=True)
    assert applied.eeprom_changed is True
    assert applied.reset == "ok"
    assert resets == [mouse.sysfs_dir]
    assert io.sent[0] == compxctl.LIVE_PACKETS[1000][0]


def test_applied_rate_to_host_needs_no_device(tmp_path: Path, monkeypatch) -> None:
    mouse = find_mouse(tmp_path)
    poll = tmp_path / "mousepoll"
    poll.write_text("0\n")
    monkeypatch.setattr(compxctl, "MOUSEPOLL_PATH", poll)
    monkeypatch.setattr(
        compxctl, "HidrawSession", lambda *a, **k: pytest.fail("the device must not be touched")
    )
    applied = compxctl.apply_rate(mouse, 500, "host", persist=False)
    assert (applied.host_interval_ms, applied.eeprom_changed) == (2, None)
    assert poll.read_text().strip() == "2"


def test_applied_rate_to_both_writes_both(tmp_path: Path, monkeypatch) -> None:
    mouse = find_mouse(tmp_path)
    poll = tmp_path / "mousepoll"
    poll.write_text("0\n")
    io = FakeHidrawIO(
        [
            _live_ack(0x01),
            None,
            _read_reply(0x0000, bytes((0x08, 0x4D))),
            _write_ack(0x0000, bytes((0x01, 0x54))),
            _read_reply(0x0000, bytes((0x01, 0x54))),
        ]
    )
    monkeypatch.setattr(compxctl, "MOUSEPOLL_PATH", poll)
    monkeypatch.setattr(compxctl, "HidrawSession", _session_factory(io))
    monkeypatch.setattr(compxctl, "reset_usb_device", lambda *a, **k: Path("/dev/bus/usb/003/002"))
    monkeypatch.setattr(compxctl, "wait_for_mouse", lambda name, **k: mouse)

    applied = compxctl.apply_rate(mouse, 1000, "both", persist=True)
    assert poll.read_text().strip() == "1"
    assert (applied.host_interval_ms, applied.eeprom_changed, applied.reset) == (1, True, "ok")


def test_applied_rate_refuses_the_device_without_persist(tmp_path: Path, monkeypatch) -> None:
    poll = tmp_path / "mousepoll"
    poll.write_text("0\n")
    monkeypatch.setattr(compxctl, "MOUSEPOLL_PATH", poll)
    with pytest.raises(RuntimeError, match="--persist"):
        compxctl.apply_rate(find_mouse(tmp_path), 1000, "both", persist=False)
    # `rate both` without --persist must refuse *before* touching the host:
    # a command that fails may not leave half of itself applied.
    assert poll.read_text().strip() == "0"


def test_applied_rate_does_not_reset_when_nothing_changed(tmp_path: Path, monkeypatch) -> None:
    """The EEPROM already holds it: no write, and no reason to drop the bus."""
    mouse = find_mouse(tmp_path)
    io = FakeHidrawIO([_live_ack(0x01), None, _read_reply(0x0000, bytes((0x01, 0x54)))])
    monkeypatch.setattr(compxctl, "HidrawSession", _session_factory(io))
    monkeypatch.setattr(
        compxctl, "reset_usb_device", lambda *a, **k: pytest.fail("must not reset")
    )
    applied = compxctl.apply_rate(mouse, 1000, "device", persist=True)
    assert (applied.eeprom_changed, applied.reset) == (False, "skipped")


def test_applied_json_separates_the_action_from_the_state() -> None:
    payload = compxctl.applied_json(_applied())
    assert payload["eeprom_code"] == "0x01"
    assert payload["reset"] == "ok"
    assert payload["requested_rate_hz"] == 1000
    assert compxctl.applied_json(_applied(reset="skipped", eeprom_changed=None))["eeprom_code"] is None


def test_applied_text_says_what_the_host_now_advertises(tmp_path: Path) -> None:
    text = " ".join(compxctl.applied_text(_applied(), find_mouse(tmp_path)))
    assert "Device EEPROM updated to 1000 Hz" in text
    assert "re-enumerated" in text
    assert "1000 Hz (1ms, ep_81)" in text


def test_applied_text_explains_a_descriptor_that_does_not_follow(tmp_path: Path) -> None:
    """Measured on the verified unit: the stored code does not move bInterval."""
    text = " ".join(compxctl.applied_text(_applied(rate_hz=500), find_mouse(tmp_path)))
    assert "does not change the endpoint" in text


def test_applied_text_asks_for_a_replug_when_the_reset_failed(tmp_path: Path) -> None:
    text = " ".join(
        compxctl.applied_text(_applied(reset="failed", reset_error="no permission"), find_mouse(tmp_path))
    )
    assert "Re-plug" in text
    assert "no permission" in text


def test_applied_text_is_quiet_when_the_reset_was_skipped(tmp_path: Path) -> None:
    """Nothing was written, so there is nothing to re-plug for either."""
    text = compxctl.applied_text(_applied(reset="skipped"), find_mouse(tmp_path))
    assert all("reset" not in line.lower() for line in text)
    assert all("Re-plug" not in line for line in text)


def test_applied_text_asks_for_the_replug_with_no_reset(tmp_path: Path) -> None:
    text = " ".join(compxctl.applied_text(_applied(reset="manual"), find_mouse(tmp_path)))
    assert "Re-plug the mouse" in text


def test_applied_text_warns_when_the_device_stays_away(tmp_path: Path) -> None:
    """A device that never came back is reported as missing, not as its old self."""
    text = " ".join(compxctl.applied_text(_applied(), None))
    assert "has not come back" in text


# ==========================================================================
# CLI and commands
# ==========================================================================


@pytest.mark.parametrize("rate", [125, 500, 1000])
def test_parser_rate_targets(rate: int) -> None:
    args = compxctl.build_parser().parse_args(["rate", "host", str(rate)])
    assert (args.target, args.hz, args.func) == ("host", rate, compxctl.cmd_rate)


def test_parser_rate_alone_means_show() -> None:
    args = compxctl.build_parser().parse_args(["rate"])
    assert args.target is None and args.hz is None


@pytest.mark.parametrize("bad", ["250", "8000", "0"])
def test_parser_rate_rejects_unsupported_rates(bad: str) -> None:
    with pytest.raises(SystemExit):
        compxctl.build_parser().parse_args(["rate", "host", bad])


def test_parser_rejects_a_target_without_a_rate() -> None:
    """`rate host` alone is a user error, not a silent no-op."""
    with pytest.raises(SystemExit) as excinfo:
        compxctl.main(["rate", "host"])
    assert excinfo.value.code == 2


def test_parser_knows_the_json_and_no_reset_flags() -> None:
    args = compxctl.build_parser().parse_args(["rate", "device", "500", "--persist", "--json", "--no-reset"])
    assert (args.json, args.no_reset) == (True, True)
    for command in ("status", "battery", "dpi"):
        assert compxctl.build_parser().parse_args([command, "--json"]).json is True


def test_parser_dpi_accepts_the_list_literal() -> None:
    assert compxctl.build_parser().parse_args(["dpi", "list"]).value == "list"
    assert compxctl.build_parser().parse_args(["dpi"]).value is None


def test_parser_dpi_rejects_non_numeric() -> None:
    with pytest.raises(SystemExit):
        compxctl.build_parser().parse_args(["dpi", "turbo"])


def test_bare_invocation_shows_the_rate(monkeypatch, capsys, tmp_path: Path) -> None:
    _mouse_for(monkeypatch, tmp_path)
    monkeypatch.setattr(compxctl, "read_mousepoll", lambda path=None: 0)
    assert compxctl.main([]) == 0
    out = capsys.readouterr().out
    assert "1000 Hz" in out
    assert "Host override: none" in out


def test_rate_host_writes_mousepoll(monkeypatch, capsys, tmp_path: Path) -> None:
    poll = tmp_path / "mousepoll"
    poll.write_text("0\n")
    _mouse_for(monkeypatch, tmp_path)
    monkeypatch.setattr(compxctl, "MOUSEPOLL_PATH", poll)
    assert compxctl.main(["rate", "host", "500"]) == 0
    assert poll.read_text().strip() == "2"
    assert "all USB mice" in capsys.readouterr().out


def test_rate_device_refuses_without_persist(monkeypatch, capsys, tmp_path: Path) -> None:
    """The EEPROM write must never happen by accident."""
    _mouse_for(monkeypatch, tmp_path)

    def fail(*args, **kwargs):
        raise AssertionError("the device must not be touched without --persist")

    monkeypatch.setattr(compxctl, "HidrawSession", fail)
    assert compxctl.main(["rate", "device", "1000"]) == 1
    assert "--persist" in capsys.readouterr().err


def test_no_mouse_found_is_reported(monkeypatch, capsys) -> None:
    monkeypatch.setattr(compxctl, "find_mouses", lambda *a, **k: [])
    assert compxctl.main(["rate"]) == 1
    assert "No CompX mouse found" in capsys.readouterr().err


def test_dpi_without_a_valid_active_level_refuses_to_guess(monkeypatch, capsys, tmp_path: Path) -> None:
    """The old version silently wrote slot 0; this one asks for --slot."""
    writes: list[str] = []

    class FakeSession:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def active_dpi_level(self):
            return (0x00, 0x55)

        def dpi_slots(self):
            raise AssertionError("must not read slots before resolving the target")

        def write_dpi(self, slot, code):
            writes.append("write")

    mouse = _mouse_for(monkeypatch, tmp_path)
    monkeypatch.setattr(compxctl, "HidrawSession", FakeSession)
    assert compxctl.main(["dpi", "800"]) == 1
    assert writes == []
    assert "Pass --slot" in capsys.readouterr().err


def test_dpi_refuses_to_overwrite_an_extended_slot(monkeypatch, capsys, tmp_path: Path) -> None:
    class FakeSession:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def active_dpi_level(self):
            return (0x07, 0x4E)

        def write_dpi(self, slot, code):
            raise AssertionError("must not write")

    _mouse_for(monkeypatch, tmp_path)
    monkeypatch.setattr(compxctl, "HidrawSession", FakeSession)
    assert compxctl.main(["dpi", "800"]) == 1
    assert "extended" in capsys.readouterr().err


def test_dpi_slot_out_of_range_is_rejected_before_io(monkeypatch, capsys, tmp_path: Path) -> None:
    class FakeSession:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    _mouse_for(monkeypatch, tmp_path)
    monkeypatch.setattr(compxctl, "HidrawSession", FakeSession)
    assert compxctl.main(["dpi", "--slot", "9", "800"]) == 1
    assert "slot must be 0..5" in capsys.readouterr().err


def test_probe_rejects_a_window_outside_config_memory(monkeypatch, capsys, tmp_path: Path) -> None:
    class FakeSession:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(compxctl, "HidrawSession", FakeSession)
    assert compxctl.main(["probe", "--start", "0xFFFF", "--length", "8"]) == 1
    assert "leaves the config memory" in capsys.readouterr().err


def test_battery_text_marks_charging_correctly() -> None:
    assert compxctl._battery_text(100, False, 2) == "Battery: 100% (not charging) [2.4G mode]"
    assert compxctl._battery_text(42, True, 2) == "Battery: 42% (charging) [2.4G mode]"
    assert compxctl._battery_text(7, True, 0x7F) == "Battery: 7% (charging)"


def test_slot_text_covers_empty_plain_and_extended() -> None:
    assert compxctl._slot_text({"x": 0xFF, "y": 0xFF, "mul": 0xFF, "crc": 0xFF, "dpi": None}) == "empty"
    assert compxctl._slot_text({"x": 0x13, "y": 0x13, "mul": 0x00, "crc": 0x2F, "dpi": 1000}) == "1000"
    assert (
        compxctl._slot_text({"x": 0x7B, "y": 0x7B, "mul": 0x44, "crc": 0x00, "dpi": None})
        == "raw:0x7B(mul=0x44)"
    )


def test_dpi_table_text_marks_the_active_slot() -> None:
    slots = [
        {"index": 0, "addr": 0x0C, "x": 0x07, "y": 0x07, "mul": 0, "crc": 0x47, "crc_ok": True, "dpi": 400},
        {"index": 1, "addr": 0x10, "x": 0x13, "y": 0x13, "mul": 0, "crc": 0x2F, "crc_ok": True, "dpi": 1000},
    ]
    lines = compxctl._dpi_table_text(slots, (1, 0x54), 0)
    assert lines[0].startswith("  slot  addr")
    assert lines[1].startswith("  *0   0x000C")
    assert lines[3] == "Active DPI level: slot 0 (index 0x01)"


def test_dpi_table_text_reports_an_unknown_active_level() -> None:
    lines = compxctl._dpi_table_text([], None, None)
    assert lines[-1] == "Active DPI level: unknown (0x0004 unset)"
    lines = compxctl._dpi_table_text([], (0xFF, 0x00), None)
    assert lines[-1] == "Active DPI level: unknown (0x0004 holds 0xFF)"


# ==========================================================================
# --json end to end
# ==========================================================================


def test_battery_json_on_stdout(monkeypatch, capsys, tmp_path: Path) -> None:
    _mouse_for(monkeypatch, tmp_path)
    io = FakeHidrawIO([_reply(b"\x09\x04\x00\x00\x00", b"\x02\x64\x00")])
    monkeypatch.setattr(compxctl, "HidrawSession", _session_factory(io))
    assert compxctl.main(["battery", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "battery"
    assert doc["battery"] == {"percent": 100, "charging": False, "state": 2, "state_label": "2.4G mode"}
    assert doc["schema"] == compxctl.JSON_SCHEMA


def test_status_json_reports_what_failed_instead_of_dying(monkeypatch, capsys, tmp_path: Path) -> None:
    """A status bar needs a document even when one readout fails.

    The failing readout answers with the wrong register, so the session knows
    frames are arriving and keeps asking: the battery is still read.
    """
    _mouse_for(monkeypatch, tmp_path)
    io = FakeHidrawIO(
        [
            _read_reply(0x0000, bytes((0x01,))),  # stored rate: a one-byte read
            _read_reply(0x0004, bytes((0x01, 0x54))),  # active DPI level
            *[_read_reply(0x0008, bytes((0x00, 0x00, 0x00, 0x00)))] * compxctl.READ_ATTEMPTS,
            _reply(b"\x09\x04\x00\x00\x00", b"\x02\x64\x00"),  # battery still answered
        ]
    )
    monkeypatch.setattr(compxctl, "read_mousepoll", lambda path=None: 0)
    monkeypatch.setattr(compxctl, "HidrawSession", _session_factory(io))
    assert compxctl.main(["status", "--json"]) == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["stored_rate_hz"] == 1000
    assert doc["active_dpi"] == {"index": 1, "slot": 0, "dpi": None}
    assert doc["dpi_slots"] == []
    assert doc["battery"]["percent"] == 100
    assert doc["warnings"] and "DPI slots" in doc["warnings"][0]
    assert captured.err == ""  # warnings belong in the document


def test_status_json_skips_the_rest_of_a_silent_device(monkeypatch, capsys, tmp_path: Path) -> None:
    """A mouse that is switched off costs one retry budget, not four.

    Everything after the first silent readout is skipped and said so, which is
    what keeps `status --json` usable in a status bar when the mouse is asleep.
    """
    _mouse_for(monkeypatch, tmp_path)
    io = FakeHidrawIO(
        [
            _read_reply(0x0000, bytes((0x01,))),  # stored rate
            _read_reply(0x0004, bytes((0x01, 0x54))),  # active DPI level
            *([None] * compxctl.READ_ATTEMPTS),  # the device goes quiet here
        ]
    )
    monkeypatch.setattr(compxctl, "read_mousepoll", lambda path=None: 0)
    monkeypatch.setattr(compxctl, "HidrawSession", _session_factory(io))
    assert compxctl.main(["status", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["stored_rate_hz"] == 1000
    assert doc["dpi_slots"] == []
    assert doc["battery"] is None  # never asked: the device had stopped answering
    assert any("stopped answering" in warning for warning in doc["warnings"])
    # Four attempts for the silent readout, and nothing sent after it.
    assert len(io.sent) == 2 + compxctl.READ_ATTEMPTS


def test_status_text_warns_on_stderr(monkeypatch, capsys, tmp_path: Path) -> None:
    _mouse_for(monkeypatch, tmp_path)
    io = FakeHidrawIO([None] * compxctl.READ_ATTEMPTS)
    monkeypatch.setattr(compxctl, "read_mousepoll", lambda path=None: 0)
    monkeypatch.setattr(compxctl, "HidrawSession", _session_factory(io))
    assert compxctl.main(["status"]) == 0
    captured = capsys.readouterr()
    assert "Stored rate: n/a" in captured.out
    assert "warning: stored rate" in captured.err


def test_dpi_list_json(monkeypatch, capsys, tmp_path: Path) -> None:
    _mouse_for(monkeypatch, tmp_path)
    block = bytes((0x07, 0x07, 0x00, 0x47, 0x13, 0x13, 0x00, 0x2F))
    io = FakeHidrawIO(
        [
            _read_reply(0x000C, block),
            _read_reply(0x0014, block),
            _read_reply(0x001C, block),
            _read_reply(0x0024, block),
            _read_reply(0x0004, bytes((0x01, 0x54))),
        ]
    )
    monkeypatch.setattr(compxctl, "HidrawSession", _session_factory(io))
    assert compxctl.main(["dpi", "list", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "dpi"
    assert doc["active_dpi"]["slot"] == 0
    assert len(doc["dpi_slots"]) == 8
    assert doc["dpi_slots"][0]["dpi"] == 400


def test_dpi_write_json_reports_the_previous_value(monkeypatch, capsys, tmp_path: Path) -> None:
    _mouse_for(monkeypatch, tmp_path)
    block = bytes((0x07, 0x07, 0x00, 0x47, 0x13, 0x13, 0x00, 0x2F))
    io = FakeHidrawIO(
        [
            _read_reply(0x000C, block),
            _read_reply(0x0014, block),
            _read_reply(0x001C, block),
            _read_reply(0x0024, block),
            None,  # the write itself
            _write_ack(0x000C, bytes((0x13, 0x13, 0x00, 0x2F))),
            _read_reply(0x000C, bytes((0x13, 0x13, 0x00, 0x2F))),
        ]
    )
    monkeypatch.setattr(compxctl, "HidrawSession", _session_factory(io))
    assert compxctl.main(["dpi", "--slot", "0", "1000", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["write"]["slot"] == 0
    assert doc["write"]["dpi"] == 1000
    assert doc["write"]["verified"] is True
    assert doc["write"]["previous"]["dpi"] == 400


def test_rate_show_json(monkeypatch, capsys, tmp_path: Path) -> None:
    _mouse_for(monkeypatch, tmp_path)
    monkeypatch.setattr(compxctl, "read_mousepoll", lambda path=None: 0)
    assert compxctl.main(["rate", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "rate"
    assert doc["polling"]["effective_rate_hz"] == 1000


def test_rate_apply_json_carries_the_action_and_the_state(monkeypatch, capsys, tmp_path: Path) -> None:
    mouse = _mouse_for(monkeypatch, tmp_path)
    io = FakeHidrawIO(
        [
            _live_ack(0x01),
            None,
            _read_reply(0x0000, bytes((0x08, 0x4D))),
            _write_ack(0x0000, bytes((0x01, 0x54))),
            _read_reply(0x0000, bytes((0x01, 0x54))),
        ]
    )
    monkeypatch.setattr(compxctl, "HidrawSession", _session_factory(io))
    monkeypatch.setattr(compxctl, "read_mousepoll", lambda path=None: 0)
    monkeypatch.setattr(compxctl, "reset_usb_device", lambda *a, **k: Path("/dev/bus/usb/003/002"))
    monkeypatch.setattr(compxctl, "wait_for_mouse", lambda name, **k: mouse)
    assert compxctl.main(["rate", "device", "1000", "--persist", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["applied"]["eeprom_changed"] is True
    assert doc["applied"]["reset"] == "ok"
    assert doc["polling"]["advertised_rate_hz"] == 1000


def test_json_error_is_a_document_too(monkeypatch, capsys) -> None:
    """`--json` prints exactly one parseable document, error or not."""
    monkeypatch.setattr(compxctl, "find_mouses", lambda *a, **k: [])
    assert compxctl.main(["status", "--json"]) == 1
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["command"] == "status"
    assert "No CompX mouse found" in doc["error"]["message"]
    assert captured.err == ""


def test_plain_error_goes_to_stderr(monkeypatch, capsys) -> None:
    monkeypatch.setattr(compxctl, "find_mouses", lambda *a, **k: [])
    assert compxctl.main(["status"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Error: ")
