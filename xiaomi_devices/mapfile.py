"""Fetching and decoding a robot vacuum's map from the Xiaomi cloud.

The map is NOT available from the device itself. Two dead ends worth recording so nobody
repeats them:

* The map service on the device answers but returns nothing usable — its properties come back
  as "no value". The device does not hand out map bytes for this model.
* The map is NOT in the property-history API. Querying historical device data with any map
  key returns an empty list, while the same call for a battery property returns records. The
  call is fine; the map was never a property.

The map is a FILE in the account's object store, and the route is:

1. the object name is ``{user_id}/{device_id}/{map_name}``, and for this device family
   ``map_name`` is the literal string ``"0"``
2. ask ``/v2/home/get_interim_file_url`` for that object name to get a signed download URL
3. download it — a base64 (``_``/``-`` swapped for ``/``/``+``) zlib blob
4. decompress and parse

Archive layout::

    [27-byte header][width*height pixel bytes][JSON]

Header (little-endian int16 unless noted)::

    0   map_index          4   frame_type (int8; 73 = full frame, 80 = delta)
    5   vacuum x, y, angle (three int16)
    11  charger x, y, angle
    17  pixel size in mm per pixel
    19  image width          21  image height
    23  image left (raw)     25  image top (raw)

Pixels: ``byte >> 2`` is a segment id (1..61 = a room); otherwise ``byte & 3`` gives
0 = outside, 1 = floor, 2 = wall.

The JSON payload may contain a ``rism`` key which is a NESTED map in exactly the same format.
That nested layer is where rooms live, and its JSON carries
``seg_inf = {segment_id: {"name": <base64 utf-8>}}`` — the room labels. Room extents convert to
millimetres as ``(x + image_left) * pixel_size``.
"""

import base64
import json
import sys
import zlib
from typing import Dict, List, Optional, Tuple

from . import config

HEADER_SIZE = 27
FRAME_FULL = 73
FRAME_DELTA = 80

MAP_NAME_DEFAULT = "0"

# Pixel masks
SEGMENT_SHIFT = 2
SEGMENT_MAX = 62
MASK_TYPE = 0b11
TYPE_OUTSIDE, TYPE_FLOOR, TYPE_WALL = 0, 1, 2
RISM_WALL_BIT = 7
RISM_SEGMENT_MASK = 0x7F


def fetch_archive(conn, user_id: str, device_id: str, map_name: str = MAP_NAME_DEFAULT,
                  server: Optional[str] = None) -> Tuple[Optional[bytes], str]:
    """Download the raw map archive for a device. Returns (bytes, reason)."""
    import requests

    obj_name = f"{user_id}/{device_id}/{map_name}"
    try:
        resp = None
        from . import cloud
        resp = cloud.api_call(conn, "/v2/home/get_interim_file_url",
                              {"obj_name": obj_name}, server=server)
    except Exception as exc:
        return None, f"could not request a download URL: {type(exc).__name__}: {exc}"

    url = ((resp or {}).get("result") or {}).get("url")
    if not url:
        return None, (
            f"the account returned no download URL for {obj_name} (code "
            f"{(resp or {}).get('code')}). The object may not exist yet — a map appears after "
            f"the device has run and uploaded one."
        )

    try:
        download = requests.get(url, timeout=30)
    except Exception as exc:
        return None, f"download failed: {type(exc).__name__}: {exc}"
    if download.status_code != 200:
        return None, f"download returned HTTP {download.status_code}"
    return download.content, ""


def _decode_b64_zlib(blob: str) -> bytes:
    """The archive's own encoding: base64 with the URL-safe alphabet, then zlib."""
    return zlib.decompress(base64.decodebytes(
        blob.replace("_", "/").replace("-", "+").encode("utf8")))


def _i16(raw: bytes, offset: int) -> int:
    return int.from_bytes(raw[offset:offset + 2], byteorder="little", signed=True)


def _i8(raw: bytes, offset: int) -> int:
    return int.from_bytes(raw[offset:offset + 1], byteorder="big", signed=True)


