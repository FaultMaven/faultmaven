"""A source build of the Dashboard refreshes its Alpine packages (#2349).

The Dashboard's Dockerfile keys its ``apk upgrade`` layer on the ``PKG_REFRESH``
build arg (faultmaven-dashboard#240); nothing else in it busts that layer's
cache. This repository's source build passes the arg:
``docker-compose.dashboard-build.yml`` maps ``FM_DASHBOARD_PKG_REFRESH`` to it,
and ``faultmaven.sh``'s ``compose_files_args``, which ``start`` and ``build``
both call, exports that variable whenever ``--build-dashboard`` is given.

The function is run as ``faultmaven.sh`` spells it, cut out of the script, and
the value is read back from a child process: ``docker compose`` sees only what
is exported.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
UNSET = "<unset>"


def test_the_dashboard_build_passes_pkg_refresh_from_the_variable():
    compose = yaml.safe_load(
        (REPO_ROOT / "docker-compose.dashboard-build.yml").read_text()
    )

    assert compose["services"]["dashboard"]["build"]["args"] == {
        "PKG_REFRESH": "${FM_DASHBOARD_PKG_REFRESH:-}"
    }


def _compose_files_args() -> str:
    text = (REPO_ROOT / "faultmaven.sh").read_text()
    match = re.search(r"^compose_files_args\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert match, "compose_files_args() is not in faultmaven.sh"
    return match.group(0)


def _run(*, build_dashboard: bool, preset: str | None = None) -> tuple[str, str]:
    """Returns (FM_DASHBOARD_PKG_REFRESH as a child process sees it, COMPOSE_FILES)."""
    script = "\n".join(
        [
            _compose_files_args(),
            "BUILD_MODE=false",
            f"BUILD_DASHBOARD={'true' if build_dashboard else 'false'}",
            "compose_files_args",
            f'bash --noprofile --norc -c \'printf "%s\\n" "${{FM_DASHBOARD_PKG_REFRESH-{UNSET}}}"\'',
            'printf "%s\\n" "${COMPOSE_FILES[*]}"',
        ]
    )
    env = {"PATH": "/usr/bin:/bin"}
    if preset is not None:
        env["FM_DASHBOARD_PKG_REFRESH"] = preset
    out = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    value, files = out.rstrip("\n").split("\n")
    return value, files


def test_build_dashboard_exports_the_utc_date():
    before = dt.datetime.now(dt.timezone.utc).date().isoformat()
    value, files = _run(build_dashboard=True)
    after = dt.datetime.now(dt.timezone.utc).date().isoformat()

    assert value in {before, after}
    assert "-f docker-compose.dashboard-build.yml" in files


def test_a_value_already_in_the_environment_wins():
    value, _ = _run(build_dashboard=True, preset="pinned-by-caller")

    assert value == "pinned-by-caller"


def test_without_build_dashboard_nothing_is_exported():
    value, files = _run(build_dashboard=False)

    assert value == UNSET
    assert "dashboard-build" not in files
