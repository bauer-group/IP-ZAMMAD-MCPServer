"""The image must agree with itself about which port it serves.

Written after breaking exactly this: EXPOSE and the HEALTHCHECK were moved to
8080 while the application still took the library default of 8000, so a
`docker run` with no environment came up serving perfectly well and reported
itself unhealthy. Compose files always pass MCP_PORT explicitly and therefore
hid the mismatch entirely.

These are cheap string checks rather than a container build on purpose — the
build runs in CI, but a broken port default should fail in the unit suite,
seconds after it is introduced.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    if not DOCKERFILE.is_file():
        # The image's own test stage copies src/, tests/ and extensions/ but not
        # the Dockerfile, so these checks cannot run from inside the build they
        # are checking. They still gate every push: the Tests workflow and any
        # local run execute against a full checkout, where the file is present.
        pytest.skip("Dockerfile not present (running inside the image build)")
    return DOCKERFILE.read_text(encoding="utf-8")


def _env_mcp_port(text: str) -> int:
    match = re.search(r"^\s*MCP_PORT=(\d+)", text, re.M)
    assert match, "the image must pin MCP_PORT so it does not depend on the library default"
    return int(match.group(1))


def _expose(text: str) -> int:
    match = re.search(r"^EXPOSE\s+(\d+)", text, re.M)
    assert match, "Dockerfile has no EXPOSE"
    return int(match.group(1))


def _healthcheck_port(text: str) -> int:
    match = re.search(r"localhost:\$\{MCP_PORT:-(\d+)\}/healthz", text)
    assert match, "the healthcheck must probe ${MCP_PORT:-<default>}/healthz"
    return int(match.group(1))


def test_the_image_serves_and_probes_the_same_port(dockerfile: str) -> None:
    """The failure this file exists for.

    With ENV MCP_PORT unset, the server binds the library default while the
    healthcheck probes the Dockerfile default. Nothing is broken except the
    container's own opinion of itself, which is the worst kind of broken: it
    restarts a working process on a schedule.
    """
    assert _env_mcp_port(dockerfile) == _healthcheck_port(dockerfile), (
        "ENV MCP_PORT and the HEALTHCHECK default disagree — `docker run` with "
        "no environment would report unhealthy while serving correctly"
    )


def test_expose_matches_what_the_server_binds(dockerfile: str) -> None:
    """EXPOSE is only metadata, but it is the metadata every reader and every
    `docker run -P` trusts."""
    assert _expose(dockerfile) == _env_mcp_port(dockerfile)


def test_the_container_port_avoids_nothing_on_the_host(dockerfile: str) -> None:
    """8080 is deliberate and safe INSIDE the container.

    Zammad's own nginx publishes 8080 on the host, which is why the development
    compose maps this to 8000 there. Inside the container there is a separate
    network namespace, so the conventional port costs nothing.
    """
    assert _env_mcp_port(dockerfile) == 8080, (
        "if this changes, update docker-compose.development.yml's port mapping "
        "and the note in .env.example that explains the 8080/8000 split"
    )
