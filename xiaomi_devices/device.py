"""Local control of a Dreame/Xiaomi vacuum over miIO on the LAN.

This is the NON-INTERACTIVE half. Nothing in this module prompts, reads stdin, or can block
on a human — an always-on service is expected to import and call it directly.

Device behaviour that shapes this code:

* **The robot deep-sleeps when docked.** ARP failure, no ping, and no miIO reply while the
  router still lists it as associated all mean ASLEEP, not offline. A bare connect to a
  sleeping unit is indistinguishable from a dead one, so we broadcast a miIO hello and listen
  first.
* **Property reads must be batched.** A single large request times out on this hardware; the
  library's own cap is 10, and we batch at 10.
* **A failed property omits its 'value' key.** Code that assumes every reply carries 'value'
  will raise on a device that simply does not support a property, so replies are tolerated in
  both shapes.
* **An accepted command is not a successful one.** The robot can accept an action and fault
  out seconds later. Every motion call therefore reads the state back and reports whether the
  run actually started — see ``verify_started``.
"""

import json
import socket
import time
from typing import Dict, Optional, Tuple

from miio import DreameVacuum

from . import config, faults

# --- service map -----------------------------------------------------------------------
# These are this device family's own service ids, not a generic layout. SIID_VACUUM is the
# primary vacuum service; SIID_BATTERY carries charge state; SIID_ERROR carries the fault.
SIID_BATTERY = 2
PIID_BATTERY_LEVEL = 1
PIID_BATTERY_CHARGING = 2
SIID_VACUUM = 18
SIID_ERROR = 22
PIID_ERROR_CODE = 1
SIID_CONSUMABLE = 19
PIID_FAN_SPEED = 6
PIID_WATER_USAGE = 20
PIID_ADDITIONAL_CLEANUP = 21
AIID_START = 1
AIID_PAUSE = 2
AIID_LOCATE_SERVICE = 17
AIID_BATTERY_START_CHARGE = 1

# Property name -> (siid, piid)
PROPERTIES = {
    "mode":            (SIID_VACUUM, 1),
    "cleaning_time":   (SIID_VACUUM, 2),
    "cleaning_area":   (SIID_VACUUM, 3),
    "fan_speed":       (SIID_VACUUM, PIID_FAN_SPEED),
    "water_tank":      (SIID_VACUUM, 9),
    "total_time":      (SIID_VACUUM, 13),
    "total_count":     (SIID_VACUUM, 14),
    "total_area":      (SIID_VACUUM, 15),
    "task_status":     (SIID_VACUUM, 18),
    "water_usage":     (SIID_VACUUM, PIID_WATER_USAGE),
    "battery_level":   (SIID_BATTERY, PIID_BATTERY_LEVEL),
    "battery_charging": (SIID_BATTERY, PIID_BATTERY_CHARGING),
    "error_code":      (SIID_ERROR, PIID_ERROR_CODE),
    "consumable_filter":     (SIID_CONSUMABLE, 1),
    "consumable_side_brush": (SIID_CONSUMABLE, 2),
    "consumable_main_brush": (SIID_CONSUMABLE, 3),
}

MODE_MAP = {
    0: "idle", 1: "paused", 2: "cleaning", 3: "returning",
    4: "cleaning (segment)", 5: "cleaning", 6: "docked",
    7: "idle", 8: "idle", 9: "idle", 10: "idle", 11: "idle", 12: "idle",
    13: "manual control", 14: "idle (docked, low power)", 15: "idle", 16: "idle",
    17: "idle", 18: "cleaning (segment)", 19: "cleaning (zone)", 20: "cleaning (spot)",
    21: "moving (mapping)", 23: "moving (to target)",
}
CHARGING_MAP = {1: "charging", 2: "on battery", 4: "charged", 5: "returning to charger"}
FAN_MAP = {0: "quiet", 1: "standard", 2: "strong", 3: "max"}
WATER_MAP = {1: "low", 2: "medium", 3: "high"}
FAN_VALUES = {v: k for k, v in FAN_MAP.items()}
WATER_VALUES = {v: k for k, v in WATER_MAP.items()}

# The mode value that means "clean these specific segments" on this device family.
SEGMENT_MODE_ID = 18

HELLO = bytes.fromhex("21310020" + "ff" * 28)

# Actions that move the robot. Used to decide whether a state readback should be read as
# success or as a command that died on the spot.
MOTION_ACTIONS = ("start", "clean_rooms", "clean_segments")

