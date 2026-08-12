# ADR 0001 — Decoding attachment documents safely

Status: accepted
Date: 2026-08-12
Applies to: `app/bg-zammad-mcp/src/zammad/extract.py`, `app/bg-zammad-mcp/src/zammad/uploads.py`

## Context

Attachment bytes reach this server from customers, usually by e-mail. Turning
them into text means running parsers over untrusted input inside a process that
holds per-user Zammad credentials. The obvious library for DOCX is
`python-docx`.

## Decision

1. **DOCX is extracted with the standard library** — `zipfile` plus an XML
   parse, collecting `w:t` nodes from `word/document.xml` — rather than with
   `python-docx`.
2. **PDF, XLSX and RTF use libraries** (`pypdf`, `openpyxl`, `striprtf`), all
   pure Python.
3. **Our own XML parsing goes through `defusedxml`**, never through
   `xml.etree.ElementTree` directly.
4. **Every parser runs behind four controls**, listed below.

## Reasons

### Security surface

`python-docx` depends on `lxml`, whose default parser runs with
`resolve_entities=True`. A crafted DOCX could then resolve an external entity
against a `file://` target — a local-file-read vector in a process holding
credentials.

Measured on this repository's runtime, CPython 3.14.3, `xml.etree.ElementTree`
refuses external entities outright:

```pycon
>>> ET.fromstring('<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/hostname">]><r>&x;</r>')
xml.etree.ElementTree.ParseError: undefined entity &x;
```

So dropping `lxml` closes the file-read vector. It does **not** close the
denial-of-service one: the same measurement showed *internal* entities are
expanded, which is the billion-laughs building block. The two controls are
therefore complementary, not alternatives. Parsing runs through `defusedxml`
with `forbid_dtd=True`; measured on defusedxml 0.7.1, both the internal and the
external form raise `EntitiesForbidden`, and a bare DTD raises `DTDForbidden`.

Enforcing this in the parser rather than by scanning the leading bytes matters:
a byte-level check misses a declaration placed past its window or encoded as
UTF-16.

### Build

The production image is `python:3.14-alpine`. musl has no manylinux wheels, so
`lxml` would be compiled from source at image-build time. `pypdf`, `openpyxl`,
`striprtf` and `defusedxml` are pure Python and install cleanly there.

### Scope

We need a document's text, not its object model. Collecting `w:t` nodes covers
body prose and table cells, which is what a support agent reads.

## Controls

1. **No DTD, no entity declarations.** XML is parsed through `defusedxml` with
   `forbid_dtd=True`, so the parser itself refuses both. A missing `defusedxml`
   is a refusal, never a fall back to the stdlib parser — degrading silently
   would switch the control off in exactly the deployment that forgot to
   install the extra.
2. **ZIP ratio and absolute cap.** The sum of `ZipInfo.file_size` and each
   member's expansion factor are checked from the central directory before any
   member is decompressed. Members are read by exact name; nothing is ever
   extracted to disk, so path traversal is not reachable.
3. **Worker thread.** Extraction runs under `anyio.to_thread.run_sync`, so a
   large document cannot stall the event loop for other users.
4. **Wall-clock budget.** A hard timeout per extraction.

A control that trips yields `extraction.status = "failed"` with a reason. No
control failure is silent.

### What control 4 does and does not do

`anyio.to_thread.run_sync` is **not cancellable** unless `abandon_on_cancel=True`
is passed. Without it the deadline passes, the await keeps waiting for the
thread, the call eventually succeeds, and the timeout never fires — measured, a
2 s parse sailed through a 0.2 s budget without raising. With the flag the
timeout is real, but the abandoned thread runs to completion in the background.

The budget therefore bounds the **request**, not the CPU. What bounds the CPU is
control 2, which is why the size and ratio caps are the primary defence and the
timeout is the backstop.

## Consequences

- Roughly 50 lines of our own DOCX code to maintain.
- Text in text boxes, SmartArt and footnotes may be missed. An extractor that
  finds nothing reports `partial`, not an empty success, so the gap is visible
  rather than silent.
- `.doc`, the pre-2007 binary format, is not covered and returns as a blob.
- **Residual risk, stated rather than hidden:** control 1 covers the XML *we*
  parse. `openpyxl` does its own parsing with plain `ElementTree`, so an XLSX
  carrying an internal entity bomb is bounded by controls 2–4 rather than
  refused outright. Accepted: hand-rolling XLSX extraction to close it would
  trade a narrow denial-of-service window for a much wider correctness surface
  — shared strings, inline strings and cached formula values.
- This is **not** virus scanning, and the executable denylist on the write path
  is not either: a `.zip` containing an `.exe` passes both. Both are tripwires
  against the obvious accident and must never be described as more.

## Related

- Design: `docs/superpowers/specs/2026-08-12-ticket-attachment-support-design.md`
- Plan: `docs/superpowers/plans/2026-08-12-ticket-attachment-support.md`
