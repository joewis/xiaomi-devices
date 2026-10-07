#!/usr/bin/env python3
"""MCP server exposing a Xiaomi/Dreame vacuum as a shared agent tool.

A THIN WRAPPER over the xiaomi_devices library. The library holds the behaviour — device
protocol, fault table, map decoding, room resolution — and this file only adapts it to MCP.
It lives in the gateway's backend directory because that is where the gateway expects backend
programs; the substance is not here.

Non-interactive by construction: it imports only the local half of the library, so it cannot
reach the cloud login and cannot block on a human. Cloud work (map fetch) is a separate,
interactive command line.
"""

import json
import sys

from mcp.server.mcpserver import MCPServer

from xiaomi_devices import config, device as dev, faults, rooms

server = MCPServer("xiaomi-vacuum", "1.0.0")


def _j(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _describe_fault(values) -> dict:
    return faults.describe(values.get("error_code"))


@server.tool(
    name="vacuum_status",
    description=(
        "Get the robot vacuum's current state: cleaning/docked/returning, battery and charge "
        "state, suction and water level, cleaning progress, any fault code, and remaining "
        "consumable life. Use for 'how is the vacuum', 'is the vacuum done', 'is the vacuum "
        "stuck', 'what is the vacuum doing', 'does the filter need changing'."
    ),
)
async def vacuum_status() -> str:
    """Current state, battery, fault and consumables."""
    try:
        vac, ip = dev.connect()
    except dev.DeviceError as exc:
        return _j({"reachable": False, "error": str(exc)})

    values, failed = dev.read_all(vac)
    mode = values.get("mode")
    fault = _describe_fault(values)
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
        out["note"] = (f"the device reports a fault: {fault['message']} "
                       f"(code {fault['code']}, {fault['subsystem']})")
    return _j(out)


@server.tool(
    name="vacuum_where",
    description=(
        "Find the robot vacuum on the network: its address, and whether it is awake or asleep. "
        "Use for 'where is the vacuum', 'is the vacuum awake', 'what address is the vacuum on'."
    ),
)
async def vacuum_where() -> str:
    """Network position and awake/asleep state."""
    ip, token, reason = config.device()
    out = {"address": ip, "reachable": False}
    if not ip or not token:
        out["error"] = reason
        return _j(out)

    found = dev.wake()
    out["reachable"] = bool(found)
    if not found:
        out["note"] = (
            "no reply to a broadcast hello. This device deep-sleeps when docked, so asleep, "
            "off-dock and powered off all look the same from here."
        )
    return _j(out)


@server.tool(
    name="vacuum_capabilities",
    description=(
        "List what this robot vacuum can and cannot do — which actions it supports, whether it "
        "can clean a specific room by name, and how its map is obtained. Use when asked what "
        "the vacuum is capable of, or whether room-targeted cleaning is possible."
    ),
)
async def vacuum_capabilities() -> str:
    """Static statement of this device family's capabilities."""
    room_map, room_reason = config.rooms()
    return _j({
        "available": {
            "status": "state, battery, charge state, faults, progress, consumables",
            "lifetime": "runs, time and area cleaned",
            "control": "start, pause/stop, dock, locate (beep), fan speed, water level",
            "room_cleaning": (
                "yes — clean named rooms, via the device's own segment ids"
                if room_map else
                f"supported by the device, but no room map is configured here: {room_reason}"
            ),
        },
        "not_available": {
            "live_local_map": (
                "the device will not serve map bytes over its local API. The map comes from "
                "the account's cloud as a snapshot, which suits a floor plan that does not "
                "move. A live map needs the device rooted."
            ),
            "position": (
                "no live position is reported locally, so a fault tells you that the device "
                "stopped, not where it is."
            ),
        },
        "notes": [
            "this device deep-sleeps when docked; a failed connect usually means asleep.",
            "'stop' and 'pause' are the same action on this model — there is no hard stop.",
            "an accepted command is not a successful one: a fault can appear seconds later, "
            "so motion calls report whether the run actually started.",
        ],
    })


@server.tool(
    name="vacuum_rooms",
    description=(
        "List the rooms this vacuum can be sent to clean by name. Use when asked which rooms "
        "can be cleaned, or which name to use for targeted cleaning."
    ),
)
async def vacuum_rooms() -> str:
    """The room names available for targeted cleaning."""
    return _j(rooms.describe())


@server.tool(
    name="vacuum_control",
    description=(
        "Control the robot vacuum: start, pause, stop, send it to its dock, make it beep, or "
        "change suction and water level. Use for 'start the vacuum', 'send it home', 'stop the "
        "vacuum', 'find the vacuum', 'make it quieter'. Note that stop and pause are the same "
        "action on this model, and that the reply says whether the command actually took "
        "effect."
    ),
)
async def vacuum_control(action: str, value: str = "") -> str:
    """Send a control action; the reply reports whether it took effect."""
    action = (action or "").strip().lower()
    value = (value or "").strip().lower()

    try:
        vac, ip = dev.connect()
    except dev.DeviceError as exc:
        return _j({"action": action, "sent": False, "error": str(exc)})

    try:
        result = dev.send_action(vac, action, value)
    except Exception as exc:
        return _j({"action": action, "sent": False,
                   "error": f"{type(exc).__name__}: {exc}"})
    result["ip"] = ip
    return _j(result)


@server.tool(
    name="vacuum_clean_rooms",
    description=(
        "Send the robot vacuum to clean one or more NAMED ROOMS and nothing else. Use this "
        "rather than a whole-floor start when a specific room is asked for, e.g. 'clean the "
        "living room', 'do the master bedroom'. It is more precise and uses less battery. An "
        "unrecognised room name is refused before anything is sent."
    ),
)
async def vacuum_clean_rooms(rooms_arg: str) -> str:
    """Clean named rooms. Refuses unknown names rather than guessing."""
    segment_ids, reason = rooms.resolve(rooms_arg)
    if reason:
        return _j({"action": "clean_rooms", "sent": False, "error": reason})

    try:
        vac, ip = dev.connect()
    except dev.DeviceError as exc:
        return _j({"action": "clean_rooms", "sent": False, "error": str(exc)})

    try:
        result = dev.send_action(vac, "clean_segments",
                                 ",".join(str(s) for s in segment_ids))
    except Exception as exc:
        return _j({"action": "clean_rooms", "sent": False,
                   "error": f"{type(exc).__name__}: {exc}"})

    known, _names, _reason = rooms.load()
    display = {v: k for k, v in known.items()}
    result["rooms_requested"] = [r.strip() for r in rooms_arg.split(",") if r.strip()]
    result["segments"] = [{"id": s, "name": display.get(s, str(s))} for s in segment_ids]
    result["ip"] = ip
    return _j(result)


if __name__ == "__main__":
    server.run()
