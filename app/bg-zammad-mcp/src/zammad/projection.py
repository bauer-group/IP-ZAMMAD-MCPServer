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


def project_many(payload: Any, keep: tuple[str, ...], *, full: bool = False) -> Any:
    """Project a Zammad list response, whatever shape it arrived in.

    Zammad returns a bare array normally, and ``{"records": [...],
    "total_count": n}`` (or ``{"tickets": ...}``) once with_total_count is set,
    so the wrapper has to be preserved rather than assumed away — the total
    count is precisely the signal that tells a model its result was truncated.
    """
    if full:
        return payload
    if isinstance(payload, list):
        return [project(item, keep) for item in payload]
    if isinstance(payload, dict):
        out = dict(payload)
        for key in ("records", "tickets", "users", "organizations", "assets"):
            value = out.get(key)
            if isinstance(value, list):
                out[key] = [project(item, keep) for item in value]
        return out
    return payload


def trim_articles(
    payload: Any,
    *,
    max_body_chars: int,
    limit: int | None = None,
    newest_first: bool = False,
    full: bool = False,
) -> Any:
    """Bound an article list: fewest fields, shortest bodies, newest first.

    Zammad's ``index_by_ticket`` has no pagination, so this is the only place a
    ceiling can be applied at all. When articles are dropped the result says so
    explicitly, and says which end was kept, because "the last 20 messages" and
    "all 20 messages" lead to very different answers.
    """
    if full or not isinstance(payload, list):
        return payload

    articles = list(payload)
    total = len(articles)
    if newest_first:
        articles.reverse()
    truncated_list = limit is not None and total > limit
    if limit is not None:
        articles = articles[:limit]

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

    # The wrapper is UNCONDITIONAL. Two reasons, both learned the hard way:
    #
    # * `order` used to be emitted only when something was dropped, so the
    #   recommended call — newest_first=True with a limit the thread does not
    #   exceed — returned a reversed bare list with no ordering signal at all.
    #   A model reads articles[0] as the opening message when it is the latest,
    #   and reports that confidently. Silent, and wrong in the worst place.
    # * Flipping between a bare array and an object based on how many articles
    #   a ticket happens to have makes the response shape depend on data the
    #   caller cannot see. Four extra keys are cheaper than a broken chain.
    result: dict[str, Any] = {
        "articles": out,
        "total_count": total,
        "returned": len(out),
        "order": "newest first" if newest_first else "oldest first",
    }
    if truncated_list:
        result["note"] = (
            f"Only {len(out)} of {total} articles are included. Raise limit, "
            "flip newest_first, or read specific articles with get_ticket_article."
        )
    return result


__all__ = [
    "ORGANIZATION_FIELDS",
    "TICKET_FIELDS",
    "USER_FIELDS",
    "parse_fields",
    "project",
    "project_many",
    "strip_html",
    "trim_articles",
    "truncate",
]