# Segment select tuple: [segment_id, iterations, fan_speed, water_grade, order]
# The POSITIONAL ORDER is part of the device protocol and is not self-describing, which is
# why it is spelled out here rather than inlined at the call site.
SEGMENT_ITERATIONS = 1


class DeviceError(RuntimeError):
    """Raised when the device cannot be reached or a command cannot be sent."""


def wake(timeout: float = 45.0) -> Optional[str]:
    """Broadcast a miIO hello until the device answers. Returns its IP, or None.

    This is why a naive connect fails: a docked robot is asleep, and a single probe to a
    sleeping unit looks exactly like a probe to a device that is switched off.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(4)
        try:
            sock.sendto(HELLO, ("255.255.255.255", 54321))
            while True:
                data, addr = sock.recvfrom(2048)
                if data[:2] == b"\x21\x31":
                    return addr[0]
        except Exception:
            pass
        finally:
            sock.close()
        time.sleep(3)
    return None


def connect(ip: Optional[str] = None, token: Optional[str] = None, timeout: float = 5.0):
    """Return a connected device handle, waking it if necessary.

    With no explicit address/token, they are resolved from the environment or the local
    settings file. Raises DeviceError with a reason the caller can show a person.
    """
    if not ip or not token:
        cfg_ip, cfg_token, reason = config.device()
        ip = ip or cfg_ip
        token = token or cfg_token
        if not ip or not token:
            raise DeviceError(reason)

    found = wake()
    if not found:
        raise DeviceError(
            "device did not answer a broadcast miIO hello — it is asleep, off its dock, or "
            "powered off. This is not proof that it is broken."
        )
    return DreameVacuum(found, token, timeout=timeout), found


def read(device, names) -> Tuple[Dict[str, object], list]:
    """Read named properties, batching at 10. Returns (values, failed_names).

    A property the device does not support comes back without a 'value' key; that is a
    missing field, not an exception.
    """
    entries = [(n, PROPERTIES[n]) for n in names if n in PROPERTIES]
    got, failed = {}, []
    for i in range(0, len(entries), 10):
        chunk = entries[i:i + 10]
        payload = [{"did": n, "siid": s, "piid": p} for n, (s, p) in chunk]
        try:
            replies = device.send("get_properties", payload)
        except Exception:
            replies = []
        for reply in replies:
            if reply.get("code") == 0 and "value" in reply:
                got[reply.get("did")] = reply["value"]
            else:
                failed.append(reply.get("did"))
    return got, failed


def read_all(device) -> Tuple[Dict[str, object], list]:
    """Read every property this module knows about."""
    return read(device, list(PROPERTIES))


def snapshot(device) -> dict:
    """A small state readback: mode, battery, charge state, and any fault.

    The fault is included deliberately. Reporting only the mode makes a command that was
    accepted and then died look identical to one that succeeded.
    """
    got, _ = read(device, ["mode", "battery_level", "battery_charging", "error_code"])
    mode = got.get("mode")
    return {
        "state": MODE_MAP.get(mode, f"unknown ({mode})"),
        "raw_mode": mode,
        "battery_pct": got.get("battery_level"),
        "charging": CHARGING_MAP.get(got.get("battery_charging"), got.get("battery_charging")),
        "fault": faults.describe(got.get("error_code")),
    }


def verify_started(action: str, snap: dict) -> dict:
    """Did a command actually take effect, or did it die immediately?

    A device can accept an action and then report a fault seconds later, leaving the mode back
    at idle. A caller that checked only "was the command sent" would report a run that never
    happened, so this is the check that makes the answer honest.
    """
    if snap.get("fault"):
        f = snap["fault"]
        return {
            "confirmed": False,
            "why": (
                f"the device reports a fault right after the command: {f['message']} "
                f"(code {f['code']}, {f['subsystem']}). It accepted the command but the run "
                f"did not proceed."
            ),
        }
    if action in MOTION_ACTIONS and str(snap.get("state", "")).startswith("idle"):
        return {
            "confirmed": False,
            "why": (
                f"the device is '{snap.get('state')}' immediately after a motion command, so "
                f"it did not start moving."
            ),
        }
    return {"confirmed": True, "why": f"the device is '{snap.get('state')}'"}


def consumables(values: dict) -> dict:
    """Remaining life, parsed from the 'total-used' strings this firmware reports.

    The strings look like "18000-0" = 18000 units total, 0 used. Note that per-part percentage
    services read 0 on this firmware while these strings read as unused — when the two
    disagree, report the disagreement rather than picking the flattering number.
    """
    out = {}
    for name, part in (
        ("consumable_filter", "filter"),
        ("consumable_side_brush", "side_brush"),
        ("consumable_main_brush", "main_brush"),
    ):
        raw = values.get(name)
        if not isinstance(raw, str) or "-" not in raw:
            continue
        try:
            total_s, used_s = raw.split("-", 1)
            total, used = int(total_s), int(used_s)
        except ValueError:
            out[part] = {"raw": raw}
            continue
        remaining = max(total - used, 0)
        out[part] = {
            "remaining_pct": round(100.0 * remaining / total, 1) if total else None,
            "remaining_units": remaining,
            "total_units": total,
            "used_units": used,
        }
    return out


def segment_payload(segment_ids, fan: int = 1, water: int = 1) -> list:
    """Build the argument list for cleaning specific segments.

    Shape: [{"piid": mode, "value": 18}, {"piid": 21, "value": '{"selects": [[...]]}'}]
    """
    selects = [
        [int(sid), SEGMENT_ITERATIONS, int(fan), int(water), i + 1]
        for i, sid in enumerate(segment_ids)
    ]
    return [
        {"piid": 1, "value": SEGMENT_MODE_ID},
        {"piid": PIID_ADDITIONAL_CLEANUP, "value": json.dumps({"selects": selects})},
    ]


def send_action(device, action: str, value: str = "") -> dict:
    """Perform a control action and report whether it took effect.

    Supported: start, pause, stop, dock, locate, set_fan_speed, set_water_level,
    clean_segments (pass segment ids in ``value`` as a comma-separated list).
    """
    action = (action or "").strip().lower()
    value = (value or "").strip().lower()

    if action == "set_fan_speed":
        if value not in FAN_VALUES:
            return {"action": action, "sent": False,
                    "error": f"value must be one of {sorted(FAN_VALUES)}"}
        device.send("set_properties",
                    [{"did": "s", "siid": SIID_VACUUM, "piid": PIID_FAN_SPEED,
                      "value": FAN_VALUES[value]}])
    elif action == "set_water_level":
        if value not in WATER_VALUES:
            return {"action": action, "sent": False,
                    "error": f"value must be one of {sorted(WATER_VALUES)}"}
        device.send("set_properties",
                    [{"did": "s", "siid": SIID_VACUUM, "piid": PIID_WATER_USAGE,
                      "value": WATER_VALUES[value]}])
    elif action == "clean_segments":
        ids = [s for s in value.replace(" ", "").split(",") if s]
        if not ids or not all(s.isdigit() for s in ids):
            return {"action": action, "sent": False,
                    "error": "value must be a comma-separated list of segment ids"}
        device.send("action", {"did": f"call-{SIID_VACUUM}-{AIID_START}", "siid": SIID_VACUUM,
                               "aiid": AIID_START, "in": segment_payload(ids)})
    else:
        actions = {
            "start":  (SIID_VACUUM, AIID_START, [{"piid": 1, "value": 2}]),
            # stop and pause are the same action on this device family; there is no hard stop
            "pause":  (SIID_VACUUM, AIID_PAUSE, None),
            "stop":   (SIID_VACUUM, AIID_PAUSE, None),
            "dock":   (SIID_BATTERY, AIID_BATTERY_START_CHARGE, None),
            "locate": (AIID_LOCATE_SERVICE, AIID_START, None),
        }
        if action not in actions:
            return {"action": action, "sent": False,
                    "error": f"unknown action; valid: {sorted(list(actions) + ['set_fan_speed', 'set_water_level', 'clean_segments'])}"}
        siid, aiid, params = actions[action]
        payload = {"did": f"call-{siid}-{aiid}", "siid": siid, "aiid": aiid}
        payload["in"] = params if params is not None else []
        device.send("action", payload)

    time.sleep(2)  # give the device a moment to act before reading back
    snap = snapshot(device)
    out = {"action": action, "sent": True, "resulting_state": snap,
           "started": verify_started(action, snap)}
    if value:
        out["value"] = value
    if action == "stop":
        out["note"] = ("'stop' and 'pause' are the same action on this device; the robot halts "
                       "but does not return to the dock. Use 'dock' for that.")
    return out
