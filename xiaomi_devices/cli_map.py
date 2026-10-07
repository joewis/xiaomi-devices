#!/usr/bin/env python3
"""Interactive CLI: log in to the Xiaomi cloud, fetch a device's map, decode it.

This is the half that may ask a person for something (a QR scan, a captcha, an emailed code),
so it is a separate command from the always-on device control. Nothing here runs unattended.

Typical first run::

    xiaomi-map devices            # confirm the account and the device are visible
    xiaomi-map fetch --qr         # scan with the phone app; caches the session
    xiaomi-map decode map.raw --png map.png
    xiaomi-map rooms map.raw --write   # propose and save a room mapping
"""

import argparse
import json
import pathlib
import sys

# The cloud half needs optional dependencies (colorama for the interactive login, Pillow for
# image rendering). Import them lazily so a missing extra produces an instruction rather than
# a traceback — this command is only usable with them, but the message should say so.
try:
    from . import cloud, config, mapfile

    _IMPORT_ERROR = None
except ImportError as _exc:  # pragma: no cover - depends on install extras
    cloud = config = mapfile = None
    _IMPORT_ERROR = _exc

_CLOUD_EXTRA_HINT = (
    "this command needs the cloud extras, which are not installed.\n"
    "  install them with:  pip install 'xiaomi-devices[cloud]'\n"
    "  (the non-interactive 'xiaomi-ctl' command needs neither)"
)


def _out_dir(args) -> pathlib.Path:
    path = pathlib.Path(args.out).expanduser() if args.out else (
        pathlib.Path.home() / ".local" / "share" / "xiaomi-devices")
    path.mkdir(parents=True, exist_ok=True)
    return path


def cmd_devices(args) -> int:
    conn, reason = cloud.login(mode=args.login)
    if not conn:
        print(f"  {reason}", file=sys.stderr)
        return 1

    devices, reason = cloud.list_devices(conn, server=args.server or None)
    if not devices:
        print(f"  {reason}", file=sys.stderr)
        return 1
    for dev in devices:
        print(f"  {dev.get('_server', '?'):>3}  {str(dev.get('model', '')):<26} "
              f"{str(dev.get('name', ''))[:24]:<24} did={dev.get('did')}")
    return 0


def cmd_fetch(args) -> int:
    conn, reason = cloud.login(mode=args.login)
    if not conn:
        print(f"  {reason}", file=sys.stderr)
        return 1

    user_id = str(conn.userId)
    device_id = args.did
    if not device_id:
        devices, reason = cloud.list_devices(conn, server=args.server or None)
        if not devices:
            print(f"  {reason}", file=sys.stderr)
            return 1
        picked, reason = cloud.pick_device(devices, model=args.model, name=args.name)
        if not picked:
            print(f"  {reason}", file=sys.stderr)
            return 1
        device_id = picked.get("did")
        print(f"  device: {picked.get('name')} ({picked.get('model')}) did={device_id}")

    out_dir = _out_dir(args)
    ok = 0
    for map_name in args.map_names:
        raw, reason = mapfile.fetch_archive(conn, user_id, device_id, map_name,
                                            server=args.server or None)
        if not raw:
            print(f"  map {map_name}: {reason}", file=sys.stderr)
            continue
        path = out_dir / f"map-{device_id}-{map_name}.raw"
        path.write_bytes(raw)
        path.chmod(0o600)
        print(f"  map {map_name}: {len(raw)} bytes -> {path}")
        ok += 1

    if not ok:
        return 1
    print("  next: decode it with 'xiaomi-map rooms <file>'")
    return 0


