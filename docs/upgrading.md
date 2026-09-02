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

Every heading below is a **released tag**, not a hand-picked number. That is
worth stating because it was got wrong for a while: releases are cut by
semantic-release, and a `feat!` or `fix!` commit bumps the MAJOR. Two sections
here had been written ahead of time as minor bumps and are now renumbered to the
tags they actually shipped in — what was written as `3.1.0` is `4.0.0`, and
`2.1.0` is `3.0.0`. A third release, `2.0.0`, had no heading at all: its changes
were appended to the `1.0.0` section and have been split back out. Read by the
tag you are on, not by the position in the list.

Only releases that ask something of you get a section of their own; the rest are
summarised near the end, and `CHANGELOG.md` has the complete list.

---

## 6.1.0

`content_type` reaches the three tools that write an article as part of a ticket
write, and the value is now checked everywhere.

### `content_type` on every tool that writes an article body

`create_ticket`, `update_ticket` and `update_tickets` gain the `content_type`
parameter that `reply_to_customer` and `add_internal_note` already had. It
defaults to `'text/plain'` on all five, and the three ticket-write tools send it
explicitly even when it equals Zammad's column default, so every surface puts
the same thing on the wire.

Those three built their article without the field, so Zammad's
`ticket_articles.content_type` default applied to every body they wrote —
omitting the field does not make Zammad sniff the body. `create_ticket`
meanwhile described its `article_body` as "plain text or HTML" and answered
HTTP 201 for HTML it then had Zammad escape, so the agent and the customer both
read literal `<p>` tags in what looked like a successful call.

**What to do:** pass `content_type='text/html'` when the body is HTML, on any of
the five tools. Callers that send plain text need change nothing — but see the
next subsection if you pass `content_type` today.

### Only `text/plain` and `text/html` are accepted

`reject_unknown_content_type` guards all five surfaces and raises before any
request leaves the server. Three of them gained the parameter in this release,
so nothing existing can break there. The break is on `reply_to_customer` and
`add_internal_note`, where the parameter had been an unchecked string since they
were split out of `create_ticket_article` in 1.0.0: `'html'`, `'text/HTML'`,
`'text/markdown'` and `'text/html; charset=utf-8'` were forwarded and stored,
and now raise.

Zammad validates none of it. The column is `varchar(20)`, so
`'text/html; charset=utf-8'` is truncated rather than refused, and an unknown
value is not inert — per [ADR 0002](adr/0002-article-content-type.md), Zammad's
`sanitizeable?` matches `/html/i`, so `'html'` switches the HTML sanitizer ON
and `'text/markdown'` switches it OFF behind the caller's back, while everything
that renders the article matches the whole value against `text/html`.

**What to do:** grep your prompts and scripts for `content_type` and make every
value exactly `text/plain` or `text/html`. Which kind of breakage you get
depends on the value, and the harmless-looking one is the one to fix first. An
HTML near-miss was already reaching the customer as literal tags, so that call
now fails loudly instead of silently. Anything else — `'text/markdown'`, a
charset suffix on plain text — rendered exactly like `text/plain` and looked
fine, so those break with no prior symptom. Two limits worth knowing: on
`update_ticket` and `update_tickets` the check runs only when `article_body` is
set, and `extra_fields` remains an unvalidated passthrough, so a hand-built
`article` block there still bypasses this guard as it did before.

### `update_tickets` refuses an unknown `article_visibility` instead of publishing it

`update_tickets` no longer assembles its own article payload. It calls the
shared builder, which checks `article_visibility` against `'customer_visible'`
and `'internal'` and raises before the batch is sent, so no ticket is touched.

The local copy read visibility as `article_visibility == "internal"`, which is
`False` for any value it did not recognise. A typo — `'agents_only'`,
`'private'`, `'Internal'` — therefore sent `internal: false` and wrote what the
agent meant as an agent-only note as a customer-visible one, once per ticket in
the batch, and the call succeeded. With `article_type='email'` it was worse: the
guard against an internal e-mail compares the same exact string, so a typo
slipped past it too and the mail went out to every listed ticket's customer.
`create_ticket` and `update_ticket` already refused the same typo — the one tool
that fans a mistake across a hundred tickets was the one that did not check.

