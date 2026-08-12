# Ticket attachment support — read and write

Status: approved (2026-08-12)
Scope: `app/bg-zammad-mcp`

Written in English to match the rest of the repository; the design conversation
was held in German.

## Problem

The server can list attachment metadata and can read text files. Everything
else is refused. Three distinct defects sit behind that:

1. **Binary attachments are unreadable.** `download_ticket_attachment` raises a
   `ToolError` for anything that is not a text MIME type. A screenshot on a
   ticket cannot be looked at, which is the single most common thing a support
   agent needs from an attachment.
2. **The refusal is keyed on the declared MIME type, not on the bytes.** A file
   uploaded as `application/msword` that is actually RTF — plain text with
   control words — is refused, even though nothing about it is binary. This is
   the case that triggered the work: `Technische-Daten-Liquid-Liquid.rtf`,
   131 793 bytes, refused as binary while being text.
3. **There is no write path at all.** Nothing in the tool surface can attach a
   file to a ticket.

### Why the current code believes binary is impossible

`src/zammad/tools/attachments.py` documents the refusal as a platform limit:
"This context has no byte-preserving path". That is true of the shim the tools
call, and false of the layer underneath it.

- `bg_mcpcore.tools.protocol.ToolContext.request()` returns the raw
  `httpx.Response`.
- `ToolContext.request_json()` decodes it — JSON when the content type says so,
  `response.text` otherwise, which is a lossy UTF-8 decode.
- `server._DecodingCtx.request()` — the only `request` the Zammad tool modules
  can see — delegates exclusively to `request_json()`.

So `response.content` exists and is reachable. The gap is in this repository,
not in the library. No change to `bg-mcpcore` is required.

## Goals

- Read images so the model can actually see them.
- Read PDF, DOCX, XLSX and RTF as text.
- Decide file type from the bytes, not from the upload-time label.
- Write attachments from three sources: literal text, base64, and a copy of an
  attachment that already exists in Zammad.
- Keep the existing size guard that rejects an oversized file *before*
  transferring it.

## Non-goals

- Virus scanning. The executable denylist below is a tripwire against the
  obvious accident, not an AV product, and must never be described as one.
- Scanning inside archives. A `.zip` containing a `.exe` passes.
- Fetching attachments from arbitrary URLs. Rejected during design: it is an
  SSRF vector (internal endpoints, cloud metadata services) that would need an
  allowlist to be safe, and no use case demanded it.
- OCR of scanned documents.
- Exposing attachments as MCP resources. Protocol-clean, but claude.ai
  connectors barely consume resources and it does not address writing.

## Architecture

Three units, each with one job:

| Unit | Responsibility | Depends on |
| --- | --- | --- |
| `src/zammad/media.py` (new) | Type detection: magic-byte sniffing, MIME normalisation, classification into `image` / `text` / `document` / `opaque` | nothing |
| `src/zammad/extract.py` (new) | Document → text (PDF, DOCX, XLSX, RTF), one isolated extractor per format | the optional `documents` extra |
| `src/zammad/tools/attachments.py` | Tool wiring, limits, error messages | the two above |
| `src/zammad/tools/articles.py`, `tickets.py` | The `attachments` write parameter | a shared upload helper |

`media.py` is deliberately dependency-free and pure: bytes plus a declared type
in, a classification out. The logic that broke on the RTF file is therefore
unit-testable without HTTP.

### Byte-preserving transport

`server._DecodingCtx` gains one method:

```python
async def request_raw(self, method: str, path: str, **kwargs) -> httpx.Response:
    """Byte-preserving upstream call — the response body is NOT decoded."""
```

It calls `self._ctx.request(...)` directly and raises the same typed
`ZammadError` on a non-2xx status, so error handling stays uniform with
`request()`. The `ToolContext` Protocol in `src/zammad/tools/__init__.py` is
extended to declare it. Only the attachment module uses it; the other 17 tool
modules are untouched.

## Read path

### Flow

```
1. GET /ticket_articles/{article_id}          -> metadata (filename, size, declared MIME)
2. size > max_bytes                           -> refuse, NO transfer          [unchanged]
3. request_raw GET /ticket_attachment/...     -> bytes
4. classify: magic bytes > filename extension > declared MIME
5. route by classification
```

Step 2 must survive the rewrite. It is the only reason a 200 MB file can be
rejected without moving it, and "we can do bytes now, so download first and
check after" would silently drop that protection.

Step 4 is the actual repair. Today the declared MIME type decides alone.

### Routing table

| Classification | Returned as |
| --- | --- |
| `image/png`, `image/jpeg`, `image/gif`, `image/webp` | `ImageContent` block — the model sees the image |
| text-like (`text/*`, JSON/XML/YAML/CSV, RTF) | decoded text in `content` |
| PDF, DOCX, XLSX | extracted text in `content`, with `extraction` describing what happened |
| anything else | metadata plus a base64 `EmbeddedResource` blob |

