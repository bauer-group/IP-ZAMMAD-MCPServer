#!/usr/bin/env python
"""Generate docs/tools.md from the live tool registration.

The tool catalogue was documented by hand and drifted immediately: four
different counts appeared across the docs (33, ~33, ~36, 36) while
`docs/tools.md` was linked from the spec but had never existed. A hand-written
inventory of 75 tools is guaranteed to be wrong within a release, so this
derives it from the same `server.register()` the running server uses.

    python scripts/generate-tools-doc.py            # write docs/tools.md
    python scripts/generate-tools-doc.py --check    # fail if stale (CI)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "app" / "bg-zammad-mcp"
OUT = REPO / "docs" / "tools.md"

HEADER = """# Tool reference

<!-- GENERATED FILE — do not edit by hand.
     Run `python scripts/generate-tools-doc.py` after changing the tool surface.
     CI fails if this file is out of date. -->

Every tool the Zammad MCP server exposes, grouped by tag. All of them run as the
**authenticated Zammad user**, so Zammad's own permission system decides what is
visible and changeable — a tool listed here will still refuse an action the
caller is not allowed to perform.

Annotation legend:

| Marker | Meaning |
| --- | --- |
| **read** | `readOnlyHint` — safe to auto-run; changes nothing. |
| **write** | Additive, or affects only the caller's own reversible state. |
| **destructive** | `destructiveHint` — overwrites or removes state others rely on. An MCP client should ask a human first. |
"""


class _NullCtx:
    """Registration only — no tool is executed here."""

    settings = None

    async def request(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise RuntimeError("generate-tools-doc must not perform requests")


def _kind(tool: Any) -> str:
    annotations = tool.annotations
    if annotations is not None and annotations.readOnlyHint:
        return "read"
    if annotations is not None and annotations.destructiveHint:
        return "destructive"
    return "write"


def _first_sentence(text: str | None) -> str:
    if not text:
        return ""
    flat = " ".join(text.split())
    for stop in (". ", "! ", "? "):
        head, sep, _ = flat.partition(stop)
        if sep:
            return (head + stop.strip()).strip()
    return flat


async def _render() -> str:
    sys.path.insert(0, str(APP / "src"))
    import server  # noqa: PLC0415

    from fastmcp import FastMCP  # noqa: PLC0415

    mcp: FastMCP = FastMCP("doc-generator")
    total = server.register(mcp, _NullCtx())
    tools = sorted(await mcp.list_tools(run_middleware=False), key=lambda t: t.name)

    # Group by the tag that best describes the tool, preferring the more
    # specific one so "worklist" wins over the broad "tickets".
    order = [
        ("worklist", "Worklist"),
        ("bulk", "Bulk operations"),
        ("communication", "Communication"),
        ("audit", "Audit & correction"),
        ("ai", "Zammad AI (feature-gated)"),
        ("knowledge", "Knowledge base & wording"),
        ("tickets", "Tickets"),
        ("people", "Users & organizations"),
        ("reporting", "Time accounting"),
        ("reference", "Reference data"),
    ]
    seen: set[str] = set()
    sections: list[str] = []
    for tag, title in order:
        rows = [t for t in tools if tag in t.tags and t.name not in seen]
        if not rows:
            continue
        seen.update(t.name for t in rows)
        sections.append(f"\n## {title}\n")
        sections.append("| Tool | | What it does |")
        sections.append("| --- | --- | --- |")
        for tool in rows:
            sections.append(f"| `{tool.name}` | {_kind(tool)} | {_first_sentence(tool.description)} |")
        sections.append("")

    leftover = [t for t in tools if t.name not in seen]
    if leftover:
        sections.append("\n## Other\n")
        sections.append("| Tool | | What it does |")
        sections.append("| --- | --- | --- |")
        for tool in leftover:
            sections.append(f"| `{tool.name}` | {_kind(tool)} | {_first_sentence(tool.description)} |")
        sections.append("")

    counts = {
        "read": sum(1 for t in tools if _kind(t) == "read"),
        "write": sum(1 for t in tools if _kind(t) == "write"),
        "destructive": sum(1 for t in tools if _kind(t) == "destructive"),
    }
    summary = (
        f"\n**{total} tools** — {counts['read']} read-only, {counts['write']} additive writes, "
        f"{counts['destructive']} destructive.\n"
    )
    return HEADER + summary + "\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if docs/tools.md is stale")
    args = parser.parse_args()

    rendered = asyncio.run(_render())
    current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    if rendered == current:
        print(f"ok    {OUT.relative_to(REPO)}")
        return 0
    if args.check:
        print(
            f"STALE {OUT.relative_to(REPO)}\n"
            "The tool surface changed. Run:\n"
            "    python scripts/generate-tools-doc.py",
            file=sys.stderr,
        )
        return 1
    OUT.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
