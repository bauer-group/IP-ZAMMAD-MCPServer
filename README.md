# bg-zammad-mcp — BAUER GROUP Zammad MCP Server

> **OAuth-gated remote MCP bridge** that exposes your self-hosted
> [Zammad](https://zammad.org) helpdesk (v6.x / v7.x) to AI clients —
> Claude Web Connectors, Claude Desktop, Microsoft 365 Copilot,
> ChatGPT Connectors, Cursor, Continue.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker Image](https://img.shields.io/badge/ghcr.io-bg--zammad--mcp-blue?logo=docker)](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/pkgs/container/ip-zammad-mcpserver%2Fbg-zammad-mcp)
[![FastMCP](https://img.shields.io/badge/built%20with-FastMCP-purple)](https://gofastmcp.com)
[![Zammad v6/v7](https://img.shields.io/badge/Zammad-v6%20%2F%20v7-orange)](https://zammad.org)

---

## Highlights

| Feature | What it means |
| --- | --- |
| **User-context** | The user's Zammad access token is forwarded end-to-end. Zammad sees every API call as coming from the actual person, not a shared service account — its own role and permission system enforces fine-grained access. |
| **Zammad as OAuth IdP** | Primary auth mode uses Zammad's built-in **OAuth2 Applications** feature (Admin → Manage → OAuth2 Applications → Add). No external IdP required. |
| **External OIDC also supported** | Falls back to any standard OIDC provider — Entra ID, Keycloak, Authentik, Zitadel, Auth0, Okta — paired with a static Zammad API token. |
| **Role-based MCP access** | `MCP_ALLOWED_ROLES=Admin,Agent` lets you say "this MCP is for staff only, never expose to Customers" as a coarse gate above Zammad's per-endpoint permissions. |
| **v6 / v7 compatible** | The shared `/api/v1` surface is exercised against both majors. Runtime version probe surfaces the live release in the boot banner. |
| **36 hand-curated tools** | Tickets, articles, users, organizations, groups, tags, reference data, notifications. Auto-run safety encoded as MCP tool annotations. |
| **Multi-arch** | `linux/amd64` and `linux/arm64` images on GHCR. |
| **Three deploy flavours** | Local development, self-hosted Traefik, Coolify-managed — same image, same source. |
| **Test-gated builds** | The Docker multi-stage build fails if pytest fails — no green push when tests are red. |
| **Restart-safe sessions** | Encrypted OAuth state store: Redis-backed (production) or disk-backed (single-node fallback), both Fernet-encrypted at rest. |
| **Token-bucket rate limiting** | Per-OAuth-subject (or proxy-aware client-IP) limiter on every MCP request. |

---

## Architecture

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
        │   │  Inbound: Zammad OAuth | external OIDC | none      │ │
        │   └────────────────────────────────────────────────────┘ │
        │   ┌────────────────────────────────────────────────────┐ │
        │   │  Role allowlist middleware (Admin/Agent/Customer)  │ │
        │   └────────────────────────────────────────────────────┘ │
        │   ┌────────────────────────────────────────────────────┐ │
        │   │  36 hand-curated MCP tools                         │ │
        │   │   list_tickets, search_tickets, create_ticket,     │ │
        │   │   get_me, list_users, list_groups, …               │ │
        │   └────────────────────────────────────────────────────┘ │
        │   ┌────────────────────────────────────────────────────┐ │
        │   │  Outbound httpx client                             │ │
        │   │    Bearer <upstream user token>  (zammad mode)     │ │
        │   │    Token token=<api token>       (oidc / none)     │ │
        │   └─────────────────────────┬──────────────────────────┘ │
        └────────────────────────────┼─────────────────────────────┘
                                     │ HTTPS
                                     ▼
                        ┌────────────────────────┐
                        │  Zammad REST API       │
                        │  /api/v1/* (v6 / v7)   │
                        └────────────────────────┘
```

In `AUTH_MODE=zammad`, the OAuth token authenticates the inbound MCP
call AND the outbound Zammad call. Everything happens in the
authenticated user's context. In OIDC / none modes, the inbound and
outbound trust boundaries are separate (external IdP inbound + static
API token outbound) — Zammad sees one shared identity for every user.

---

## Quick Start

### 1 · Generate environment file

```bash
python scripts/generate-env.py
```

Then edit `.env` to fill:

- `ZAMMAD_URL` (your Zammad base URL)
- `AUTH_MODE=zammad` (the recommended default)
- `ZAMMAD_OAUTH_CLIENT_ID` + `ZAMMAD_OAUTH_CLIENT_SECRET` — generated in
  Zammad: **Admin → Manage → OAuth2 Applications → Add**.
- `PUBLIC_BASE_URL` matching the hostname you'll expose.
- `MCP_ALLOWED_ROLES=Admin,Agent` (or whatever role mix fits your case).

### 2 · Pick a deployment flavour

```bash
# Local development (LOG_FORMAT=console, AUTH_MODE=none allowed for testing)
docker compose -f docker-compose.development.yml up -d

# Self-hosted Traefik (HTTPS, Let's Encrypt)
docker compose -f docker-compose.traefik.yml up -d

# Coolify — paste env into the dashboard, deploy from this compose file
docker compose -f docker-compose.coolify.yml up -d
```

### 3 · Connect from a client

See [docs/client-setup.md](docs/client-setup.md). Short version:

| Client | URL |
| --- | --- |
| Claude Web | Settings → Connectors → Add custom → `https://your-host/mcp` |
| Claude Desktop | Add to `mcp.json`: `{"command": "npx", "args": ["mcp-remote", "https://your-host/mcp"]}` |
| Microsoft 365 Copilot Studio | Custom connector → MCP → `https://your-host/mcp` |
| Cursor / Continue | Add to `mcp.json` with the same URL |

---

## Configuring Zammad as the OAuth2 provider

This is the primary auth mode (`AUTH_MODE=zammad`) and uses Zammad's
built-in OAuth2 Applications feature shipped in Zammad v6.0 and later.

1. Sign in to Zammad as an **administrator**.
2. Open **Admin → Manage → OAuth2 Applications**.
3. Click **+ Add** and fill in:

   | Field | Value |
   | --- | --- |
   | **Name** | `BAUER GROUP MCP` (any human-friendly label) |
   | **Redirect URI** | `${PUBLIC_BASE_URL}/auth/callback` (must match exactly) |
   | **Scopes** | `read write` |
   | **Confidential** | Yes |

4. Save and copy the generated **Client ID** + **Client Secret** into
   `.env` (`ZAMMAD_OAUTH_CLIENT_ID` / `ZAMMAD_OAUTH_CLIENT_SECRET`).
   The secret cannot be retrieved later — save it immediately.

Full walkthrough with the user-context flow diagram: [docs/authentication.md](docs/authentication.md).

---

## Role-based MCP access

`MCP_ALLOWED_ROLES` is a coarse gate that controls **who can use the MCP
server at all**, on top of Zammad's own per-endpoint permission system.

```env
# Internal MCP for agents and admins (the default)
MCP_ALLOWED_ROLES=Admin,Agent

# Admins only - automation-style deployments
MCP_ALLOWED_ROLES=Admin

# Customer-facing self-service MCP
MCP_ALLOWED_ROLES=Customer
```

Custom Zammad role names work — pass whatever appears in Zammad's role
list. Casing and whitespace are normalised. Full details:
[docs/role-based-access.md](docs/role-based-access.md).

---

## Repository Layout

```text
IP-ZAMMAD-MCPServer/
├── README.md                        ← you are here
├── LICENSE                          ← MIT
├── CHANGELOG.md
├── .env.example                     ← canonical config surface
├── docker-compose.development.yml   ← local dev
├── docker-compose.traefik.yml       ← self-hosted production
├── docker-compose.coolify.yml       ← Coolify deployment
│
├── scripts/
│   ├── generate-env.py              ← cross-platform .env secret generator
│   └── dev-inspector.py             ← one-command MCP Inspector launcher
│
├── app/
│   └── bg-zammad-mcp/               ← the only image this repo builds
│       ├── Dockerfile               ← multi-stage, test-gated
│       ├── pyproject.toml
│       ├── README.md                ← internal architecture
│       ├── src/
│       │   ├── main.py              ← Typer CLI (serve / tools / health / probe)
│       │   ├── config.py            ← Pydantic Settings + AUTH_MODE / role validation
│       │   ├── server.py            ← FastMCP construction + middleware wiring
│       │   ├── rate_limit.py        ← Token-bucket limiter (sub/IP keyed)
│       │   ├── logging_setup.py     ← structlog + Rich
│       │   ├── auth/
│       │   │   ├── provider_factory.py
│       │   │   ├── zammad_oauth.py  ← Zammad as OAuth2 IdP
│       │   │   ├── generic_oidc.py  ← External OIDC fallback
│       │   │   ├── role_middleware.py ← MCP_ALLOWED_ROLES enforcement
│       │   │   ├── client_storage.py ← Encrypted Redis/disk OAuth store
│       │   │   └── upstream_token.py ← Resolves user's upstream Zammad token
│       │   ├── zammad/
│       │   │   ├── client.py        ← async httpx wrapper (Bearer + Token=)
│       │   │   ├── errors.py        ← Typed exception hierarchy
│       │   │   ├── version_probe.py ← v6/v7 detection
│       │   │   └── tools/
│       │   │       ├── tickets.py
│       │   │       ├── articles.py
│       │   │       ├── users.py
│       │   │       ├── organizations.py
│       │   │       ├── groups.py
│       │   │       ├── tags.py
│       │   │       ├── reference.py
│       │   │       └── notifications.py
│       │   └── static/
│       │       ├── index.html       ← landing page served at /
│       │       └── logo.svg         ← consent-screen brand asset
│       └── tests/                   ← pytest (70+ tests, gates Docker build)
│
├── docs/
│   ├── ZAMMAD-MCP-SPEC.md           ← design specification
│   ├── installation.md
│   ├── authentication.md
│   ├── role-based-access.md
│   ├── client-setup.md
│   ├── testing.md
│   └── troubleshooting.md
│
└── .github/
    ├── CODEOWNERS
    ├── dependabot.yml
    ├── config/
    │   ├── docker-base-image-monitor/base-images.json
    │   └── release/semantic-release.json
    └── workflows/
        ├── docker-release.yml       ← semantic-release + build/push
        ├── docker-maintenance.yml   ← auto-merge Dependabot
        ├── check-base-images.yml    ← daily base-image scan
        ├── ai-issue-summary.yml
        └── teams-notifications.yml
```

---

## Supported Versions

| Component | Tested | Notes |
| --- | --- | --- |
| Python | 3.13 / 3.14 | Container ships 3.14 Alpine |
| Zammad | **v6.0+** / **v7.x** | OAuth2 Applications feature requires v6.0; v5.x and earlier can use `AUTH_MODE=oidc` + `ZAMMAD_API_TOKEN` |
| FastMCP | ≥ 3.0 | `OIDCProxy`, `OAuthProxy`, streamable-http transport |
| Redis | 7.x / 8.x | OAuth client storage (recommended for production) |

---

## Documentation

- [docs/installation.md](docs/installation.md) — step-by-step deploy for each flavour
- [docs/authentication.md](docs/authentication.md) — Zammad OAuth2 setup + external OIDC walkthroughs
- [docs/role-based-access.md](docs/role-based-access.md) — `MCP_ALLOWED_ROLES` model
- [docs/client-setup.md](docs/client-setup.md) — adding the connector in each AI client
- [docs/testing.md](docs/testing.md) — local & remote testing with MCP Inspector
- [docs/troubleshooting.md](docs/troubleshooting.md) — common errors & fixes
- [docs/ZAMMAD-MCP-SPEC.md](docs/ZAMMAD-MCP-SPEC.md) — design specification (source of truth)

---

## Related Projects

- **[zammad/zammad](https://github.com/zammad/zammad)** — upstream Zammad helpdesk.
- **[jlowin/fastmcp](https://github.com/jlowin/fastmcp)** — the MCP framework
  underneath. `OAuthProxy`, `OIDCProxy`, and the streamable-HTTP transport
  all live here.
- **[bauer-group/Shlink-MCPServer](https://github.com/bauer-group/IP-Shlink-MCPServer)** —
  sister project: same architectural pattern applied to self-hosted Shlink
  (URL shortener).

---

## License

MIT — see [LICENSE](LICENSE).

---

*Maintained by [BAUER GROUP](https://bauer-group.com) · Today, Tomorrow, Together*
