#!/usr/bin/env python3
"""
dev-inspector.py — One-command MCP Inspector launcher for local dev.

Spins up the MCP Inspector against a local or remote bg-zammad-mcp instance.
Picks sensible defaults (transport, OAuth flow, scopes) so the operator
doesn't need to remember Inspector's CLI flag set.

Usage
-----
From the repo root:

    python scripts/dev-inspector.py                          # connects to http://localhost:8000/mcp
    python scripts/dev-inspector.py --url https://x/mcp      # remote MCP
    python scripts/dev-inspector.py --no-auth                # for AUTH_MODE=none deployments
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_URL = "http://localhost:8000/mcp"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dev-inspector.py",
        description="Launch the MCP Inspector against a bg-zammad-mcp deployment.",
    )
    p.add_argument(
        "--url",
        default=os.environ.get("ZAMMAD_MCP_INSPECTOR_URL", DEFAULT_URL),
        help=f"MCP server URL (default: {DEFAULT_URL})",
    )
    p.add_argument(
        "--no-auth",
        action="store_true",
        help="Skip OAuth flow (use only with AUTH_MODE=none deployments)",
    )
    p.add_argument(
        "--cli",
        action="store_true",
        help="Run Inspector in CLI mode instead of the web UI",
    )
    p.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="Extra args forwarded to `npx @modelcontextprotocol/inspector`",
    )
    return p.parse_args(argv)


def find_npx() -> str | None:
    """Locate `npx`. Returns None if not on PATH."""
    return shutil.which("npx")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    npx = find_npx()
    if npx is None:
        print(
            "error: `npx` not found on PATH. Install Node.js 18+ and retry.\n"
            "       https://nodejs.org/en/download",
            file=sys.stderr,
        )
        return 1

    cmd: list[str] = [
        npx,
        "-y",
        "@modelcontextprotocol/inspector",
    ]

    if args.cli:
        cmd.append("--cli")

    # Inspector takes the server URL as a positional argument when using
    # streamable-http transport; auto-detect is reliable for the bg-zammad-mcp
    # endpoint shape.
    cmd.extend(["--transport", "streamable-http", args.url])

    if args.no_auth:
        cmd.append("--no-auth")

    # Forward extras transparently.
    cmd.extend(arg for arg in args.extra if arg)

    print(f"[dev-inspector] connecting to {args.url}")
    print(f"[dev-inspector] exec: {' '.join(cmd)}")
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
