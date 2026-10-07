"""MCP surface for this package.

Kept as a subpackage so the agent-facing tools ship with the library rather than living in a
separate directory that drifts from it.

``main`` is the console entry point. It imports the server lazily so that a missing ``mcp``
extra produces an instruction rather than an import traceback.
"""

import sys

__all__ = ["main"]

_MCP_EXTRA_HINT = (
    "the MCP server needs the mcp extra, which is not installed.\n"
    "  install it with:  pip install 'xiaomi-devices[mcp]'"
)


def main() -> int:
    """Console entry point for ``xiaomi-vacuum-mcp``."""
    try:
        from .server import main as serve
    except ImportError as exc:
        print(_MCP_EXTRA_HINT, file=sys.stderr)
        print(f"  (missing module: {exc.name})", file=sys.stderr)
        return 2
    serve()
    return 0