**What to do:** check every prompt or script that passes `article_visibility` to
`update_tickets`; the value must be exactly `internal` or `customer_visible`. If
a caller was passing anything else, the articles those runs wrote are
customer-visible and need reviewing in Zammad — `internal` is on every article
`list_ticket_articles` and `get_ticket_full` return. To find the tickets, grep
your logs for `audit.write` with `tool=update_tickets`: the trail records the
`ticket_ids` of every bulk call, though not the visibility that was passed, so it
gives you the list to check rather than the answer.

---

## 6.0.1

### The published image is `linux/amd64` only

The release workflow now passes `platforms: 'linux/amd64'`. It passed
`'linux/amd64,linux/arm64'` from the commit that added it onward, but no arm64
image was ever found: `docker buildx imagetools inspect` on the published tag
answered with a single image manifest, not a manifest list. The commit
attributes the dropped platform to the shared `bauer-group/automation-templates`
build workflow, which is outside this repository and cannot be checked from
here. The workflow input was the harmless half; the README advertised multi-arch
as a feature, which is the half that could talk someone into provisioning an ARM
host.

**What to do:** nothing for the image — 6.0.1 publishes the same amd64 artifact
6.0.0 did, and only corrects what was claimed about it. If you need arm64, build
it yourself from `app/bg-zammad-mcp`: the base is `python:3.14-alpine`, no stage
carries a `--platform` flag, and nothing in the Dockerfile is
architecture-specific. One thing to know when you build: `pyproject.toml` pulls
`bg-mcpcore` from a git ref on `@main`, so it resolves at build time — two builds
of the same tag on different days can carry different library code.

---

## 6.0.0

The attachment limits introduced in 5.1.0 were replaced twenty-five minutes
later, by this release. If you are coming from 5.0.x you never ran the old
names; read this section rather than 5.1.0's for anything you must configure.

### Three attachment settings became one — `ZAMMAD_ATTACHMENT_MAX_TRANSFER_BYTES`

`ZAMMAD_ATTACHMENT_MAX_READ_BYTES` (was 5 MiB — the *default* for the
`max_bytes` parameter of `download_ticket_attachment`, which a caller could
raise per call), `ZAMMAD_ATTACHMENT_READ_CEILING_BYTES` (20 MiB — the hard
ceiling on that parameter) and `ZAMMAD_ATTACHMENT_MAX_UPLOAD_BYTES` (10 MiB) are
gone. One setting replaces all three: `ZAMMAD_ATTACHMENT_MAX_TRANSFER_BYTES`,
default `10485760` (10 MiB), accepted between 1 byte and 100 MiB. It is the
largest single file the server will move in EITHER direction. Unchanged:
`ZAMMAD_ATTACHMENT_MAX_ARTICLE_BYTES` at 25 MiB, and the boot-time check that it
be at least the transfer limit — the server refuses to start otherwise.

Two numbers on one quantity can only ever disagree, and these did: 5 MiB for
reading against 10 MiB for uploading, so the server could attach a file it then
refused to read back.

**What to do:** in whatever sets this container's environment, delete the three
old names and set `ZAMMAD_ATTACHMENT_MAX_TRANSFER_BYTES` to the largest of the
numbers you were relying on. Note that for the shipped compose files that place
is *not* `.env` — none of them ever passed a `ZAMMAD_ATTACHMENT_*` variable
through, so a limit you really changed lives in a compose `environment:` block
you edited by hand, a `docker run --env-file`, or your platform's environment UI.
Uploads: if you had raised the upload limit above 10 MiB, carry that number over
or uploads that used to work start failing. Reads: the per-call `max_bytes`
override is gone with the ceiling, so the transfer limit is now a hard cap — your
old `ZAMMAD_ATTACHMENT_READ_CEILING_BYTES` is the safe carry-over. Anything above
25 MiB also needs `ZAMMAD_ATTACHMENT_MAX_ARTICLE_BYTES` raised to match, or the
server will not boot.

### The removed names are ignored, not rejected

The shared settings base sets `extra="ignore"`, so the three retired variables
are neither rejected nor logged. A deployment that set them starts cleanly and
silently uses the new defaults instead. This is the first time this document has
had to say that about a removed variable, and it is why step 2 of the general
procedure — diff `.env.example` against your own configuration — is not optional
here: every other break in this release announces itself, and this one does not.

