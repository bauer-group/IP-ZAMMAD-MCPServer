"""
OAuth client-storage factory.

FastMCP's OAuth providers (OIDCProxy, OAuthProxy) persist five classes of
state:

  * DCR-registered client metadata
  * Active authorization codes + PKCE challenges
  * Refresh-token hash -> upstream-token-id mappings
  * FastMCP-issued JWT JTI -> upstream-token mappings  (THIS IS HOW WE GET
    THE ZAMMAD USER TOKEN BACK AT TOOL-CALL TIME)
  * In-flight OAuth transactions (state, nonce, callback context)

If the provider is constructed WITHOUT an explicit `client_storage`, the
FastMCP lib falls back to a DiskStore at `platformdirs.user_data_dir() /
"oauth-proxy"`. Inside a Docker container that path lives on ephemeral
container storage - every restart wipes everything, forcing every client
to re-register via DCR and every user to re-authenticate. This module
fixes that by always supplying an explicit, durable backend.

Two operator-selectable backends, both encrypted at rest:

* AUTH_REDIS_URL set
  -> RedisStore wrapped in FernetEncryptionWrapper using the operator-
    provided AUTH_STORAGE_ENCRYPTION_KEY. Recommended for production:
    survives container restarts (Redis has its own AOF persistence),
    survives container REPLACEMENT (same state across rolling deploys),
    supports horizontal scaling (multiple replicas share one Redis).

* AUTH_REDIS_URL unset
  -> DiskStore at AUTH_DISK_STORAGE_PATH (default /app/data/oauth-storage)
    wrapped in FernetEncryptionWrapper using a key DERIVED from
    AUTH_JWT_SIGNING_KEY via HKDF. Matches FastMCP's own DiskStore
    encryption behaviour but at a known, mount-able path. Operators
    MUST mount this path as a Docker volume; otherwise the disk store
    is just as ephemeral as FastMCP's platformdirs default.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from config import Settings

if TYPE_CHECKING:
    from key_value.aio.protocols import AsyncKeyValue

logger = structlog.stdlib.get_logger("bg-zammad-mcp.auth.storage")


def build_client_storage(settings: Settings) -> "AsyncKeyValue":
    """Construct the configured encrypted OAuth state store.

    Always returns a concrete AsyncKeyValue - never None. Called from each
    provider builder before the provider is instantiated.
    """
    from cryptography.fernet import Fernet
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    if settings.auth_redis_url:
        # Redis-backed mode: operator-controlled key, audit-friendly because
        # the key is a discrete secret in their secret manager / Coolify UI.
        from key_value.aio.stores.redis import RedisStore

        assert settings.auth_storage_encryption_key is not None  # validator-enforced
        key_bytes = settings.auth_storage_encryption_key.get_secret_value().encode("ascii")
        redis_store = RedisStore(url=settings.auth_redis_url)

        logger.info(
            "auth.storage_configured",
            backend="redis",
            url=_sanitize_redis_url(settings.auth_redis_url),
        )
        return FernetEncryptionWrapper(
            key_value=redis_store,
            fernet=Fernet(key_bytes),
        )

    # Disk-backed fallback for operators running without Redis. Encryption
    # key is derived from AUTH_JWT_SIGNING_KEY (already mandatory for any
    # auth mode) via HKDF + Fernet-format encoding - same construction
    # FastMCP uses for its own default DiskStore.
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key
    from key_value.aio.stores.disk import DiskStore

    disk_path = Path(settings.auth_disk_storage_path)
    disk_path.mkdir(parents=True, exist_ok=True)

    derived_key = derive_jwt_key(
        high_entropy_material=settings.auth_jwt_signing_key.get_secret_value(),
        salt="fastmcp-storage-encryption-key",
    )

    logger.info(
        "auth.storage_configured",
        backend="disk",
        path=str(disk_path),
        warning_if_not_mounted=(
            "Mount this path as a Docker volume in production; otherwise OAuth "
            "state is wiped on every container restart."
        ),
    )
    return FernetEncryptionWrapper(
        key_value=DiskStore(directory=disk_path),
        fernet=Fernet(key=derived_key),
    )


def _sanitize_redis_url(url: str) -> str:
    """Strip credentials from a Redis URL before logging.

    redis://user:pass@host:6379/0 -> redis://***@host:6379/0
    """
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("redis", url)
    _credentials, host_part = rest.split("@", 1)
    return f"{scheme}://***@{host_part}"


__all__ = ["build_client_storage"]
