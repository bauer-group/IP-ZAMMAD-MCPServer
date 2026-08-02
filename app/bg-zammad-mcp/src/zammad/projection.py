"""Response trimming — keeping Zammad's payloads inside a model's context window.

Zammad returns every column of every record, including the long tail of nulls
that a mature instance accumulates through the Object Manager. One
``list_tickets(per_page=100, expand=true)`` measured at roughly 118 KiB, and
``list_ticket_articles`` is genuinely unbounded: Zammad does not paginate it and
returns full HTML bodies, so a two-year e-mail thread arrives in one response.

An agent that spends its context on null custom fields cannot hold a triage
session. So list-shaped tools project down to the fields a model actually reasons
about, and article bodies are bounded and de-HTML-ed. Both are opt-out:

* ``full=True``          - the raw Zammad record, for when something is missing.
* ``fields="a,b,c"``     - an explicit whitelist, for when you know what you want.

Trimming is deliberately NOT applied to single-record reads (``get_ticket``,
``get_user``): one record is affordable, and that is where a model goes when the
projection dropped something it needs.
"""

from __future__ import annotations

import re
from typing import Any

# What an agent reasons about when scanning a list of tickets: who it is from,
# what state it is in, who owns it, and when it last moved. Everything else is
# one get_ticket away.
TICKET_FIELDS = (
    "id",
    "number",
    "title",
    "state",
    "state_id",
    "priority",
    "priority_id",
    "group",
    "group_id",
    "owner",
    "owner_id",
    "customer",
    "customer_id",
    "organization",
    "organization_id",
    "created_at",
    "updated_at",
    "article_count",
    "pending_time",
    "escalation_at",
)

USER_FIELDS = (
    "id",
    "login",
    "firstname",
    "lastname",
    "email",
    "phone",
    "organization",
    "organization_id",
    "roles",
    "active",
    "out_of_office",
    "updated_at",
)

ORGANIZATION_FIELDS = (
    "id",
    "name",
    "domain",
    "shared",
    "active",
    "note",
    "updated_at",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKLINE_RE = re.compile(r"\n{3,}")


def strip_html(text: str) -> str:
    """Flatten an HTML article body to readable plain text.

    Not a parser and not trying to be: Zammad article bodies are e-mail HTML,
    and the goal is to stop paying for markup, not to render it faithfully.
    Block-level tags become newlines so paragraph structure survives, which is
    what makes the difference between a readable thread and a wall of text.
    """
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    for entity, char in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
    ):
        text = text.replace(entity, char)
    text = _WS_RE.sub(" ", text)
    return _BLANKLINE_RE.sub("\n\n", text).strip()


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Cut a string to length, returning whether it was cut.

    The caller reports the flag rather than silently shortening, because a model
    that cannot tell a complete body from a truncated one will confidently
    summarise half a conversation.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip() + " […]", True


def parse_fields(fields: str | None) -> tuple[str, ...] | None:
    """Turn a user-supplied CSV whitelist into a field tuple (None = default)."""
    if not fields:
        return None
    picked = tuple(f.strip() for f in fields.split(",") if f.strip())
    return picked or None


def project(record: Any, keep: tuple[str, ...]) -> Any:
    """Keep only `keep` on a record, preserving anything that is not a dict."""
    if not isinstance(record, dict):
        return record
    return {key: record[key] for key in keep if key in record}


