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
    parse_fields,
    project,
    project_many,
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


def test_project_many_handles_a_bare_array() -> None:
    out = project_many([_FAT_TICKET, _FAT_TICKET], TICKET_FIELDS)
    assert isinstance(out, list)
    assert all("preferences" not in item for item in out)


def test_project_many_preserves_the_total_count_wrapper() -> None:
    """with_total_count changes the response from an array to an object, and the
    count is exactly the signal that tells a model its result was truncated -
    projecting it away would defeat the pagination fix."""
    payload = {"records": [_FAT_TICKET], "total_count": 4000}
    out = project_many(payload, TICKET_FIELDS)
    assert out["total_count"] == 4000
    assert "preferences" not in out["records"][0]


def test_full_returns_the_raw_payload() -> None:
    payload = [_FAT_TICKET]
    assert project_many(payload, TICKET_FIELDS, full=True) is payload


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
    out = trim_articles(_articles(1, body="y" * 500), max_body_chars=100)["articles"]
    assert out[0]["body_truncated"] is True
    assert len(str(out[0]["body"])) < 200


def test_short_bodies_are_not_flagged() -> None:
    out = trim_articles(_articles(1, body="short"), max_body_chars=100)["articles"]
    assert "body_truncated" not in out[0]
    assert out[0]["body"] == "short"


def test_html_bodies_are_flattened() -> None:
    articles = _articles(1, body="<p>Hi <b>there</b></p>")
    articles[0]["content_type"] = "text/html"
    out = trim_articles(articles, max_body_chars=1000)["articles"]
    assert "<" not in str(out[0]["body"])


def test_dropping_articles_is_never_silent() -> None:
    """Zammad does not paginate this endpoint, so this cap is the only one there
    is. A model told "here are the articles" when it got 5 of 40 would
    summarise a fifth of a conversation as the whole of it."""
    out = trim_articles(_articles(40), max_body_chars=500, limit=5)
    assert isinstance(out, dict)
    assert out["total_count"] == 40
    assert out["returned"] == 5
    assert len(out["articles"]) == 5
    assert "40" in out["note"]


def test_the_shape_never_depends_on_how_many_articles_there_are() -> None:
    """A bare array for short threads and an object for long ones would make the
    response type depend on data the caller cannot see."""
    short = trim_articles(_articles(3), max_body_chars=500, limit=10)
    long = trim_articles(_articles(40), max_body_chars=500, limit=5)
    assert set(short) >= {"articles", "total_count", "returned", "order"}
    assert set(long) >= {"articles", "total_count", "returned", "order"}
    assert "note" not in short, "nothing was dropped, so there is nothing to note"
    assert "note" in long


def test_ordering_is_always_stated_even_when_nothing_was_dropped() -> None:
    """The regression this file exists for: newest_first reversed the list but
    only said so when it ALSO truncated, so the recommended call on a short
    thread returned a reversed list with no signal at all."""
    out = trim_articles(_articles(3), max_body_chars=500, limit=10, newest_first=True)
    assert out["order"] == "newest first"
    assert [a["id"] for a in out["articles"]] == [2, 1, 0]


def test_newest_first_reverses_and_says_so() -> None:
    out = trim_articles(_articles(10), max_body_chars=500, limit=3, newest_first=True)
    assert out["order"] == "newest first"
    assert [a["id"] for a in out["articles"]] == [9, 8, 7]


def test_article_noise_is_dropped(  ) -> None:
    out = trim_articles(_articles(1), max_body_chars=500)["articles"]
    assert "preferences" not in out[0]
    assert "origin_by_id" not in out[0]
    # ...but everything needed to judge who said what to whom survives.
    for kept in ("id", "type", "sender", "internal", "created_at", "body"):
        assert kept in out[0]


def test_full_skips_article_trimming_entirely() -> None:
    raw = _articles(3)
    assert trim_articles(raw, max_body_chars=1, full=True) is raw