def parse_archive(raw_b64: bytes) -> Tuple[dict, bytes, Optional[dict]]:
    """Decode an archive into (header, pixel bytes, payload dict or None)."""
    unzipped = _decode_b64_zlib(raw_b64.decode())

    header = {
        "map_index": _i16(unzipped, 0),
        "frame_type": _i8(unzipped, 4),
        "vacuum_position": [_i16(unzipped, 5), _i16(unzipped, 7), _i16(unzipped, 9)],
        "charger_position": [_i16(unzipped, 11), _i16(unzipped, 13), _i16(unzipped, 15)],
        "pixel_size": _i16(unzipped, 17),
        "width": _i16(unzipped, 19),
        "height": _i16(unzipped, 21),
    }
    # The left/top fields are stored pre-multiplied; dividing recovers the grid offset.
    pixel_size = header["pixel_size"] or 1
    header["left"] = round(_i16(unzipped, 23) / pixel_size)
    header["top"] = round(_i16(unzipped, 25) / pixel_size)

    width, height = header["width"], header["height"]
    pixels = unzipped[HEADER_SIZE:HEADER_SIZE + width * height]

    payload = None
    tail = unzipped[HEADER_SIZE + width * height:]
    if tail:
        try:
            payload = json.loads(tail.decode("utf8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
    return header, pixels, payload


def _decode_names(payload: Optional[dict]) -> Dict[int, str]:
    """Room labels out of a payload's seg_inf block."""
    names: Dict[int, str] = {}
    for key, val in ((payload or {}).get("seg_inf") or {}).items():
        if isinstance(val, dict) and val.get("name"):
            try:
                names[int(key)] = base64.b64decode(val["name"]).decode("utf-8")
            except Exception:
                names[int(key)] = "<undecodable>"
    return names


def _segment_boxes(pixels: bytes, header: dict, rism: bool) -> Dict[int, list]:
    """Bounding box of each segment, in pixel coordinates."""
    width, height = header["width"], header["height"]
    boxes: Dict[int, list] = {}
    for y in range(height):
        row = width * y
        for x in range(width):
            value = pixels[row + x]
            if rism:
                if value >> RISM_WALL_BIT:
                    continue
                segment = value & RISM_SEGMENT_MASK
            else:
                segment = value >> SEGMENT_SHIFT
                if segment < 1 or segment >= SEGMENT_MAX:
                    continue
            if segment < 1:
                continue
            box = boxes.get(segment)
            if box is None:
                boxes[segment] = [x, y, x, y]
            else:
                if x < box[0]:
                    box[0] = x
                if y < box[1]:
                    box[1] = y
                if x > box[2]:
                    box[2] = x
                if y > box[3]:
                    box[3] = y
    return boxes


def rooms_from_map(raw_b64: bytes) -> Tuple[dict, str]:
    """Decode an archive and return its rooms with names and millimetre extents.

    Prefers the nested ``rism`` layer, which is where the named rooms live; falls back to the
    outer layer, which has the segmentation but not the labels.
    """
    try:
        header, pixels, payload = parse_archive(raw_b64)
    except Exception as exc:
        return {}, f"could not decode the archive: {type(exc).__name__}: {exc}"

    if header["frame_type"] != FRAME_FULL:
        return {}, (
            f"the archive holds a partial frame (frame_type {header['frame_type']}, expected "
            f"{FRAME_FULL}); a delta frame carries no room information. Fetch again after the "
            f"device has uploaded a complete map."
        )

    result = {"header": header, "rooms": {}}

    rism_blob = (payload or {}).get("rism")
    if rism_blob:
        try:
            r_header, r_pixels, r_payload = parse_archive(rism_blob.encode()
                                                          if isinstance(rism_blob, str)
                                                          else rism_blob)
            names = _decode_names(r_payload)
            boxes = _segment_boxes(r_pixels, r_header, rism=True)
            result["layer"] = "rism"
            result["rooms"] = _to_mm(boxes, r_header, names)
        except Exception as exc:
            result["rism_error"] = f"{type(exc).__name__}: {exc}"

    if not result["rooms"]:
        boxes = _segment_boxes(pixels, header, rism=False)
        names = _decode_names(payload)
        result["layer"] = "outer"
        result["rooms"] = _to_mm(boxes, header, names)

    if not result["rooms"]:
        return {}, "the map decoded but contained no rooms"
    return result, ""


def _to_mm(boxes: Dict[int, list], header: dict, names: Dict[int, str]) -> dict:
    """Convert pixel bounding boxes to millimetre extents in the device's coordinates."""
    pixel_size = header["pixel_size"] or 1
    out = {}
    for segment, (x0, y0, x1, y1) in boxes.items():
        out[segment] = {
            "name": names.get(segment, f"segment {segment}"),
            "mm": [
                (x0 + header["left"]) * pixel_size,
                (y0 + header["top"]) * pixel_size,
                (x1 + header["left"]) * pixel_size,
                (y1 + header["top"]) * pixel_size,
            ],
            "pixels": [x0, y0, x1, y1],
        }
    return out


def render_ascii(pixels: bytes, header: dict, rism: bool = False, max_width: int = 110) -> str:
    """A rough text rendering, for eyeballing a map without an image viewer."""
    width, height = header["width"], header["height"]
    if not width or not height:
        return "(empty)"
    step = max(1, (width + max_width - 1) // max_width)
    lines = []
    for y in range(0, height, step):
        row = []
        for x in range(0, width, step):
            value = pixels[width * y + x]
            if rism:
                if value >> RISM_WALL_BIT:
                    char = "#"
                else:
                    segment = value & RISM_SEGMENT_MASK
                    char = " " if segment == 0 else _letter(segment)
            else:
                segment = value >> SEGMENT_SHIFT
                if segment < 1 or segment >= SEGMENT_MAX:
                    kind = value & MASK_TYPE
                    char = "." if kind == TYPE_OUTSIDE else (" " if kind == TYPE_FLOOR else "#")
                else:
                    char = _letter(segment)
            row.append(char)
        lines.append("".join(row))
    return "\n".join(lines)


def _letter(segment: int) -> str:
    return chr(ord("A") + (segment - 1) % 26) if segment < 27 else "*"


def render_png(raw_b64: bytes, path: str, rism: bool = True) -> Tuple[Optional[str], str]:
    """Render the map to a PNG. Needs Pillow; returns (path, reason)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None, "Pillow is not installed, so no image can be rendered"

    try:
        outer_header, _, outer_payload = parse_archive(raw_b64)
        if rism and (outer_payload or {}).get("rism"):
            blob = outer_payload["rism"]
            header, pixels, payload = parse_archive(
                blob.encode() if isinstance(blob, str) else blob)
        else:
            header, pixels, payload = outer_header, *_parse_outer(raw_b64)
    except Exception as exc:
        return None, f"could not decode the archive: {type(exc).__name__}: {exc}"

    width, height = header["width"], header["height"]
    image = Image.new("RGB", (width, height), (30, 30, 30))
    target = image.load()
    palette = [(90, 160, 90), (200, 170, 80), (90, 140, 210), (190, 110, 190),
               (110, 200, 190), (210, 120, 110), (150, 190, 110), (170, 160, 200)]

    for y in range(height):
        for x in range(width):
            value = pixels[width * y + x]
            if rism:
                if value >> RISM_WALL_BIT:
                    target[x, y] = (235, 235, 235)
                else:
                    segment = value & RISM_SEGMENT_MASK
                    target[x, y] = ((55, 55, 65) if segment == 0
                                    else palette[(segment - 1) % len(palette)])
            else:
                segment = value >> SEGMENT_SHIFT
                if segment < 1 or segment >= SEGMENT_MAX:
                    kind = value & MASK_TYPE
                    target[x, y] = ((30, 30, 30) if kind == TYPE_OUTSIDE
                                    else ((60, 60, 70) if kind == TYPE_FLOOR else (235, 235, 235)))
                else:
                    target[x, y] = palette[(segment - 1) % len(palette)]

    scale = 6
    image = image.resize((width * scale, height * scale), Image.NEAREST)
    draw = ImageDraw.Draw(image)
    names = _decode_names(payload)
    boxes = _segment_boxes(pixels, header, rism=rism)
    for segment, box in boxes.items():
        cx = (box[0] + box[2]) * scale // 2
        cy = (box[1] + box[3]) * scale // 2
        if 0 <= cx < image.width and 0 <= cy < image.height:
            label = names.get(segment, str(segment))[:14]
            draw.rectangle([cx - 40, cy - 9, cx + 40, cy + 9], fill=(0, 0, 0))
            draw.text((cx - 36, cy - 5), label, fill=(255, 255, 255))

    image.save(path)
    return path, ""


def _parse_outer(raw_b64: bytes):
    header, pixels, payload = parse_archive(raw_b64)
    return pixels, payload


def suggest_rooms(raw_b64: bytes, drop_unnamed: bool = True) -> Tuple[dict, str]:
    """Build a starter room mapping from a decoded map.

    Names come from the device's own map, so this proposes the mapping — a person still
    decides what to call each room and whether any should stay out. Unnamed segments are
    omitted by default because they are usually a balcony or a stray area rather than a room
    worth naming.
    """
    decoded, reason = rooms_from_map(raw_b64)
    if reason:
        return {}, reason

    aliases, names = {}, {}
    for segment, info in decoded["rooms"].items():
        label = info["name"]
        names[str(segment)] = label
        if label.startswith("segment ") and drop_unnamed:
            continue
        aliases[label.strip().lower()] = segment
    if not aliases:
        return {}, "the map had no named rooms to build a mapping from"
    return {"aliases": aliases, "names": names}, ""
