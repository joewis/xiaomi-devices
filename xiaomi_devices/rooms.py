"""Speaking a room name instead of a segment number.

The device is told to clean a room by its own segment id. Those ids come from the device's
map, and a person refers to rooms by name, so something has to hold the mapping.

WHY THIS IS LOCAL, NOT IN THIS REPO: a segment id is only meaningful for one home. Shipping a
mapping would ship somebody's floor plan, and room labels are personal. So the repo carries
the FORMAT; the mapping lives in the user's own config file (``~/.config/xiaomi-devices/
rooms.json`` by default, or ``$XIAOMI_DEVICES_DIR/rooms.json``).

File format::

    {
      "aliases": {
        "kitchen": 1,
        "study": 2,
        "upstairs": 3
      },
      "names": {          # optional, for display only
        "1": "Kitchen",
        "2": "Study"
      }
    }

Anything that reads a room name goes through here, so the lookup is one implementation with
the cases as arguments rather than a branch per caller. Unknown names fail closed: the caller
gets an error listing what is known, and nothing is sent to the device.
"""

from typing import Dict, Optional, Tuple

from . import config


def load() -> Tuple[Dict[str, int], Dict[str, str], str]:
    """(aliases, display_names, reason_if_absent)."""
    data, reason = config.rooms()
    if reason:
        return {}, {}, reason

    raw_aliases = data.get("aliases") or {}
    aliases = {}
    for name, seg in raw_aliases.items():
        try:
            aliases[name.strip().lower()] = int(seg)
        except (TypeError, ValueError):
            continue

    names = {str(k): str(v) for k, v in (data.get("names") or {}).items()}
    if not aliases:
        return {}, names, f"no usable room aliases in {config.rooms_path()}"
    return aliases, names, ""


def resolve(spoken: str) -> Tuple[Optional[list], str]:
    """Turn a spoken room name (or several, comma-separated) into segment ids.

    Returns (segment_ids, reason). A name that is not known is an error, never a guess.
    """
    aliases, _names, reason = load()
    if reason:
        return None, reason

    wanted = [r.strip().lower() for r in (spoken or "").split(",") if r.strip()]
    if not wanted:
        return None, "no rooms given"

    unknown = [w for w in wanted if w not in aliases]
    if unknown:
        return None, (
            f"no such room: {', '.join(unknown)}. Known rooms: "
            f"{', '.join(sorted(aliases))}"
        )
    # de-duplicate while keeping the order the person said them in
    return list(dict.fromkeys(aliases[w] for w in wanted)), ""


def describe() -> dict:
    """What rooms are available, for showing a person."""
    aliases, names, reason = load()
    if reason:
        return {"error": reason}
    by_segment: Dict[int, list] = {}
    for alias, seg in aliases.items():
        by_segment.setdefault(seg, []).append(alias)
    return {
        "rooms": [
            {
                "segment_id": seg,
                "name": names.get(str(seg), f"segment {seg}"),
                "spoken_as": sorted(aliases_),
            }
            for seg, aliases_ in sorted(by_segment.items())
        ]
    }


def write(spec: dict, names: Optional[dict] = None) -> str:
    """Write a room map for this machine. Used when generating one from a decoded map."""
    import json

    path = config.rooms_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"aliases": spec}
    if names:
        payload["names"] = names
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    path.chmod(0o600)
    return str(path)
