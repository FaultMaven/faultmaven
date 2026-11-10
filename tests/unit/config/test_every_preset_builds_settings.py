"""Every preset builds the settings, with only the credentials its provider needs.

A preset writes its defaults into the environment the settings then read, so a
preset naming a value its field rejects fails every process started with it.
The ``enterprise`` preset named ``DEPLOYMENT_MODE=multi-tenant``, from before
ADR-004's two-value ``DeploymentMode``, and no ``CONFIG_PRESET=enterprise``
process could start (#1784).

Each case is a fresh interpreter with no environment of the parent's and an
empty working directory, isolated as ``test_get_settings_cold_process.py`` is,
so no ``.env`` and no CI profile variable can supply what the preset lacks.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven
from faultmaven.config.presets import PRESETS, PresetName, preset_required_keys
from faultmaven.config.settings import DeploymentMode

pytestmark = pytest.mark.unit

ROOT = str(Path(faultmaven.__file__).resolve().parents[1])

_CHILD = """
import json, sys

root = sys.argv[1]
from faultmaven.config.settings import DeploymentMode, get_settings
import faultmaven

assert faultmaven.__file__.startswith(root), (faultmaven.__file__, root)
settings = get_settings()
# ``use_enum_values``: the field holds the member's value, not the member.
print("RESULT " + json.dumps({
    "mode": DeploymentMode(settings.deployment_mode).value,
    "is_cloud": settings.is_cloud,
}))
"""

#: ``minimal`` is an alias of ``local``: the same definition.
_DISTINCT_PRESETS = [name for name in PresetName if name is not PresetName.MINIMAL]


def _build_settings_under(tmp_path: Path, preset: PresetName, **extra: str) -> dict:
    definition = PRESETS[preset]
    credentials = {
        key: "test-credential"
        for key in preset_required_keys(
            definition, definition.defaults["CHAT_PROVIDER"]
        )
    }
    work = tmp_path / "cwd"
    work.mkdir()
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    result = subprocess.run(
        [sys.executable, str(script), ROOT],
        cwd=work,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "PYTHONPATH": ROOT,
            "CONFIG_PRESET": preset.value,
            **credentials,
            **extra,
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, result.stdout[-2000:]
    return json.loads(lines[-1].removeprefix("RESULT "))


@pytest.mark.parametrize("preset", _DISTINCT_PRESETS, ids=lambda p: p.value)
def test_the_preset_builds_the_settings(tmp_path, preset):
    mode = _build_settings_under(tmp_path, preset)["mode"]

    assert mode in {member.value for member in DeploymentMode}


def test_the_enterprise_preset_is_a_cloud_deployment(tmp_path):
    assert _build_settings_under(tmp_path, PresetName.ENTERPRISE)["is_cloud"] is True


def test_an_explicit_deployment_mode_beats_the_enterprise_preset(tmp_path):
    answer = _build_settings_under(
        tmp_path, PresetName.ENTERPRISE, DEPLOYMENT_MODE="standalone"
    )

    assert answer["mode"] == "standalone"
    assert answer["is_cloud"] is False
