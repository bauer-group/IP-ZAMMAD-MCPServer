## [5.0.5](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v5.0.4...v5.0.5) (2026-08-10)

### ♻️ Refactoring

* **ui:** dropped the duplicate documentation card ([248a6e6](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/248a6e6679a3ae5d6458950917bffed596668b72))

## [5.0.4](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v5.0.3...v5.0.4) (2026-08-10)

### 🐛 Bug Fixes

* **ui:** removed the landing-page card that never resolved ([6e9832d](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/6e9832def4b57a204014827f335e3f645df0b913))

## [5.0.3](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v5.0.2...v5.0.3) (2026-08-06)

## [5.0.2](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v5.0.1...v5.0.2) (2026-08-03)

### 🐛 Bug Fixes

* **knowledge:** get_kb_answer never returned the answer body ([51f2bde](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/51f2bde245d5c0c2020283de755b273d0b70746d))

## [5.0.1](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v5.0.0...v5.0.1) (2026-08-02)

### 🐛 Bug Fixes

* **build:** skipped the Dockerfile port checks inside the image build ([921832c](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/921832c36f6aa8f47bc091b414a335e16543a697))

## [5.0.0](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v4.0.1...v5.0.0) (2026-08-02)

### ⚠ BREAKING CHANGES

* moved the container port to 8080, host stays 8000

### 🔨 Build

* moved the container port to 8080, host stays 8000 ([fb38e6e](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/fb38e6e5fb1672eb3ecac14318074c6822bcd519))

## [4.0.1](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v4.0.0...v4.0.1) (2026-08-02)

### 🐛 Bug Fixes

* **branding:** replaced the invented logo with the real BAUER GROUP mark ([3ad49d3](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/3ad49d317c6bd68ccabadd57709fcd1f6ba9f3e6))

## [4.0.0](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v3.0.0...v4.0.0) (2026-08-02)

### ⚠ BREAKING CHANGES

* **tools:** closed the last vocabulary splits and two field gaps

### 🚀 Features

* **tools:** closed the last vocabulary splits and two field gaps ([ac6ea35](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/ac6ea3535e4615356c607feb5bd0ce8e76d0e47b))

## [3.0.0](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v2.0.0...v3.0.0) (2026-08-02)

### ⚠ BREAKING CHANGES

* **tools:** unified the collection envelope and pagination

### 🚀 Features

* **tools:** unified the collection envelope and pagination ([6221eda](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/6221eda183b702a2c11984e9214148aa6d88a3d6))

## [2.0.0](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v1.0.1...v2.0.0) (2026-08-02)

### ⚠ BREAKING CHANGES

* **tools:** unified the identifier and visibility vocabulary

### 🚀 Features

* **tools:** unified the identifier and visibility vocabulary ([378989e](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/378989eec0f82c4b38eeac3d8e632cf7ace0d7e2))

## [1.0.1](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v1.0.0...v1.0.1) (2026-08-02)

### 🐛 Bug Fixes

* **auth:** opened dynamic client registration to any MCP client ([7f39400](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/7f39400e417b2aa9c851ef5bf1cb146b819e1db0))

## [1.0.0](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v0.1.10...v1.0.0) (2026-08-02)

### ⚠ BREAKING CHANGES

* **tools:** suggest_kb_answers is renamed to
draft_kb_answer_from_ticket and no longer returns knowledge-base
suggestions. Use search_knowledge_base to find existing answers.
* **tools:** create_ticket_article is replaced by reply_to_customer
and add_internal_note. create_ticket's `type` parameter is renamed to
`ticket_type`, and its opening article is customer-visible by default.

### 🚀 Features

* **profile:** added prompts, resources and the framework registry tools ([ccec77b](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/ccec77bb10633d9b8856918a442006e0a344db53))
* **tools:** added the Zammad 7 workflow surface (36 -> 75 tools) ([3c5bb8c](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/3c5bb8c2ab9dbffc5a2adab73fb9e31c1f52ef90)), closes [#show](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/issues/show)
* **tools:** split the article write path by visibility and fixed search ([3969d4b](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/3969d4b31740b89bb9fb919462c45bbe5fd84e0c))

### 🐛 Bug Fixes

* **auth:** corrected the OAuth scope and added fail-closed boot guards ([10b964a](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/10b964a323ad06f3b1039b97df9fafd02605626e))
* **docker:** made the image test stage a real gate ([1813e81](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/1813e812a87ff9a106733507a417b3b60ab8e7cb))
* **test:** pinned the OAuth store path in the wiring fixture ([91483d6](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/91483d6c16d0af7438f7a7627381807200d5b35f))
* **tools:** corrected the knowledge-base AI tool to match its endpoint ([19fcf51](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/19fcf512c1303d922e90f9426779e2d360328607)), closes [KnowledgeBaseAnswersController#create](https://github.com/bauer-group/KnowledgeBaseAnswersController/issues/create)

### ⚡ Performance

* **auth:** pooled the verifier client, cached verifications, hardened DCR ([c7d3367](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/c7d336735fcf8684a23916b8fe2fbe03e8ef545b))
* **tools:** trimmed list responses and tagged the surface ([3ccf127](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/3ccf127563da0dc0e4960bce8fa45496235f8e5b))

## [0.1.10](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v0.1.9...v0.1.10) (2026-07-31)

## [0.1.9](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v0.1.8...v0.1.9) (2026-07-25)

### 🐛 Bug Fixes

* **ci:** added the missing permissions block ([cf2e8ac](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/commit/cf2e8acb34a4798f91a26146a3e7f20d55ecbb57))

## [0.1.8](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/compare/v0.1.7...v0.1.8) (2026-06-23)

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
