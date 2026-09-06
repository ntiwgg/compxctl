"""Unit tests for compxctl.py — pure protocol helpers only, no hardware.

The module imports hidapi/pyusb/evdev lazily (they are needed only inside the
device-touching commands), so importing it needs nothing but the stdlib. These
tests therefore run in a clean environment and never open a mouse: they cover
the checksum identity, the write/read frame builders, the read-reply parser,
the EEPROM packet builder against the known-good captures, the DPI codec, hex
argument parsing and the argparse command contract.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

# Make `import compxctl` work no matter where pytest was launched from: the
# module lives at the repository root (it is installed by `pip install .` in
# CI, but a plain `pytest` run from elsewhere needs the source on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import compxctl  # noqa: E402


# ==========================================================================
# Checksum identity and the known-good frames
# ==========================================================================


@pytest.mark.parametrize("rate", sorted(compxctl._KNOWN_GOOD_EEPROM_PACKETS))
def test_known_good_eeprom_packet_is_17_bytes(rate: int) -> None:
    """Every known-good EEPROM capture is a full 17-byte config frame."""
    packet = compxctl._KNOWN_GOOD_EEPROM_PACKETS[rate]
    assert len(packet) == compxctl.PROBE_FRAME_BYTES


@pytest.mark.parametrize("rate", sorted(compxctl._KNOWN_GOOD_EEPROM_PACKETS))
def test_known_good_eeprom_packet_sums_to_0x55(rate: int) -> None:
    """Known-good captures obey the 0x55 checksum identity."""
    packet = compxctl._KNOWN_GOOD_EEPROM_PACKETS[rate]
    assert compxctl._verify_frame(packet)


@pytest.mark.parametrize("rate", sorted(compxctl._KNOWN_GOOD_EEPROM_PACKETS))
def test_compx_checksum_returns_known_good_tail(rate: int) -> None:
    """_compx_checksum over the 16 leading bytes equals the stored tail."""
    packet = compxctl._KNOWN_GOOD_EEPROM_PACKETS[rate]
    assert compxctl._compx_checksum(packet[:-1]) == packet[-1]


def test_compx_checksum_identity_holds_for_any_body() -> None:
    """Appending _compx_checksum(body) always makes the frame sum to 0x55."""
    for body in (b"", b"\x00", b"\x08\x04" + b"\x00" * 14, os.urandom(16)):
        frame = body + bytes((compxctl._compx_checksum(body),))
        assert (sum(frame) & 0xFF) == 0x55
        assert compxctl._verify_frame(frame)


def test_verify_frame_detects_corruption() -> None:
    """A single flipped byte breaks the 0x55 identity."""
    frame = bytearray(compxctl._KNOWN_GOOD_EEPROM_PACKETS[1000])
    frame[8] ^= 0xFF
    assert not compxctl._verify_frame(bytes(frame))


# ==========================================================================
# EEPROM packet builder vs. the known-good captures
# ==========================================================================


@pytest.mark.parametrize("rate", sorted(compxctl._KNOWN_GOOD_EEPROM_PACKETS))
def test_eeprom_packet_matches_known_good(rate: int) -> None:
    """_eeprom_packet must reproduce the hardware-verified capture byte for byte."""
    built = compxctl._eeprom_packet(compxctl.INTERVAL_CODE_BY_RATE[rate])
    assert built == compxctl._KNOWN_GOOD_EEPROM_PACKETS[rate]


def test_eeprom_packet_writes_only_rate_pair_at_0x0000() -> None:
    """The EEPROM frame carries exactly two payload bytes (rate + complement)."""
    for rate in compxctl.POLLING_VARIANTS:
        packet = compxctl._eeprom_packet(compxctl.INTERVAL_CODE_BY_RATE[rate])
        code = compxctl.INTERVAL_CODE_BY_RATE[rate]
        # 08 07 00 + AH AL LN(=2) + rate byte + complement + pad + tail.
        assert packet[0:6] == b"\x08\x07\x00\x00\x00\x02"
        assert packet[6] == code
        assert packet[7] == (0x55 - code) & 0xFF
        assert packet[6:8] == bytes((code, 0x55 - code))


@pytest.mark.parametrize("rate", sorted(compxctl.POLLING_VARIANTS))
def test_polling_variant_eeprom_packet_sums_to_0x55(rate: int) -> None:
    """The third packet of every rate variant is the checksummed EEPROM write."""
    packet = compxctl.POLLING_VARIANTS[rate][compxctl.EEPROM_PACKET_INDEX]
    assert len(packet) == compxctl.PROBE_FRAME_BYTES
    assert compxctl._verify_frame(packet)
    assert packet == compxctl._KNOWN_GOOD_EEPROM_PACKETS[rate]


def test_verify_packet_generation_passes_on_known_good() -> None:
    """The COMPX_SELFCHECK verifier finds no drift in the current packets."""
    compxctl._verify_packet_generation()  # raises AssertionError on drift


def test_verify_battery_request_passes_on_known_good() -> None:
    """The COMPX_SELFCHECK verifier accepts the current battery request."""
    assert compxctl._build_battery_request() == compxctl._KNOWN_GOOD_BATTERY_REQUEST
    assert compxctl._verify_frame(compxctl._build_battery_request())
    compxctl._verify_battery_request()  # raises AssertionError on drift


# ==========================================================================
# Frame builders
# ==========================================================================


def test_build_write_frame_returns_17_bytes_sums_to_0x55() -> None:
    """Every write frame is a full 17-byte frame satisfying the 0x55 identity."""
    frame = compxctl._build_write_frame(0x0000, b"\x08\x4d")
    assert len(frame) == compxctl.PROBE_FRAME_BYTES
    assert compxctl._verify_frame(frame)
    assert compxctl._compx_checksum(frame[:-1]) == frame[-1]


def test_build_write_frame_encodes_address_length_and_payload() -> None:
    """Header bytes AH/AL/LN and the payload land at the documented offsets."""
    frame = compxctl._build_write_frame(0x1234, b"\xab\xcd")
    # 08 07 00 AH AL LN payload pad...
    assert frame[:3] == b"\x08\x07\x00"
    assert frame[3] == 0x12  # AH = high byte of the big-endian address
    assert frame[4] == 0x34  # AL = low byte
    assert frame[5] == 0x02  # LN = payload length
    assert frame[6:8] == b"\xab\xcd"
    assert frame[8:16] == b"\x00" * 8  # pad to 16 leading bytes


def test_build_write_frame_is_eeprom_packet_for_rate_pair() -> None:
    """_eeprom_packet delegates to _build_write_frame for the 2-byte pair."""
    frame = compxctl._build_write_frame(0x0000, b"\x01\x54")
    assert frame == compxctl._eeprom_packet(compxctl.INTERVAL_CODE_BY_RATE[1000])


@pytest.mark.parametrize("bad_payload", [b"", b"\x00" * 11])
def test_build_write_frame_rejects_out_of_range_payload(bad_payload: bytes) -> None:
    """Payloads outside 1..10 bytes are impossible config writes."""
    with pytest.raises(ValueError):
        compxctl._build_write_frame(0x0000, bad_payload)


def test_build_read_frame_layout_and_identity() -> None:
    """The read command is 08 08 00 AH AL LN + pad, summing to 0x55."""
    frame = compxctl._build_read_frame(0x000C, 8)
    assert len(frame) == compxctl.PROBE_FRAME_BYTES
    assert frame[:3] == b"\x08\x08\x00"
    assert frame[3:6] == b"\x00\x0c\x08"
    assert frame[6:16] == b"\x00" * 10
    assert compxctl._verify_frame(frame)
    assert compxctl._compx_checksum(frame[:-1]) == frame[-1]


@pytest.mark.parametrize("bad_length", [0, 11, 100])
def test_build_read_frame_rejects_length_over_ceiling(bad_length: int) -> None:
    """The firmware ceiling is 10 bytes; longer read commands are refused."""
    with pytest.raises(ValueError):
        compxctl._build_read_frame(0x0000, bad_length)


def test_build_read_frame_allows_ceiling_length() -> None:
    """A 10-byte read is the maximum allowed by the firmware."""
    frame = compxctl._build_read_frame(0x0000, 10)
    assert frame[5] == 10
    assert compxctl._verify_frame(frame)


def _read_reply(addr: int, payload: bytes) -> bytes:
    """Build a synthetic firmware reply: 09 08 00 AH AL LN + payload + tail."""
    body = (
        b"\x09\x08\x00"
        + bytes(((addr >> 8) & 0xFF, addr & 0xFF, len(payload)))
        + payload
        + b"\x00" * (compxctl.PROBE_FRAME_BYTES - 7 - len(payload))
    )
    return body + bytes((compxctl._compx_checksum(body),))


@pytest.mark.parametrize("payload", [b"\x01\x54", b"\x13\x13\x00\x2f", b"\x01", b"\x00" * 10])
def test_parse_read_reply_extracts_payload(payload: bytes) -> None:
    """LN says how long the payload is; only that slice is returned."""
    reply = _read_reply(0x0000, payload)
    assert len(reply) == compxctl.PROBE_FRAME_BYTES
    assert compxctl._verify_frame(reply)
    assert compxctl._parse_read_reply(reply) == payload


def test_parse_read_reply_requires_full_frame() -> None:
    """A short frame is refused loudly instead of being mis-parsed."""
    with pytest.raises(ValueError):
        compxctl._parse_read_reply(b"\x09\x08\x00\x00\x00\x02\x01\x54")


def test_parse_read_reply_payload_length_tracked() -> None:
    """LN = 4 returns 4 bytes even when trailing bytes would look like data."""
    reply = _read_reply(0x000C, b"\x13\x13\x00\x2f")
    parsed = compxctl._parse_read_reply(reply)
    assert len(parsed) == 4
    assert parsed == reply[6:10]


@pytest.mark.parametrize("bad_ln", [0x00, 0x0B, 0x40])
def test_parse_read_reply_rejects_invalid_length_byte(bad_ln: int) -> None:
    """A full frame whose LN byte lies outside 1..10 is refused loudly.

    LN says how many payload bytes follow, and the firmware ceiling is 10
    (PROBE_MAX_READ_BYTES); 0x00 or anything past 0x0A can never be a real
    reply, so the parser must not trust the slice.
    """
    reply = bytearray(_read_reply(0x0000, b"\x01\x54"))
    reply[5] = bad_ln
    with pytest.raises(ValueError):
        compxctl._parse_read_reply(bytes(reply))


# ==========================================================================
# DPI codec
# ==========================================================================


@pytest.mark.parametrize(
    ("dpi", "code"),
    [
        (400, 0x07),
        (800, 0x0F),
        (1000, 0x13),
        (1600, 0x1F),
        (2400, 0x2F),
        (6000, 0x77),
        (6400, 0x7F),
    ],
)
def test_dpi_code(dpi: int, code: int) -> None:
    """The stored code is (dpi / 50) - 1."""
    assert compxctl.dpi_code(dpi) == code


@pytest.mark.parametrize("bad_dpi", [0, 123, 6500, 6450, 25, -400])
def test_dpi_code_rejects_unrepresentable_values(bad_dpi: int) -> None:
    """Values outside a 50-multiple in 50..6400 cannot be encoded."""
    assert compxctl.dpi_code(bad_dpi) is None


@pytest.mark.parametrize("code", [0x07, 0x0F, 0x13, 0x1F, 0x2F, 0x77, 0x7F])
def test_dpi_round_trip_decode(code: int) -> None:
    """decode(code) undoes encode: (code + 1) x 50."""
    dpi = compxctl.dpi_decode(code)
    assert dpi is not None
    assert compxctl.dpi_code(dpi) == code


def test_dpi_decode_extended_encoding_returns_none() -> None:
    """Codes above 0x7F and mul != 0 are not understood yet -> None."""
    assert compxctl.dpi_decode(0xEF) is None  # extended code
    assert compxctl.dpi_decode(0x80) is None  # just past the plain ceiling
    assert compxctl.dpi_decode(0x07, mul=0x44) is None  # extended multiplier


def test_dpi_codec_round_trip_matches_hardware_table() -> None:
    """Every hardware-confirmed (dpi, code) pair survives the codec."""
    for dpi, code in compxctl._DPI_CODEC_ROUND_TRIPS:
        assert compxctl.dpi_code(dpi) == code
        assert compxctl.dpi_decode(code) == dpi


def test_verify_dpi_codec_passes() -> None:
    """The COMPX_SELFCHECK codec verifier is happy with the current codec."""
    compxctl._verify_dpi_codec()  # raises AssertionError on drift


# ==========================================================================
# Hex argument parsing
# ==========================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0x000C", 12), ("0x10", 16), ("000C", 12), ("0xff", 255), ("10", 16)],
)
def test_parse_hex_arg(text: str, expected: int) -> None:
    """Hex CLI values parse to the documented integers."""
    assert compxctl._parse_hex_arg(text) == expected


@pytest.mark.parametrize("bad_text", ["zz", "0xZZ", "12.5", ""])
def test_parse_hex_arg_rejects_invalid_text(bad_text: str) -> None:
    """Non-hex input is refused loudly at the argparse boundary."""
    with pytest.raises(argparse.ArgumentTypeError):
        compxctl._parse_hex_arg(bad_text)


def test_parser_rejects_invalid_probe_hex_start() -> None:
    """A bad --start never reaches cmd_probe; argparse exits with an error."""
    with pytest.raises(SystemExit):
        compxctl.build_parser().parse_args(["probe", "--start", "not-hex"])


# ==========================================================================
# CLI contract (argparse only — never touches hardware)
# ==========================================================================


@pytest.mark.parametrize("rate", [125, 500, 1000])
def test_parser_set_accepts_known_rates(rate: int) -> None:
    """`set` accepts exactly the three supported polling rates."""
    args = compxctl.build_parser().parse_args(["set", str(rate)])
    assert args.func == compxctl.cmd_set
    assert args.rate == rate


@pytest.mark.parametrize("bad_rate", [0, 250, 999, 8000])
def test_parser_set_rejects_unknown_rates(bad_rate: int) -> None:
    """Rates outside POLLING_VARIANTS are refused by argparse."""
    with pytest.raises(SystemExit):
        compxctl.build_parser().parse_args(["set", str(bad_rate)])


def test_parser_dpi_bare_lists() -> None:
    """Bare `dpi` and `dpi list` both mean the listing command."""
    bare = compxctl.build_parser().parse_args(["dpi"])
    listed = compxctl.build_parser().parse_args(["dpi", "list"])
    assert bare.func == compxctl.cmd_dpi and listed.func == compxctl.cmd_dpi
    assert bare.value is None
    assert listed.value == "list"


def test_parser_dpi_accepts_numeric_value() -> None:
    """`dpi 800` parses the positional as an integer, not the literal list."""
    args = compxctl.build_parser().parse_args(["dpi", "800"])
    assert args.value == 800


def test_parser_dpi_rejects_non_numeric_value() -> None:
    """A DPI value that is neither 'list' nor an integer exits non-zero."""
    with pytest.raises(SystemExit):
        compxctl.build_parser().parse_args(["dpi", "turbo"])


def test_parser_empty_argv_has_no_command() -> None:
    """A bare `compxctl` selects no subparser: main() then falls back to status."""
    args = compxctl.build_parser().parse_args([])
    assert args.command is None
    assert not hasattr(args, "func")


# ==========================================================================
# main() dispatch — the selected cmd_* is replaced, so no device is touched
# ==========================================================================


def _record_call(namespace: list, args: argparse.Namespace) -> int:
    namespace.append(args)
    return 0


def test_main_empty_argv_dispatches_to_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """No subcommand -> main() runs cmd_status, never the device layer."""
    called: list[argparse.Namespace] = []
    monkeypatch.setattr(compxctl, "cmd_status", lambda args: _record_call(called, args))
    assert compxctl.main([]) == 0
    assert len(called) == 1


def test_main_dispatches_set_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """`set 1000` routes to cmd_set with the parsed rate."""
    called: list[argparse.Namespace] = []
    monkeypatch.setattr(compxctl, "cmd_set", lambda args: _record_call(called, args))
    assert compxctl.main(["set", "1000"]) == 0
    assert len(called) == 1
    assert called[0].rate == 1000


def test_main_dispatches_dpi_list_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """`dpi list` routes to cmd_dpi with the literal 'list' value."""
    called: list[argparse.Namespace] = []
    monkeypatch.setattr(compxctl, "cmd_dpi", lambda args: _record_call(called, args))
    assert compxctl.main(["dpi", "list"]) == 0
    assert len(called) == 1
    assert called[0].value == "list"


def test_main_selfcheck_runs_verifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """COMPX_SELFCHECK=1 makes main() run the packet verifiers first.

    The verifiers are proven reachable by making one of them fail loudly.
    """
    monkeypatch.setenv("COMPX_SELFCHECK", "1")
    called: list[argparse.Namespace] = []
    monkeypatch.setattr(compxctl, "cmd_status", lambda args: _record_call(called, args))

    def boom() -> None:
        raise AssertionError("verifier reached")

    monkeypatch.setattr(compxctl, "_verify_packet_generation", boom)
    with pytest.raises(AssertionError, match="verifier reached"):
        compxctl.main([])
    assert called == []  # the verifier aborted before any dispatch


def test_main_selfcheck_passes_on_known_good(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With healthy builders the self-check runs and the command still executes."""
    monkeypatch.setenv("COMPX_SELFCHECK", "1")
    called: list[argparse.Namespace] = []
    monkeypatch.setattr(compxctl, "cmd_status", lambda args: _record_call(called, args))
    assert compxctl.main([]) == 0
    assert len(called) == 1


