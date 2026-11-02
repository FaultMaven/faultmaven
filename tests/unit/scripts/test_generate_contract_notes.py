"""``contract-notes.json``: what the generator builds, and that ``--check`` holds it.

The clients read the changelog and each published value's meaning from this
file instead of parsing ``contract_version.py`` (#2411), so a stale copy is as
wrong as a stale ``openapi.json``. The generator empties the process
environment when it is imported, so it runs in a child process.
"""

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from faultmaven.api.contract_version import API_CONTRACT_VERSION, CONTRACT_CHANGES

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"

_NOTES = """
import json, sys
sys.path.insert(0, sys.argv[1])
import generate_api_docs
print(generate_api_docs.render_notes(json.loads(sys.argv[2])), end="")
"""

# Points the generator's paths at a scratch root, writes every artifact
# render_outputs names for the fixture spec, optionally makes the notes stale,
# and runs main() --check as CI does, without building the app.
_CHECK = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import generate_api_docs as g
root, spec, stale = Path(sys.argv[2]), json.loads(sys.argv[3]), sys.argv[4] == "1"
g.PROJECT_ROOT = root
g.DOCS_DIR = root / "docs" / "reference" / "api"
g.SPEC_PATH = g.DOCS_DIR / "openapi.json"
g.MARKDOWN_PATH = g.DOCS_DIR / "README.md"
g.NOTES_PATH = g.DOCS_DIR / "contract-notes.json"
g.build_spec = lambda: spec
g.DOCS_DIR.mkdir(parents=True)
for path, content in g.render_outputs(spec).items():
    path.write_text(content)
if stale:
    g.NOTES_PATH.write_text("{}\\n")
sys.argv = ["generate_api_docs.py", "--check"]
sys.exit(g.main())
"""


def _spec(schemas: dict, version: str = API_CONTRACT_VERSION) -> dict:
    return {
        "info": {"title": "t", "version": version},
        "paths": {},
        "components": {"schemas": schemas},
    }


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script, str(SCRIPTS), *args],
        capture_output=True,
        text=True,
    )


_CLOSURE = {
    "type": "string",
    # Not alphabetical, so a sort anywhere in the pipeline would show.
    "enum": ["inquiry_only", "closed_false_alarm"],
    "x-enum-descriptions": ["Closed during inquiry.", "Not present."],
}


def test_notes_carry_every_change_and_only_the_meant_vocabularies():
    done = _run(
        _NOTES,
        json.dumps(
            _spec(
                {
                    "ClosureReason": _CLOSURE,
                    "Severity": {"type": "string", "enum": ["low", "high"]},
                    "Case": {"type": "object", "properties": {}},
                }
            )
        ),
    )
    assert done.returncode == 0, done.stderr
    notes = json.loads(done.stdout)

    assert notes["contractVersion"] == API_CONTRACT_VERSION
    assert notes["entries"] == [dataclasses.asdict(c) for c in CONTRACT_CHANGES]
    assert notes["vocabularies"] == {
        "ClosureReason": [
            {"value": "inquiry_only", "meaning": "Closed during inquiry."},
            {"value": "closed_false_alarm", "meaning": "Not present."},
        ]
    }


@pytest.mark.parametrize(
    "meanings", [["Closed during inquiry."], []], ids=["one-short", "empty-list"]
)
def test_an_enum_whose_meanings_do_not_pair_with_its_values_fails(meanings):
    # An empty list is published meanings that pair with nothing, not an
    # absent key: it is refused, where an enum without the key is left out.
    lopsided = dict(_CLOSURE, **{"x-enum-descriptions": meanings})
    done = _run(_NOTES, json.dumps(_spec({"ClosureReason": lopsided})))
    assert done.returncode != 0
    assert f"ClosureReason publishes 2 enum values but {len(meanings)}" in done.stderr


def test_a_spec_version_without_its_contract_change_fails():
    done = _run(_NOTES, json.dumps(_spec({}, version="999.0.0")))
    assert done.returncode != 0
    assert "spec publishes 999.0.0" in done.stderr


@pytest.mark.parametrize("stale", [False, True], ids=["current", "notes-stale"])
def test_check_fails_on_stale_notes_alone(tmp_path, stale):
    spec = json.dumps(_spec({"ClosureReason": _CLOSURE}))
    done = _run(_CHECK, str(tmp_path), spec, "1" if stale else "0")

    if not stale:
        assert done.returncode == 0, done.stdout + done.stderr
        return
    assert done.returncode == 1, done.stdout + done.stderr
    assert "docs/reference/api/contract-notes.json does not match" in done.stdout
    assert "openapi.json does not match" not in done.stdout
    assert "README.md does not match" not in done.stdout
    git_add = next(line for line in done.stdout.splitlines() if "git add" in line)
    assert "docs/reference/api/contract-notes.json" in git_add
    assert (
        "docs/reference/api/contract-notes.json is built from CONTRACT_CHANGES in "
        "faultmaven/api/contract_version.py and the spec's enum meanings"
    ) in done.stdout
