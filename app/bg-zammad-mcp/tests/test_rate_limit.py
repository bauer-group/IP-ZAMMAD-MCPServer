"""
Rate-limit client-ID resolver tests.

The resolver is split out as a pure function so we can unit-test the
trust model (XFF parsing + proxy-hops) without standing up FastMCP.
"""

from __future__ import annotations

import pytest

from rate_limit import resolve_client_id


def test_authenticated_caller_uses_sub_regardless_of_ip():
    assert (
        resolve_client_id(
            auth_subject="user-123",
            xff_header="1.2.3.4",
            direct_remote_ip="5.6.7.8",
            trusted_proxy_hops=1,
        )
        == "sub:user-123"
    )


def test_anonymous_with_proxy_uses_xff_rightmost():
    """1 trusted hop -> last value in XFF is what the trusted proxy saw."""
    assert (
        resolve_client_id(
            auth_subject=None,
            xff_header="9.9.9.9, 10.0.0.1, 172.16.0.5",
            direct_remote_ip="proxy-ip",
            trusted_proxy_hops=1,
        )
        == "ip:172.16.0.5"
    )


def test_anonymous_with_proxy_two_hops():
    assert (
        resolve_client_id(
            auth_subject=None,
            xff_header="spoofed, 10.0.0.1, 172.16.0.5",
            direct_remote_ip="proxy-ip",
            trusted_proxy_hops=2,
        )
        == "ip:10.0.0.1"
    )


def test_anonymous_no_proxy_falls_back_to_direct_ip():
    assert (
        resolve_client_id(
            auth_subject=None,
            xff_header="1.2.3.4",  # ignored when trusted_proxy_hops=0
            direct_remote_ip="5.6.7.8",
            trusted_proxy_hops=0,
        )
        == "ip:5.6.7.8"
    )


def test_xff_shorter_than_configured_hops_picks_leftmost_observed():
    """If we expect 3 hops but XFF only has 2, we still get the leftmost we saw."""
    assert (
        resolve_client_id(
            auth_subject=None,
            xff_header="10.0.0.1, 172.16.0.5",
            direct_remote_ip="proxy",
            trusted_proxy_hops=3,
        )
        == "ip:10.0.0.1"
    )


def test_empty_xff_uses_direct_ip():
    assert (
        resolve_client_id(
            auth_subject=None,
            xff_header="",
            direct_remote_ip="5.6.7.8",
            trusted_proxy_hops=1,
        )
        == "ip:5.6.7.8"
    )


def test_no_identity_at_all_returns_anon_sentinel():
    assert (
        resolve_client_id(
            auth_subject=None,
            xff_header=None,
            direct_remote_ip=None,
            trusted_proxy_hops=1,
        )
        == "ip:unknown"
    )


@pytest.mark.parametrize(
    "xff,expected",
    [
        ("1.2.3.4", "ip:1.2.3.4"),
        ("  1.2.3.4  ", "ip:1.2.3.4"),
        ("1.2.3.4,  10.0.0.1", "ip:10.0.0.1"),
    ],
)
def test_xff_whitespace_tolerance(xff, expected):
    assert (
        resolve_client_id(
            auth_subject=None,
            xff_header=xff,
            direct_remote_ip="proxy",
            trusted_proxy_hops=1,
        )
        == expected
    )
