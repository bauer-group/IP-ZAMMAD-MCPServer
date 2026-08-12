# Cookbook

Worked recipes for the things agents are actually asked to do, and the sharp
edges each one runs into. The reusable versions of the first few ship as MCP
**prompts** (`review_my_queue`, `triage_ticket`, `draft_customer_reply`,
`close_duplicate`, `handover_summary`) — pick them from your client's prompt
menu rather than retyping them.

The full tool list is generated at [tools.md](tools.md).

---

## 0. One shape for every collection

Read this once and the other ten recipes need no shape explanations. Every tool
that returns more than one record answers with the same object:

```json
{
  "items":       [ ... ],
  "returned":    25,
  "total_count": 412,
  "page":        1,
  "per_page":    25,
  "has_more":    true
}
```

No key ever vanishes. `total_count` and `has_more` are `null` when the backing
endpoint genuinely cannot tell: Zammad's index actions ignore
`with_total_count` and answer with a bare array, so `list_tickets` does not know
the total until you reach the last page, while `search_tickets` knows it from
the first. `page` and `per_page` are `null` for the endpoints that ignore
pagination entirely and always return everything (`list_all_tags`,
`list_object_attributes`).

**`has_more` is three-valued on purpose.** `false` is a proof, not a guess —
the page came back short, or a known total is exhausted. `null` means a full
page with no total, which is genuinely unknown, so ask for the next one. It is
never `false` while records remain, and that is the property that lets you stop
paging without wondering whether you missed something.

Tools with something of their own add it alongside: `order` on articles,
`ticket_id` on a history, `open_items` on a checklist.

## 1. "What's on my plate?"

**Do not start with a search.** Start with the agent's own overviews:

```text
list_my_queues
  → [{ id, name, link, count }, …]
list_queue_tickets(view="<link from above>")
```

Why it matters: `list_my_queues` returns per-queue counts, reflects the
overviews the team actually configured, and needs no search index at all. A
`search_tickets` on `state.name:open` answers a different question — every open
ticket you *may see*, not the ones anybody decided you should work on.

Note the naming asymmetry: the list gives you `link`, and that value is what
`list_queue_tickets` wants as its `view`.

---

## 2. Read a ticket properly before answering it

```text
get_ticket_full(ticket_id=4711)
```

One call: the ticket, every article the caller may see, and the related user,
organization, group, state and priority records. Do **not** call `get_ticket`
plus `list_ticket_articles` — that is two round trips for the same thing.

For a long e-mail thread, read the recent end on its own:

```text
list_ticket_articles(ticket_id=4711, newest_first=true, per_page=10, max_body_chars=2000)
```

Bodies come back as plain text (HTML is flattened) and capped. Like every
collection here the answer is the shared envelope, so "the last 10 of 96" and
"all 10" are told apart by reading `has_more` rather than guessing from the
length. `order` rides along to say which end you are looking at.

**The thing to actually check while reading:** which articles are `internal`.
Those are invisible to the customer. Answering as though they have seen an
internal note is the most common way to confuse them.

---

## 3. Reply to a customer

```text
reply_to_customer(ticket_id=4711, body="…")          # e-mail, customer sees it
add_internal_note(ticket_id=4711, body="…")          # agents only, never sent
```

Visibility is in the tool name and cannot be overridden — that is deliberate.
The old single tool defaulted to `internal=True` regardless of type, which meant
a correctly-formed "send an e-mail" call **sent the mail and hid it from the
customer in their own ticket**.

Ground the reply before writing it:

```text
search_text_modules(query="…")     # approved house wording
search_knowledge_base(query="…")   # documented answers
```

---

## 4. Close a ticket with a note, in one request

```text
update_ticket(ticket_id=4711, state="closed", article_body="Resolved: replaced the toner.")
```

One atomic PUT rather than an update plus a separate note. The attached article
is internal; use `reply_to_customer` first if the customer should be told.

For a pending state, `pending_time` is **required** by Zammad:

```text
update_ticket(ticket_id=4711, state="pending reminder", pending_time="2026-08-12T09:00:00Z")
```

---

## 5. Apply the team's own workflow instead of hand-rolling one

```text
list_macros
apply_macro_to_tickets(macro_id=7, ticket_ids=[4711])
```

A macro bundles state, owner, priority, tags and a note into one server-side
action that someone already approved. Prefer it over reproducing the same
changes field by field.

Single tickets go through `apply_macro_to_tickets` too, with a one-element list
— `update_ticket` with a macro id silently does nothing.

---

## 6. Bulk-close a batch

```text
update_tickets(ticket_ids=[…], attributes={"state": "closed"})
```

Note the shape: the field changes go in `attributes`, not as top-level
arguments — that is Zammad's `mass_update` body, and getting it wrong is a
schema error rather than a silent no-op.

One transaction: if any ticket is refused, nothing changes and the error names
the blocking tickets. Capped at 100 ids per call. This is annotated
**destructive**, so a well-behaved client will ask you first — which is the
point.

---

## 7. Merge a duplicate

```text
merge_tickets(source_ticket_id=4711, target_ticket_id=4700)
```

**Every ticket argument on this server is a numeric `*_ticket_id`** — there is
no tool that wants a ticket number instead. Zammad's own API is not consistent
here (its merge route takes a number for the target, and `links/add` takes one
for the source, on the opposite side), but that asymmetry is resolved inside the
tools rather than published to you.

