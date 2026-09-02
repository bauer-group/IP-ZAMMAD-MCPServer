# ADR 0002 — Letting article bodies declare HTML

Status: accepted
Date: 2026-09-02
Applies to: `app/bg-zammad-mcp/src/zammad/tools/tickets.py`,
`app/bg-zammad-mcp/src/zammad/tools/articles.py`,
`app/bg-zammad-mcp/src/zammad/tools/bulk.py`

## Context

Zammad stores an article's format in `ticket_articles.content_type`. Two of this
server's five article-writing tools sent it; the three that let an article ride
along with a ticket write did not:

| Tool | Article payload built in | `content_type` before |
| --- | --- | --- |
| `reply_to_customer` | `articles.py`, inline | yes |
| `add_internal_note` | `articles.py`, inline | yes |
| `create_ticket` | `tickets.py`, `_article()` | **no** |
| `update_ticket` | `tickets.py`, `_article()` | **no** |
| `update_tickets` | `bulk.py`, a second inline copy | **no** |

`create_ticket` advertised its body as *"plain text or HTML"* while pinning
every one of them to plain text, so a model that followed the description
produced a broken ticket and got HTTP 201 for it.

### What Zammad actually does

Verified against Zammad 7.1.1 source and then against the live instance:

* `POST /api/v1/tickets` and `POST /api/v1/ticket_articles` share one handler,
  `CreatesTicketArticles#article_create`. There is no separate nested-article
  path, and `content_type` survives `param_cleanup` because it is a real column.
* Omitting the field does **not** trigger body sniffing. The column default
  applies: `varchar(20) NOT NULL DEFAULT 'text/plain'`. All five references to
  `content_type` in `Ticket::Article` are reads.
* Nothing upstream validates the value. Anything ≤ 20 characters is persisted
  with HTTP 201 — and is not inert, because `sanitizeable?` matches `/html/i`:
  `'html'` switches the HTML sanitizer **on**, `'text/markdown'` switches it
  **off**, behind the caller's back.
* A plain-text body carrying markup is escaped at every render — `text2html` is
  `CGI.escapeHTML` server-side, `linkify-string` escapes client-side — and the
  outgoing mail gets only a `text/plain` part. Agent and customer both read
  literal `<p>` tags.

Measured on the production instance (ticket 268579, since deleted):

| Article | How it was written | Stored `content_type` |
| --- | --- | --- |
| 289356 | `create_ticket`, nested, field omitted | `text/plain`, markup escaped |
| 289358 | `add_internal_note`, `text/html` | `text/html`, markup intact |
| 289359 | `PUT /tickets`, nested, `text/html` | `text/html`, markup intact |

Row 3 is the one that mattered: it proves the nested path accepts the field
rather than merely that the flat one does.

## Decision

1. **All five surfaces take `content_type`**, defaulting to `text/plain`.
2. **The value is always sent**, even when it equals Zammad's column default, so
   every surface puts the same thing on the wire.
3. **Only `text/plain` and `text/html` are accepted.** `reject_unknown_content_type`
   guards every surface, including the two that previously took the parameter
   without checking it.
4. **One builder.** `bulk.py` calls the shared `build_article()` instead of
   assembling its own dict.

## Reasons

### Why an exact two-value enum rather than a permissive string

Zammad's *model* layer is loose here — `%r{text/html}i` — but nothing that
renders the article is. The mail builder (`== 'text/html'`), the classic UI, the
desktop Vue UI and the mobile Vue UI all compare by exact equality, as does this
server's own read path in `projection.py`. A value like `TEXT/HTML` would be
HTML to the model and plain text to everything a human sees.

`text/html; charset=utf-8` fails for a second, independent reason: at 24
characters it exceeds the `varchar(20)` column and is truncated rather than
refused. Accepting either form would produce a silent, hard-to-attribute defect,
so both are rejected at the tool boundary with a message naming the two values
that work.

### The security trade-off, stated plainly

Enabling `text/html` **widens** the attack surface. It does not narrow it, and
the opposite claim is the intuitive-but-wrong one:

* A `text/plain` body today is **inert**. It is stored unsanitized — `sanitizeable?`
  is false — but escaped categorically at every render, so no markup in it can
  ever execute.
* A `text/html` body is filtered once at write time by `HtmlSanitizer::Strict`
  and then injected as live HTML through `v-html`. Escaping is categorical; an
  allowlist scrubber is a filter, and filters have bypass histories.
* Sanitization runs once. `checks_html_sanitized.rb` returns early when the
  attribute has not changed, so a body stored before a later-discovered
  sanitizer bypass is never re-sanitized.

This was accepted because the capability is not new to the deployment. Both
`reply_to_customer` and `add_internal_note` have offered it since the first
release, and every agent typing in Zammad's own UI produces exactly this. What
changed is that three tools stopped lying about supporting it. The alternative —
removing `content_type` everywhere and narrowing the descriptions to plain text
only — would have been the smaller surface, but it would also have made the
server unable to author the article format Zammad's web UI produces by default.

The guard in decision 3 is what keeps the widening bounded: the sanitizer can
only be reached deliberately, never through a typo that Zammad would have
accepted.

## Consequences

* `update_tickets` gained validation it never had. Its local copy of the article
  dict read visibility as `article_visibility == "internal"`, which is `False`
  for any unrecognised value — so a typo published an agent-only note to every
  customer in the batch, once per ticket. Routing it through `build_article()`
  closes that as a side effect.
* A previously accepted junk `content_type` on `reply_to_customer` /
  `add_internal_note` now raises instead of being stored. This is a behaviour
  change, and an intended one: those values were already broken, just quietly.
* `extra_fields` remains an unvalidated passthrough on `update_ticket` and
  `update_tickets`. A caller can still hand-build an `article` block there and
  bypass both this guard and the `article_visibility` vocabulary — the same
  escape hatch that existed before, now the only one. Locking it down is a
  separate decision, not made here.

## Related

* ADR 0001 — the other place where untrusted content meets a parser.
* `docs/upgrading.md` — the caller-facing note for this change.
