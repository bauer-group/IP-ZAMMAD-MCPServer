"""
Zammad version-probe tests.
"""

from __future__ import annotations

import pytest
import respx

from zammad.client import ZammadClient
from zammad.version_probe import probe_zammad_version


BASE = "https://zammad.example.com/api/v1"


@respx.mock
@pytest.mark.parametrize(
    "raw_version,expected_major",
    [
        ("6.4.0", 6),
        ("7.0.0", 7),
        ("7.0.0-snapshot", 7),
        ("6.5.0-1736.1738254", 6),
        ("9.10.11", 9),
    ],
)
async def test_probe_parses_major(raw_version, expected_major):
    respx.get(f"{BASE}/version").respond(200, json={"version": raw_version})
    client = ZammadClient(base_url=BASE, api_token="x")
    try:
        info = await probe_zammad_version(client)
    finally:
        await client.aclose()
    assert info.raw == raw_version
    assert info.major == expected_major
    assert info.major_label == f"v{expected_major}"


@respx.mock
async def test_probe_returns_unknown_on_404():
    respx.get(f"{BASE}/version").respond(404, json={"error": "not found"})
    client = ZammadClient(base_url=BASE, api_token="x")
    try:
        info = await probe_zammad_version(client)
    finally:
        await client.aclose()
    assert info.raw is None
    assert info.major is None
    assert info.major_label == "unknown"


@respx.mock
async def test_probe_returns_unknown_on_malformed_payload():
    respx.get(f"{BASE}/version").respond(200, json={"oops": "no version key"})
    client = ZammadClient(base_url=BASE, api_token="x")
    try:
        info = await probe_zammad_version(client)
    finally:
        await client.aclose()
    assert info.raw is None
    assert info.major is None
