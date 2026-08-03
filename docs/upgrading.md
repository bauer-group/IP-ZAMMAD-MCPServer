# Upgrading

## Two version numbers that are not the same thing

`ZAMMAD_MCP_VERSION` is **this server's** version. Zammad's is its own, and the
two are unrelated: a 5.x server talks to a Zammad 7.x helpdesk. The variable
name invites the opposite reading, so it is worth stating once — the supported
backend is Zammad 7.x regardless of the number in that variable, and
`get_zammad_version` reports what your instance actually runs.

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

## 3.1.0

Found by calling all 77 tools against a live Zammad 7.1.1 rather than by
reading them. Four more places where the same concept had two names, plus two
gaps where a field was reachable on one object and not its neighbour.

### `set_article_visibility(internal=…)` → `visibility=…`

Takes `'customer_visible'` or `'internal'` — the vocabulary `create_ticket`,
`update_ticket` and `update_tickets` already use. It was the last bare
`internal: bool` on the surface, which is the exact shape
`create_ticket_article` was split in two to remove; a boolean also gives no
hint which way round it goes.

### `add_tag(item=…)` / `remove_tag(item=…)` → `tag=…`

The parameter was named after Zammad's wire parameter while the RESPONSE was
named after the concept, so a single call spoke both dialects at once: you
passed `item` and got back `{"tag": …}`.

### `update_tickets` takes the same arguments as `update_ticket`

The generic `attributes={...}` bag is gone, replaced by the same named
parameters the single-ticket tool has (`state`, `state_id`, `pending_time`,
`priority`, `priority_id`, `owner`, `owner_id`, `group`, `group_id`,
`customer`, `customer_id`) plus `extra_fields` for custom attributes. Knowing
one tool taught you nothing about the other, so
`update_tickets(state='closed')` — the natural thing to write after
`update_ticket(state='closed')` — was a validation error.

It also inherits the name-or-ID guard: passing `group` and `group_id` together
is refused rather than silently half-applied.

### `update_ticket` accepts associations by name

`group`, `owner` and `customer` join the `_id` forms it already had.
`create_ticket` took names, `update_ticket` took only IDs, so moving a ticket to
a group you had just created one in meant going to find an ID first. The guard
covers all five associations now, not just state and priority.

### `note` and `extra_fields` on users and organizations

`create_user` and `update_user` gain `note` — Zammad's User carries that column
exactly as Organization does (verified on 7.1.1), it simply was not offered.
All four user/organization write tools gain `extra_fields`, so custom
Object-Manager attributes are reachable there as they already were on tickets.
Both are additive.

---

## 2.1.0

One response shape for every collection, and one pagination vocabulary. This is
the second half of the 2.0.0 unification: that release made similar operations
*take* the same arguments, this one makes them *return* the same thing.

### Every collection returns the same envelope

```json
{"items": [...], "returned": 25, "total_count": 412, "page": 1, "per_page": 25, "has_more": true}
```

Twenty tools previously returned nine different shapes: a bare array from
`list_tickets`, `{records, total_count}` from `search_tickets`, `{count,
fields}` from `list_ticket_fields`, `{total, items, open}` from
`get_ticket_checklist`, `{ticket_id, entry_count, history}` from
`get_ticket_history`, `{overview, total_count, fetched_count, page, per_page,
tickets}` from `list_queue_tickets`, and so on. Six different spellings of "how
many": `total_count`, `count`, `total`, `returned`, `entry_count`,
`fetched_count`.

Worse than the count of shapes was that three tools **changed shape based on a
parameter**: `search_tickets`, `search_users` and `search_organizations`
returned a wrapped object with `with_total_count=true` (the default) and a bare
array with `false`. Code written against one call could break on the next.

**What to do:** read `result["items"]` where you previously read the array, the
`records` key, or a domain key like `tickets` / `fields` / `history`.

### `with_total_count` is gone as a parameter

It is now always on for search tools. It only ever existed to let a caller
reshape the response, which is precisely the thing being removed; the total is
what distinguishes "25 matches" from "25 of 4000", and it costs one count on an
index Elasticsearch has already built.

### `has_more` is three-valued — do not treat `null` as `false`

