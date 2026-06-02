# Testing

This server is a thin consumer of the shared **bg-mcpcore** framework, so the
cross-cutting machinery (settings invariants, OAuth-state storage, rate limiter,
generic OIDC, logging redaction, the HTTP client + retry) is tested **once, in
bg-mcpcore's own suite**. The tests here cover only the Zammad-specific seams.

| Layer | Tool | Purpose |
| --- | --- | --- |
| **Unit / integration** | `pytest` | Settings validation, the per-user on-behalf-of token resolver, the role-allowlist gate, the typed error mapping. Runs in the Docker test stage. |
| **End-to-end** | MCP Inspector | Drives the full OAuth flow and exercises tools against a live Zammad. |
| **Local smoke** | `AUTH_MODE=none` | Run the whole stack unauthenticated against a real Zammad for iteration. |

## Unit / integration tests

```bash
cd app/bg-zammad-mcp
pip install ".[test]"
pytest -v
```

The suite covers the Zammad-specific surface:

- **`test_config.py`** — the `Settings` subclass: per-`AUTH_MODE` credential
  validation (`validate_provider_auth`) on top of bg-mcpcore's universal
  fail-closed invariants (none-in-production, JWT signing key, Fernet storage
  key), plus role-allowlist parsing and the Zammad URL accessors.
- **`test_zammad_errors.py`** — the typed exception hierarchy mapped from
  Zammad's JSON error bodies (raised by the tool decoding shim on non-2xx).

The per-user on-behalf-of resolver (`per_user_token`) and the role/claim gate
(`access_control`) are now bg-mcpcore building blocks, tested once in
**bg-mcpcore's own suite** — this server consumes them via the profile.

```text
$ pytest -q
........................                                      [100%]
24 passed
```

**Test-gated Docker builds:** the Dockerfile's production stage `COPY --from=test`
declaration forces the build to fail if any test fails. The test stage installs
bg-mcpcore from its GitHub tag (the builder/test images include `git`), so the
exact dependency used in production is the one tested. There is no path to a
green image with red tests.

## MCP Inspector (end-to-end)

The Inspector is the official MCP debugging UI:

```bash
npx @modelcontextprotocol/inspector
```

1. **Transport:** Streamable HTTP.
2. **URL:** `https://your-mcp.example.com/mcp`.
3. The Inspector handles DCR registration + the OAuth flow.

In the Inspector you can browse the live tool catalogue (36 tools — `list_tickets`,
`search_tickets`, `get_me`, …), run a tool against real Zammad data with the
authenticated user's permissions, and inspect the AccessToken claims FastMCP
attached (including the upstream Zammad token in `claims.upstream_access_token`).

## Local smoke against a real Zammad

To exercise the full stack without registering an OAuth application, run it
unauthenticated (development only):

```bash
ENVIRONMENT=development \
AUTH_MODE=none \
ZAMMAD_URL=https://your-zammad.example.com \
ZAMMAD_API_TOKEN=<a personal access token> \
docker compose -f docker-compose.development.yml up
```

Then point the MCP Inspector at `http://localhost:8000/mcp`. No OAuth prompts —
the endpoint is unprotected — but every tool call hits your real Zammad. The
container refuses to start with `AUTH_MODE=none` and `ENVIRONMENT=production`.

> `ZAMMAD_URL` must be present in the environment (the profile resolves it via
> `${env:ZAMMAD_URL}`); the compose files and `.env`/`env_file` provide it.

## Liveness

Container/orchestrator liveness uses the unauthenticated `/healthz` route
(returns `{"status": "ok"}`), wired into the Docker `HEALTHCHECK`. There is no
longer a `health` / `probe` / `tools` CLI subcommand — the server exposes only
`serve`.