Realistically the blast radius is close to nil, which is why this reads as a
completeness item rather than an incident: only 5.1.0 ever shipped those names,
it stood for twenty-five minutes, and the bundled compose files never forwarded
them.

**What to do:** run
`grep -rE 'MAX_READ_BYTES|READ_CEILING_BYTES|MAX_UPLOAD_BYTES'` across wherever
you set environment for this container. Any hit is a setting that is no longer
doing anything. A deployment that never ran 5.1.0 has nothing to find.

### `download_ticket_attachment` no longer accepts `max_bytes`

The parameter is gone from the published schema; passing it fails the call with
`Unexpected keyword argument`. The rest of the signature is unchanged:
`ticket_id`, `article_id`, `attachment_id`, and `mode` taking `'auto'`
(default), `'text'` or `'raw'`.

A per-call override that could only ever go down is noise once the response
itself is bounded — and it needed a second setting, the read ceiling, whose only
job was to stop a model that hit the guard from retrying with a bigger number.

**What to do:** delete the argument from any prompt, macro or script that passes
it. Unlike the settings replacement above, this break is loud. There is no
per-call replacement: how much is moved is
`ZAMMAD_ATTACHMENT_MAX_TRANSFER_BYTES`, and how much comes back is
`ZAMMAD_ATTACHMENT_MAX_TEXT_BYTES` / `ZAMMAD_ATTACHMENT_MAX_BLOB_BYTES` — all
operator settings.

### Two new limits bound the response, not the file

`ZAMMAD_ATTACHMENT_MAX_TEXT_BYTES` (default `262144`, 256 KiB; accepted 1 KiB to
8 MiB) caps the text or extracted-text body returned to the caller.
`ZAMMAD_ATTACHMENT_MAX_BLOB_BYTES` (default `2097152`, 2 MiB; accepted 1 KiB to
32 MiB) caps what comes back as raw base64 bytes. Both measure decoded bytes, as
every attachment limit here does.

The old read guard measured the FILE, and what costs is the RESPONSE — so it
blocked the free case and admitted the expensive one. A 9 MB PDF extracting to
20 KB of text was refused, while a 4 MB log file passed every check and returned
four megabytes of text, roughly a million tokens. A base64 blob is worse still:
it inflates 4/3 on the way out and no model reads it.

**What to do:** nothing to configure, but know two behaviour changes if you came
from 5.1.0. A text body over 256 KiB now arrives truncated where it used to
arrive whole, and a non-image binary between 2 MiB and 5 MiB that used to come
back as a base64 blob now comes back as metadata only. Raise either setting if
that matters; both are read once at startup, so restart after changing them. Two
exemptions: images never hit the blob limit, and a PDF, DOCX, XLSX or RTF larger
than it is still returned as extracted text, because extraction runs first.
Forcing `mode='raw'` opts out of both.

### The result gains `truncated` and `full_text_bytes`, and `content_kind` gains `metadata_only`

Every `download_ticket_attachment` result now carries `truncated`, `false` unless
a text or extracted-text body was cut at the text limit. When it is `true` the
result also carries `full_text_bytes`, the size of the whole body before the cut;
the key is absent otherwise. `size_bytes` keeps reporting the real file size in
both cases. `content_kind` takes one more value than before — `image`, `text`,
`extracted_text`, `blob`, and the new `metadata_only`, which means the bytes were
deliberately withheld: the result carries the filename, both MIME types,
`size_bytes` and a sentence saying why, with `content` left `null`.
`list_ticket_attachments` rows are unchanged and carry neither field.

A silently shortened document reads exactly like a complete one, which is the
failure worth avoiding: a model summarises half a file and reports it as the
whole. The cut is taken on encoded bytes and decoded with `errors='ignore'`, so
no replacement character appears at the seam.

**What to do:** check `truncated` before summarising or quoting an attachment,
and treat `content_kind` of `metadata_only` as "content withheld", not as an
empty file — the bytes exist, they were not sent. If you match on `content_kind`,
add the one new value.

---

## 5.1.0

The attachment surface. No new tools — the surface stays at 77 — but
`download_ticket_attachment` stopped being text-only, the three article-creating
tools learned to send files, and the limits on both directions became settings.
Everything is additive: an existing call keeps working.

