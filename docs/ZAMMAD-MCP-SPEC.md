# Zammad MCP Server — Design Specification

> Source of truth for the architecture and contract. Tooling references
> this document; it should evolve with the code.

## Goals

1. Bridge a self-hosted [Zammad](https://zammad.org) (v6.x / v7.x) helpdesk
   to MCP-aware AI clients (Claude Web, Claude Desktop, MS Copilot,
   ChatGPT, Cursor, Continue, Inspector).
2. Preserve **user-context** end-to-end: when an agent triages a ticket
   via the AI, Zammad sees the action as coming from that agent — not
   from a service account.
3. Provide a coarse **role-based MCP access gate** in addition to
   Zammad's fine-grained server-side permission system.
4. Support all standard deployment topologies (local, Traefik, Coolify)
   without forking the image.

## Non-goals

- Auto-generating tools from an upstream OpenAPI spec. Zammad does not
  publish a maintained machine-readable OpenAPI document — third-party
  efforts lag the live API by several minor releases. We hand-curate the
  tool surface and trade auto-discovery for type accuracy, idiomatic
  naming, and version-spanning behaviour.
- Bridging Zammad's WebSocket / live-feed API. WebSocket transport is
  not part of MCP's standard set; clients that need live updates would
  poll the relevant `list_*` tools.

## High-level architecture

```text
                              AI Clients
                ┌────────────┬───────────────┬────────────┐
                │ Claude Web │ Claude Desktop│ MS Copilot │ ...
                └─────┬──────┴───────┬───────┴──────┬─────┘
                      │              │              │
                      └──────────────┼──────────────┘
                                     │ HTTPS (OAuth 2.1 + PKCE,
                                     │        Streamable HTTP /mcp)
                                     ▼
                         ┌───────────────────────┐
                         │   Traefik / Coolify   │
                         └──────────┬────────────┘
                                    │
                                    ▼
        ┌──────────────────────────────────────────────────────────┐
        │   bg-zammad-mcp container  (FastMCP, port 8000)          │
        │   ┌────────────────────────────────────────────────────┐ │
        │   │  Inbound auth: zammad-oauth | external OIDC | none │ │
        │   │   /authorize  /token  /register  /auth/callback    │ │
        │   │   /.well-known/oauth-protected-resource            │ │
        │   └────────────────────────────────────────────────────┘ │
        │   ┌────────────────────────────────────────────────────┐ │
        │   │  Role allowlist middleware (zammad-mode only)      │ │
        │   │   MCP_ALLOWED_ROLES enforcement                    │ │
        │   └────────────────────────────────────────────────────┘ │
        │   ┌────────────────────────────────────────────────────┐ │
        │   │  ~33 hand-curated MCP tools                        │ │
        │   │   tickets, articles, users, organizations,         │ │
        │   │   groups, tags, reference, notifications           │ │
        │   └────────────────────────────────────────────────────┘ │
        │   ┌────────────────────────────────────────────────────┐ │
        │   │  Outbound httpx client                             │ │
        │   │   user's Bearer token (zammad mode)                │ │
        │   │   - or -                                           │ │
        │   │   Token token=<static api-token> (oidc/none)       │ │
        │   └─────────────────────────┬──────────────────────────┘ │
        └────────────────────────────┼─────────────────────────────┘
                                     │ HTTPS
                                     ▼
                        ┌────────────────────────┐
                        │  Zammad REST API       │
                        │  /api/v1/* (v6 / v7)   │
                        └────────────────────────┘
```

## Auth modes

| Mode | Inbound | Outbound | Role gate |
| --- | --- | --- | --- |
| `zammad` (default) | Zammad as OAuth2 IdP (OAuth2 Applications feature, v6.0+) | User's bearer token forwarded | Enforced |
| `oidc` | External OIDC IdP (Entra, Keycloak, ...) | Static `ZAMMAD_API_TOKEN` | No-op (no role claims) |
| `none` (dev only) | No auth | Static `ZAMMAD_API_TOKEN` | No-op |

## User-context flow (AUTH_MODE=zammad)

```text
1. AI client -> MCP /mcp                                          (no token)
2. MCP -> AI client                                               (401 + WWW-Authenticate)
3. AI client -> MCP /authorize                                    (PKCE challenge)
4. MCP -> Zammad /oauth/authorize?...                             (redirect)
5. User logs into Zammad, consents to read+write
6. Zammad -> MCP /auth/callback?code=...                          (redirect)
7. MCP -> Zammad /oauth/token                                     (POST code)
8. Zammad -> MCP                                                  (access_token + refresh_token)
9. MCP -> Zammad /api/v1/users/me                                 (validate + fetch roles)
10. MCP stores upstream token in client_storage, indexed by jti
11. MCP -> AI client                                              (FastMCP-issued JWT)

For every subsequent tool call:
12. AI client -> MCP /mcp  (FastMCP JWT)
13. Role middleware checks JWT claims against MCP_ALLOWED_ROLES
14. Tool dispatcher retrieves upstream token by jti
15. ZammadClient -> Zammad API                                    (Authorization: Bearer <upstream>)
```

The Zammad access token never leaves the server, and the AI client never
sees a token that Zammad would directly accept. The FastMCP-issued JWT
is signed with `AUTH_JWT_SIGNING_KEY` and validates only against this
specific MCP instance.

## Tool surface

| Module | Tool count | Coverage |
| --- | --- | --- |
| `tickets.py` | 6 | list / search / get / create / update / delete |
| `articles.py` | 3 | list (by ticket) / get / create |
| `users.py` | 6 | `get_me` + list / search / get / create / update |
| `organizations.py` | 5 | list / search / get / create / update |
| `groups.py` | 2 | list / get (read-only) |
| `tags.py` | 5 | list (per-object / all) / search / add / remove |
| `reference.py` | 4 | states / priorities / roles / version |
| `notifications.py` | 5 | online notifications + ticket subscribe/unsubscribe |
| **Total** | **~36** | — |

The full live catalogue is at [tools.md](./tools.md). The tool surface is the
hand-written `python` source registered via the profile (`server:register`).

## Compatibility matrix

| Component | Supported |
| --- | --- |
| Zammad | v6.0+, v7.x (tested) |
| Python (runtime) | 3.13 / 3.14 |
| FastMCP | ≥ 3.0.0 |
| Redis (OAuth state) | 7.x / 8.x |
| Container base | `python:3.14-alpine` |
| Architectures | linux/amd64, linux/arm64 |

v5.x / v4.x / v3.x of Zammad lack the OAuth2 Applications UI. They can
still be used via `AUTH_MODE=oidc` + a static `ZAMMAD_API_TOKEN` — Zammad
has supported Personal Access Tokens since v3.5.

## Trust model

- **`AUTH_JWT_SIGNING_KEY`** — symmetric key for FastMCP-issued JWTs and
  (in disk-storage mode) for deriving the storage encryption key via HKDF.
  Must survive restarts. Compromise lets an attacker forge MCP session
  tokens.
- **`AUTH_STORAGE_ENCRYPTION_KEY`** — Fernet key for at-rest encryption
  of OAuth state in Redis-backed storage. Compromise + access to Redis
  exposes upstream Zammad tokens.
- **`ZAMMAD_OAUTH_CLIENT_SECRET`** — proves to Zammad that this MCP
  instance is the legitimate client. Compromise lets an attacker stand
  up a parallel MCP that impersonates this one to Zammad's IdP layer.
- **`ZAMMAD_API_TOKEN`** — static Personal Access Token. Has whatever
  permissions the owner Zammad user has. Used in `oidc`/`none` modes for
  every API call regardless of which MCP user triggered them.

## Rate-limit identity

Per-client token-bucket. Client ID is derived as:

1. Authenticated → `sub:<oauth-subject>` (most stable, never spoofable
   by the caller).
2. Anonymous + proxy → `ip:<value at XFF position -trusted_proxy_hops>`.
3. Anonymous + no proxy → `ip:<request.client.host>`.
4. Stdio transport → `ip:local` (single shared bucket; stdio is
   local-only by definition).

`RATE_LIMITER_TRUSTED_PROXY_HOPS` is the trust seam: it's the number of
proxies in front of this server that we trust to have stamped XFF
accurately. Anything to the left of position `-N` was forwarded by a
proxy we don't control and could be spoofed by the client.

## Persistence model

- **OAuth state** survives container restarts via Redis (recommended) or
  encrypted disk (mounted volume). Without this, every restart forces
  DCR re-registration and user re-authentication.
- **Upstream Zammad tokens** are stored in the same backend, encrypted
  at rest with the same key (Fernet in Redis mode, HKDF-derived Fernet
  in disk mode). The token itself never leaves the server.
- **Cache of /users/me responses** lives in process memory only; it's
  cheap to rebuild on container restart.

## Failure modes

| Failure | Behaviour |
| --- | --- |
| Zammad unreachable at startup | Version probe fails, version logged as `unknown`, container still serves OAuth endpoints. Tool calls error out. |
| Zammad unreachable mid-session | Tool call raises `ZammadTransportError` after 3 retries. AI client sees a JSON-RPC error. Session state is not invalidated. |
| `/users/me` 401 | Token verification fails. MCP rejects the inbound request with 401. AI client retries the OAuth flow. |
| Redis unreachable (Redis mode) | OAuth state lookups fail. New auth flows error out; existing sessions can continue until next refresh. Container logs `auth.storage_lookup_failed`. |
| Disk-store path not writable (disk mode) | Container refuses to start. |
| Role allowlist miss | Request rejected with `RoleNotAllowedError`. Logged as `auth.role_denied`. |
