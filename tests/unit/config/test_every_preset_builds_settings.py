"""Every preset builds the settings, with only the credentials its provider needs.

A preset writes its defaults into the environment the settings then read, so a
preset naming a value its field rejects fails every process started with it.
The ``enterprise`` preset named ``DEPLOYMENT_MODE=multi-tenant``, from before
ADR-004's two-value ``DeploymentMode``, and no ``CONFIG_PRESET=enterprise``
process could start (#1784).

Each case is a fresh interpreter with no environment of the parent's and an
empty working directory, isolated as ``test_get_settings_cold_process.py`` is,
so no ``.env`` and no CI profile variable can supply what the preset lacks. The
child reports the preset's keys as the settings process saw them, so a case
also fails when the preset was never applied. The suite's network guard is
installed in the child too: the ``local`` preset names a model server.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven
from faultmaven.config.presets import PRESETS, PresetName, preset_required_keys

pytestmark = pytest.mark.unit

ROOT = str(Path(faultmaven.__file__).resolve().parents[1])

_CHILD = """
import json, os, sys

root, keys = sys.argv[1], json.loads(sys.argv[2])
from tests import network_guard

network_guard.install()

from faultmaven.config.settings import get_settings
import faultmaven

assert faultmaven.__file__.startswith(root), (faultmaven.__file__, root)
settings = get_settings()
print("RESULT " + json.dumps({
    "environ": {key: os.environ.get(key) for key in keys},
    "mode": settings.deployment_mode,
    "is_cloud": settings.is_cloud,
    "network_refusals": network_guard.drain_refusals(),
}))
"""


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
        [sys.executable, str(script), ROOT, json.dumps(list(definition.defaults))],
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
    answer = json.loads(lines[-1].removeprefix("RESULT "))
    assert answer["network_refusals"] == []
    return answer


#: The deployment mode each preset builds. ``minimal`` is an alias of ``local``
#: (the same definition); a preset added without an entry here fails its case.
_MODE = {
    PresetName.LOCAL: "standalone",
    PresetName.STANDARD: "standalone",
    PresetName.ENTERPRISE: "cloud",
}


@pytest.mark.parametrize(
    "preset",
    [name for name in PresetName if name is not PresetName.MINIMAL],
    ids=lambda name: name.value,
)
def test_the_preset_builds_the_settings(tmp_path, preset):
    answer = _build_settings_under(tmp_path, preset)

    assert answer["environ"] == {
        key: str(value) for key, value in PRESETS[preset].defaults.items()
    }
    assert answer["mode"] == _MODE[preset]
    assert answer["is_cloud"] is (_MODE[preset] == "cloud")


def test_an_explicit_deployment_mode_beats_the_enterprise_preset(tmp_path):
    answer = _build_settings_under(
        tmp_path, PresetName.ENTERPRISE, DEPLOYMENT_MODE="standalone"
    )

    assert answer["mode"] == "standalone"
    assert answer["is_cloud"] is False
