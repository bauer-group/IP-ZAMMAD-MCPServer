"""
Zammad version probe.

Calls /api/v1/version at startup so the log banner shows the live Zammad
major version, and surfaces the detected major (v6 / v7 / unknown) for any
tool that needs to branch on it. The result is informational: the bulk of
the API is identical across v6 and v7, and tools that DO need to branch
(knowledge-base shape, mention endpoint format) read this value at call
time, not at boot.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from .client import ZammadClient
from .errors import ZammadError

logger = structlog.stdlib.get_logger("bg-zammad-mcp.zammad.version")


@dataclass(frozen=True)
class ZammadVersionInfo:
    """Result of the startup probe."""

    raw: str | None
    major: int | None

    @property
    def major_label(self) -> str:
        if self.major is None:
            return "unknown"
        return f"v{self.major}"


async def probe_zammad_version(
    client: ZammadClient, *, bearer_token: str | None = None
) -> ZammadVersionInfo:
    """Best-effort version detection.

    Returns ZammadVersionInfo(raw=None, major=None) on any failure -
    the probe is non-fatal because some operators run /version behind
    extra protection and it doesn't affect tool correctness.
    """
    try:
        payload = await client.version(bearer_token=bearer_token)
    except ZammadError as exc:
        logger.warning("zammad.version_probe_failed", error=str(exc))
        return ZammadVersionInfo(raw=None, major=None)

    raw = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(raw, str):
        return ZammadVersionInfo(raw=None, major=None)

    # Zammad reports e.g. "6.4.0", "7.0.0-snapshot", "6.5.0-1736.1738254..."
    head = raw.split(".", 1)[0].strip()
    try:
        major = int(head)
    except ValueError:
        return ZammadVersionInfo(raw=raw, major=None)

    return ZammadVersionInfo(raw=raw, major=major)


__all__ = ["ZammadVersionInfo", "probe_zammad_version"]
