"""Tests for the pure logic — no network, no device, no credentials.

Everything here runs offline. The map tests use a synthetic archive built the same way the
device builds one, so the decoder is exercised end to end without shipping anybody's floor
plan in the repo.
"""

import base64
import json
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xiaomi_devices import faults, mapfile  # noqa: E402

# --- fault table ------------------------------------------------------------------------


def test_no_fault_is_none():
    assert faults.describe(0) is None
    assert faults.describe("0") is None
    assert faults.describe(None) is None
    assert faults.describe("") is None


def test_stuck_or_trapped():
    d = faults.describe(faults.STUCK_OR_TRAPPED)
    assert d["code"] == 32
    assert d["message"] == "Robot stuck or trapped"
    assert d["subsystem"] == "navigation"


def test_wheel_codes_are_distinct_from_navigation():
    # 1 and 6 are the wheel-specific codes; 32 is the generic navigation one. Keeping these
    # distinguishable matters, because a report that conflates them misdescribes the fault.
    assert faults.describe(1)["subsystem"] == "wheel"
    assert faults.describe(6)["subsystem"] == "wheel"
    assert faults.describe(32)["subsystem"] == "navigation"


def test_unknown_code_still_describes():
    d = faults.describe(9999)
    assert d["code"] == 9999
    assert "9999" in d["message"]


def test_no_severity_word_that_minimises():
    # The word "transient" is banned from this project's vocabulary: it reads as "that
    # happens sometimes, it's ok". This guards the table against it coming back.
    for code, (message, _subsystem) in faults.FAULTS.items():
        assert "transient" not in message.lower(), f"code {code} uses the banned word"


# --- segment payload --------------------------------------------------------------------


def test_segment_payload_shape():
    from xiaomi_devices import device

    payload = device.segment_payload([4], fan=1, water=1)
    assert payload[0] == {"piid": 1, "value": device.SEGMENT_MODE_ID}
    selects = json.loads(payload[1]["value"])["selects"]
    # [segment_id, iterations, fan_speed, water_grade, order] — order is part of the protocol
    assert selects == [[4, 1, 1, 1, 1]]


def test_segment_payload_multiple_rooms_keep_order():
    from xiaomi_devices import device

    payload = device.segment_payload([5, 2])
    selects = json.loads(payload[1]["value"])["selects"]
    assert selects == [[5, 1, 1, 1, 1], [2, 1, 1, 1, 2]]


# --- command verification ---------------------------------------------------------------


def test_verify_started_reports_failure_on_fault():
    """A command that faults immediately must NOT be reported as started."""
    from xiaomi_devices import device

    snap = {"state": "idle", "fault": faults.describe(32)}
    verdict = device.verify_started("clean_rooms", snap)
    assert verdict["confirmed"] is False
    assert "did not proceed" in verdict["why"]


def test_verify_started_accepts_a_running_cleaning():
    from xiaomi_devices import device

    snap = {"state": "cleaning (segment)", "fault": None}
    assert device.verify_started("clean_rooms", snap)["confirmed"] is True


def test_verify_started_flags_idle_after_motion():
    from xiaomi_devices import device

    snap = {"state": "idle", "fault": None}
    assert device.verify_started("start", snap)["confirmed"] is False


def test_verify_started_does_not_judge_non_motion_actions():
    from xiaomi_devices import device

    snap = {"state": "idle", "fault": None}
    assert device.verify_started("set_fan_speed", snap)["confirmed"] is True


# --- consumables ------------------------------------------------------------------------


def test_consumables_parse_total_used():
    from xiaomi_devices import device

    out = device.consumables({"consumable_main_brush": "18000-4500"})
    assert out["main_brush"]["remaining_units"] == 13500
    assert out["main_brush"]["remaining_pct"] == 75.0


