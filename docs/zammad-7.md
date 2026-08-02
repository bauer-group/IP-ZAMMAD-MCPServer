# Zammad 7

This server targets **Zammad 7.x only**. Zammad 6.x is no longer supported: the
support claim was never backed by a test against a real instance of either
major, so it has been dropped rather than left as an unverified promise.

The complete, generated tool list lives in [tools.md](tools.md). This page
covers the two things that list cannot tell you: what Zammad 7 exposes that we
deliberately do **not** wire up, and the handful of Zammad behaviours that will
otherwise cost you an afternoon.

---

## Zammad's OAuth2 is plain Doorkeeper

This is the single most surprising thing about the deployment, so it is worth
stating plainly. Zammad is not an OIDC provider. It runs
[Doorkeeper](https://github.com/doorkeeper-gem/doorkeeper) with a minimal
configuration, which has four consequences:

| | |
| --- | --- |
| **One scope, `full`** | `config/initializers/doorkeeper.rb` sets `default_scopes :full` and leaves `optional_scopes` commented out, and the OAuth application form has no scope field. Doorkeeper validates every authorize request against `server_scopes == ["full"]`, so any other value fails with `invalid_scope` — *after* the user has already logged in. `ZAMMAD_OAUTH_SCOPES` must be `full`; the server refuses to boot otherwise. |
| **Opaque tokens** | Not JWTs. There is no JWKS endpoint and no offline verification, which is why every request is validated against `GET /api/v1/users/me`. See [authentication.md](authentication.md) for the caching that makes this affordable. |
| **No discovery document** | There is no `/.well-known/openid-configuration` on the Zammad side. The MCP server publishes its *own* RFC 8414 / RFC 9728 metadata; that is what Claude reads. |
| **2-hour access tokens** | Doorkeeper's default, with `use_refresh_token` enabled. |

`GET /api/v1/users/me` is called with `expand=true` on purpose: without it Zammad
returns numeric `role_ids` and no `roles` array, and the role allowlist has
nothing to match against.

---

## Coverage matrix

### Wired up

| Area | Notes |
| --- | --- |
| Overviews | `GET /ticket_overviews`. The agent's real worklist, and index-free — see the Elasticsearch note below. |
| Tickets | Incl. `?all=true` one-call read, condition search, `update_title` / `update_customer`, custom Object-Manager fields, `pending_time`. |
| Articles | Split by visibility into `reply_to_customer` / `add_internal_note`; raw e-mail source via `ticket_article_plain`. |
| Macros | `GET /macros`, `POST /tickets/mass_macro`. |
| Bulk | `POST /tickets/mass_update`. |
| Links & merge | `/links`, `ticket_merge`, `ticket_related`, `ticket_customer`. |
| Checklists | Incl. the 7.x `checklist_items/create_bulk` route and templates. |
| Time accounting | `/tickets/{id}/time_accountings`. |
| Attachments | List, and download with a size cap. |
| History | `ticket_history`, plus article visibility correction and deletion. |
| Knowledge base | `POST /knowledge_bases/search`, answers, text modules. |
| AI | `POST /tickets/{id}/summarize` and `.../knowledge_base_answers` — feature-gated, see below. |
| Field discovery | `GET /ticket_create` (agent-safe) and `object_manager_attributes` (admin-only). |

### Deliberately not wired up

These exist in Zammad 7 and are **out of scope** for an agent surface, not
oversights:

| Area | Why not |
| --- | --- |
| Admin configuration (triggers, schedulers, core workflows, object manager writes, channels, roles/permissions writes) | Changing how the helpdesk itself behaves is not ticket handling. An agent that can rewrite a trigger can change what every future ticket does. |
| Webhooks, report profiles, calendars & SLA definitions | Configuration, same reasoning. SLA *state* is readable on the ticket (`escalation_at`). |
| Ticket shared drafts, `ticket_split`, `ticket_stats`, `ticket_recent` | Fit the human UI's workflow, not an agent's. |
| Chat / Telegram / WhatsApp channels | Not part of the ticket-handling path this server is for. |
| Data privacy tasks, maintenance, monitoring endpoints | Operator concerns with real blast radius. |

If you need one of these, it is a deliberate decision rather than a missing
wrapper — open an issue and say what workflow it unblocks.

---

## Behaviours that will otherwise surprise you

**Field-scoped search needs Elasticsearch.** `search_tickets` with
`state.name:open` only works if the instance runs Elasticsearch. Without it
Zammad falls back to a SQL `LIKE` over the whole query string and returns an
empty array with HTTP 200 — not an error. A model reads that as "you have no
open tickets". Two mitigations are in place: the tool description says so, and
`search_tickets_by_condition` (`POST /tickets/search` with a selector condition)
is index-independent. `list_my_queues` is index-independent too, and is usually
the better answer anyway.

**`list_tickets` is oldest-first.** `GET /tickets` does `reorder(id: :asc)` and
accepts no sort parameter, so on an established helpdesk it returns tickets from
the year the system was installed. Use search or an overview.

**`expand` beats `full` beats `all`.** `tickets#show` takes the *first* of those
three that is set. `get_ticket_full` therefore sends only `all=true` — adding the
module's usual `expand=true` would silently win and return a ticket with no
articles.

**A macro id alone does nothing.** `PUT /tickets/{id}` with `macro.id` returns
HTTP 200 and ignores the macro unless `macro.perform_changes` is supplied too,
and even then that parameter *filters* which of the macro's actions run. Single
tickets therefore go through `mass_macro` with a one-element list.

**A failed merge returns HTTP 200.** `ticket_merge` reports failure in the body
(`{"result": "failed"}`), not the status code. It is parsed and raised, so a
merge that did not happen is not reported as success. Note the parameter
asymmetry too: the source is an **ID**, the target is a **ticket number**.

**Pending states need a `pending_time`.** Setting a `pending ...` state without
one is rejected by Zammad.

**An unknown overview slug returns HTTP 200 with an empty body**, not 404.
`list_queue_tickets` raises rather than let that read as an empty queue.

**The AI endpoints are feature-gated.** `summarize_ticket` and
`suggest_kb_answers` return HTTP 422 unless `ai_assistance_ticket_summary` is
enabled *and* an AI provider is configured. Both tools detect that specific
failure and say the feature is not enabled, so the model summarises the thread
itself instead of retrying. `summarize_ticket` also returns a null result while
its background job runs, and polls before giving up.

**`list_all_tags` is an admin route.** `/tag_list` requires `admin.tag` and 403s
for a plain agent; `search_tags` is the agent-safe path.

---

## Version-specific additions used here

| Endpoint | Since | Why it matters |
| --- | --- | --- |
| `PUT /tickets/{id}/update_title` | 7.0 | Uses `Service::Ticket::ForcedUpdate`, bypassing Core Workflow restrictions that can silently block the same change made through the generic `PUT`. |
| `PUT /tickets/{id}/update_customer` | 7.0 | Same, for reassigning the customer/organization. |
| `POST /checklist_items/create_bulk` | 7.x | Adds a whole checklist in one request instead of one call per item. |
| `POST /tickets/{id}/summarize` | 7.x | Zammad's own ticket summarisation, when licensed. |
| `POST /tickets/{id}/knowledge_base_answers` | 7.x | Suggests knowledge-base answers for a ticket, when licensed. |

Every route in this server was verified against `zammad/zammad@stable` rather
than assumed. If Zammad changes one, the tool will fail loudly with a typed
error rather than silently doing nothing.
