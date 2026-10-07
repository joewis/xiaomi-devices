"""Where local settings come from.

Design rule: **the repo carries the contract, never the value.** Nothing here hardcodes a
credential, a device address, or a room name — those are supplied by the person running it,
through the environment or through files outside the repo.

Resolution order for everything: an already-set environment variable wins (a caller may have
chosen deliberately), then a project-local env file, then a per-user config file, then a
built-in default where a default is harmless.

Deliberately absent: any default for a credential. A missing credential returns a REASON, not
an exception — a component that refuses to start without a network credential is worse than
one that starts with fewer capabilities. Callers degrade instead of failing.
"""

import json
import os
import pathlib
from typing import Optional, Tuple

# Where a person is expected to keep their own values. Not created automatically.
LOCAL_DIR = pathlib.Path(
    os.environ.get("XIAOMI_DEVICES_DIR", pathlib.Path.home() / ".config" / "xiaomi-devices")
)

# Variables this package reads. Names only — the repo names the variable, never the value.
ENV_XIAOMI_USER = "XIAOMI_USER"
ENV_XIAOMI_PASS = "XIAOMI_PASS"
ENV_DEVICE_IP = "XIAOMI_DEVICE_IP"
ENV_DEVICE_TOKEN = "XIAOMI_DEVICE_TOKEN"
ENV_CLOUD_SERVER = "XIAOMI_CLOUD_SERVER"

# File names under LOCAL_DIR.
FILE_SECRETS = "secrets.env"
FILE_DEVICE = "device.json"
FILE_ROOMS = "rooms.json"
FILE_SESSION = "cloud_session.json"


def _read_env_file(path: pathlib.Path) -> dict:
    """Parse a simple KEY=VALUE file. Missing file is not an error."""
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _from_sources(name: str, secrets_file: bool = False):
    """Env var first, then the project secrets file. Returns None if unset."""
    if os.environ.get(name):
        return os.environ[name]
    if secrets_file:
        return _read_env_file(LOCAL_DIR / FILE_SECRETS).get(name)
    return None


def cloud_credentials() -> Tuple[Optional[str], Optional[str], str]:
    """(username, password, reason_if_absent).

    Both are required together for password login. A caller that gets ``None`` for the username
    should report the reason rather than guessing.
    """
    user = _from_sources(ENV_XIAOMI_USER, secrets_file=True)
    pw = _from_sources(ENV_XIAOMI_PASS, secrets_file=True)
    if not user or not pw:
        return None, None, (
            f"no cloud credentials: set {ENV_XIAOMI_USER} and {ENV_XIAOMI_PASS} in the "
            f"environment, or put them in {LOCAL_DIR / FILE_SECRETS} (mode 600). "
            f"QR-mode login needs neither."
        )
    return user, pw, ""


def device() -> Tuple[Optional[str], Optional[str], str]:
    """(ip, token, reason_if_absent) for LOCAL control.

    The address may also live in device.json; the token may not — keep credentials in the
    secrets file so one file can be shared without disclosing anything.
    """
    ip = _from_sources(ENV_DEVICE_IP)
    token = _from_sources(ENV_DEVICE_TOKEN, secrets_file=True)
    if not ip:
        try:
            ip = json.loads((LOCAL_DIR / FILE_DEVICE).read_text()).get("ip")
        except (OSError, ValueError):
            ip = None
    if not ip or not token:
        missing = "address" if not ip else "token"
        return None, None, (
            f"no local device {missing}: set {ENV_DEVICE_IP} / {ENV_DEVICE_TOKEN}, or write "
            f"{LOCAL_DIR / FILE_DEVICE} (address only — put the token in "
            f"{LOCAL_DIR / FILE_SECRETS})."
        )
    return ip, token, ""


def cloud_server() -> str:
    """Which Xiaomi regional server the account lives on. Not every account is on 'sg'."""
    return _from_sources(ENV_CLOUD_SERVER) or "cn"


def session_path() -> pathlib.Path:
    return LOCAL_DIR / FILE_SESSION


def rooms_path() -> pathlib.Path:
    return LOCAL_DIR / FILE_ROOMS


def rooms() -> Tuple[dict, str]:
    """(mapping, reason_if_absent).

    The room map is deliberately LOCAL, not shipped: room ids and names are derived from a
    map of a specific home, and a floor plan with room labels is personal. What the repo
    carries is the *format*, documented in rooms.py — not somebody's home.
    """
    path = rooms_path()
    try:
        data = json.loads(path.read_text())
    except OSError:
        return {}, (
            f"no room map at {path}. Generate one from your own device with the map command; "
            f"see the README."
        )
    except ValueError as exc:
        return {}, f"{path} is not valid JSON: {exc}"

    if not isinstance(data, dict) or "aliases" not in data:
        return {}, (
            f"{path} does not look like a room map. Expected an object with an 'aliases' "
            f"key mapping a spoken name to a segment id."
        )
    return data, ""
