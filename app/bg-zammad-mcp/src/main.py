"""bg-zammad-mcp — CLI entrypoint.

A thin, profile-driven bg-mcpcore server: the declarative profile
(``profiles/zammad.json``) plus the Zammad-specific seams in ``server.py``,
``auth/`` and ``zammad/tools/``. ``make_cli`` builds the Typer ``serve`` command,
applies the dual-stack socket patch, and assembles the FastMCP instance from the
profile + settings.

Container liveness: hit the unauthenticated ``/healthz`` route (served by the
framework) — there is no longer a ``health`` CLI subcommand.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

# Make `server`, `config`, `auth.*`, `zammad.*` importable when run from source
# (`python src/main.py`) as well as when installed (src is flattened to root).
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bg_mcpcore import load_profile, make_cli  # noqa: E402

from config import Settings  # noqa: E402

try:
    _VERSION = _pkg_version("bg-zammad-mcp")
except PackageNotFoundError:
    _VERSION = "0.0.0+local"

app = make_cli(
    load_profile(str(_SRC / "profiles" / "zammad.json")),
    settings_cls=Settings,
    version=_VERSION,
    static_dir=str(_SRC / "static"),
)

if __name__ == "__main__":
    app()