RTF is a special case: it is text, so it is never refused, but raw RTF is
prose buried in control words. It is therefore run through the stripper and
reported as `content_kind = "extracted_text"`. If `striprtf` is unavailable it
falls back to the raw text with `extraction.status = "failed"` — degraded but
still readable, which is strictly better than today's refusal.

### Return shape

The tool returns a `ToolResult`. MCP separates *structured content* (JSON
matching the output schema) from *content blocks* (what the model actually
receives); an image must travel as an `ImageContent` block or it is merely an
expensive base64 string inside JSON.

`structured_content` keeps every field it has today —
`ticket_id`, `article_id`, `attachment_id`, `filename`, `mime_type`,
`size_bytes`, `content` — and adds:

| Field | Meaning |
| --- | --- |
| `detected_mime_type` | what the bytes say, which may differ from `mime_type` |
| `content_kind` | `text` \| `image` \| `extracted_text` \| `blob` |
| `extraction` | `{status, tool, reason}` — `status` is `ok`, `partial`, `failed`, or `not_applicable` for images, plain text and blobs, where no extraction was attempted |
| `decoding` | `{charset, lossy}` for text and extracted-text results; `null` for images and blobs |

Additive only: no existing consumer breaks. `content` stays exactly as it is
for text files.

### Text decoding

Today's decode is `httpx`'s `response.text`, i.e. UTF-8 with
`errors="replace"`, applied invisibly. The replacement happens silently, so a
caller cannot tell a clean decode from a mangled one. The new path is explicit:

1. charset from the response `Content-Type` header, if present and valid;
2. otherwise UTF-8, strict;
3. otherwise `cp1252` (the realistic source of Windows-authored helpdesk files),
   strict;
4. otherwise `latin-1`, which cannot fail.

Whichever succeeds is reported in `decoding.charset`, and `decoding.lossy` is
true whenever a replacement character was produced. The model can then say the
file was undecodable instead of reading through U+FFFD soup.

### Escape hatch

A `mode` parameter: `auto` (the routing table), `text` (force a text decode —
rescues anything the sniffer does not recognise), `raw` (force the base64
blob). No file can end up permanently unreachable, which was the original
complaint.

## Document extraction

| Format | Implementation | Rationale |
| --- | --- | --- |
| PDF | `pypdf` | Writing a PDF text extractor is not a serious option |
| XLSX | `openpyxl` (`read_only=True`, `data_only=True`) | Shared strings, inline strings and cached formula values are fiddly enough that the library wins. Uses `ElementTree` internally |
| DOCX | stdlib `zipfile` + `xml.etree.ElementTree` | See the decision record below |
| RTF | `striprtf` | Small, pure Python, solves the triggering case |

All four ship as an optional extra, `bg-zammad-mcp[documents]`, installed by the
Dockerfile. If the extra is absent the server degrades to blob-plus-metadata
rather than failing to start.

### Decision record: DOCX without `python-docx`

**Context.** House standard prefers established libraries over hand-rolled
code. The obvious choice for DOCX is `python-docx`.

**Decision.** Extract DOCX text with the standard library — read
`word/document.xml` from the ZIP and collect `w:t` nodes — instead of using
`python-docx`.

**Reasons.**

1. *Security surface.* `python-docx` depends on `lxml`, whose default parser
   runs with `resolve_entities=True`. Attachment bytes arrive from customers by
   e-mail, so a crafted DOCX is an untrusted input; entity resolution against
   `file://` targets is a local-file-read vector. Measured on this repository's
   runtime (CPython 3.14.3), `xml.etree.ElementTree` refuses external entities
   outright — `<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/hostname">]><r>&x;</r>`
   raises `ParseError: undefined entity` — so the whole vector disappears with
   the dependency.
2. *Build.* The production image is `python:3.14-alpine`. musl has no manylinux
   wheels, so `lxml` would be compiled from source at image-build time. `pypdf`,
   `openpyxl` and `striprtf` are pure Python and install cleanly there.
3. *Scope.* We need the text of a document, not its object model. Collecting
   `w:t` nodes covers body prose and table cells, which is what a support agent
   reads.

**Consequences.** Roughly 50 lines of our own code to maintain. Content in
text boxes, SmartArt and footnotes may be missed; when the extractor produces
nothing it reports `extraction.status = "partial"` rather than an empty string,
so the gap is visible instead of silent. `.doc` (the pre-2007 binary format) is
not covered and falls through to blob.

**Documented in the repository** as `docs/adr/0001-attachment-decoding-safety.md`,
which also carries the safety controls below. This is the repository's first
ADR and establishes `docs/adr/` as the location for further ones.

