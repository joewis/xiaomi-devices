"""Xiaomi cloud: authentication, session reuse, and signed API calls.

This is the INTERACTIVE half of the package. Logging in requires a human — a captcha, an
emailed code, or a QR scan — so nothing here is called from the always-on local path.

Two things learned the hard way, both encoded below:

* **Session reuse matters.** A successful login yields a service token valid for roughly a
  couple of days. It is cached to disk so a second run needs no human at all. Do not make
  someone solve a captcha twice for the same session.
* **The QR path is the reliable one.** Password login against this account repeatedly failed
  at the email step even when the captcha was accepted, and the failure mode is opaque. QR
  skips both the captcha and the email entirely because it borrows the already-authenticated
  phone app. Offer QR first.

Also: never run this with debug logging enabled. The extractor logs signed URLs and session
material at DEBUG level, which writes live credentials to disk.
"""

import json
import os
import time
from typing import Optional, Tuple

from . import config
from .vendor import token_extractor as te

# How long a cached session is trusted. The token lasts about 72h; refresh conservatively
# before that rather than discovering expiry mid-run.
SESSION_MAX_AGE_H = 48

# Region list comes from the extractor so it stays in step with the vendored copy.
SERVERS = te.SERVERS


def _session_path():
    return config.session_path()


def load_session() -> Optional[dict]:
    """Return a cached session if it is still fresh, else None."""
    path = _session_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None

    age_h = (time.time() - data.get("saved_at", 0)) / 3600
    if age_h > SESSION_MAX_AGE_H:
        return None
    if not all(data.get(k) for k in ("ssecurity", "user_id", "service_token")):
        return None
    data["_age_h"] = age_h
    return data


def save_session(conn) -> bool:
    """Persist what is needed to re-authenticate later, so a human logs in once."""
    data = {
        "ssecurity": getattr(conn, "_ssecurity", None),
        "user_id": getattr(conn, "userId", None),
        "service_token": getattr(conn, "_serviceToken", None),
        "saved_at": int(time.time()),
    }
    if not all(data.values()):
        return False

    path = _session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
    return True


def connector_from_session() -> Optional[object]:
    """A connector pre-loaded with the cached session, ready for API calls."""
    session = load_session()
    if not session:
        return None
    conn = te.PasswordXiaomiCloudConnector()
    conn._ssecurity = session["ssecurity"]
    conn.userId = session["user_id"]
    conn._serviceToken = session["service_token"]
    return conn


def login(mode: str = "qr", on_message=print) -> Tuple[Optional[object], str]:
    """Log in and return (connector, reason).

    ``mode`` is one of:
      * ``"session"`` — use the cached session only; never prompt
      * ``"qr"``      — scan a QR with the phone app (skips captcha and email)
      * ``"password"``— captcha and/or emailed code

    ``on_message`` receives progress text; the default prints it. Pass a no-op to stay quiet.
    """
    if mode == "session":
        conn = connector_from_session()
        if conn is None:
            return None, (
                f"no usable cached session at {_session_path()} (absent, stale, or "
                f"incomplete). Run a login instead."
            )
        return conn, ""

    if mode == "qr":
        conn = te.QrCodeXiaomiCloudConnector()
        on_message("Scan the QR below with the Xiaomi phone app, then confirm on the phone.")
        on_message("There is no captcha and no emailed code in this mode.")
    elif mode == "password":
        user, password, reason = config.cloud_credentials()
        if not user or not password:
            return None, reason
        # The extractor parses argv at import and would expose credentials there, so they are
        # set on the parsed namespace after import instead.
        te.args.username = user
        te.args.password = password
        conn = te.PasswordXiaomiCloudConnector()
        on_message("You may be asked for a captcha and/or the code sent to your email.")
    else:
        return None, f"unknown login mode {mode!r}; expected session, qr, or password"

    try:
        if not conn.login():
            return None, "login was refused or did not complete"
    except Exception as exc:
        return None, f"login failed: {type(exc).__name__}: {exc}"

    if save_session(conn):
        on_message(f"session saved to {_session_path()} (valid about "
                   f"{SESSION_MAX_AGE_H}h, so no further login is needed today)")
    else:
        on_message("login succeeded but the session could not be saved")
    return conn, ""


def api_call(conn, path: str, data: dict, server: Optional[str] = None) -> dict:
    """Make a signed, encrypted call to the account API.

    ``path`` is the API path (e.g. ``/v2/home/get_interim_file_url``); ``data`` is the payload
    object, which is JSON-encoded here so callers pass a dict rather than a string.
    """
    url = conn.get_api_url(server or config.cloud_server()) + path
    return conn.execute_api_call_encrypted(url, {"data": json.dumps(data)})


def list_devices(conn, server: Optional[str] = None) -> Tuple[list, str]:
    """Enumerate the account's devices across regions. Returns (devices, reason).

    Two details this must get right, because getting either wrong makes a perfectly good login
    look like an empty account:

    * the owner of a home is ``conn.userId``, not the home's own uid field
    * devices live under ``result["device_info"]``, not ``result["dev_list"]``
    """
    servers = [server] if server else SERVERS
    found = []
    for region in servers:
        try:
            homes = conn.get_homes(region)
        except Exception:
            continue
        if not homes or homes.get("code") != 0:
            continue

        targets = []
        for home in (homes.get("result", {}) or {}).get("homelist", []) or []:
            targets.append((home["id"], conn.userId))
        try:
            shared = conn.get_dev_cnt(region)
            if shared and shared.get("code") == 0:
                for fam in ((shared.get("result", {}) or {}).get("share", {}) or {}).get(
                        "share_family", []) or []:
                    targets.append((fam["home_id"], fam["home_owner"]))
        except Exception:
            pass

        for home_id, owner in targets:
            try:
                devices = conn.get_devices(region, home_id, owner)
            except Exception:
                continue
            for dev in ((devices or {}).get("result", {}) or {}).get("device_info") or []:
                dev = dict(dev)
                dev["_server"] = region
                found.append(dev)

    if not found:
        return [], (
            f"the account listed no devices on any server ({', '.join(servers)}). If the device "
            f"exists, the account may live on a different regional server — try others."
        )
    return found, ""


def pick_device(devices: list, model: Optional[str] = None, name: Optional[str] = None,
                did: Optional[str] = None) -> Tuple[Optional[dict], str]:
    """Choose one device from a listing, by model substring, name, or id."""
    for dev in devices:
        if did and str(dev.get("did")) == str(did):
            return dev, ""
    for dev in devices:
        if model and model.lower() in str(dev.get("model", "")).lower():
            return dev, ""
    for dev in devices:
        if name and name.lower() in str(dev.get("name", "")).lower():
            return dev, ""
    return None, (
        f"no device matched (model={model!r} name={name!r} did={did!r}). Found: "
        + ", ".join(f"{d.get('name')}[{d.get('model')}]" for d in devices)
    )
