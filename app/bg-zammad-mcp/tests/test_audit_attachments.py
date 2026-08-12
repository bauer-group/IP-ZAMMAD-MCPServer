"""What the audit log must say about a file leaving the helpdesk.

audit.py records identifiers and deliberately no content. Without an addition,
a call that mailed a document to a customer is logged identically to one that
did not - and "what did the agent send out" is exactly the question the trail
exists to answer.
"""

from __future__ import annotations

from audit import _identifiers as identifiers


def test_attachment_count_and_names_are_recorded() -> None:
    fields = identifiers(
        {
            "ticket_id": 4711,
            "body": "Anbei die Auswertung.",
            "attachments": [
                {"filename": "auswertung.csv", "text": "a;b"},
                {
                    "filename": "datenblatt.pdf",
                    "copy_from": {"ticket_id": 1, "article_id": 2, "attachment_id": 3},
                },
            ],
        }
    )
    assert fields["ticket_id"] == 4711
    assert fields["attachment_count"] == 2
    assert fields["attachment_filenames"] == ["auswertung.csv", "datenblatt.pdf"]


def test_attachment_contents_are_never_recorded() -> None:
    fields = identifiers(
        {"ticket_id": 1, "attachments": [{"filename": "a.csv", "text": "SECRET-PAYLOAD"}]}
    )
    serialised = repr(fields)
    assert "SECRET-PAYLOAD" not in serialised
    assert "body" not in fields


def test_base64_payloads_never_reach_the_log() -> None:
    fields = identifiers(
        {"ticket_id": 1, "attachments": [{"filename": "x.bin", "data_base64": "QUJDREVG"}]}
    )
    assert "QUJDREVG" not in repr(fields)
    assert fields["attachment_filenames"] == ["x.bin"]


def test_a_write_without_attachments_gains_no_attachment_fields() -> None:
    fields = identifiers({"ticket_id": 1, "body": "nur Text"})
    assert "attachment_count" not in fields
    assert "attachment_filenames" not in fields


def test_a_long_filename_list_is_truncated_but_the_count_stays_true() -> None:
    fields = identifiers(
        {"ticket_id": 1, "attachments": [{"filename": f"f{i}.txt"} for i in range(30)]}
    )
    assert fields["attachment_count"] == 30
    assert len(fields["attachment_filenames"]) == 10
    assert fields["attachment_filenames_truncated_from"] == 30


def test_an_entry_without_a_filename_still_counts() -> None:
    """A copy_from that inherits its name has no filename in the arguments."""
    fields = identifiers(
        {
            "ticket_id": 1,
            "attachments": [{"copy_from": {"ticket_id": 1, "article_id": 2, "attachment_id": 3}}],
        }
    )
    assert fields["attachment_count"] == 1
    assert fields["attachment_filenames"] == ["<unnamed>"]
