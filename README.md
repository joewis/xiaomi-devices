# xiaomi-devices

Tools for Xiaomi-ecosystem robot vacuums: local control over the LAN, and map retrieval from
the Xiaomi cloud.

Built for a Dreame/Xiaomi 1C (`dreame.vacuum.mc1808`), but the local controls and the map
decoder target the wider Dreame family.

## Two halves, deliberately separate

The package is split along a lifecycle boundary, not a stylistic one:

| | **Local** (`xiaomi-ctl`) | **Cloud** (`xiaomi-map`) |
|---|---|---|
| Talks to | the device, over miIO | Xiaomi's account service |
| Asks a human? | never | captcha, emailed code, or QR scan |
| Runs | continuously, from a service | rarely, by hand |

An always-on service must never be able to block on a human input prompt, so the
non-interactive half imports none of the interactive code. That is why this is a library with
two commands rather than one program.

## Install

```bash
pip install .
# or, for map fetching and image rendering too:
pip install '.[cloud]'
```

## Configure

Nothing personal is committed. Settings come from the environment, or from files under
`~/.config/xiaomi-devices/` (override with `XIAOMI_DEVICES_DIR`).

```bash
mkdir -p ~/.config/xiaomi-devices
cat > ~/.config/xiaomi-devices/secrets.env <<'EOF'
XIAOMI_USER=you@example.com
XIAOMI_PASS=...
XIAOMI_DEVICE_TOKEN=<32 hex characters>
EOF
chmod 600 ~/.config/xiaomi-devices/secrets.env
```

The device address and region can go in `device.json` / the environment:

```bash
export XIAOMI_DEVICE_IP=192.168.1.50
export XIAOMI_CLOUD_SERVER=cn     # cn, de, us, ru, tw, sg, in, i2
```

The device token can be obtained with the bundled extractor's upstream tool, or through the
login path in this package.

## Use

```bash
xiaomi-ctl status                  # state, battery, faults, consumables
xiaomi-ctl control start
xiaomi-ctl control dock
xiaomi-ctl control clean_rooms "<room name>"

xiaomi-map devices                 # list the account's devices
xiaomi-map fetch --qr              # scan with the phone app; no captcha, no emailed code
xiaomi-map decode map.raw --png map.png
xiaomi-map rooms map.raw --write   # propose a room mapping from your own map
```

### Room-targeted cleaning

The device is told to clean a room by its internal segment id. The mapping from a spoken name
to an id is generated from your own map and stays on your machine:

```bash
xiaomi-map fetch --qr
xiaomi-map rooms ~/.local/share/xiaomi-devices/map-<did>-0.raw --write
```

Then edit `~/.config/xiaomi-devices/rooms.json` to rename rooms to whatever you call them and
delete any you do not want addressable. An unrecognised name is refused before anything is
sent to the device.

## How the map works, and what it is not

The map is **not** available from the device locally: the local API accepts a map request and
returns nothing usable. It is also **not** a historical property — querying device history
with any map key returns an empty list while the same call for battery returns records.

It is a file in the account's object store:

1. object name `{user_id}/{device_id}/{map_name}`, where `map_name` is `"0"` for this family
2. `POST /v2/home/get_interim_file_url` for that object name returns a signed URL
3. download it — base64 (URL-safe alphabet) of a zlib blob

The archive is `[27-byte header][width*height pixels][JSON]`. Room names live in a nested
`rism` layer inside the JSON; `seg_inf` there maps segment ids to base64-encoded labels. See
the module docstring in `xiaomi_devices/mapfile.py` for the full layout.

**This yields a snapshot, not a live map.** That suits a floor plan that does not move. A live
map requires rooting the device.

## A note on fault reporting

Fault codes are mapped to descriptions and a subsystem, because a bare number tells a reader
nothing. The descriptions describe **what the device reports and which subsystem it is in** —
they deliberately do not grade how serious a fault is or whether it is likely to clear,
because the code alone cannot tell you that. The same code can come from a momentary
obstruction or from a broken part, and only the physical situation distinguishes them. Report
it and ask.

Related: an accepted command is not a successful one. The device can accept an action and
report a fault a few seconds later, leaving the mode back at idle. Every motion call therefore
reads the state back and reports whether the run actually started — see `verify_started`.

## Tests

```bash
python tests/test_core.py
```

Offline and self-contained: no device, no network, no credentials. The map tests build
synthetic archives in the device's own format, so the decoder is exercised without shipping
anybody's floor plan.

## Layout

```
xiaomi_devices/
  config.py     where local settings come from (env -> files -> reason, never a hardcoded value)
  cloud.py      login (session / QR / password), signed API calls, session caching
  device.py     miIO transport: wake, read, control, command verification
  mapfile.py    fetch and decode map archives, room extraction, rendering
  faults.py     the fault table
  rooms.py      spoken name -> segment id, from a local mapping
  vendor/       the token extractor (MIT, see LICENSE.token_extractor)
cli/            xiaomi-ctl (non-interactive) and xiaomi-map (interactive)
mcp/            an MCP server wrapping the local half
```

## Credits

The login path vendors [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)
by Piotr Machowski (MIT) — it is the only login implementation found that handles an account
with two-factor authentication. The map format and the device's service and fault definitions
follow the [Valetudo](https://github.com/Hypfer/Valetudo) and
[Xiaomi Cloud Map Extractor](https://github.com/PiotrMachowski/Home-Assistant-custom-components-Xiaomi-Cloud-Map-Extractor)
projects.

## Licence

MIT. The vendored extractor keeps its own upstream MIT licence, included alongside it.
