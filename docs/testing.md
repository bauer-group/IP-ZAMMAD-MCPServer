# Testing

Three layers of verification ship with this repo, in order of increasing
realism:

| Layer | Tool | Purpose |
| --- | --- | --- |
| **Unit / integration** | `pytest` | Config, auth, role mw, client, version probe. Runs in the Docker test stage. |
| **Health & version probe** | `bg-zammad-mcp health` / `probe` | Verifies the live Zammad backend is reachable and the version is what you expect. |
| **End-to-end** | MCP Inspector | Drives the full OAuth flow and exercises tools against a live Zammad. |

## Unit / integration tests

```bash
cd app/bg-zammad-mcp
pip install ".[test]"
pytest -v
```

The test suite covers:

- Every branch of the `Settings` validator (AUTH_MODE × ENVIRONMENT
  matrix, Fernet key validation, role allowlist parsing).
- `ZammadClient` auth-header building — Bearer vs `Token token=`, the
  most common configuration mistake.
- Retry policy on transient (`5xx`) and permanent (`4xx`) errors.
- Role allowlist middleware — every branch (passes, denies, audit-only,
  empty allowlist, role-name shape variations).
- Rate-limit client-ID resolution — XFF parsing under different
  proxy-hop counts, the trust-model invariant.
- Provider factory dispatch on each `AUTH_MODE`.
- Upstream-token resolver — every lookup path (embedded claims,
  storage by JTI, storage by sub).
- Version probe parsing (v6.x, v7.x, snapshot tags, malformed).

```text
$ pytest -v
================ test session starts ================
collected 70 items

tests/test_config.py .............. PASSED
tests/test_zammad_client.py ........ PASSED
tests/test_zammad_errors.py ........ PASSED
tests/test_role_middleware.py ...... PASSED
tests/test_rate_limit.py ........... PASSED
tests/test_provider_factory.py .... PASSED
tests/test_upstream_token.py ...... PASSED
tests/test_version_probe.py ....... PASSED
================ 70 passed in 0.85s ================
```

**Test-gated Docker builds:** the Dockerfile's production stage `COPY
--from=test` declaration forces the build to fail if any test fails. CI
and local builds use the same gate — there is no path to a green image
with red tests.

## Health probe

`bg-zammad-mcp health` calls `/api/v1/monitoring/health_check` and exits 0
on success, 1 on failure. Used by the container's Docker `HEALTHCHECK`
directive but also useful from the shell:

```bash
docker exec bg-zammad-mcp python src/main.py health
# 2026-05-27 10:32:11 [info     ] health.ok zammad={'healthy': True, 'message': 'Successful', ...}
```

A failed probe writes:

```text
2026-05-27 10:32:11 [error    ] health.failed error=Zammad timed out after 3 retries: ConnectError(...)
```

## Zammad version probe

`bg-zammad-mcp probe` calls `/api/v1/version` and prints the detected
major + raw version string. Useful to confirm whether you're talking to
v6 or v7 before deciding tool surface caveats:

```bash
docker exec bg-zammad-mcp python src/main.py probe
# 2026-05-27 10:32:11 [info     ] probe.detected version=7.0.0 major=v7
```

## MCP Inspector

The Inspector is the official MCP debugging UI. The recommended
end-to-end test is:

```bash
npx @modelcontextprotocol/inspector
```

1. **Transport:** Streamable HTTP.
2. **URL:** `https://your-mcp.example.com/mcp`.
3. The Inspector handles DCR registration + the OAuth flow.

In the Inspector UI you can:

- **Tools** tab — view the live tool catalogue (~33 tools, names like
  `list_tickets`, `search_tickets`, `get_me`).
- **Tools → list_tickets → Run** — see real Zammad data with the
  authenticated user's permissions.
- **Auth** tab — see which AccessToken claims FastMCP attached
  (including the upstream Zammad token in `claims.upstream_access_token`,
  if the verifier path picked it up).
- **Metadata** tab — RFC 9728 / RFC 8414 documents at
  `/.well-known/oauth-protected-resource` and
  `/.well-known/oauth-authorization-server`.

## Smoke test against a real Zammad

If you only have one Zammad instance and don't want to register a real
OAuth application, you can run the full stack in `AUTH_MODE=none`
locally:

```bash
ENVIRONMENT=development \
AUTH_MODE=none \
ZAMMAD_URL=https://your-zammad.example.com \
ZAMMAD_API_TOKEN=<a personal access token> \
docker compose -f docker-compose.development.yml up
```

Then point the MCP Inspector at `http://localhost:8000/mcp`. No OAuth
prompts — the endpoint is unprotected — but you get to see every tool
call hit your real Zammad.

This is purely for local iteration. The container refuses to start with
`AUTH_MODE=none` and `ENVIRONMENT=production`.

## Generating a tool catalogue

The CLI can dump the current tool surface as Markdown:

```bash
docker exec bg-zammad-mcp python src/main.py tools -o /tmp/tools.md
```

Used by CI to keep `docs/tools.md` in sync with the registered tools.
