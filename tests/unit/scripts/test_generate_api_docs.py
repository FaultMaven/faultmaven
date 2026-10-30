"""The API reference README renders an enum's meanings beside its values.

``x-enum-descriptions`` is contract text (contract 14.9.0), so the human half
of the reference has to show it, not only the values. The generator empties
the process environment when it is imported, so it is run in a child process.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"

_RENDER = """
import json, sys
sys.path.insert(0, sys.argv[1])
import generate_api_docs
print(generate_api_docs.render_markdown(json.loads(sys.argv[2])))
"""


def _render(schemas: dict) -> str:
    spec = {"info": {"title": "t", "version": "1.0.0"}, "paths": {}}
    spec["components"] = {"schemas": schemas}
    done = subprocess.run(
        [sys.executable, "-c", _RENDER, str(SCRIPTS), json.dumps(spec)],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_each_value_is_rendered_with_its_meaning():
    readme = _render(
        {
            "ClosureReason": {
                "type": "string",
                "enum": ["inquiry_only", "closed_false_alarm"],
                "x-enum-descriptions": ["Closed during inquiry.", "Not present."],
            }
        }
    )
    assert "- `inquiry_only` — Closed during inquiry.\n" in readme
    assert "- `closed_false_alarm` — Not present.\n" in readme


def test_an_enum_without_meanings_keeps_its_values_line():
    readme = _render({"Severity": {"type": "string", "enum": ["low", "high"]}})
    assert "**Values:** `low`, `high`\n" in readme