### Safety controls around every parser

The parsers see hostile bytes, so they sit behind four barriers:

1. **No DTD.** Any XML member containing a `<!DOCTYPE` declaration is rejected
   before parsing. Measured behaviour: ElementTree does not resolve external
   entities but *does* expand internal ones, which is the billion-laughs
   building block. OOXML never legitimately contains a DTD, so rejecting it
   costs nothing and removes the entire entity class structurally, rather than
   relying on a claim about parser internals.
2. **ZIP ratio and absolute cap.** For DOCX and XLSX, the sum of
   `ZipInfo.file_size` is checked against `compress_size` and against an
   absolute ceiling before anything is read. A zip bomb is refused before the
   first byte is decompressed. Members are read by exact name with a bounded
   read; nothing is ever extracted to disk, so path traversal is not reachable.
3. **Worker thread.** Extraction runs under `anyio.to_thread.run_sync`. Parsing
   a 20 MB PDF on the event loop would stall the server for every concurrent
   user.
4. **Wall-clock budget.** A hard timeout per extraction, which is also the
   practical answer to quadratic-blowup inputs that survive the checks above.

A control that trips produces `extraction.status = "failed"` with a reason, never
a silent empty result.

## Write path

Zammad has **no endpoint that attaches a file to an existing article**.
Attachments are created only by `POST /ticket_articles`, which accepts:

```json
"attachments": [
  {"filename": "report.csv", "data": "<base64>", "mime-type": "text/csv"}
]
```

Note `mime-type` with a hyphen. A misspelled key is ignored without error and
the file reaches the customer as `application/octet-stream`; this gets its own
test.

Every write therefore creates an article, which fixes the tool shape:
`reply_to_customer`, `add_internal_note` and `create_ticket` each gain an
optional `attachments` parameter. Visibility stays encoded in the tool name, so
the trap that `articles.py` was split in two to close stays closed, and message
plus file remain one article — one e-mail to the customer, not two.

### Input model

Flat with a validator rather than a discriminated union: a flat schema is easier
for a model to fill in than a `oneOf`, and the validator can explain the mistake
in a sentence.

```python
class CopyRef(BaseModel):
    ticket_id: int
    article_id: int
    attachment_id: int

class AttachmentInput(BaseModel):
    filename:    str | None = None   # required unless copy_from supplies it
    text:        str | None = None   # source 1 — server base64-encodes it
    data_base64: str | None = None   # source 2 — real bytes
    copy_from:   CopyRef | None = None  # source 3 — server-side byte copy
    mime_type:   str | None = None   # optional; derived from the extension otherwise
```

Exactly one of `text` / `data_base64` / `copy_from` must be set. All three
converge on `(filename, bytes, mime_type)`, then the guardrails run, then one
payload goes to Zammad.

`filename` and `mime_type` are required for `text` and `data_base64`
(`mime_type` is derived from the extension when omitted). With `copy_from` both
are inherited from the source attachment, and either may be given explicitly to
rename or relabel the copy.

`copy_from` is the highest-value source: the bytes never enter the model's
context window, so the copy is free in tokens, byte-identical, and unbounded by
context size. It reads the source attachment with the caller's own permissions,
which Zammad enforces exactly as it does for a download.

### Guardrails

| Setting | Default | Effect |
| --- | --- | --- |
| `ZAMMAD_ATTACHMENT_MAX_UPLOAD_BYTES` | 10 MiB | per file, measured on decoded bytes |
| `ZAMMAD_ATTACHMENT_MAX_ARTICLE_BYTES` | 25 MiB | sum per call |
| `ZAMMAD_ATTACHMENT_UPLOAD_ENABLED` | `true` | `false` removes the parameter from the schema via FastMCP's `ArgTransform(hide=True)` — not a runtime error the model discovers by trying |
| `ZAMMAD_ATTACHMENT_MAX_READ_BYTES` | 5 MiB | default for the read tool's `max_bytes` |
| `ZAMMAD_ATTACHMENT_READ_CEILING_BYTES` | 20 MiB | hard ceiling on `max_bytes` |

Limits measure decoded bytes because base64 inflates by 4/3; a limit on the
string would pass only 7.5 MB of payload at a nominal 10 MB and would be
inexplicable to the caller. Zammad's own body limit applies to the inflated
size, so the article ceiling keeps headroom: 25 MiB of payload is roughly
33 MiB of body.

The last two settings replace the hardcoded `DEFAULT_MAX_BYTES` and
`MAX_ALLOWED_BYTES` constants, which are currently buried in a tool module and
unreachable for an operator.

### Executable denylist

Rejected by extension and by magic bytes (`MZ` for PE, `\x7fELF`):
`.exe .com .scr .pif .bat .cmd .msi .lnk .hta .reg .vbs .vbe .js .jse .wsf .ps1 .jar`

