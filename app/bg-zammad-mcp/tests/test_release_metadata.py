"""The version must say the same thing in every file that carries it.

Releases are cut by semantic-release, which writes CHANGELOG.md and — via a
`prepareCmd` in .github/config/release/semantic-release.json — stamps the same
number into pyproject.toml and the Dockerfile's OCI label. A shared-workflow
change on 2026-08-31 started dropping that prepareCmd, so four releases in a row
moved CHANGELOG.md alone: the tree, the package metadata and every published
image kept claiming 6.0.0 while the tag said 6.1.2. Nothing failed. The release
job was green each time, because a release that bumps nothing is not an error to
git — it simply has less to commit.

Comparing pyproject.toml against the Dockerfile alone would not have caught it:
those two were wrong *together*, and agreeing with each other is exactly what
they did. CHANGELOG.md is the third source, written by the half of the pipeline
that kept working, so the disagreement only shows up against it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
PYPROJECT = APP / "pyproject.toml"
DOCKERFILE = APP / "Dockerfile"


def _find_changelog() -> Path | None:
    """Walk up for CHANGELOG.md; it is not copied into the image's test stage."""
    for base in (APP, *APP.parents):
        candidate = base / "CHANGELOG.md"
        if candidate.is_file():
            return candidate
    return None


CHANGELOG = _find_changelog()


def _released_version() -> str:
    """The newest version heading semantic-release wrote, e.g. `## [6.1.2](…)`."""
    assert CHANGELOG is not None
    for line in CHANGELOG.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,3} \[?(\d+\.\d+\.\d+)\]?", line)
        if match:
            return match.group(1)
    raise AssertionError("no version heading found in CHANGELOG.md")


@pytest.fixture(scope="module")
def released() -> str:
    if CHANGELOG is None:
        # The image's test stage copies pyproject.toml but neither CHANGELOG.md
        # nor the Dockerfile, so this cannot run from inside the build. It still
        # gates every push: CI and any local run work from a full checkout.
        pytest.skip("CHANGELOG.md not present (running inside the image build)")
    return _released_version()


def test_pyproject_version_matches_the_newest_release(released: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    assert match, "pyproject.toml declares no version"
    assert match.group(1) == released, (
        f"pyproject.toml says {match.group(1)}, CHANGELOG.md says {released}. "
        "The release pipeline's prepareCmd did not run — check the shared "
        "semantic-release action before releasing again."
    )


def test_the_image_label_matches_the_newest_release(released: str) -> None:
    if not DOCKERFILE.is_file():
        pytest.skip("Dockerfile not present (running inside the image build)")
    text = DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r'^LABEL org\.opencontainers\.image\.version="([^"]+)"', text, re.M)
    assert match, "the Dockerfile declares no org.opencontainers.image.version"
    assert match.group(1) == released, (
        f"the image label says {match.group(1)}, CHANGELOG.md says {released}. "
        "Every image published from this tree would carry the wrong version."
    )
