#!/usr/bin/env python3
"""Non-interactive CLI: read and control a Xiaomi vacuum on the LAN.

Never prompts, never reads stdin. Safe to run from a service, a script, or a cron job.

    xiaomi-ctl status
    xiaomi-ctl rooms
    xiaomi-ctl control start
    xiaomi-ctl control clean_rooms "<room name>"
    xiaomi-ctl locate
"""

import argparse
import json
import sys

from . import config, faults, rooms
from . import device as dev


def _emit(payload) -> int:
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_status(args) -> int:
    try:
        vac, ip = dev.connect()
    except dev.DeviceError as exc:
        return _emit({"reachable": False, "error": str(exc)})

    values, failed = dev.read_all(vac)
    mode = values.get("mode")
    fault = faults.describe(values.get("error_code"))
    out = {
        "reachable": True,
        "ip": ip,
        "state": dev.MODE_MAP.get(mode, f"unknown ({mode})"),
        "raw_mode": mode,
        "battery_pct": values.get("battery_level"),
        "charging": dev.CHARGING_MAP.get(values.get("battery_charging"),
                                         values.get("battery_charging")),
        "fault": fault,
        "fan_speed": dev.FAN_MAP.get(values.get("fan_speed"), values.get("fan_speed")),
        "water_level": dev.WATER_MAP.get(values.get("water_usage"), values.get("water_usage")),
        "water_tank_attached": values.get("water_tank") == 1,
        "current_clean": {"time_min": values.get("cleaning_time"),
                          "area_m2": values.get("cleaning_area")},
        "lifetime": {"time_min": values.get("total_time"),
                     "runs": values.get("total_count"),
                     "area_m2": values.get("total_area")},
        "consumables": dev.consumables(values),
    }
    if failed:
        out["unavailable_fields"] = failed
    if fault:
        out["note"] = f"the device reports a fault: {fault['message']} (code {fault['code']})"
    return _emit(out)


def cmd_rooms(args) -> int:
    return _emit(rooms.describe())


def cmd_control(args) -> int:
    action = args.action
    value = " ".join(args.value) if args.value else ""

    # A room name is resolved here, before anything is sent to the device, so an unknown name
    # is an error rather than a guess.
    if action in ("clean_rooms", "clean-room", "cleanroom"):
        segment_ids, reason = rooms.resolve(value)
        if reason:
            return _emit({"action": action, "sent": False, "error": reason})
        action, value = "clean_segments", ",".join(str(s) for s in segment_ids)

    try:
        vac, ip = dev.connect()
    except dev.DeviceError as exc:
        return _emit({"action": action, "sent": False, "error": str(exc)})

    try:
        result = dev.send_action(vac, action, value)
    except Exception as exc:
        return _emit({"action": action, "sent": False,
                      "error": f"{type(exc).__name__}: {exc}"})
    result["ip"] = ip
    return _emit(result)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="xiaomi-ctl", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="current state, battery, faults, consumables")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("rooms", help="room names available for targeted cleaning")
    p.set_defaults(func=cmd_rooms)

    p = sub.add_parser("control", help="send a control action")
    p.add_argument("action", help="start, pause, stop, dock, locate, clean_rooms, "
                                  "set_fan_speed, set_water_level, clean_segments")
    p.add_argument("value", nargs="*", help="value or room name(s) for the action")
    p.set_defaults(func=cmd_control)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