* `false` — proven complete. The page came back short, or a known total is
  exhausted.
* `true` — proven incomplete.
* `null` — a full page with no total available. Genuinely unknown; fetch the
  next page to find out.

Zammad's index actions ignore `with_total_count` and answer with a bare array
(measured on 7.1.1), so `list_*` tools genuinely cannot know the total until
they reach the end — at which point it becomes arithmetic and is reported.
Search actions know it from the first page. Guessing `false` in the unknown case
is the expensive direction: a model stops paging and reports a partial answer
as complete.

### `limit` is now `per_page` everywhere

Affects `search_tickets`, `search_tickets_by_condition`, `search_users`,
`search_organizations` and `list_ticket_articles`. Zammad accepts both spellings
interchangeably on both index and search actions — verified on 7.1.1, where
`/tickets?limit=3&page=2` and `?per_page=3&page=2` return the same window — so
the split published two words for one concept with nothing to distinguish them.
The differing ceilings are real and remain: 100 on index-backed `list_*`, 200 on
`search_*`.

`list_ticket_articles` gains a real `page` in the trade. Its old `limit` could
only ever reach one end of a thread; the middle of a long conversation was
unreachable without pulling all of it.

### Smaller consequences

* `get_ticket_checklist` answered "no checklist" with a differently-shaped
  object than "here is the checklist", so a caller had to branch on a condition
  it could not see before calling. Both paths now return the envelope, with
  `open` renamed `open_items`.
* `list_queue_tickets` reported `fetched_count` as the size of the whole joined
  queue rather than of the page it returned, so a 5-ticket page out of 200
  claimed to have fetched 200. `returned` is computed from what ships.
* `list_ticket_attachments` and `list_object_attributes` returned bare arrays
  and now return the envelope; `total_count` equals `returned` for them, because
  those endpoints ignore pagination and always send everything.

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

### One vocabulary for article visibility

`article_internal` is gone from `create_ticket`, `update_ticket` and
`update_tickets`, replaced by `article_visibility` taking `'customer_visible'`
or `'internal'`.

The boolean had a different default on each of them — `False` on create, `True`
in bulk, and `update_ticket` hardcoded `internal=True` with no parameter at all,
so "close this with a note explaining the fix" produced a note the customer
could never read. That is the same defect the article tools were split in two to
close, reintroduced through a side door. One named vocabulary makes it visible
at every call site.

### Every ticket is addressed by ID

`merge_tickets(target_ticket_number=…)` → `merge_tickets(target_ticket_id=…)`
and `link_tickets(source_ticket_number=…)` → `link_tickets(source_ticket_id=…)`.

Zammad's API is asymmetric here — the merge route takes a NUMBER for the target
while `links/add` takes one for the SOURCE, i.e. on opposite sides — and that
asymmetry used to be published to callers. There was no rule to learn, only two
neighbouring tools that disagreed. Both now take IDs and resolve the number
internally, at the cost of one extra GET each.

### `update_ticket(tags=…)` is now `replace_tags`

Same behaviour, honest name: it REPLACES the ticket's whole tag list. `add_tag`
and `remove_tag` change one tag without touching the others. The old name read
as additive and silently discarded every tag not listed.

### `update_ticket` refuses `state` and `state_id` together

(Likewise `priority`/`priority_id`.) Zammad accepts both and silently applies
one, so half of what was asked for disappeared without an error. Also,
`create_ticket` now accepts `group_id`/`customer_id`/`state`/`priority` — the
five associations were previously split the opposite way between create and
update, which meant learning two rules for one object.

### Other renames

* `add_ticket_time_entry(ticket_article_id=…)` → `article_id`, matching the five
  other tools that take an article.
* `unlink_tickets` returns `removed_count` instead of `removed`, because
  `remove_tag` returns `removed` as a plain boolean and one key must not be a
  bool in one tool and a count in another.
* `list_ticket_articles` now ALWAYS returns
  `{articles, total_count, returned, order}`. It used to return a bare array
  when nothing was dropped — and, worse, `order` was only reported in the
  truncating branch, so `newest_first=true` on a short thread returned a
  reversed list with no indication that it was reversed.

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