def test_consumables_surface_unparseable_values():
    """A value that cannot be parsed is reported raw, not silently dropped.

    Surfacing it matters: silently omitting a consumable reads as "nothing to report", while
    the truth is "something came back that we did not understand".
    """
    from xiaomi_devices import device

    out = device.consumables({"consumable_main_brush": "not-a-pair"})
    assert out == {"main_brush": {"raw": "not-a-pair"}}


def test_consumables_skip_missing_entries():
    from xiaomi_devices import device

    assert device.consumables({}) == {}


# --- map decoding -----------------------------------------------------------------------
# Build an archive in the device's own format: base64(url-safe) of zlib of
# [27-byte header][pixels][json]. A synthetic 4x3 map with two rooms and one wall pixel.


def _build_archive(pixels, width, height, payload):
    header = bytearray(27)
    struct.pack_into("<h", header, 0, 1)     # map_index
    header[4] = mapfile.FRAME_FULL           # frame_type
    struct.pack_into("<h", header, 17, 50)   # pixel size mm
    struct.pack_into("<h", header, 19, width)
    struct.pack_into("<h", header, 21, height)
    blob = bytes(header) + bytes(pixels) + json.dumps(payload).encode()
    return base64.b64encode(zlib.compress(blob)).replace(b"/", b"_").replace(b"+", b"-")


def _segment_pixel(segment):
    """A pixel carrying a segment id in the outer layer's encoding."""
    return (segment << mapfile.SEGMENT_SHIFT) & 0xFF


def test_decode_outer_layer_segments():
    width, height = 4, 3
    # segments 1 and 2 on the first two pixels, the rest floor
    pixels = [_segment_pixel(1), _segment_pixel(2), 1, 1,
              1, 1, 1, 1,
              1, 1, 1, 1]
    raw = _build_archive(pixels, width, height, {})
    decoded, reason = mapfile.rooms_from_map(raw)
    assert reason == ""
    assert set(decoded["rooms"]) == {1, 2}
    assert decoded["layer"] == "outer"


def test_decode_rejects_delta_frame():
    width, height = 2, 2
    header = bytearray(27)
    header[4] = mapfile.FRAME_DELTA
    struct.pack_into("<h", header, 17, 50)
    struct.pack_into("<h", header, 19, width)
    struct.pack_into("<h", header, 21, height)
    blob = bytes(header) + bytes([1, 1, 1, 1]) + b"{}"
    raw = base64.b64encode(zlib.compress(blob))
    _, reason = mapfile.rooms_from_map(raw)
    assert "partial frame" in reason


def test_decode_reads_room_names_from_seg_inf():
    """The nested rism layer carries the labels; they must come through."""
    width, height = 2, 2
    rism_pixels = [1, 2, 1, 2]
    name = base64.b64encode("Kitchen".encode()).decode()
    rism = _build_archive(rism_pixels, width, height,
                          {"seg_inf": {"1": {"name": name}}})
    outer_pixels = [1, 1, 1, 1]
    raw = _build_archive(outer_pixels, width, height,
                         {"rism": rism.decode() if isinstance(rism, bytes) else rism})
    decoded, reason = mapfile.rooms_from_map(raw)
    assert reason == ""
    assert decoded["layer"] == "rism"
    assert decoded["rooms"][1]["name"] == "Kitchen"


def test_suggest_rooms_drops_unnamed_by_default():
    width, height = 2, 1
    rism_pixels = [1, 2]
    named = base64.b64encode("Hall".encode()).decode()
    rism = _build_archive(rism_pixels, width, height,
                          {"seg_inf": {"1": {"name": named}}})
    raw = _build_archive([1, 1], width, height,
                         {"rism": rism.decode() if isinstance(rism, bytes) else rism})
    suggested, reason = mapfile.suggest_rooms(raw)
    assert reason == ""
    assert suggested["aliases"] == {"hall": 1}
    # the unnamed segment is still recorded as a display name, just not addressable
    assert suggested["names"]["2"] == "segment 2"


if __name__ == "__main__":
    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n  {len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
