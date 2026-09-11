"""Tests for compxctl 2.0.

Two rules shape this suite:

* Anything that can be a pure function is tested as one.
* The device layer is driven through a fake pyusb object injected into
  UsbSession, so the suite needs no hardware, no pyusb and no root. The one
  thing it cannot fake is the real sysfs tree, which is covered separately by
  `find_mouses` against a synthetic tree built in tmp_path — including the
  hex-vs-decimal trap in USB descriptor attributes, which is a real bug this
  suite now locks down.
"""

from __future__ import annotations

import os
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


def make_sysfs_tree(
    root: Path,
    *,
    vendor: str = "25a7",
    product: str = "fa7b",
    speed: str = "12",
    interval: str | None = "1ms",
    b_interval: str = "01",
    endpoint_address: str = "81",
) -> Path:
    """A minimal stand-in for /sys/bus/usb/devices with one CompX mouse."""
    device = root / "3-2"
    _write(device / "idVendor", vendor)
    _write(device / "idProduct", product)
    _write(device / "product", "2.4G Dual Mode Mouse")
    _write(device / "speed", speed)

    interface = root / "3-2:1.0"
    _write(interface / "bInterfaceClass", "03")
    _write(interface / "bInterfaceSubClass", "01")
    _write(interface / "bInterfaceProtocol", "02")
    endpoint = interface / "ep_81"
    _write(endpoint / "bmAttributes", "03")
    _write(endpoint / "bEndpointAddress", endpoint_address)
    _write(endpoint / "bInterval", b_interval)
    if interval is not None:
        _write(endpoint / "interval", interval)

    config = root / "3-2:1.1"
    _write(config / "bInterfaceClass", "03")
    _write(config / "bInterfaceSubClass", "01")
    _write(config / "bInterfaceProtocol", "01")

    _write(root / "1-1" / "idVendor", "1234")
    _write(root / "1-1" / "idProduct", "5678")
    return root


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
    root = make_sysfs_tree(tmp_path)
    mouses = compxctl.find_mouses(root)
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
    assert [m.product for m in compxctl.find_mouses(root / "nonexistent")] == []


def test_find_mouses_prefers_the_boot_mouse_interface(tmp_path: Path) -> None:
    """The config interface (protocol 01) must not be mistaken for the mouse."""
    root = make_sysfs_tree(tmp_path)
    mouse = compxctl.find_mouses(root)[0]
    assert mouse.interface_dir.name == "3-2:1.0"


def test_find_mouses_falls_back_to_binterval_when_interval_is_absent(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path, interval=None, b_interval="04", speed="12")
    endpoint = compxctl.find_mouses(root)[0].endpoint
    assert endpoint is not None
    assert endpoint.interval_source == "bInterval"
    assert endpoint.rate_hz == 250


def test_find_mouses_reports_unknown_interval(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path, interval=None)
    (root / "3-2:1.0" / "ep_81" / "bInterval").unlink()
    endpoint = compxctl.find_mouses(root)[0].endpoint
    assert endpoint is not None
    assert endpoint.interval_source == "unknown"
    assert endpoint.rate_hz is None


@pytest.mark.parametrize("pid", ["fa7b", "fa7c", "fa03", "fa93"])
def test_find_mouses_accepts_every_supported_pid(tmp_path: Path, pid: str) -> None:
    root = make_sysfs_tree(tmp_path, product=pid)
    assert compxctl.find_mouses(root)[0].product == int(pid, 16)


def test_find_mouses_skips_interface_without_interrupt_endpoint(tmp_path: Path) -> None:
    root = make_sysfs_tree(tmp_path)
    endpoint = root / "3-2:1.0" / "ep_81"
    _write(endpoint / "bmAttributes", "02")  # bulk, not interrupt
    mouse = compxctl.find_mouses(root)[0]
    assert mouse.endpoint is None
    assert mouse.rate_hz is None


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
    mouse = compxctl.find_mouses(make_sysfs_tree(tmp_path))[0]
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


