"""Tests for the response-trimming layer.

The point of this layer is that an agent can hold a triage session without
running out of context. That makes two properties load-bearing and worth
pinning: the trimmed shape must keep the fields a model actually reasons about,
and truncation must never be silent — a model that cannot distinguish "all 20
messages" from "the last 20 of 400" will confidently summarise the wrong thing.
"""

from __future__ import annotations

from zammad.projection import (
    TICKET_FIELDS,
    collection,
    parse_fields,
    project,
    strip_html,
    trim_articles,
    truncate,
)

_FAT_TICKET = {
    "id": 42,
    "number": "10042",
    "title": "Printer on fire",
    "state": "open",
    "owner": "Aya Agent",
    "updated_at": "2026-08-01T10:00:00Z",
    # The long tail a mature Zammad instance accumulates.
    "preferences": {"channel_id": 3},
    "cost_centre": None,
    "escalation_response_at": None,
    "article_ids": list(range(200)),
    "create_article_type_id": 11,
}


# ── projection ───────────────────────────────────────────────────────────────


def test_projection_keeps_what_a_model_reasons_about() -> None:
    trimmed = project(_FAT_TICKET, TICKET_FIELDS)
    for kept in ("id", "number", "title", "state", "owner", "updated_at"):
        assert kept in trimmed
    for dropped in ("preferences", "article_ids", "create_article_type_id"):
        assert dropped not in trimmed


def test_projection_never_invents_absent_fields() -> None:
    """A projection must not turn a missing field into a null one - that would
    read to a model as "this ticket has no owner" rather than "not returned"."""
    trimmed = project({"id": 1}, TICKET_FIELDS)
    assert trimmed == {"id": 1}


def test_non_dict_records_pass_through_untouched() -> None:
    assert project("not a record", TICKET_FIELDS) == "not a record"


def test_full_returns_the_raw_payload() -> None:
    """The escape hatch skips the projection, not the envelope."""
    out = collection([_FAT_TICKET], TICKET_FIELDS, full=True)
    assert out["items"] == [_FAT_TICKET]


def test_explicit_field_whitelist_wins() -> None:
    picked = parse_fields("id, title ,")
    assert picked == ("id", "title")
    assert project(_FAT_TICKET, picked) == {"id": 42, "title": "Printer on fire"}


def test_empty_field_string_falls_back_to_the_default() -> None:
    assert parse_fields("") is None
    assert parse_fields(None) is None
    assert parse_fields(" , ") is None


# ── html and truncation ──────────────────────────────────────────────────────


def test_strip_html_keeps_paragraph_structure() -> None:
    """Block tags become newlines, because a wall of text and a readable thread
    summarise very differently."""
    html = "<p>Hello,</p><p>the printer is <b>still</b> broken.</p><br>Thanks"
    out = strip_html(html)
    assert "<" not in out
    assert "still" in out
    assert "\n" in out


def test_strip_html_decodes_the_common_entities() -> None:
    assert strip_html("A&nbsp;&amp;&nbsp;B &lt;tag&gt;") == "A & B <tag>"


def test_truncate_reports_whether_it_cut() -> None:
    text, cut = truncate("abcdefghij", 5)
    assert cut is True
    assert text.startswith("abcde")
    assert "[…]" in text

    text, cut = truncate("abc", 5)
    assert (text, cut) == ("abc", False)


def test_truncate_zero_disables_the_cap() -> None:
    long_body = "x" * 10_000
    assert truncate(long_body, 0) == (long_body, False)


# ── article trimming ─────────────────────────────────────────────────────────


def _articles(count: int, body: str = "hello") -> list[dict[str, object]]:
    return [
        {
            "id": i,
            "ticket_id": 42,
            "type": "email",
            "sender": "Customer",
            "internal": False,
            "body": body,
            "content_type": "text/plain",
            "created_at": f"2026-08-0{(i % 9) + 1}T10:00:00Z",
            "origin_by_id": 7,
            "preferences": {"noise": True},
        }
        for i in range(count)
    ]


def test_article_bodies_are_capped_and_flagged() -> None:
    out = trim_articles(_articles(1, body="y" * 500), max_body_chars=100)["items"]
    assert out[0]["body_truncated"] is True
    assert len(str(out[0]["body"])) < 200


def test_short_bodies_are_not_flagged() -> None:
    out = trim_articles(_articles(1, body="short"), max_body_chars=100)["items"]
    assert "body_truncated" not in out[0]
    assert out[0]["body"] == "short"


def test_html_bodies_are_flattened() -> None:
    articles = _articles(1, body="<p>Hi <b>there</b></p>")
    articles[0]["content_type"] = "text/html"
    out = trim_articles(articles, max_body_chars=1000)["items"]
    assert "<" not in str(out[0]["body"])


def test_dropping_articles_is_never_silent() -> None:
    """Zammad does not paginate this endpoint, so this cap is the only one there
    is. A model told "here are the articles" when it got 5 of 40 would
    summarise a fifth of a conversation as the whole of it."""
    out = trim_articles(_articles(40), max_body_chars=500, per_page=5)
    assert isinstance(out, dict)
    assert out["total_count"] == 40
    assert out["returned"] == 5
    assert len(out["items"]) == 5
    # The signal that something is missing. `note` used to carry it as prose;
    # has_more carries it as a value a caller can branch on.
    assert out["has_more"] is True


def test_the_shape_never_depends_on_how_many_articles_there_are() -> None:
    """A bare array for short threads and an object for long ones would make the
    response type depend on data the caller cannot see."""
    short = trim_articles(_articles(3), max_body_chars=500, per_page=10)
    long = trim_articles(_articles(40), max_body_chars=500, per_page=5)
    assert set(short) == set(long), "the keys must not depend on the thread length"
    assert set(short) >= {"items", "total_count", "returned", "order", "has_more"}
    # Same keys either way; only the VALUES differ, which is the whole point.
    assert short["has_more"] is False, "the thread fit, so there is nothing more"
    assert long["has_more"] is True


def test_ordering_is_always_stated_even_when_nothing_was_dropped() -> None:
    """The regression this file exists for: newest_first reversed the list but
    only said so when it ALSO truncated, so the recommended call on a short
    thread returned a reversed list with no signal at all."""
    out = trim_articles(_articles(3), max_body_chars=500, per_page=10, newest_first=True)
    assert out["order"] == "newest first"
    assert [a["id"] for a in out["items"]] == [2, 1, 0]


def test_newest_first_reverses_and_says_so() -> None:
    out = trim_articles(_articles(10), max_body_chars=500, per_page=3, newest_first=True)
    assert out["order"] == "newest first"
    assert [a["id"] for a in out["items"]] == [9, 8, 7]


def test_article_noise_is_dropped(  ) -> None:
    out = trim_articles(_articles(1), max_body_chars=500)["items"]
    assert "preferences" not in out[0]
    assert "origin_by_id" not in out[0]
    # ...but everything needed to judge who said what to whom survives.
    for kept in ("id", "type", "sender", "internal", "created_at", "body"):
        assert kept in out[0]


def test_full_skips_article_trimming_entirely() -> None:
    """`full` is an escape hatch for missing fields, not for a second response
    shape — the records come back untouched, still inside the one envelope."""
    raw = _articles(3)
    out = trim_articles(raw, max_body_chars=1, full=True)
    assert out["items"] == raw
    assert out["returned"] == 3