def cmd_decode(args) -> int:
    raw = pathlib.Path(args.file).read_bytes()
    decoded, reason = mapfile.rooms_from_map(raw)
    if reason:
        print(f"  {reason}", file=sys.stderr)
        return 1

    header = decoded["header"]
    print(f"  layer={decoded['layer']}  grid={header['width']}x{header['height']}  "
          f"pixel={header['pixel_size']}mm")
    print(f"  device at {header['vacuum_position']}  charger at {header['charger_position']}")
    for segment, info in sorted(decoded["rooms"].items()):
        mm = info["mm"]
        print(f"    segment {segment:>2}  {info['name']:<26} "
              f"x={mm[0] // 10}..{mm[2] // 10}cm y={mm[1] // 10}..{mm[3] // 10}cm")

    if args.ascii:
        header_, pixels, payload = mapfile.parse_archive(raw)
        if payload and payload.get("rism"):
            blob = payload["rism"]
            header_, pixels, _ = mapfile.parse_archive(
                blob.encode() if isinstance(blob, str) else blob)
            print()
            print(mapfile.render_ascii(pixels, header_, rism=True))
        else:
            print()
            print(mapfile.render_ascii(pixels, header_, rism=False))

    if args.png:
        written, reason = mapfile.render_png(raw, args.png)
        if written:
            print(f"  image -> {written}")
        else:
            print(f"  image not written: {reason}", file=sys.stderr)
    return 0


def cmd_rooms(args) -> int:
    raw = pathlib.Path(args.file).read_bytes()
    suggested, reason = mapfile.suggest_rooms(raw, drop_unnamed=not args.keep_unnamed)
    if reason:
        print(f"  {reason}", file=sys.stderr)
        return 1

    print("  proposed room mapping:")
    for name, segment in sorted(suggested["aliases"].items()):
        print(f"    {name!r} -> segment {segment}")

    if args.write:
        path = config.rooms_path()
        if path.exists() and not args.force:
            print(f"  refusing to overwrite {path}; pass --force to replace it", file=sys.stderr)
            return 1
        import json as _json
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(suggested, indent=2, ensure_ascii=False) + "\n")
        path.chmod(0o600)
        print(f"  written -> {path}")
        print("  review it: rename rooms to whatever you actually call them, and delete any "
              "you do not want addressable")
    else:
        print("  (nothing written; pass --write to save this as your room mapping)")
    return 0


def main(argv=None) -> int:
    if _IMPORT_ERROR is not None:
        print(_CLOUD_EXTRA_HINT, file=sys.stderr)
        print(f"  (missing module: {_IMPORT_ERROR.name})", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(prog="xiaomi-map", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add_login(p):
        p.add_argument("--login", choices=["session", "qr", "password"], default="qr",
                       help="session uses the cached login without prompting (default: qr)")

    p = sub.add_parser("devices", help="list the devices on the account")
    add_login(p)
    p.add_argument("--server", default=config.cloud_server(),
                   help="regional server (default: %(default)s)")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("fetch", help="download a device's raw map archive")
    add_login(p)
    p.add_argument("--server", default=config.cloud_server())
    p.add_argument("--model", default="vacuum", help="match a device by model substring")
    p.add_argument("--name", default=None, help="match a device by name")
    p.add_argument("--did", default=None, help="skip discovery and use this device id")
    p.add_argument("--map-names", default=mapfile.MAP_NAME_DEFAULT,
                   help="comma-separated map names to fetch (default: %(default)s)")
    p.add_argument("--out", default=None, help="output directory")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("decode", help="decode a fetched archive")
    p.add_argument("file")
    p.add_argument("--ascii", action="store_true", help="also print a text rendering")
    p.add_argument("--png", default=None, help="also render a PNG to this path")
    p.set_defaults(func=cmd_decode)

    p = sub.add_parser("rooms", help="propose a room mapping from an archive")
    p.add_argument("file")
    p.add_argument("--write", action="store_true", help="save it as the room mapping")
    p.add_argument("--force", action="store_true", help="overwrite an existing mapping")
    p.add_argument("--keep-unnamed", action="store_true",
                   help="include segments the map has no name for")
    p.set_defaults(func=cmd_rooms)

    args = parser.parse_args(argv)
    if args.command == "fetch":
        args.map_names = [m.strip() for m in str(args.map_names).split(",") if m.strip()]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