This release stood for twenty-five minutes before 6.0.0 replaced three of its
five settings and removed `download_ticket_attachment`'s `max_bytes`. Read this
section for what the surface *is*; take the configuration from 6.0.0.

### `download_ticket_attachment` reads every file type, not only text

The tool no longer refuses binaries. Images (PNG, JPEG, GIF, WebP) come back as
an MCP image block the model can actually see; text comes back decoded, with the
charset that worked and a `lossy` flag; PDF, DOCX, XLSX and RTF come back as
extracted text with an `extraction` block saying what happened; everything else
comes back as metadata plus a base64 blob. The type is decided from the bytes —
magic bytes first, then the extension, then the label Zammad stored, then the
shape of the content — so a file mislabelled at upload still reads correctly. A
`mode` parameter takes `'auto'` (default), `'text'` to force a decode or `'raw'`
to force the blob. The result gained `detected_mime_type`, `content_kind`,
`extraction` and `decoding`; `mime_type` still carries Zammad's label, kept so a
caller can see the two disagree.

The old refusal was not squeamishness. The shared request helper decodes a 2xx
body as JSON when the content type says JSON and otherwise as httpx's
`Response.text` — a UTF-8 decode with `errors='replace'` — so for a PNG or a PDF
every non-UTF-8 byte became U+FFFD irreversibly, and handing that to a model
would have looked like a file and been corrupt data. The tool therefore raised,
which put an ordinary customer PDF permanently out of reach. A byte-preserving
path now bypasses that decode. Label-first typing had its own failure: a customer
RTF arriving as `application/msword` was refused as binary while being plain
text.

**What to do:** stop branching on `mime_type` and read `content_kind` instead.
Treat the set as open and give an unknown value a default branch — 6.0.0 adds
`metadata_only`. `content` is now `null` for images and blobs, where the payload
rides in the content blocks rather than in the structured result, so code that
read the result's `content` unconditionally must handle `null`.

### `reply_to_customer`, `add_internal_note` and `create_ticket` take `attachments`

All three gain an `attachments` list, at most 10 entries. Each entry needs
exactly one of three sources: `text` (literal content, the server base64-encodes
it), `data_base64` (raw bytes), or `copy_from` — a
`{ticket_id, article_id, attachment_id}` reference to a file already in Zammad,
whose bytes move server-side and never enter the conversation. `filename` is
required except with `copy_from`, which inherits it. Filenames ending in one of
17 executable extensions are refused, as is any content whose first bytes mark it
as a Windows or ELF executable, whatever it is called.

Files ride on these tools rather than on an attach tool of their own because
Zammad has no endpoint that adds a file to an existing article; the payoff is
that visibility stays encoded in the tool name and a reply with a file stays ONE
article, so the customer receives one mail instead of two. The executable
denylist exists because an unattended agent putting an executable into a
customer's inbox under the helpdesk's name is the obvious accident. It is
deliberately asymmetric with the read path, which will happily return a `.js`
file as text — reading what a customer sent is not the risk.

**What to do:** prefer `text` for content you just generated and `copy_from` for
moving a file between tickets — it costs no tokens and stays byte-identical. Do
not describe the denylist as virus scanning to anyone: a `.zip` containing an
executable passes it.

### `ZAMMAD_ATTACHMENT_UPLOAD_ENABLED` is the one deliberate off-switch

Five `ZAMMAD_ATTACHMENT_*` settings arrived with this release. Three of them —
the two read limits and the upload limit — were replaced in 6.0.0 and are
documented there; do not configure from their old names. Two survive unchanged:
`ZAMMAD_ATTACHMENT_UPLOAD_ENABLED` (default `true`) and
`ZAMMAD_ATTACHMENT_MAX_ARTICLE_BYTES` (25 MiB, all files in one article
together). One limit is deliberately not a setting: at most 10 files per article,
whatever their size.

Setting `ZAMMAD_ATTACHMENT_UPLOAD_ENABLED=false` REMOVES the `attachments`
parameter from the three tools rather than rejecting it at call time. Publishing
a capability the server does not have means the model reads the schema, sends a
file, and learns only from the error.

**What to do:** decide whether this deployment should be able to send files at
all. It ships enabled.

### The audit log now names the files a write sent

