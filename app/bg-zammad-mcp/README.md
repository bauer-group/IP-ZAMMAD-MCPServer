# Zammad MCP Server

OAuth-gated remote MCP bridge for self-hosted [Zammad](https://zammad.org)
(v6.x / v7.x). See the [repository README](../../README.md) and
[docs/](../../docs/) for installation, authentication setup, and
client-connection instructions.

## Internal Architecture

```text
src/
  config.py              Pydantic-Settings model + AUTH_MODE / ROLE validation
  logging_setup.py       structlog + Rich (console / json modes)
  server.py              FastMCP construction + lifespan + middleware wiring
  main.py                Typer CLI (serve / tools / health / probe)
  rate_limit.py          Token-bucket limiter, OAuth-subject / proxy-aware IP keyed
  auth/
    provider_factory.py  AUTH_MODE -> concrete provider
    zammad_oauth.py      Zammad as OAuth2 provider (user-context token forwarding)
    generic_oidc.py      External OIDC (Entra, Keycloak, ...) + ZAMMAD_API_TOKEN
    role_middleware.py   Role-based MCP access gating (Admin / Agent / Customer)
    client_storage.py    Encrypted OAuth state store (Redis or disk fallback)
    upstream_token.py    Helper to retrieve the user's Zammad bearer token
  zammad/
    client.py            Async httpx wrapper (Bearer or Token=, retries, errors)
    errors.py            Typed exception hierarchy
    version_probe.py     Detect Zammad v6/v7 at startup
    tools/
      __init__.py        Registers all tools with the FastMCP instance
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