def envelope(
    items: list[Any],
    *,
    page: int | None = None,
    per_page: int | None = None,
    total_count: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Wrap a collection in the one shape every list-shaped tool returns.

    Zammad speaks four different collection dialects and we used to publish all
    of them: a bare array from index actions, ``{"records": …, "total_count": n}``
    from search actions, ``{"assets": …, "index": …}`` from overviews, and
    endpoints like ``/object_manager_attributes`` that ignore pagination and
    return everything. A model that pattern-matches from the last tool it used
    then reads ``result["records"]`` on a tool that returns a bare list.

    Every key here is ALWAYS present, because a key that vanishes cannot be
    discovered — a model that has only ever seen the paginated shape has no
    reason to test for the other one. ``None`` means "not knowable here", which
    is a different and much more useful statement than absence:

    * ``total_count`` - total matching records. ``None`` for an index-backed
      tool mid-listing: Zammad ignores ``with_total_count`` there and returns a
      bare array, so the total cannot be had until the last page reveals it.
    * ``page`` / ``per_page`` - ``None`` when the tool does not paginate at all
      (``/tag_list`` and ``/object_manager_attributes`` ignore both and return
      the full set; verified on 7.1.1).
    * ``has_more`` - deliberately three-valued, and the reason this helper
      exists. ``returned < per_page`` PROVES this is the last page;
      ``page * per_page < total_count`` PROVES there is another. Only
      ``returned == per_page`` with no total is actually unknown, and that is
      reported as ``None`` rather than guessed. Guessing ``False`` is the
      expensive direction: the model stops paging and reports a partial answer
      as complete.
    """
    returned = len(items)
    has_more: bool | None
    if per_page is None:
        has_more = False  # the endpoint returned everything it has
        # ...and if it returned everything, the total is not unknown — it is
        # what we are holding. Reporting None would understate what we actually
        # know and push the caller to look for a page that does not exist.
        if total_count is None:
            total_count = returned
    elif returned < per_page:
        has_more = False  # a short page is the last page
        # Being on the last page makes the total arithmetic rather than
        # unknown: every earlier page was full, and this one holds the
        # remainder. It is the only way a caller of an index-backed list_*
        # tool ever learns a total at all.
        if total_count is None:
            total_count = ((page or 1) - 1) * per_page + returned
    elif total_count is not None:
        has_more = (page or 1) * per_page < total_count
    else:
        has_more = None  # a full page with no total: genuinely unknown

    return {
        "items": items,
        "returned": returned,
        "total_count": total_count,
        "page": page,
        "per_page": per_page,
        "has_more": has_more,
        **extra,
    }


def unwrap(payload: Any) -> tuple[list[Any], int | None]:
    """Pull the records and the total out of whatever Zammad sent back.

    The five spellings are Zammad's, not ours: index actions send a bare array,
    search actions send ``records``, and various endpoints use ``tickets``,
    ``users``, ``organizations`` or ``assets``. Normalising here is what lets
    `envelope` present one shape outward.
    """
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        for key in ("records", "tickets", "users", "organizations", "assets"):
            value = payload.get(key)
            if isinstance(value, list):
                total = payload.get("total_count")
                return value, total if isinstance(total, int) else None
    return [], None


def collection(
    payload: Any,
    keep: tuple[str, ...] | None = None,
    *,
    page: int | None = None,
    per_page: int | None = None,
    full: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """Normalise a Zammad list response, project it, and wrap it — in one step.

    This is what every list-shaped tool returns. Keeping it in one function is
    the point: the code this replaced returned whatever wrapper Zammad happened
    to send, so the response shape was decided by the upstream endpoint and by
    whether ``with_total_count`` was passed, rather than by us.

    ``full=True`` skips the projection but NOT the envelope — the escape hatch
    is for missing fields, not for a second response shape. ``keep=None`` means
    the same thing permanently, for the handful of collections (groups, macros,
    notifications) whose records are already small enough to send whole.
    """
    items, total = unwrap(payload)
    if keep is not None and not full:
        items = [project(item, keep) for item in items]
    return envelope(items, page=page, per_page=per_page, total_count=total, **extra)


def trim_articles(
    payload: Any,
    *,
    max_body_chars: int,
    page: int = 1,
    per_page: int | None = None,
    newest_first: bool = False,
    full: bool = False,
) -> Any:
    """Bound an article list: fewest fields, shortest bodies, newest first.

    Zammad's ``index_by_ticket`` has no pagination at all — it returns the whole
    thread, full HTML bodies included — so paging happens here or nowhere. It
    used to happen as a bare ``limit``, which could only ever reach one end of
    the thread: the middle of a long conversation was unreachable without
    pulling all of it. Slicing by page instead costs nothing extra (the whole
    thread is already in hand) and makes this behave like every other paginated
    tool.
    """
    if not isinstance(payload, list):
        return payload

    articles = list(payload)
    total = len(articles)
    if newest_first:
        articles.reverse()
    if per_page is not None:
        start = (page - 1) * per_page
        articles = articles[start : start + per_page]

    keep = (
        "id",
        "ticket_id",
        "type",
        "sender",
        "from",
        "to",
        "cc",
        "subject",
        "internal",
        "created_at",
        "created_by",
        "attachments",
    )
    out: list[dict[str, Any]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        if full:
            out.append(article)
            continue
        trimmed = project(article, keep)
        body = article.get("body")
        if isinstance(body, str):
            if str(article.get("content_type", "")).lower() == "text/html":
                body = strip_html(body)
            body, was_cut = truncate(body, max_body_chars)
            trimmed["body"] = body
            if was_cut:
                trimmed["body_truncated"] = True
        out.append(trimmed)

    # `order` rides along in the shared envelope rather than replacing it. It
    # is not decoration: it used to be emitted only when articles were dropped,
    # so the recommended call — newest_first with a page the thread does not
    # fill — came back reversed with no ordering signal at all. A model reads
    # items[0] as the opening message when it is in fact the latest, and says
    # so confidently.
    return envelope(
        out,
        page=page,
        per_page=per_page,
        total_count=total,
        order="newest first" if newest_first else "oldest first",
    )


__all__ = [
    "ORGANIZATION_FIELDS",
    "TICKET_FIELDS",
    "USER_FIELDS",
    "collection",
    "envelope",
    "parse_fields",
    "project",
    "strip_html",
    "trim_articles",
    "truncate",
    "unwrap",
]