An audit entry for a write carrying `attachments` gains `attachment_count` and
`attachment_filenames` — the first ten names, plus
`attachment_filenames_truncated_from` when there were more. The count is always
exact. Content never appears: the helper reads only the `filename` key of each
entry, deliberately, because `text` and `data_base64` sit in the same dicts.

"What did the agent send to the customer" is precisely the question this trail
exists to answer, and a count alone cannot answer it. Filenames were taken
knowingly: `Kuendigung_Mueller.pdf` carries personal information.

**What to do:** nothing switches this on — uploads ship enabled, so filenames
start appearing in `audit.write` at INFO level as soon as an agent attaches a
file. Before upgrading, confirm that your log retention and your aggregator's
access rules are fit for personal data. If they are not,
`ZAMMAD_ATTACHMENT_UPLOAD_ENABLED=false` is the only lever: it removes the
parameter entirely, so nothing is uploaded and no filename is logged. There is no
setting that keeps uploads while suppressing the names.

### Text extraction rides on an optional `documents` extra

`pyproject.toml` gains a `documents` extra — `pypdf`, `openpyxl`, `striprtf` and
`defusedxml`, every one pure Python because the production image is
`python:3.14-alpine` and musl has no manylinux wheels, so a C extension would be
compiled at image-build time. The image already installs it.

Without the extra nothing crashes and no call fails. `download_ticket_attachment`
still returns the file: a PDF, DOCX or XLSX comes back as a blob with
`extraction.status` `"failed"` and `extraction.reason` naming what is missing.
RTF is the one type that does not fall back to a blob — it is text underneath, so
a failed extraction degrades to the decoded raw text. `defusedxml` is not treated
differently at the boundary; what it does not do is fall back to `xml.etree`,
whose parser expands internal entities, so a silent fallback would switch the
entity guard off in exactly the deployment that forgot the extra.

**What to do:** nothing if you run the published image. If you install from the
source tree and want PDF/DOCX/XLSX/RTF as text, run `pip install ".[documents]"`
in `app/bg-zammad-mcp` — the server is not published to any package index, so the
`pip install 'bg-zammad-mcp[documents]'` in the error message names the extra
rather than a command you can run. For a local test run use
`pip install -e ".[test,dev,documents]"`, which is what CI does; without it the
parser-dependent tests skip instead of running.

### Read ADR 0001 before pointing this at a public helpdesk

Turning attachments into text means running parsers over untrusted input inside a
process that holds per-user Zammad credentials, so the decisions are written down
in [ADR 0001](adr/0001-attachment-decoding-safety.md). DOCX avoids `python-docx`
— it is `zipfile` plus a `defusedxml` parse — because `python-docx` pulls in
`lxml`, whose default parser runs with `resolve_entities=True` and would open a
`file://` read vector.

Four controls back the parsers, but not all four back every format: the
`forbid_dtd` parser setting and the ZIP total-size and per-member expansion caps
cover the OOXML formats, DOCX and XLSX; PDF and RTF extraction is covered only by
the worker thread and the wall-clock budget. The ADR also states what it does not
close: `openpyxl` does its own parsing with plain `ElementTree`, so an XLSX
carrying an internal entity bomb is bounded by the size caps rather than refused
outright — accepted, and written down rather than hidden. It also records that
the timeout bounds the request rather than the CPU, because an abandoned worker
thread runs to completion, so the size and ratio caps are the primary defence.

**What to do:** read the ADR before enabling this where the helpdesk receives
mail from the public. Your lever over how much any parser is handed is
`ZAMMAD_ATTACHMENT_MAX_TRANSFER_BYTES` (6.0.0 and later). And do not present
either the document parsers or the executable denylist as virus scanning.

---

## 5.0.2

### `get_kb_answer` returns the answer body

`get_kb_answer` now makes the second request its module docstring always
described, and comes back with the answer's content records. One constant carried
two spellings of the same model that are not interchangeable — the Elasticsearch
index name `KnowledgeBase::Answer::Translation` and the asset-graph key
`KnowledgeBaseAnswerTranslation` — and it is split into
`ANSWER_TRANSLATION_INDEX` and `ANSWER_TRANSLATION_ASSET`, each named after what
it addresses.

