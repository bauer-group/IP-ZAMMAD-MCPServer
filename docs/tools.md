# Tool reference

<!-- GENERATED FILE — do not edit by hand.
     Run `python scripts/generate-tools-doc.py` after changing the tool surface.
     CI fails if this file is out of date. -->

Every tool the Zammad MCP server exposes, grouped by tag. All of them run as the
**authenticated Zammad user**, so Zammad's own permission system decides what is
visible and changeable — a tool listed here will still refuse an action the
caller is not allowed to perform.

Annotation legend:

| Marker | Meaning |
| --- | --- |
| **read** | `readOnlyHint` — safe to auto-run; changes nothing. |
| **write** | Additive, or affects only the caller's own reversible state. |
| **destructive** | `destructiveHint` — overwrites or removes state others rely on. An MCP client should ask a human first. |

**75 tools** — 45 read-only, 16 additive writes, 14 destructive.

## Worklist

| Tool | | What it does |
| --- | --- | --- |
| `list_my_notifications` | read | List online notifications (in-app alerts) for the currently authenticated user. |
| `list_my_queues` | read | List the ticket overviews - the work queues - visible to the current user, each with a live ticket count. |
| `list_queue_tickets` | read | List the tickets inside one ticket overview, in the overview's own sort order. |
| `list_ticket_subscribers` | read | List the users currently subscribed (mentioned) on a ticket. |
| `mark_all_notifications_read` | write | Mark every online notification for the current user as read. |
| `mark_notification_read` | write | Mark a single online notification as read or unread. |
| `subscribe_to_ticket` | write | Subscribe the currently authenticated user to a ticket so they receive notifications on changes. |


## Bulk operations

| Tool | | What it does |
| --- | --- | --- |
| `apply_macro_to_tickets` | destructive | Run one pre-approved macro over up to 100 tickets as a SINGLE server-side transaction: if any one ticket is refused, nothing at all is changed. |
| `list_macros` | read | List the macros available to the current user. |
| `update_tickets` | destructive | Apply ONE change set to MANY tickets in a single Zammad transaction - the bulk action behind an overview's mass-edit bar, and far cheaper than looping `update_ticket`. |


## Communication

| Tool | | What it does |
| --- | --- | --- |
| `add_internal_note` | write | Add an INTERNAL note to a ticket - visible to agents only, never to the customer, and never delivered anywhere. |
| `download_ticket_attachment` | read | Read the CONTENT of one ticket attachment. |
| `get_article_plain` | read | Fetch the raw source of an e-mail article - the original message with its headers, as Zammad received it. |
| `get_ticket_article` | read | Fetch a single article (message) by its ID. |
| `list_ticket_articles` | read | List the articles (messages, notes, replies) on a ticket: body, sender, type, timing, and whether the article is internal (hidden from the customer). |
| `list_ticket_attachments` | read | List every file attached to a ticket, flattened across all of its articles. |
| `reply_to_customer` | write | Send a CUSTOMER-VISIBLE reply on a ticket. |


## Audit & correction

| Tool | | What it does |
| --- | --- | --- |
| `delete_ticket_article` | destructive | Permanently delete one article from a ticket. |
| `get_ticket_history` | read | Read the complete audit trail of a ticket - the tool that answers 'who closed this and when', 'who reassigned it', 'when did the priority change', 'was that a human or a trigger'. |
| `set_article_visibility` | destructive | Change who can see an existing article on a ticket - the fix for a message filed with the wrong audience. |
| `unsubscribe_from_ticket` | write | Stop the currently authenticated user receiving notifications about a ticket - the counterpart to `subscribe_to_ticket`. |


## Zammad AI (feature-gated)

| Tool | | What it does |
| --- | --- | --- |
| `draft_kb_answer_from_ticket` | write | Ask Zammad's AI to DRAFT A NEW knowledge base article out of a ticket, so a solution worked out once can be reused. |
| `summarize_ticket` | write | Ask Zammad's own AI assistant to summarise a ticket - the customer's problem, what has been done so far, and what is still open. |


## Knowledge base & wording

| Tool | | What it does |
| --- | --- | --- |
| `get_kb_answer` | read | Read the FULL text of one knowledge base answer, in every locale it has been translated into. |
| `list_text_modules` | read | List this Zammad's text modules - the house-approved, pre-written wording for replies: greetings, closings, standard explanations, legal boilerplate. |
| `search_knowledge_base` | read | Search the organisation's own knowledge base and ground your answer in what it says, rather than improvising one. |
| `search_text_modules` | read | Search the house-approved reply wording by keyword. |


## Tickets