@pytest.mark.parametrize("payload", [b"\x01\x54", b"\x13\x13\x00\x2f", b"\x00" * 10])
def test_parse_read_reply_slices_by_length_byte(payload: bytes) -> None:
    reply = _read_reply(0x0000, payload)
    assert compxctl.parse_read_reply(reply) == payload


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
# USB layer over a fake pyusb
# ==========================================================================


class FakeUSBError(Exception):
    def __init__(self, message: str, errno: int | None = None) -> None:
        super().__init__(message)
        self.errno = errno


class FakeDevice:
    """Records SET_REPORT packets and replays scripted interrupt-IN replies."""

    def __init__(self, replies: list[bytes | None], transfer_result: int | None = None) -> None:
        self.sent: list[bytes] = []
        self.replies = list(replies)
        self.transfer_result = transfer_result
        self.timeouts: list[int] = []

    def ctrl_transfer(self, **kwargs) -> int:
        packet = bytes(kwargs["data_or_wLength"])
        self.sent.append(packet)
        return len(packet) if self.transfer_result is None else self.transfer_result

    def read(self, address, size, timeout=None) -> bytes:
        self.timeouts.append(timeout)
        if not self.replies:
            raise AssertionError("fake device ran out of scripted replies")
        reply = self.replies.pop(0)
        if reply is None:
            raise FakeUSBError("timeout", errno=compxctl.errno.ETIMEDOUT)
        return reply


class FakeUtil:
    ENDPOINT_IN = 0x80
    ENDPOINT_TYPE_INTR = 0x03

    @staticmethod
    def endpoint_direction(address: int) -> int:
        return address & 0x80

    @staticmethod
    def endpoint_type(attributes: int) -> int:
        return attributes & 0x03

    @staticmethod
    def find_descriptor(interface, custom_match):
        for endpoint in interface:
            if custom_match(endpoint):
                return endpoint
        return None


class FakeEndpoint:
    bEndpointAddress = 0x81
    bmAttributes = 0x03


class FakeCore:
    USBError = FakeUSBError


def make_session(device: FakeDevice) -> compxctl.UsbSession:
    """A UsbSession with the fake pyusb objects injected, no hardware."""
    session = compxctl.UsbSession()
    session._device = device
    session._usb_core = FakeCore
    session._usb_util = FakeUtil
    return session


def _install_endpoint(session: compxctl.UsbSession, device: FakeDevice) -> None:
    device.get_active_configuration = lambda: {(1, 0): [FakeEndpoint()]}


def test_session_read_returns_payload(tmp_path: Path) -> None:
    device = FakeDevice([_read_reply(0x0000, b"\x01\x54")])
    session = make_session(device)
    _install_endpoint(session, device)
    assert session.read(0x0000, 2) == b"\x01\x54"
    assert device.sent == [compxctl.build_read_frame(0x0000, 2)]


def test_session_read_does_not_retry_a_timeout() -> None:
    device = FakeDevice([None])
    session = make_session(device)
    _install_endpoint(session, device)
    with pytest.raises(RuntimeError, match="timed out"):
        session.read(0x0000, 2)
    assert len(device.sent) == 1


def test_session_read_drains_a_stale_frame_then_succeeds() -> None:
    stale = _reply(b"\x09\x08\x00\xab\xcd\x02", b"\x00\x00")
    device = FakeDevice([stale, _read_reply(0x0000, b"\x01\x54")])
    session = make_session(device)
    _install_endpoint(session, device)
    assert session.read(0x0000, 2) == b"\x01\x54"
    assert len(device.sent) == 2


def test_session_read_gives_up_after_read_attempts() -> None:
    stale = _reply(b"\x09\x08\x00\xab\xcd\x02", b"\x00\x00")
    device = FakeDevice([stale] * compxctl.READ_ATTEMPTS)
    session = make_session(device)
    _install_endpoint(session, device)
    with pytest.raises(RuntimeError, match="retried"):
        session.read(0x0000, 2)
    assert len(device.sent) == compxctl.READ_ATTEMPTS