Zammad's asset serializer strips the namespace colons; its search index keeps
them. The single constant held the colon form and was used for both, so the asset
lookup never matched, always returned an empty list, and `get_kb_answer` always
took its early return — the second request that fetches the content was never
made. The one tool whose purpose is "read this knowledge base answer" returned
the title, the category, the locale and the author, and no answer text. It
shipped that way in 1.0.0 and stayed broken through 5.0.1. No test caught it: the
fixture built its asset map from the same constant, so the fake and the code
agreed with each other and both disagreed with Zammad.

**What to do:** nothing, unless you worked around it. A prompt or script that
quotes `search_knowledge_base` excerpts instead of the article, or that tells the
model `get_kb_answer` returns metadata only, is now wrong and costs you the full
text — drop it and read the answer. Expect the response to grow by the article's
HTML and the call to cost two upstream GETs rather than one.

---

## 5.0.0

### The container listens on 8080; the host port did not move

The server binds 8080 **inside** the container: `MCP_PORT=8080` is pinned in the
image alongside `EXPOSE 8080`, and the `HEALTHCHECK` expands `${MCP_PORT:-8080}`
from the container's own environment at runtime.

Nothing host-facing changed. `docker-compose.development.yml` still publishes
`"${ZAMMAD_MCP_PORT:-8000}:${MCP_PORT:-8080}"` — host 8000, as before — and
`PUBLIC_BASE_URL` still defaults to `http://localhost:8000`. The Traefik and
Coolify stacks publish no host port at all; they `expose:` the container port and
route to it, the Traefik load-balancer label reading the same `${MCP_PORT:-8080}`.
8080 is safe inside a container's own network namespace, and the host stays 8000
precisely so Zammad's nginx — which publishes 8080 there — can sit beside it on
one box.

The half-done version of this move is what makes it worth a section. Before this
release the Dockerfile set only `EXPOSE 8000` and probed `${MCP_PORT:-8000}`,
nothing in it set `MCP_PORT`, and `src/config.py` does not override the port, so
the process took the `bg-mcpcore` default. Moving `EXPOSE` and the healthcheck
alone would have left the application behind: `docker run` with no environment
would serve perfectly and report itself unhealthy, an orchestrator restarting a
working process on a schedule. Compose passes `MCP_PORT` explicitly and hid that
completely, which is why the pin exists.

**What to do:** if you deploy from a shipped compose file, nothing — compose sets
`MCP_PORT` itself, so old and new files stay coherent either way. If anything
else targets the **container** port directly — a hand-written `-p 8000:8000`, a
`containerPort` or `targetPort` of 8000, or a reverse proxy on the container
network pointing at `:8000` — move it to 8080, or set `MCP_PORT=8000` in the
container's environment, which moves the server, the image healthcheck, the
`expose:` entry and the Traefik label together. The in-container probe moved with
it: `docker exec -it bg-zammad-mcp curl -fsS http://localhost:8080/healthz`. Do
not reach for `MCP_PORT` to fix a *host*-side clash — that knob is
`ZAMMAD_MCP_PORT`, and `MCP_PORT` will not move what is published on the host.

### A missed retarget looks like a healthy container behind a dead route

Nothing in the code — this is what the failure looks like, and it is worth
knowing before you go looking for it in the wrong place.

The container will not tell you it happened. `docker ps` reports `healthy`, the
log shows uvicorn on 8080, and `/healthz` answers 200 from inside, because the
`HEALTHCHECK` probes the same variable the server binds. The probe tracks the
process, not the route in front of it, so it stays green whatever the mapping
says. The refused request dies one layer out, where this server writes nothing; a
reverse proxy in front will log its own 502, but the server's log stays clean.

