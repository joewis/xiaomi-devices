"""Import shim for the vendored token extractor.

WHY THIS FILE EXISTS: ``token_extractor`` is a SCRIPT, not a library. At import time it
builds an argparse parser and calls ``parse_args()`` at module level, and it calls
``parser.error()`` (which raises SystemExit) when ``-ni`` is passed without credentials.
Importing it naively therefore either dies or silently consumes the caller's argv.

We vendor the file rather than depending on a clone in a scratch directory because it holds
the ONLY login path that works against this account (see below), and a git clone in /tmp does
not survive a reboot.

The shim swaps in a harmless argv for the duration of the import, then restores it, so the
import is safe from any context. Nothing else about the module is modified.

Why this file is vendored at all: ``micloud`` (a common alternative) cannot log in to accounts
that require two-factor authentication — its login expects ``result == "ok"`` with no 2FA
handling and raises MiCloudAccessDenied. This extractor's connector classes handle captcha +
emailed code (password mode) and phone-app confirmation (QR mode), and expose
``execute_api_call_encrypted`` for arbitrary signed calls.

Upstream: https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor (MIT).
See LICENSE.token_extractor in this directory.
"""

import sys

# The import-time argv the module will see. Kept minimal and honest: no credentials, no
# non-interactive flag (that combination is what triggers parse_args()'s error path).
_SAFE_ARGV = ["token_extractor", "-s", "", "-l", "ERROR"]

_restore = sys.argv
try:
    sys.argv = list(_SAFE_ARGV)
    # noqa: E402 - the import is intentionally not at the top of the file
    from . import token_extractor as token_extractor  # noqa: F401
finally:
    sys.argv = _restore

__all__ = ["token_extractor"]
