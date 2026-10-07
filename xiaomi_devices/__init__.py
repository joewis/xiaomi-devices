"""xiaomi_devices — tools for Xiaomi-ecosystem devices on a LAN.

Two halves with deliberately different lifecycles:

* **Cloud** (``xiaomi_devices.cloud``, ``xiaomi_devices.mapfile``) — authenticates to
  Xiaomi's account service, which is INTERACTIVE: it needs a captcha, an emailed code, or a
  QR scan by a human. It runs rarely (the map changes only when the home changes) and caches
  its session.
* **Local** (``xiaomi_devices.device``) — talks to the device over miIO on the LAN. This is
  NON-INTERACTIVE and must never block.

Keeping these separate is the reason this is a library with two entry points rather than one
program: an always-on service must never end up able to sit on a human input prompt.

Nothing here hardcodes credentials, device addresses, or room names. Those are resolved at
call time from the environment or from local files (see ``xiaomi_devices.config``).
"""

__version__ = "0.1.0"

__all__ = ["config", "cloud", "device", "faults", "mapfile", "rooms"]