**What to do:** after redeploying, do not accept `docker ps` as proof. Run the
verification block in [operations.md](operations.md#verifying-a-deployment)
against the host name your clients actually use — it is host-facing from the
first `curl`, which is exactly the half `docker ps` cannot see.

---

## 4.0.0

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

## 3.0.0

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

## 2.0.0

One concept carried two names in several places, and one of those splits was
dangerous rather than merely untidy. This release settles how an article's
audience is stated and how a ticket is addressed.

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

---

## 1.0.1

### `MCP_ALLOWED_CLIENT_REDIRECT_URIS` now defaults to empty

The default went from four entries — the two Claude callbacks and loopback on
`localhost` and `127.0.0.1` — to an empty list, and all three compose files
changed their fallback from that same list to an empty one. Empty is not "allow
this list": it means the allowlist argument is never passed to the OAuth proxy at
all, so FastMCP's own default applies and any dynamically-registered client may
name any redirect URI.

It is a deliberate loosening, made for interoperability. Dynamic client
registration exists precisely because a server cannot know its clients in
advance, so enumerating vendor callbacks worked against the mechanism: it pinned
the deployment to the agents known on the day it was configured and refused every
other one at registration time, with an error a client cannot act on. Claude,
Copilot, Cursor and Continue all use different callbacks, desktop and IDE clients
use a loopback port chosen at runtime, and ChatGPT prefers Client ID Metadata
Documents over DCR entirely — no list could be both complete and current.

What still bounds the damage, and is always on: the consent screen names the
client and its redirect target before any token is issued, `MCP_ALLOWED_ROLES`
decides who may use the server at all, every call runs with that user's own
Zammad permissions, and writes are audit-logged. The residual risk the allowlist
removes is narrower than it looks — a user talked into approving a client an
attacker registered.

**What to do:** decide rather than inherit. If you deployed 1.0.0 and never set
the variable, you had an allowlist and from 1.0.1 you do not; if you want it
back, set `MCP_ALLOWED_CLIENT_REDIRECT_URIS` explicitly — a trailing `*` is a
wildcard, the MCP spec requires every entry to be loopback or HTTPS, and
`.env.example` carries a ready-to-paste restrictive value. Note that the shipped
compose files no longer supply the old default either, so redeploying from an
updated compose file flips it even if your own configuration is untouched.
Confirm which way your instance came up: the boot line
`auth.zammad_oauth_configured` reports `allowed_client_redirect_uris` as a count
of patterns, or as the literal `any`.

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

## Releases that ask nothing of you: 4.0.1, 5.0.1, 5.0.3, 5.0.4, 5.0.5

4.0.1 replaced the bundled `logo.svg` — the icon FastMCP puts on the OAuth
consent screen — with the real BAUER GROUP mark, and documented the
already-existing `MCP_ICON_URL` (plus `MCP_DISPLAY_NAME` and `MCP_WEBSITE_URL`)
in `.env.example`; leave it empty and the bundled logo is served at
`${PUBLIC_BASE_URL}/logo.svg`. 5.0.1 let the Dockerfile port test skip when the
Dockerfile is not in the build context, which is what let the image build again.
5.0.3 is documentation, one dependency change and an empty rebuild commit:
`pyproject.toml` now tracks `bg-mcpcore` at `@main` instead of a pinned commit,
and the base-image update touches no file at all — nothing here pins a
base-image digest, so the daily monitor records the new upstream digest in a
repository variable and dispatches a release purely to rebuild on it. 5.0.4 and
5.0.5 removed two status-page cards: the "Zammad Backend" card, which rendered a
literal `$zammad_url` because the bg-mcpcore migration left the framework's five
template variables and no seam for a sixth, and a Documentation card the footer
link already duplicated.

None of them changes a tool signature, a response shape, a variable you must set,
or a default you were relying on.

**What to do:** nothing, with two footnotes — one if you pin an exact image tag,
one if you build the image yourself. `v5.0.0` has a git tag and a changelog entry
but never published an image: the release job cuts the tag and the image build
runs after it, and that build failed on the port-coherence check 5.0.1 fixed. So
`ZAMMAD_MCP_VERSION=5.0.0` will not resolve — use `5.0.1` or later. And since
5.0.3 a build resolves `bg-mcpcore` from `@main` rather than a fixed commit. A
published tag you pull is unchanged, but a rebuild of the same commit is not
reproducible, so when a local build breaks, look at what moved in the library
before suspecting this repository.

---

## Earlier: the bg-mcpcore migration (0.1.x)

The cross-cutting machinery — settings, inbound auth, encrypted OAuth storage,
logging, rate limiting, routes, the outbound client — moved into the shared
`bg-mcpcore` library, and the server became profile-driven.

Operationally the only visible change was the CLI: the `health`, `probe` and
`tools` subcommands are gone. Container liveness is the unauthenticated
`/healthz` route; upstream reachability is the `bg.health` tool.