def test_session_send_rejects_a_short_transfer() -> None:
    device = FakeDevice([], transfer_result=3)
    session = make_session(device)
    with pytest.raises(OSError, match="rejected"):
        session.send(b"\x06\x11\x00\x02\x00\x00\x00\x00")


def test_session_battery_maps_reply_fields() -> None:
    device = FakeDevice([_reply(b"\x09\x04\x00\x00\x00", b"\x02\x64\x00")])
    session = make_session(device)
    _install_endpoint(session, device)
    assert session.battery() == (100, False, 2)
    assert device.sent == [compxctl.build_battery_request()]


def test_session_battery_rejects_an_impossible_percentage() -> None:
    """A garbage reply must fail loudly instead of printing 250%."""
    device = FakeDevice([_reply(b"\x09\x04\x00\x00\x00", b"\x02\xFA\x00")])
    session = make_session(device)
    _install_endpoint(session, device)
    with pytest.raises(RuntimeError, match="impossible percentage"):
        session.battery()


def test_session_write_dpi_verifies_all_four_bytes() -> None:
    payload = bytes((0x13, 0x13, 0x00, 0x2F))
    device = FakeDevice([_read_reply(0x000C, payload)])
    session = make_session(device)
    _install_endpoint(session, device)
    session.write_dpi(0, 0x13)
    # one write, then one readback of the same four bytes
    assert device.sent == [
        compxctl.build_write_frame(0x000C, payload),
        compxctl.build_read_frame(0x000C, 4),
    ]


def test_session_write_dpi_detects_a_partial_readback() -> None:
    """The old version only compared x; a wrong y slipped through as "verified"."""
    device = FakeDevice([_read_reply(0x000C, bytes((0x13, 0x99, 0x00, 0x2F)))])
    session = make_session(device)
    _install_endpoint(session, device)
    with pytest.raises(RuntimeError, match="Readback mismatch"):
        session.write_dpi(0, 0x13)


def test_session_persist_rate_skips_when_already_stored() -> None:
    device = FakeDevice([_read_reply(0x0000, bytes((0x01, 0x54)))])
    session = make_session(device)
    _install_endpoint(session, device)
    assert session.persist_rate(1000) is False
    assert device.sent == [compxctl.build_read_frame(0x0000, 2)]


def test_session_persist_rate_writes_and_reads_back() -> None:
    device = FakeDevice(
        [
            _read_reply(0x0000, bytes((0x08, 0x4D))),  # currently 125 Hz
            _read_reply(0x0000, bytes((0x01, 0x54))),  # after the write
        ]
    )
    session = make_session(device)
    _install_endpoint(session, device)
    assert session.persist_rate(1000) is True
    assert device.sent[1] == compxctl.eeprom_packet(1000)


def test_session_persist_rate_reports_a_failed_readback() -> None:
    device = FakeDevice(
        [
            _read_reply(0x0000, bytes((0x08, 0x4D))),
            _read_reply(0x0000, bytes((0x08, 0x4D))),  # write did not stick
        ]
    )
    session = make_session(device)
    _install_endpoint(session, device)
    with pytest.raises(RuntimeError, match="EEPROM readback mismatch"):
        session.persist_rate(1000)


def test_persist_rate_refuses_a_frame_that_drifts_from_the_capture(monkeypatch) -> None:
    """The EEPROM write is the one irreversible thing here; it must be guarded."""
    device = FakeDevice([_read_reply(0x0000, bytes((0x08, 0x4D)))])
    session = make_session(device)
    _install_endpoint(session, device)
    monkeypatch.setattr(compxctl, "eeprom_packet", lambda rate: b"\x00" * 17)
    with pytest.raises(AssertionError, match="verified capture"):
        session.persist_rate(1000)
    assert len(device.sent) == 1  # only the read; nothing was written


def test_apply_live_rate_sends_the_interval_packets_only() -> None:
    device = FakeDevice([])
    session = make_session(device)
    compxctl.apply_live_rate(session, 500)
    assert device.sent == list(compxctl.LIVE_PACKETS[500])
    assert len(device.sent) == 2  # never the EEPROM packet


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


