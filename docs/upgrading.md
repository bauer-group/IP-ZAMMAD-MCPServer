# Upgrading

## How to upgrade, generally

1. Read the release notes for breaking changes.
2. Diff `.env.example` against your `.env` — new variables appear there first,
   and an unknown one is silently ignored rather than rejected.
3. Pull the new image and redeploy.
4. Run the verification block in [operations.md](operations.md#verifying-a-deployment).

Nothing here owns data: Zammad holds the tickets, and the OAuth state store is
disposable at the cost of a re-authentication. So an upgrade is low-risk and a
rollback is a tag change.

---

## 1.0.0

The tool surface grew from 36 to 75 tools and several long-standing defects were
fixed. Most of it is additive, but four things change behaviour you may be
relying on.

### `create_ticket_article` is gone — replaced by two tools

`reply_to_customer` and `add_internal_note`.

Zammad models *who can see this* (`internal`) and *how it was delivered*
(`type`) as independent fields, and the dangerous combination looked harmless:
`{"type": "email", "internal": true}` **sends the mail to the customer and then
hides the article from them** in their own ticket view. The old tool defaulted
`internal` to `True` regardless of type, so the natural "reply by e-mail" call
produced exactly that, returned HTTP 201, and looked like success.

Visibility now lives in the tool name and neither tool exposes an `internal`
flag, so the bad state is unreachable.

**What to do:** update any prompt, macro or script that named
`create_ticket_article`. There is no compatibility shim — a silent alias would
reintroduce the ambiguity the split exists to remove.

### `create_ticket`: `type` → `ticket_type`, and the opening article is now visible

The `type` parameter carried a `Field(alias="type")`, which collided with the
*article* type an LLM was being told to send: `create_ticket(..., type='email')`
was silently accepted, set the **ticket** type, and left the opening article an
internal note. The parameter is now published as `ticket_type`.

`article_internal` also defaults to `false` now, matching Zammad's own
documented example — a ticket raised on a customer's behalf should look like one
they can read. Pass `article_internal=true` for a purely internal tracking
ticket.

### `ZAMMAD_OAUTH_SCOPES` must be `full`

If you run `AUTH_MODE=zammad`, this is the one change you must make before
deploying — **the server will refuse to start otherwise**, deliberately.

The previous default was `read write`, which Zammad rejects. Its OAuth2 server
is plain Doorkeeper with `default_scopes :full` and no optional scopes, and the
application form has no scope field, so Doorkeeper validates every authorize
request against `server_scopes == ["full"]`. Anything else fails with
`invalid_scope` — *after* the user has already entered their password, which
made it an unusually confusing failure. Verified against Doorkeeper's own
`ScopeChecker` on a live Zammad 7.1.1.

### `ZAMMAD_API_TOKEN` must be empty in `AUTH_MODE=zammad`

Also enforced at boot. The outbound resolver falls back to that token whenever a
per-user token cannot be resolved, logging a warning and continuing — which
silently converts "acts with the caller's rights" into "acts as whoever owns the
token", usually an admin. That defeats the reason for choosing this mode.

If you deliberately want that behaviour, set `MCP_ALLOW_STATIC_FALLBACK=true`.

### Smaller changes worth knowing

* **List and search results are trimmed** to the fields an agent reasons about.
  Pass `full=true` for Zammad's raw records, or `fields="a,b,c"` for an explicit
  whitelist. Article bodies are converted to plain text and capped by
  `max_body_chars`; when anything is dropped the response says so explicitly.
* **`destructiveHint` was corrected** across the surface. Purely additive writes
  (`create_*`, `add_tag`, `subscribe_to_ticket`, `mark_*_read`) no longer carry
  it, so MCP clients stop prompting for them. Bulk operations
  (`update_tickets`, `apply_macro_to_tickets`) do carry it — those are the ones
  worth a human in the loop.
* **Search tools accept `page`.** They previously could not reach past their
  first page at all, because Zammad computes `offset = (page - 1) * limit`.
* **`list_tickets` documents that it is oldest-first.** The behaviour did not
  change; the description used to claim the opposite.
* **Prompts and resources** are now published (`review_my_queue`,
  `triage_ticket`, `draft_customer_reply`, `close_duplicate`,
  `handover_summary`, plus `zammad://…` reference resources). They come from
  `extensions/extensions.json`, which you can mount over or repoint with
  `EXTENSIONS_CONFIG_PATH`.
* **New settings:** `MCP_ROLE_CACHE_TTL_SECONDS` (default 30 — previously
  present in the compose files but read by nothing),
  `MCP_ALLOWED_CLIENT_REDIRECT_URIS`, `MCP_ALLOW_STATIC_FALLBACK`,
  `EXTENSIONS_CONFIG_PATH`.

### Baseline changes

* **Python 3.14** is the floor. Every execution surface already targeted it;
  `requires-python` merely said 3.13.
* **Zammad 7.x only.** The previous "v6 / v7 (tested)" claim had no test behind
  it. If you are still on 6.x, stay on 0.1.x.

---

## Earlier: the bg-mcpcore migration (0.1.x)

The cross-cutting machinery — settings, inbound auth, encrypted OAuth storage,
logging, rate limiting, routes, the outbound client — moved into the shared
`bg-mcpcore` library, and the server became profile-driven.

Operationally the only visible change was the CLI: the `health`, `probe` and
`tools` subcommands are gone. Container liveness is the unauthenticated
`/healthz` route; upstream reachability is the `bg.health` tool.
