# Zammad MCP Server

OAuth-gated remote MCP bridge for self-hosted [Zammad](https://zammad.org)
(v6.x / v7.x). See the [repository README](../../README.md) and
[docs/](../../docs/) for installation, authentication setup, and
client-connection instructions.

## Internal Architecture

This server is a thin consumer of the shared **[bg-mcpcore](https://github.com/bauer-group/LIB-BG-MCPCore)**
framework (pulled from GitHub, pinned to a release tag). bg-mcpcore provides all
the cross-cutting machinery — the settings base, inbound auth, the encrypted
OAuth-state store, structured logging with PII redaction, rate limiting, the
`/healthz` · `/` · `/logo.svg` routes, the outbound HTTP client, and the
declarative-profile + CLI surface. Only the Zammad-specific parts live here:

```text
src/
  profiles/zammad.json   Declarative profile: backend, auth wiring, tool source
  main.py                4-line entrypoint — make_cli(load_profile(...), Settings)
  config.py              Settings(bg_mcpcore.BaseMcpSettings) — only the Zammad
                         backend / OAuth2 / role fields + per-mode validation
  server.py              The two Zammad seams referenced by the profile:
                           make_obo_resolver  outbound per-user on-behalf-of
                                              AuthHeaderSource (Token vs Bearer,
                                              fail-closed)
                           register           decoding shim -> the tool modules
                                              (so they stay unchanged)
  auth/
    zammad_oauth.py      Zammad as OAuth2 provider
                         (entry point: bg_mcpcore.auth_providers = zammad)
    role_middleware.py   Role-allowlist gate (Admin / Agent / Customer)
                         (entry point: bg_mcpcore.auth_middleware = zammad)
    upstream_token.py    Resolve the user's Zammad bearer token (fail-closed)
  zammad/
    errors.py            Typed exception hierarchy (raised by the shim on non-2xx)
    tools/
      __init__.py        ToolContext Protocol the tool modules call against
      tickets.py         list/search/get/create/update/delete tickets
      articles.py        list/get/create ticket articles (messages, notes)
      users.py           list/search/get/create/update users + get_me
      organizations.py   list/search/get/create/update organizations
      groups.py          list/get groups
      tags.py            add/remove/list tags on objects
      reference.py       ticket states / priorities / roles / version
      notifications.py   list and mark online notifications
  static/
    index.html           Landing page served at /
    logo.svg             Consent-screen brand asset served at /logo.svg
```

The CLI exposes a single `serve` command (the default). Container liveness uses
the unauthenticated `/healthz` route — there is no longer a `health` / `probe` /
`tools` subcommand.

## Two trust boundaries

- **Inbound** (AI client -> MCP) — OAuth 2.1 + PKCE via Zammad (or external OIDC)
- **Outbound** (MCP -> Zammad) — the user's Zammad bearer token (or a static
  API token in `oidc`/`none` modes)

In `zammad` mode the user's identity is preserved end-to-end: every Zammad
API call happens in the context of the authenticated user, so Zammad's own
permission system enforces fine-grained access on every endpoint.

## Build

```bash
docker build --target production -t bg-zammad-mcp . # production only
docker build --target test -t bg-zammad-mcp-test .  # run tests
docker build -t bg-zammad-mcp .                     # full pipeline (test gates production)
```

## Test

```bash
pip install ".[test]"
pytest -v
```
