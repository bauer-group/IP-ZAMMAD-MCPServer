"""
Zammad MCP Server - Structured logging

structlog is the single source of truth. Two output modes:
  - 'console' (dev): Rich-coloured human output, key=value tail
  - 'json'    (prod): one JSON object per line, aggregator-ready

Stdlib `logging` is also routed through structlog so third-party libraries
(httpx, fastmcp, uvicorn) emit in the same shape as our own log lines.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

import structlog
from rich.console import Console
from structlog.stdlib import ProcessorFormatter
from structlog.typing import EventDict, Processor

# Shared console - Rich auto-detects terminal width; force_terminal keeps
# colours alive in Docker logs that lack a real TTY.
console = Console(force_terminal=True, soft_wrap=True)

_initialized = False


def setup_logging(log_format: str = "console", log_level: str = "INFO") -> None:
    """Wire structlog + stdlib logging. Idempotent."""
    global _initialized
    if _initialized:
        return

    level = getattr(logging, log_level.upper(), logging.INFO)

    timestamper: Processor = structlog.processors.TimeStamper(
        fmt="iso", utc=True, key="timestamp"
    )

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.StackInfoRenderer(),
        _drop_sensitive_keys,
        timestamper,
    ]

    if log_format == "json":
        renderer: Processor = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True, pad_event=25)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=shared_processors,
        )
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet down very chatty libraries even at INFO.
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))
    logging.getLogger("httpcore").setLevel(max(level, logging.WARNING))
    logging.getLogger("hpack").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.format_exc_info,
            ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _initialized = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structured logger - call after setup_logging()."""
    return structlog.stdlib.get_logger(name or "bg-zammad-mcp")


# ── Processors ───────────────────────────────────────────────────────────────

# Substrings that, if present in a key, mark a value as sensitive and should
# never appear in logs verbatim. Add aggressively - false positives just print
# `***` instead of the value.
_SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "signing_key",
    "encryption_key",
    "x-api-key",
    "bearer",
)


def _drop_sensitive_keys(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
    """Replace any value whose key looks sensitive with '***'."""
    for key in list(event_dict.keys()):
        lowered = key.lower()
        if any(frag in lowered for frag in _SENSITIVE_KEY_FRAGMENTS):
            event_dict[key] = "***"
    return event_dict


# ── Startup banner ──────────────────────────────────────────────────────────


def print_banner(
    *,
    version: str,
    environment: str,
    auth_mode: str,
    public_base_url: str,
    zammad_url: str,
    zammad_version: str | None = None,
) -> None:
    """Pretty boot banner - safe to call before setup_logging finished."""
    zv = f" [dim](Zammad {zammad_version})[/dim]" if zammad_version else ""
    console.print(
        f"\n[bold cyan]Zammad MCP Server[/bold cyan] [dim]v{version}[/dim]\n"
        f"  environment : [bold]{environment}[/bold]\n"
        f"  auth_mode   : [bold]{auth_mode}[/bold]\n"
        f"  public_url  : [bold]{public_base_url}[/bold]\n"
        f"  zammad_url  : [bold]{zammad_url}[/bold]{zv}\n"
    )


def warn_no_auth() -> None:
    """Loud warning when AUTH_MODE=none - only allowed in development."""
    console.print(
        "\n[bold red on yellow]  WARNING: AUTH_MODE=none - the MCP endpoint is UNPROTECTED  [/bold red on yellow]"
        "\n[yellow]This is only permitted in ENVIRONMENT=development. Never deploy this way.[/yellow]\n"
    )


def warn_role_audit_only() -> None:
    """Loud warning when role-check audit-only is enabled."""
    console.print(
        "\n[bold black on yellow]  NOTICE: MCP_ROLE_CHECK_AUDIT_ONLY=true - role denials are logged but NOT enforced  [/bold black on yellow]"
        "\n[yellow]Use this during rollout only. Switch to enforcement once the allowlist is verified.[/yellow]\n"
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def now_iso() -> str:
    """ISO-8601 UTC timestamp - used in places that need one outside structlog."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