One trap remains, and it is handled: a *failed* merge returns HTTP 200 with the
failure in the body. The tool parses that and raises, so a merge that did not
happen is never reported as success. It is not reversible — read both tickets
first.

---

## 8. Answer "how many…?" without paying for the tickets

```text
count_tickets(query="state.name:open")
```

Returns only the count. When the *set* matters and the filter is exact — a
fixed list of states, one organization, a date window — prefer the structured
search, which is unambiguous and never touches the search index:

```text
search_tickets_by_condition(condition={
  "ticket.state_id": {"operator": "is", "value": [1, 2, 3]},
  "ticket.organization_id": {"operator": "is", "value": [12]}
})
```

---

## 9. Work with a customer's custom fields

```text
list_ticket_fields                       # agent-safe; shows custom attributes
update_ticket(ticket_id=4711, extra_fields={"cost_centre": "4711"})
```

`list_ticket_fields` uses the ticket-creation screen, which any agent may read.
`list_object_attributes` is the fuller definition but needs `admin.object` and
will 403 for a plain agent.

---

## 10. Book time while replying

```text
add_ticket_time_entry(ticket_id=4711, time_unit=45)
```

Time accounting is a separate Zammad feature and may be switched off; the tool
says so explicitly rather than failing opaquely.

---

## 11. Verify the server really acts as you

```text
get_me
```

Everything runs under the Zammad identity that logged in, so `get_me` is the
cheapest sanity check that the connection is what you think it is. If a
colleague's ticket is invisible to you, that is Zammad's group permissions
doing their job — not a missing tool.

---

## When something comes back empty

Work through it in this order — the first two account for most cases:

1. **Is it a permissions boundary?** Every call runs as *you*. An empty result
   very often just means the group is not yours.
2. **Is the search index healthy?** Field-scoped queries need Elasticsearch. If
   it is rebuilding, they return an empty list rather than an error — retry with
   `search_tickets_by_condition`, which never touches the index.
3. **Did you page?** Every collection is a page. Read `has_more`: `true` means
   fetch `page + 1`, `false` means you have everything, and `null` means the
   endpoint cannot tell — ask for the next page to find out. `list_*` tools
   only learn `total_count` on the last page; `search_*` tools report it from
   the first.
4. **Is an AI provider configured?** The AI tools need an OpenAI or Anthropic
   key set up in Zammad. Without one they say so plainly rather than returning
   a generic error.

## Read a file a customer attached

Just ask about it — no separate download step, and no need to know the format:

> "Was steht im Datenblatt an Ticket 4711?"

The agent calls `list_ticket_attachments` for the `article_id` /
`attachment_id` pair, then `download_ticket_attachment`. What comes back
depends on what the file actually is, not on how it was labelled at upload:

| File | Result |
| ---- | ------ |
| Screenshot (PNG/JPEG/GIF/WebP) | the image itself — the agent can see it |
| Text, CSV, log, JSON, XML | the text, with the character set that decoded it |
| PDF, Word, Excel, RTF | the extracted text |
| Anything else | metadata plus the raw bytes, and a sentence saying why |

A file whose upload declared the wrong type still reads correctly — an RTF sent
as `application/msword` is recognised from its bytes. If the automatic routing
gets something wrong, `mode="text"` forces a text decode and `mode="raw"`
returns the untouched bytes.

## Attach a generated file to a ticket

Ask for the analysis and the delivery in one turn. The file and the message
become a single article, so the customer receives one mail:

> "Werte die Fehlerzahlen aus Ticket 4711 aus und schick dem Kunden die
> Tabelle als CSV mit."

The agent calls `reply_to_customer` once:

```json
{
  "ticket_id": 4711,
  "body": "Anbei die Auswertung der Fehlerzahlen.",
  "attachments": [
    {"filename": "fehlerzahlen.csv", "text": "Datum;Anzahl\n2026-08-01;12\n"}
  ]
}
```

`add_internal_note` and `create_ticket` take the same `attachments` parameter.
Visibility stays in the tool name — a file sent with `add_internal_note` is as
invisible to the customer as the note itself.

## Carry a file from one ticket to another

`copy_from` moves the bytes server-side, so the file never passes through the
model's context: no token cost, byte-identical, and no size limit beyond the
configured one.

> "Nimm das Datenblatt aus #4711 mit in die Antwort auf #4890."

```json
{
  "ticket_id": 4890,
  "body": "Das Datenblatt aus dem Vorgang 4711, wie besprochen.",
  "attachments": [
    {"copy_from": {"ticket_id": 4711, "article_id": 91, "attachment_id": 7}}
  ]
}
```

Get the `article_id` / `attachment_id` pair from `list_ticket_attachments` on
the source ticket — guessing them returns 403.

**Two things this deliberately does not do.** Executables are refused, by
extension and by magic bytes, on every path including `copy_from` — but a
`.zip` containing an `.exe` passes, so this is a tripwire against the obvious
accident and not virus scanning. And an operator can switch uploading off
entirely with `ZAMMAD_ATTACHMENT_UPLOAD_ENABLED=false`, in which case the
`attachments` parameter is absent from the tool schemas rather than failing at
call time.
