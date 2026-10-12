"""No test process writes the checkout's ``./data`` (#1772, #1636).

Every path whose shipped default sits under the cwd-relative ``./data`` -- the
SQLite file, both ChromaDB trees, the evidence files, the redaction pseudonym
key, the local JWT secret, the runbook tree -- resolves, in a test session,
under a temporary root of the process's own (``tests/conftest.py``,
``TEST_DATA_ROOT_ENV``). Before that, one ``./data/chroma-kb`` served every
xdist worker of every run in a checkout, and an HNSW segment in it reached
66-72 GB in minutes, filling ``/``.

The settings fields are found by introspection, so a NEW field defaulting
under ``./data`` fails here until the conftest moves it too.
"""

from __future__ import annotations

import os
import pathlib
from unittest.mock import patch

import pytest
from pydantic_settings import BaseSettings

from tests.conftest import SHIPPED_DATA_DEFAULTS, TEST_DATA_ROOT_ENV

pytest_plugins = ["pytester"]

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

SETTINGS_MODULE = "faultmaven.config.settings"

#: The fields measured writing under ``./data`` before #1772. A positive
#: control: the introspection below must find at least these.
KNOWN_DATA_FIELDS = {
    ("DatabaseSettings", "database_url"),
    ("DatabaseSettings", "chromadb_kb_persist_dir"),
    ("DatabaseSettings", "chromadb_evidence_persist_dir"),
    ("EvidenceStorageSettings", "evidence_storage_root"),
    ("ProtectionSettings", "pseudonym_key_path"),
}


def _is_under_data(value: object) -> bool:
    if not isinstance(value, (str, pathlib.PurePath)):
        return False
    text = str(value)
    return "./data" in text or text.startswith("data/") or text == "data"


def _data_fields() -> list[tuple[type, str]]:
    """Every settings field whose SHIPPED default is under ``./data``.

    The shipped default is what the conftest recorded before moving it, or the
    field's default where the conftest moved nothing -- which is how a new
    field shows up here.
    """
    from faultmaven.config import settings as settings_module

    found = []
    for cls in vars(settings_module).values():
        if not (
            isinstance(cls, type)
            and issubclass(cls, BaseSettings)
            and cls.__module__ == SETTINGS_MODULE
        ):
            continue
        for name, field in cls.model_fields.items():
            shipped = SHIPPED_DATA_DEFAULTS.get(
                (SETTINGS_MODULE, cls.__name__, name), field.default
            )
            if _is_under_data(shipped):
                found.append((cls, name))
    return found


def _module_attributes() -> list[tuple[str, str]]:
    """The defaults with no settings field (read at call time)."""
    return [
        (module, attribute)
        for module, owner, attribute in SHIPPED_DATA_DEFAULTS
        if owner is None
    ]


def _assert_in_the_session_root(label: str, value: object) -> None:
    root = os.environ[TEST_DATA_ROOT_ENV]
    text = str(value)
    assert (root + os.sep) in text, f"{label} = {text!r} is not under {root}"
    assert str(REPO_ROOT) not in text, f"{label} = {text!r} is in the checkout"
    assert "./data" not in text and not text.startswith(
        "data"
    ), f"{label} = {text!r} is the cwd-relative ./data"


def test_the_session_root_is_outside_the_checkout_and_names_this_process():
    root = pathlib.Path(os.environ[TEST_DATA_ROOT_ENV])
    assert root.is_dir()
    assert not root.is_relative_to(REPO_ROOT)
    worker = os.environ.get("PYTEST_XDIST_WORKER") or "main"
    assert root.name.startswith(f"faultmaven-{worker}-")


def test_the_introspection_finds_every_known_data_field():
    """A positive control: an introspection that found nothing would pass the
    tests below vacuously."""
    found = {(cls.__name__, name) for cls, name in _data_fields()}
    assert KNOWN_DATA_FIELDS <= found
    assert {attribute for _module, attribute in _module_attributes()} == {
        "DEFAULT_JWT_SECRET_FILE",
        "KNOWLEDGE_ROOT",
    }
    for value in SHIPPED_DATA_DEFAULTS.values():
        assert _is_under_data(value), "the record holds the shipped ./data values"