def test_parser_dpi_accepts_the_list_literal() -> None:
    assert compxctl.build_parser().parse_args(["dpi", "list"]).value == "list"
    assert compxctl.build_parser().parse_args(["dpi"]).value is None


def test_parser_dpi_rejects_non_numeric() -> None:
    with pytest.raises(SystemExit):
        compxctl.build_parser().parse_args(["dpi", "turbo"])


def test_bare_invocation_shows_the_rate(monkeypatch, capsys, tmp_path: Path) -> None:
    mouse = compxctl.find_mouses(make_sysfs_tree(tmp_path))[0]
    monkeypatch.setattr(compxctl, "find_mouses", lambda: [mouse])
    monkeypatch.setattr(compxctl, "read_mousepoll", lambda path=None: 0)
    assert compxctl.main([]) == 0
    out = capsys.readouterr().out
    assert "1000 Hz" in out
    assert "Host override: none" in out


def test_rate_host_writes_mousepoll(monkeypatch, capsys, tmp_path: Path) -> None:
    mouse = compxctl.find_mouses(make_sysfs_tree(tmp_path))[0]
    poll = tmp_path / "mousepoll"
    poll.write_text("0\n")
    monkeypatch.setattr(compxctl, "find_mouses", lambda: [mouse])
    monkeypatch.setattr(compxctl, "MOUSEPOLL_PATH", poll)
    assert compxctl.main(["rate", "host", "500"]) == 0
    assert poll.read_text().strip() == "2"
    assert "all USB mice" in capsys.readouterr().out


def test_rate_device_refuses_without_persist(monkeypatch, capsys, tmp_path: Path) -> None:
    """The EEPROM write must never happen by accident."""
    mouse = compxctl.find_mouses(make_sysfs_tree(tmp_path))[0]
    monkeypatch.setattr(compxctl, "find_mouses", lambda: [mouse])

    def fail():
        raise AssertionError("the device must not be touched without --persist")

    monkeypatch.setattr(compxctl, "UsbSession", fail)
    assert compxctl.main(["rate", "device", "1000"]) == 1
    assert "--persist" in capsys.readouterr().err


def test_no_mouse_found_is_reported(monkeypatch, capsys) -> None:
    monkeypatch.setattr(compxctl, "find_mouses", lambda: [])
    assert compxctl.main(["rate"]) == 1
    assert "No CompX mouse found" in capsys.readouterr().err


def test_dpi_without_a_valid_active_level_refuses_to_guess(monkeypatch, capsys) -> None:
    """The old version silently wrote slot 0; this one asks for --slot."""
    calls: list[str] = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def active_dpi_level(self):
            return (0x00, 0x55)

        def dpi_slots(self):
            raise AssertionError("must not read slots before resolving the target")

        def write_dpi(self, slot, code):
            calls.append("write")

    monkeypatch.setattr(compxctl, "UsbSession", FakeSession)
    assert compxctl.main(["dpi", "800"]) == 1
    assert calls == []
    assert "Pass --slot" in capsys.readouterr().err


def test_dpi_refuses_to_overwrite_an_extended_slot(monkeypatch, capsys) -> None:
    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def active_dpi_level(self):
            return (0x07, 0x4E)

        def write_dpi(self, slot, code):
            raise AssertionError("must not write")

    monkeypatch.setattr(compxctl, "UsbSession", FakeSession)
    assert compxctl.main(["dpi", "800"]) == 1
    assert "extended" in capsys.readouterr().err


def test_dpi_slot_out_of_range_is_rejected_before_io(monkeypatch, capsys) -> None:
    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(compxctl, "UsbSession", FakeSession)
    assert compxctl.main(["dpi", "--slot", "9", "800"]) == 1
    assert "slot must be 0..5" in capsys.readouterr().err


def test_probe_rejects_a_window_outside_config_memory(monkeypatch, capsys) -> None:
    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(compxctl, "UsbSession", FakeSession)
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
