"""The one collection shape, and the three-valued `has_more` behind it.

Zammad speaks four collection dialects; `envelope` is the single place they are
flattened into one. The interesting behaviour is not the wrapping — it is
`has_more`, which must distinguish "proven last page" from "cannot tell". A
wrong `False` there makes a model stop paging and report a partial result as
complete, which no downstream test would catch.
"""

from __future__ import annotations

import pytest

from zammad.projection import envelope, unwrap


def test_every_key_is_always_present() -> None:
    """A key that vanishes cannot be discovered.

    A model that has only ever seen the paginated shape has no reason to test
    for the other one, so the response must not shed keys when a value is
    unknown — it reports None and keeps the shape.
    """
    result = envelope([])
    assert set(result) == {"items", "returned", "total_count", "page", "per_page", "has_more"}
    assert result["page"] is None
    # An unpaginated call holds everything there is, so the total is known —
    # reporting None would understate what we actually have and send the
    # caller looking for a second page that does not exist.
    assert result["total_count"] == 0


def test_returned_counts_what_actually_shipped() -> None:
    result = envelope([{"id": 1}, {"id": 2}], page=1, per_page=25, total_count=900)
    assert result["returned"] == 2
    assert result["total_count"] == 900


# ── has_more: prove it, or admit you cannot ──────────────────────────────────


def test_a_short_page_proves_it_is_the_last_one() -> None:
    """Two records against a page size of 25 needs no total to be conclusive."""
    assert envelope([{"id": 1}] * 2, page=1, per_page=25)["has_more"] is False


def test_a_known_total_proves_there_is_another_page() -> None:
    assert envelope([{"id": 1}] * 25, page=1, per_page=25, total_count=900)["has_more"] is True


def test_a_known_total_also_proves_the_end() -> None:
    """Page 4 of 100 records at 25 a page: exactly consumed, nothing left."""
    assert envelope([{"id": 1}] * 25, page=4, per_page=25, total_count=100)["has_more"] is False


def test_a_full_page_without_a_total_is_admitted_as_unknown() -> None:
    """The whole reason this is three-valued.

    Index actions ignore with_total_count, so a full page carries no evidence
    either way. Guessing False is the expensive direction — the model stops
    paging and calls a partial answer complete.
    """
    assert envelope([{"id": 1}] * 25, page=1, per_page=25)["has_more"] is None


def test_an_unpaginated_endpoint_is_complete_by_construction() -> None:
    """/tag_list and /object_manager_attributes ignore page and per_page and
    return the full set, so there is nothing further to fetch."""
    assert envelope([{"id": 1}] * 59)["has_more"] is False


def test_extra_keys_ride_along() -> None:
    """Tools with something genuinely their own — an article's ordering, an
    overview's name — keep it without inventing a competing wrapper."""
    result = envelope([], order="newest first")
    assert result["order"] == "newest first"
    assert result["items"] == []


# ── unwrap: Zammad's five spellings for "the array" ──────────────────────────


@pytest.mark.parametrize("key", ["records", "tickets", "users", "organizations", "assets"])
def test_unwrap_finds_the_records_under_any_of_zammads_names(key: str) -> None:
    items, total = unwrap({key: [{"id": 1}], "total_count": 7})
    assert items == [{"id": 1}]
    assert total == 7


def test_unwrap_handles_the_bare_array_index_actions_send() -> None:
    items, total = unwrap([{"id": 1}, {"id": 2}])
    assert items == [{"id": 1}, {"id": 2}]
    assert total is None, "an index action cannot report a total"


def test_unwrap_survives_an_unexpected_payload() -> None:
    """Zammad returns an error object rather than a list often enough that a
    KeyError here would surface as a confusing tool failure."""
    assert unwrap({"error": "nope"}) == ([], None)
    assert unwrap(None) == ([], None)


def test_unwrap_ignores_a_non_integer_total() -> None:
    _, total = unwrap({"records": [], "total_count": "many"})
    assert total is None


def test_the_last_page_makes_the_total_arithmetic() -> None:
    """Index actions cannot report a total, but reaching the end reveals it:
    every earlier page was full, and this one holds the remainder. This is the
    only way a caller of list_tickets ever learns how many there were."""
    result = envelope([{"id": 1}] * 7, page=3, per_page=25)
    assert result["has_more"] is False
    assert result["total_count"] == 57, "2 full pages of 25, plus the 7 here"


def test_a_known_total_is_never_overwritten_by_the_arithmetic() -> None:
    """Search actions report the real total; the estimate must not clobber it."""
    result = envelope([{"id": 1}] * 7, page=3, per_page=25, total_count=900)
    assert result["total_count"] == 900
