"""A ``.env`` that still sets the deleted ChromaDB variables starts unchanged (#1782).

``CHROMADB_HOST``, ``CHROMADB_PORT`` and ``CHROMADB_CONNECT_TIMEOUT`` were read
only by the KB ingester, and went with it. A deployment's ``.env`` can still
hold them. ``get_settings`` loads that file into the environment
(``load_settings_dotenv``) and ``FaultMavenSettings`` reads it again as its
dotenv source, where a key that names no field is an error unless the model
ignores extras. So the file is read here the way a deployment reads it: a fresh
interpreter in a working directory holding the ``.env``, because
``tests/conftest.py`` patches dotenv reading away in-process.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.unit

ROOT = Path(faultmaven.__file__).resolve().parent.parent
DELETED = ("CHROMADB_HOST", "CHROMADB_PORT", "CHROMADB_CONNECT_TIMEOUT")

_CHILD = """
import json, os, sys

import faultmaven
assert faultmaven.__file__.startswith(sys.argv[1]), (faultmaven.__file__, sys.argv[1])
from faultmaven.config.settings import DatabaseSettings, get_settings

settings = get_settings()
print("RESULT " + json.dumps({
    "environ": {name: os.environ.get(name) for name in sys.argv[2:]},
    "fields": sorted(
        {name.lower() for name in sys.argv[2:]} & set(DatabaseSettings.model_fields)
    ),
    "kb_dir": settings.database.chromadb_kb_persist_dir,
}))
"""


def test_a_dotenv_holding_the_deleted_variables_builds_the_settings(tmp_path):
    """The settings build, hold no such fields, and read the rest of the file.

    ``CHROMADB_KB_PERSIST_DIR`` is the control: the file was read, and its
    other settings took effect.
    """
    work = tmp_path / "cwd"
    work.mkdir()
    (work / ".env").write_text(
        "CHROMADB_HOST=chroma.example\n"
        "CHROMADB_PORT=1\n"
        "CHROMADB_CONNECT_TIMEOUT=3\n"
        "CHROMADB_KB_PERSIST_DIR=/srv/kb-from-dotenv\n"
        "DATABASE_URL=sqlite+aiosqlite:///:memory:\n"
    )
    script = tmp_path / "child.py"
    script.write_text(_CHILD)

    result = subprocess.run(
        [sys.executable, str(script), str(ROOT), *DELETED],
        cwd=work,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stderr[-3000:]
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, result.stdout[-2000:]
    answer = json.loads(lines[-1].removeprefix("RESULT "))
    assert answer["environ"] == {
        "CHROMADB_HOST": "chroma.example",
        "CHROMADB_PORT": "1",
        "CHROMADB_CONNECT_TIMEOUT": "3",
    }
    assert answer["fields"] == []
    assert answer["kb_dir"] == "/srv/kb-from-dotenv"