Applied uniformly, not only to customer-visible articles: an `.exe` on an
internal note is equally not something an unattended agent should create, and a
per-visibility exception is a rule someone will misremember. It applies to
`copy_from` as well — that a file already sits in Zammad is not a reason for an
unattended agent to forward it to a customer. Zammad itself does not filter API
uploads, so this is a real addition rather than a duplicate control.

Note the deliberate asymmetry with the read path, which happily returns a
`.js` file as text. Reading what a customer sent is not the risk; re-sending an
executable under the helpdesk's name is.

### Audit

`src/audit.py` logs identifiers from a fixed list and deliberately no content.
Without an addition, a call that mailed a file to a customer is logged
identically to one that did not. Two fields are added: `attachment_count` and
`attachment_filenames` (truncated).

Filenames are a judgement call — `Kuendigung_Mueller.pdf` carries personal
information. They are logged anyway because "what did the agent send to the
customer" is precisely the question the audit trail exists to answer, and a
count alone cannot answer it. File *contents* remain unlogged, consistent with
the module's existing rule.

## Error handling

One rule throughout: no silent degradation. Every fallback carries a status and
a human-readable reason, so the model reports "text extraction failed, the
document is encrypted" instead of speculating over an empty string. That is the
same stance as today's binary refusal, without the dead end.

Failure modes and responses:

| Situation | Response |
| --- | --- |
| Attachment not on that article | `ToolError` naming `list_ticket_attachments` (unchanged) |
| File larger than `max_bytes` | `ToolError` before any transfer (unchanged) |
| Extraction library missing | blob plus `extraction.status = "failed"`, reason names the missing extra |
| Parser raises, times out, or trips a limit | blob plus `extraction.status = "failed"` with the reason |
| Extractor yields nothing | `extraction.status = "partial"`, empty content stated explicitly |
| Text decode needed a lossy fallback | `decoding.lossy = true` |
| Upload disabled | parameter absent from the schema |
| Upload denied by size or denylist | `ToolError` naming the file and the limit or the reason |

## Testing

| Level | Focus |
| --- | --- |
| `media.py` | Sniffing table; `test_rtf_declared_as_msword_is_read_as_text` — a named regression for the case that triggered this work |
| `extract.py` | One small committed fixture per format; DOCTYPE rejection; zip bomb refused before decompression; missing library degrades instead of crashing; timeout path |
| Read tools | PNG produces an `ImageContent` block; `mode=raw` / `mode=text` override the sniffer; oversized still refused with **one** upstream call; existing `structured_content` fields unchanged |
| Write tools | Payload carries `mime-type` with a hyphen; exactly-one-source validator; per-file and per-article limits; denylist by extension and by magic bytes; `copy_from` issues 2 GETs then 1 POST; `ZAMMAD_ATTACHMENT_UPLOAD_ENABLED=false` removes the parameter from the schema |
| Audit | A write with attachments logs count and filenames; contents never appear |

The existing `tests/test_tools_attachments.py` scripted-context harness extends
to serve bytes for `request_raw`. Tool count stays at 75 — only parameters and
return fields are added — so `EXPECTED_TOOLS`, the README and the doc counts
stay stable.

## Documentation and release

- `docs/tools.md` and `.env.example` regenerated with the existing scripts.
- `docs/adr/0001-attachment-decoding-safety.md` — the decision record above plus
  the safety controls.
- `docs/zammad-7.md`: replace "List, and download with a size cap" with what the
  server actually does, including the stated non-goals.
- `docs/cookbook.md`: two recipes — attaching a CSV, and carrying a data sheet
  from one ticket to another with `copy_from`.
- `CHANGELOG.md` via semantic-release. This is a `feat` (MINOR) and it changes
  the image, so it cuts a release under the repository's Dependabot rule.

## Compatibility

Purely additive. `structured_content` keeps every current field and `content` is
unchanged for text files. The one behavioural change is that a binary
attachment no longer ends in a `ToolError` — nothing can sensibly depend on
that.

## Build sequence

1. `media.py` plus its tests — pure, no I/O, no dependencies.
2. `request_raw` on `_DecodingCtx` and the `ToolContext` Protocol.
3. Read path routing for images, text and blobs; extend the existing tests.
4. Settings for the five limits; retire the hardcoded read constants.
5. `extract.py` with the four extractors and all four safety controls.
6. Wire extraction into the read path.
7. Shared upload helper: the three sources, the guardrails, the denylist.
8. `attachments` parameter on the three write tools, plus the hide transform.
9. Audit fields.
10. Documentation, ADR, regenerated artefacts.

Steps 1–4 deliver the triggering RTF fix and image reading on their own; each
subsequent step is independently shippable.
