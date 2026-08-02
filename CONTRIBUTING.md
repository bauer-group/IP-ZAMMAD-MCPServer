# Contributing

Internal BAUER GROUP project. Small enough that process should stay out of the
way — this page is the handful of things that are not obvious from the code.

## Getting set up

```bash
cd app/bg-zammad-mcp
python -m venv .venv                 # Python 3.14 (see pyproject requires-python)
./.venv/Scripts/pip install -e ".[test,dev]"
```

`bg-mcpcore` is pulled from GitHub and pinned to a **commit SHA**, not a tag —
tags can be force-moved, and Dependabot's pip ecosystem cannot see PEP 508 VCS
references, so nothing else watches it. Bump it deliberately.

## The gate

Run all three before you commit. CI runs exactly the same commands, and so does
the Docker test stage, so a green local run means a green build.

```bash
cd app/bg-zammad-mcp
./.venv/Scripts/python -m ruff check src tests
./.venv/Scripts/python -m mypy src
./.venv/Scripts/python -m pytest -q --cov=src
```

If you touched the tool surface, also regenerate the reference — CI fails if it
is stale:

```bash
python scripts/generate-tools-doc.py
```

## Adding a tool

1. **Verify the endpoint first.** Read the route in
   [`zammad/zammad`](https://github.com/zammad/zammad) (`config/routes/*.rb`)
   and the controller, and confirm the parameter names against the strong-params
   list. Do not infer an endpoint from another one that looks similar — half the
   defects this project has had came from exactly that.
2. Put it in the module that matches its job (`src/zammad/tools/`), following the
   existing shape: a docstring listing endpoints and permissions, one
   `register(mcp, ctx)`, and a `return N` that matches the number of tools.
3. `ctx.request(method, path, **kwargs)` returns the decoded body and raises a
   typed `ZammadError` on any non-2xx. Never build your own HTTP client.
4. Raise `fastmcp.exceptions.ToolError` for bad arguments, with a message that
   tells the model how to fix the call.
5. Add the tool to `EXPECTED_TOOLS` in `tests/test_tools_inventory.py`, and to
   `DESTRUCTIVE_TOOLS` if it overwrites or removes state other people rely on.
6. Write a request-shape test: method, path, params, JSON body.
7. Tag it by adding the module to `_MODULE_TAGS` in `src/server.py`.

### Things the tests will hold you to

* **Descriptions may only name real parameters.** Every backticked lowercase
  identifier in a description must be a parameter of that tool or the name of
  another tool. This exists because a description once told the model to pass
  `type` while the schema published `article_type`, and every such call failed.
* **`destructiveHint` follows a written rule**, not taste: true when the call
  changes state other people depend on and nothing here undoes it; false when
  it is additive or touches only the caller's own reversible state.
* **The declared count must match reality.** A module's `return N` is asserted
  against what it actually registered.

## Writing for a model, not a person

Tool descriptions are the main lever on whether Claude picks the right tool, and
they are read without the surrounding context a human has. Two habits pay off:

* **Say what the tool is for, then name the sharp edge.** "Returns HTTP 200 with
  an empty body when the slug is unknown" is worth more than another sentence of
  purpose.
* **Encode intent in the name where a flag would be dangerous.** `reply_to_customer`
  and `add_internal_note` exist as two tools precisely so that visibility cannot
  be got wrong by forgetting an argument.

## Commits

Conventional Commits, past tense, English, with a body explaining *why*. A
`feat!` or `BREAKING CHANGE:` footer cuts a major release, so use it when you
mean it. Never include AI attribution or `Co-Authored-By` trailers.

## Testing against a real Zammad

Unit tests use a recording context and never touch the network. For anything
involving a route you have not exercised before, run it against a real instance:

```bash
curl -sSLO https://raw.githubusercontent.com/zammad/zammad-docker-compose/master/docker-compose.yml
printf 'VERSION=7.1\nNGINX_EXPOSE_PORT=8080\n' > .env
docker compose -p zammad7 up -d
```

Then create an API token and point the server at it with `AUTH_MODE=none`. Note
that `AUTH_MODE=zammad` needs an **HTTPS** `PUBLIC_BASE_URL` — Zammad refuses to
register an OAuth application with an `http://` callback.

### The on-behalf-of proof

`tests/test_integration_obo.py` is the one test that proves the property this
server exists for: that a call runs with the **logged-in user's** Zammad rights
rather than a shared identity. It is skipped unless you point it at an instance:

```bash
ZAMMAD_TEST_URL=http://localhost:8080 ZAMMAD_TEST_TOKEN_RESTRICTED=<OAuth token of an agent with limited groups> ZAMMAD_TEST_TOKEN_ADMIN=<OAuth token of an agent who sees more>   pytest tests/test_integration_obo.py -m integration
```

Both tokens must come from Zammad's real authorization-code flow (sign in,
`/oauth/authorize` with PKCE, exchange at `/oauth/token`), not from a Personal
Access Token — a PAT is exactly the shared-identity path the test exists to rule
out. Give the two agents access to **different groups**, otherwise the decisive
assertion (two users, same call, different answers) cannot distinguish per-user
context from a shared token.