def _assert_every_data_default_in_the_session_root() -> None:
    import importlib

    from faultmaven.config.settings import get_settings, reset_settings

    for cls, name in _data_fields():
        _assert_in_the_session_root(f"{cls.__name__}.{name}", getattr(cls(), name))
    for module, attribute in _module_attributes():
        value = getattr(importlib.import_module(module), attribute)
        _assert_in_the_session_root(f"{module}.{attribute}", value)

    reset_settings()
    try:
        settings = get_settings()
        for label, value in {
            "database.database_url": settings.database.database_url,
            "database.chromadb_kb_persist_dir": (
                settings.database.chromadb_kb_persist_dir
            ),
            "database.chromadb_evidence_persist_dir": (
                settings.database.chromadb_evidence_persist_dir
            ),
            "evidence_storage.evidence_storage_root": (
                settings.evidence_storage.evidence_storage_root
            ),
            "protection.pseudonym_key_path": settings.protection.pseudonym_key_path,
        }.items():
            _assert_in_the_session_root(label, value)
    finally:
        reset_settings()


def test_every_data_default_resolves_in_the_session_root():
    _assert_every_data_default_in_the_session_root()


@pytest.mark.parametrize(
    "environment",
    [{}, {"CHAT_PROVIDER": "openai", "REDIS_HOST": "localhost"}],
    ids=["emptied", "pinned"],
)
def test_an_emptied_environment_still_resolves_in_the_session_root(environment):
    """The way tests really empty it. The variables are gone; the field
    defaults must carry the per-process paths on their own (#1636)."""
    root = os.environ[TEST_DATA_ROOT_ENV]
    with patch.dict(os.environ, environment, clear=True):
        os.environ[TEST_DATA_ROOT_ENV] = root  # read by the helper, not the code
        _assert_every_data_default_in_the_session_root()


def test_shipped_data_defaults_restores_the_shipped_layout(
    tmp_path, monkeypatch, shipped_data_defaults
):
    from faultmaven.config.settings import DEFAULT_JWT_SECRET_FILE, get_settings
    from faultmaven.utils.runbook_id import knowledge_root

    monkeypatch.chdir(tmp_path)
    settings = get_settings()
    assert settings.database.chromadb_kb_persist_dir == "./data/chroma-kb"
    assert settings.database.database_url == (
        "sqlite+aiosqlite:///./data/faultmaven.db"
    )
    assert DEFAULT_JWT_SECRET_FILE == "data/.jwt_secret"
    assert knowledge_root() == pathlib.Path("data/knowledge")


_CHILD_CONFTEST = """
import tests.conftest  # noqa: F401  -- the real data root, per process
"""

_CHILD_TESTS = """
import os
import pathlib

import pytest


@pytest.mark.parametrize("n", range(4))
def test_record_this_workers_kb_dir(n):
    from faultmaven.bootstrap.data_init import resolve_kb_chroma_dir
    from faultmaven.config.settings import get_settings

    kb = resolve_kb_chroma_dir(get_settings())
    kb.mkdir(parents=True, exist_ok=True)  # what the KB client does on open
    worker = os.environ["PYTEST_XDIST_WORKER"]
    out = pathlib.Path(os.environ["FM_1772_OUT"], worker)
    out.write_text(str(kb))
"""


def test_two_xdist_workers_get_different_kb_dirs_outside_the_checkout(
    pytester, monkeypatch, tmp_path
):
    """Under ``-n 2`` each worker opens a KB tree of its own, outside the
    checkout and outside the child's working directory, and each is removed
    when its worker exits."""
    pytest.importorskip("xdist")
    out = tmp_path / "recorded"
    out.mkdir()
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            filter(
                None,
                [str(REPO_ROOT), str(pytester.path), os.environ.get("PYTHONPATH")],
            )
        ),
    )
    monkeypatch.setenv("FM_1772_OUT", str(out))
    # A run of its own: not this worker, and not a caller's database.
    for name in ("PYTEST_XDIST_WORKER", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    pytester.makeconftest(_CHILD_CONFTEST)
    pytester.makepyfile(test_child=_CHILD_TESTS)

    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-n", "2", "--dist", "each", timeout=300
    )

    result.assert_outcomes(passed=8)
    recorded = {path.name: pathlib.Path(path.read_text()) for path in out.iterdir()}
    assert set(recorded) == {"gw0", "gw1"}
    assert recorded["gw0"] != recorded["gw1"]
    for worker, kb in recorded.items():
        assert kb.is_absolute()
        assert not kb.is_relative_to(REPO_ROOT)
        assert not kb.is_relative_to(pytester.path)
        assert kb.parent.name.startswith(f"faultmaven-{worker}-")
        assert not kb.parent.exists(), "the worker's root outlived the worker"
    assert not (pytester.path / "data").exists()
