# Cookbook

Worked recipes for the things agents are actually asked to do, and the sharp
edges each one runs into. The reusable versions of the first few ship as MCP
**prompts** (`review_my_queue`, `triage_ticket`, `draft_customer_reply`,
`close_duplicate`, `handover_summary`) — pick them from your client's prompt
menu rather than retyping them.

The full tool list is generated at [tools.md](tools.md).

---

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
list_ticket_articles(ticket_id=4711, newest_first=true, limit=10, max_body_chars=2000)
```

Bodies come back as plain text (HTML is flattened) and capped. If anything was
dropped you get `{articles, total_count, returned, order, note}` rather than a
bare list, so you can tell "the last 10 of 96" from "all 10".

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
update_tickets(ticket_ids=[…], state="closed")
```

One transaction: if any ticket is refused, nothing changes and the error names
the blocking tickets. Capped at 100 ids per call. This is annotated
**destructive**, so a well-behaved client will ask you first — which is the
point.

---

## 7. Merge a duplicate

```text
merge_tickets(source_ticket_id=4711, target_ticket_number="10042")
```

Two traps, both handled but worth knowing: the **source is an ID and the target
is a ticket number**, and a *failed* merge returns HTTP 200 with a failure in
the body. The tool parses that and raises, so a merge that did not happen is
never reported as success. It is not reversible — read both tickets first.

---

## 8. Answer "how many…?" without paying for the tickets

```text
count_tickets(query="state.name:open")
```

Returns only the count. For anything where the *set* matters and the instance
may lack Elasticsearch, use the index-independent structured search:

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

## When something comes back empty

Work through it in this order — the first two account for most cases:

1. **Is it a permissions boundary?** Every call runs as *you*. An empty result
   very often just means the group is not yours.
2. **Is the search index healthy?** Field-scoped queries need Elasticsearch. If
   it is rebuilding, they return an empty list rather than an error — retry with
   `search_tickets_by_condition`, which never touches the index.
3. **Did you page?** Search results are pages. `with_total_count` is on by
   default, so compare `total_count` against what you got.
4. **Is an AI provider configured?** The AI tools need an OpenAI or Anthropic
   key set up in Zammad. Without one they say so plainly rather than returning
   a generic error.
