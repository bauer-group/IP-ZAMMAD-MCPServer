## [0.1.7](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v0.1.6...v0.1.7) (2026-06-16)

## [0.1.6](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v0.1.5...v0.1.6) (2026-06-11)

## [0.1.5](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v0.1.4...v0.1.5) (2026-06-08)

### 🐛 Bug Fixes

* **zammad:** honoured ZAMMAD_VERIFY_TLS and ZAMMAD_HTTP_TIMEOUT outbound ([b2a3a90](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/b2a3a9094f2abd0cddfd70df9cc1090a0d17edf6))

### ♻️ Refactoring

* **zammad:** typed tool-register seam and dropped dead config ([e769b7c](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/e769b7c4873b42a8d1ebdcb82fc10ee097ec4850))

## [0.1.4](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v0.1.3...v0.1.4) (2026-06-02)

### ♻️ Refactoring

* **zammad:** adopted declarative auth (access_control + per_user_token + request_json) ([dcfae93](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/dcfae93c3d65c53e50ea2c5085f244374020a078))

## [0.1.3](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v0.1.2...v0.1.3) (2026-06-01)

### ♻️ Refactoring

* migrated onto the shared bg-mcpcore framework (Tier 3) ([0a0d7eb](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/0a0d7eb440ee5ef88c9acee302f5cc1963ffa631))

## [0.1.2](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v0.1.1...v0.1.2) (2026-05-31)

## [0.1.1](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v0.1.0...v0.1.1) (2026-05-30)

### 🐛 Bug Fixes

* corrected GHCR image path to match repo name ([0972df5](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/0972df52ed6862e4cfbc7cdc23661e3ced42d86e))

## [0.1.0](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v0.0.0...v0.1.0) (2026-05-30)

### 🚀 Features

* added initial Zammad MCP server implementation ([202d34a](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/202d34acbb4ea05578daddf5fa96873a6d022565))

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

semantic-release manages this file in production - the first cut below is
the initial scaffolding committed by hand.

## [0.1.0] - 2026-05-27

### Added

- Initial release.
- OAuth-gated remote MCP bridge for self-hosted Zammad (v6.x / v7.x).
- Three authentication modes:
  - `zammad` (default): Zammad acts as the OAuth2 provider; the user's
    upstream access token is forwarded to every Zammad API call,
    preserving user-context end-to-end.
  - `oidc`: external OIDC IdP (Entra, Keycloak, Authentik, Zitadel,
    Auth0, Okta) + static `ZAMMAD_API_TOKEN` fallback.
  - `none`: development-only, no inbound auth, requires
    `ENVIRONMENT=development`.
- Role-based MCP access gating via `MCP_ALLOWED_ROLES` (Admin / Agent /
  Customer / custom Zammad role names). Audit-only mode for safe rollout.
- 33 hand-curated MCP tools covering tickets, articles (messages), users,
  organizations, groups, tags, reference data (states / priorities /
  roles / version), notifications, and ticket subscriptions.
- Tool annotations (readOnlyHint / destructiveHint / idempotentHint)
  signalling auto-run safety to MCP clients.
- Two outbound auth styles - OAuth Bearer (Mode 1) and Zammad-specific
  `Authorization: Token token=<x>` (Modes 2/3) - selected per-call.
- Encrypted OAuth state store with Redis (recommended) or disk-fallback
  backend, both Fernet-encrypted at rest.
- Per-client token-bucket rate limiter, keyed on OAuth subject or
  proxy-aware client IP.
- Three reference compose flavours: development, Traefik, Coolify.
- Multi-arch Docker image (linux/amd64, linux/arm64) on GHCR.
- Test-gated builds: the Docker production stage fails if pytest fails.
- Comprehensive documentation: installation, authentication, role-based
  access, client setup, troubleshooting, testing, design spec.
- CI: semantic-release, Dependabot, base-image monitor, AI issue
  summaries, Teams notifications.