# ==========================================================================
# main() error handling and cmd_dpi validation (no device is touched)
# ==========================================================================


def _fail_if_device_touched(*args, **kwargs):
    raise AssertionError("command must fail before touching the device")


def test_main_dpi_rejects_slot_outside_writable_range(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`dpi --slot 9` exits 1 with an error, before any device I/O.

    Writable slots are 0..DPI_WRITE_SLOT_MAX, so slot 9 fails at the
    boundary even though the DPI value itself is encodable.
    """
    monkeypatch.setattr(compxctl, "read_dpi_slots", _fail_if_device_touched)
    assert compxctl.main(["dpi", "--slot", "9", "800"]) == 1
    assert compxctl.DPI_SLOT_ERROR_TEXT in capsys.readouterr().err


def test_main_dpi_rejects_value_outside_codec_range(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`dpi 6500` exits 1 with an error, before any device I/O.

    6500 is a multiple of 50 but beyond the 0x7F code ceiling, so dpi_code
    refuses it at the codec boundary.
    """
    monkeypatch.setattr(compxctl, "read_dpi_slots", _fail_if_device_touched)
    assert compxctl.main(["dpi", "6500"]) == 1
    assert compxctl.DPI_VALUE_ERROR_TEXT in capsys.readouterr().err


def test_main_catches_unexpected_oserror(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An OSError escaping a command is reported cleanly, never a traceback.

    main() is the last line of defence for transport/evdev OSErrors that a
    command did not wrap itself.
    """
    def boom(args: argparse.Namespace) -> int:
        raise OSError("backend exploded")

    monkeypatch.setattr(compxctl, "cmd_status", boom)
    assert compxctl.main([]) == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "backend exploded" in err


# ==========================================================================
# Readout text helpers
# ==========================================================================


def test_battery_text_known_state_appends_link_label() -> None:
    """A known link state (0x02 = 2.4G mode) gets its bracketed label."""
    assert (
        compxctl._battery_text(100, False, 0x02)
        == "Battery: 100% (not charging) [2.4G mode]"
    )
    assert (
        compxctl._battery_text(42, True, 0x02)
        == "Battery: 42% (charging) [2.4G mode]"
    )


def test_battery_text_unknown_state_omits_link_label() -> None:
    """An unrecognised link state adds no bracketed label."""
    assert (
        compxctl._battery_text(7, True, 0x7F)
        == "Battery: 7% (charging)"
    )


def test_slot_value_text_covers_empty_plain_and_extended() -> None:
    """Slot values render as empty / plain DPI / raw code with mul."""
    empty = {"x": 0xFF, "y": 0xFF, "mul": 0xFF, "crc": 0xFF, "dpi": None}
    plain = {"x": 0x13, "y": 0x13, "mul": 0x00, "crc": 0x2F, "dpi": 1000}
    extended = {"x": 0x7B, "y": 0x7B, "mul": 0x44, "crc": 0x00, "dpi": None}
    assert compxctl._slot_value_text(empty) == "empty"
    assert compxctl._slot_value_text(plain) == "1000"
    assert compxctl._slot_value_text(extended) == "raw:0x7B(mul=0x44)"