| Tool | | What it does |
| --- | --- | --- |
| `add_checklist_items` | write | Append one or more items to a ticket's checklist in a SINGLE request. |
| `add_tag` | write | Attach a tag to a Zammad object (default: a ticket). |
| `add_ticket_time_entry` | write | Book time on a ticket. |
| `count_tickets` | read | Return ONLY the number of tickets matching a query, without the tickets themselves. |
| `create_ticket` | write | Create a new Zammad ticket. |
| `create_ticket_checklist` | write | Start a checklist on a ticket, optionally cloning a checklist TEMPLATE - the way a team encodes a repeatable procedure (onboarding, RMA, incident review). |
| `delete_ticket` | destructive | Permanently delete a Zammad ticket and all its articles. |
| `find_related_tickets` | read | Fetch Zammad's own 'related tickets' context for a ticket: OTHER OPEN tickets belonging to the SAME CUSTOMER (max 6), plus the tickets the calling agent viewed most recently (max 8). |
| `get_ticket` | read | Fetch a single Zammad ticket by its numeric ID. |
| `get_ticket_checklist` | read | Read the checklist attached to a ticket: its name and every item with the item's own id, text and tick state, in the order agents see them. |
| `get_ticket_full` | read | Fetch a ticket together with EVERYTHING needed to understand it in a single call: the ticket, every article the caller may see, and the related users, organization, group, state and priority records. |
| `link_tickets` | write | Link two tickets to each other. |
| `list_all_tags` | read | Enumerate every tag defined in this Zammad instance. |
| `list_checklist_templates` | read | List the checklist templates configured in this Zammad, so you can pick a template_id for `create_ticket_checklist`. |
| `list_customer_tickets` | read | List one customer's tickets as two ID lists, open and closed (max 15 each), with the full ticket records in an assets object keyed by ID. |
| `list_object_tags` | read | List tags currently attached to a specific Zammad object (default: a ticket). |
| `list_ticket_links` | read | List the objects explicitly linked to a ticket - other tickets, and knowledge base answers. |
| `list_ticket_time_entries` | read | List the time booked against one ticket: every entry with its time_unit, the article it was booked on, the activity type id and who booked it. |
| `list_tickets` | read | List Zammad tickets, paginated. |
| `merge_tickets` | destructive | Merge one ticket into another: every article moves to the target ticket and the source is emptied and closed as 'merged'. |
| `reassign_ticket_customer` | destructive | Move a ticket to a different customer (and optionally organization). |
| `remove_tag` | destructive | Remove a tag from a Zammad object (default: a ticket). |
| `search_tags` | read | Search tag names by prefix term. |
| `search_tickets` | read | Full-text search Zammad tickets, backed by Elasticsearch. |
| `search_tickets_by_condition` | read | Search tickets with a STRUCTURED condition instead of free text. |
| `set_checklist_item` | destructive | Tick, untick or rename ONE checklist item. |
| `unlink_tickets` | destructive | Remove a link between two tickets. |
| `update_ticket` | destructive | Update fields on an existing Zammad ticket. |
| `update_ticket_title` | destructive | Rename a ticket. |


## Users & organizations

| Tool | | What it does |
| --- | --- | --- |
| `create_organization` | write | Create a new Zammad organization. |
| `create_user` | write | Create a new Zammad user. |
| `get_me` | read | Return the currently-authenticated Zammad user (the caller). |
| `get_organization` | read | Fetch a single organization by numeric ID. |
| `get_user` | read | Fetch a single Zammad user by numeric ID. |
| `list_organizations` | read | List Zammad organizations, paginated. |
| `list_users` | read | List Zammad users (customers + agents + admins), paginated. |
| `search_organizations` | read | Search Zammad organizations by name or domain, using the same Elasticsearch-backed query syntax as `search_tickets`. |
| `search_users` | read | Search Zammad users by name, e-mail, login, or other indexed fields, using the same Elasticsearch-backed query syntax as `search_tickets`. |
| `update_organization` | destructive | Update fields on an existing organization. |
| `update_user` | destructive | Update fields on an existing Zammad user. |


## Reference data

| Tool | | What it does |
| --- | --- | --- |
| `get_group` | read | Fetch a single Zammad group by numeric ID. |
| `get_zammad_version` | read | Return the live Zammad version string. |
| `list_groups` | read | List all Zammad groups (teams / queues). |
| `list_object_attributes` | read | Return the COMPLETE Object-Manager attribute definitions: name, display label, data type (input, select, tree_select, boolean, date, datetime, integer, ...), the data_option block with the option list and default, which screens the attribute appears on, and whether it is active. |
| `list_roles` | read | List all roles (Admin / Agent / Customer / custom). |
| `list_ticket_fields` | read | Discover which fields a ticket has in THIS Zammad instance, including the custom Object-Manager attributes almost every production instance adds - nothing else on this server reveals them. |
| `list_ticket_priorities` | read | List all ticket priorities defined in this Zammad instance (typically 1 low, 2 normal, 3 high). |
| `list_ticket_states` | read | List all ticket states defined in this Zammad instance (open, closed, pending reminder, pending close, ...). |
