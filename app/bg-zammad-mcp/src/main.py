"""
Zammad MCP Server - CLI entrypoint.

Subcommands:
  serve      Run the MCP server (default when no command is given)
  tools      Print the registered tool catalogue (for docs/tools.md)
  health     Probe Zammad reachability and exit 0/1
  probe      Detect the live Zammad version (v6/v7) and exit

Designed to be the container ENTRYPOINT. `tini --` handles signal forwarding;
typer handles arg parsing and help screens.
"""

from __future__ import annotations

import asyncio
import socket as _socket
import sys
from pathlib import Path
from typing import Optional

_orig_socket = _socket.socket


class _DualStackSocket(_orig_socket):  # type: ignore[misc, valid-type]
    # asyncio hardcodes IPV6_V6ONLY=1 on v6 server sockets; coerce back to 0
    # so a "::" bind accepts both IPv6 and v4-mapped IPv4 in one listener.
    def setsockopt(self, level, optname, value):  # type: ignore[override]
        if level == _socket.IPPROTO_IPV6 and optname == _socket.IPV6_V6ONLY and value:
            value = 0
        return super().setsockopt(level, optname, value)


_socket.socket = _DualStackSocket  # type: ignore[misc]

# Allow `python src/main.py` to resolve the package even without an install.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer  # noqa: E402
import structlog  # noqa: E402

from config import Settings, get_settings  # noqa: E402
from logging_setup import setup_logging  # noqa: E402

app = typer.Typer(
    name="bg-zammad-mcp",
    help="Remote MCP server for self-hosted Zammad (v6.x / v7.x).",
    no_args_is_help=False,
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """Default to `serve` when called with no subcommand."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(serve)


# ── serve ─────────────────────────────────────────────────────────────────────


@app.command()
def serve(
    host: Optional[str] = typer.Option(
        None, "--host", help="Bind address (overrides MCP_HOST)"
    ),
    port: Optional[int] = typer.Option(
        None, "--port", help="Listen port (overrides MCP_PORT)"
    ),
    transport: Optional[str] = typer.Option(
        None,
        "--transport",
        help="MCP transport: streamable-http (default) or stdio",
    ),
) -> None:
    """Run the MCP server (default mode)."""
    settings = get_settings()
    if host is not None:
        settings.mcp_host = host
    if port:
        settings.mcp_port = port
    chosen_transport = transport or settings.mcp_transport

    asyncio.run(_serve_async(settings, chosen_transport))


async def _serve_async(settings: Settings, transport: str) -> None:
    """Build the app and hand off to FastMCP's transport runner."""
    from server import build_app

    mcp = await build_app(settings)
    # Empty MCP_HOST means "any stack, any interface" — bind "::" so the
    # dual-stack monkey-patch above produces a v6+v4 listener.
    bind_host = settings.mcp_host or "::"
    logger = structlog.stdlib.get_logger("bg-zammad-mcp.main")
    logger.info(
        "server.starting",
        transport=transport,
        host=bind_host,
        port=settings.mcp_port,
    )

    if transport == "stdio":
        await mcp.run_stdio_async()
    elif transport == "streamable-http":
        await mcp.run_http_async(
            host=bind_host,
            port=settings.mcp_port,
            transport="streamable-http",
        )
    else:
        raise typer.BadParameter(f"Unsupported transport: {transport!r}")


# ── tools ─────────────────────────────────────────────────────────────────────


@app.command(name="tools")
def list_tools(
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Write the catalogue to a file (default: stdout)"
    ),
) -> None:
    """Render the registered tool catalogue as Markdown."""
    from server import build_app

    async def _render() -> str:
        settings = get_settings()
        mcp = await build_app(settings)
        tools = await mcp.list_tools()
        tools_by_name = {getattr(t, "name", str(i)): t for i, t in enumerate(tools)}

        lines: list[str] = [
            "# Zammad MCP - Tool Catalogue",
            "",
            "> Auto-generated from the registered FastMCP tools. Do not hand-edit.",
            "",
            f"**Tool count:** {len(tools_by_name)}",
            "",
        ]
        for tool_name in sorted(tools_by_name):
            tool = tools_by_name[tool_name]
            description = getattr(tool, "description", "") or ""
            lines.append(f"## `{tool_name}`")
            lines.append("")
            if description:
                lines.append(description.strip())
                lines.append("")
        return "\n".join(lines)

    markdown = asyncio.run(_render())
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8", newline="\n")
        typer.echo(f"wrote: {output}")
    else:
        typer.echo(markdown)


# ── health ────────────────────────────────────────────────────────────────────


@app.command()
def health() -> None:
    """Probe Zammad reachability and exit 0 (healthy) or 1 (unhealthy)."""
    from zammad.client import build_zammad_client_from_settings

    async def _probe() -> int:
        settings = get_settings()
        setup_logging(log_format="console", log_level="INFO")
        logger = structlog.stdlib.get_logger("bg-zammad-mcp.health")
        client = build_zammad_client_from_settings(settings)
        try:
            result = await client.health()
            logger.info("health.ok", zammad=result)
            return 0
        except Exception as exc:  # noqa: BLE001
            logger.error("health.failed", error=str(exc))
            return 1
        finally:
            await client.aclose()

    raise typer.Exit(asyncio.run(_probe()))


# ── probe ─────────────────────────────────────────────────────────────────────


@app.command()
def probe() -> None:
    """Detect the live Zammad version and exit (0 = detected, 1 = not detected)."""
    from zammad.client import build_zammad_client_from_settings
    from zammad.version_probe import probe_zammad_version

    async def _run() -> int:
        settings = get_settings()
        setup_logging(log_format="console", log_level="INFO")
        logger = structlog.stdlib.get_logger("bg-zammad-mcp.probe")
        client = build_zammad_client_from_settings(settings)
        try:
            info = await probe_zammad_version(client)
            if info.raw:
                logger.info(
                    "probe.detected", version=info.raw, major=info.major_label
                )
                return 0
            logger.error(
                "probe.failed",
                hint="Configure ZAMMAD_API_TOKEN to allow /version access",
            )
            return 1
        finally:
            await client.aclose()

    raise typer.Exit(asyncio.run(_run()))


# ── entry ─────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    app()
