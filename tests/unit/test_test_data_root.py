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
from pydantic import Field
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
    """Whether ``value`` names the cwd-relative ``data/`` tree.

    A path, or a DSN around one: ``scheme:///`` is stripped first, so a
    relative SQLite URL (``sqlite:///data/new.db``) counts. Absolute paths
    (``sqlite:////tmp/x.db``) do not.
    """
    if not isinstance(value, (str, pathlib.PurePath)):
        return False
    text = str(value)
    _scheme, separator, rest = text.partition(":///")
    if not separator:
        rest = text
    parts = pathlib.PurePosixPath(rest).parts[:2]
    return parts[:1] == ("data",) or parts == (".", "data")


def _settings_classes() -> list[type]:
    from faultmaven.config import settings as settings_module

    return [
        cls
        for cls in vars(settings_module).values()
        if isinstance(cls, type)
        and issubclass(cls, BaseSettings)
        and cls.__module__ == SETTINGS_MODULE
    ]


def _data_fields_of(classes) -> list[tuple[type, str]]:
    """Every field of ``classes`` whose SHIPPED default is under ``./data``.

    The shipped default is what the conftest recorded before moving it, or the
    field's default where the conftest moved nothing -- which is how a new
    field shows up here. A ``default_factory`` is called, so a factory-built
    default is seen too.
    """
    found = []
    for cls in classes:
        for name, field in cls.model_fields.items():
            key = (cls.__module__, cls.__name__, name)
            if key in SHIPPED_DATA_DEFAULTS:
                shipped = SHIPPED_DATA_DEFAULTS[key]
            else:
                shipped = field.get_default(call_default_factory=True)
            if _is_under_data(shipped):
                found.append((cls, name))
    return found


def _data_fields() -> list[tuple[type, str]]:
    return _data_fields_of(_settings_classes())


def _variable_of(cls: type, name: str) -> str:
    """The environment variable a field binds (the models use no prefix)."""
    alias = cls.model_fields[name].validation_alias
    return alias if isinstance(alias, str) else name.upper()


def _set_by_the_caller(variable: str) -> bool:
    """Set, and not to a path under this session's root: the caller's choice,
    which the conftest leaves alone (the ``-m postgres`` lane's DATABASE_URL)."""
    value = os.environ.get(variable)
    return value is not None and (os.environ[TEST_DATA_ROOT_ENV] + os.sep) not in value


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
    assert not _is_under_data(value), f"{label} = {text!r} is the cwd-relative ./data"


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


@pytest.mark.parametrize(
    "field",
    [
        Field(default_factory=lambda: "./data/planted"),
        Field(default="sqlite:///data/planted.db"),
    ],
    ids=["default-factory", "relative-sqlite-dsn"],
)
def test_a_planted_data_field_fails_the_guard(field):
    """Two spellings the introspection once missed: a factory-built default,
    and a relative SQLite DSN. Planted on a settings class of their own, each
    must be found and must fail the session-root check."""
    planted = type(
        "PlantedSettings",
        (BaseSettings,),
        {"__annotations__": {"planted_path": str}, "planted_path": field},
    )

    assert _data_fields_of([planted]) == [(planted, "planted_path")]
    with pytest.raises(AssertionError, match="is not under"):
        _assert_in_the_session_root("planted", planted().planted_path)


def test_absolute_and_unrelated_values_are_not_data():
    for value in (
        "sqlite:////tmp/x.db",
        "/data/chroma-kb",
        "postgresql+asyncpg://u@h/db",
        "database",
        "metadata/x",
    ):
        assert not _is_under_data(value), value


def _assert_every_data_default_in_the_session_root() -> list[str]:
    """Assert every ``./data`` default is in the session root; return the
    variables skipped because the caller set them explicitly."""
    import importlib

    from faultmaven.config.settings import get_settings, reset_settings

    skipped = []
    for cls, name in _data_fields():
        variable = _variable_of(cls, name)
        if _set_by_the_caller(variable):
            skipped.append(variable)
            continue
        _assert_in_the_session_root(f"{cls.__name__}.{name}", getattr(cls(), name))
    for module, attribute in _module_attributes():
        value = getattr(importlib.import_module(module), attribute)
        _assert_in_the_session_root(f"{module}.{attribute}", value)

    reset_settings()
    try:
        settings = get_settings()
        for variable, label, value in (
            ("DATABASE_URL", "database.database_url", settings.database.database_url),
            (
                "CHROMADB_KB_PERSIST_DIR",
                "database.chromadb_kb_persist_dir",
                settings.database.chromadb_kb_persist_dir,
            ),
            (
                "CHROMADB_EVIDENCE_PERSIST_DIR",
                "database.chromadb_evidence_persist_dir",
                settings.database.chromadb_evidence_persist_dir,
            ),
            (
                "EVIDENCE_STORAGE_ROOT",
                "evidence_storage.evidence_storage_root",
                settings.evidence_storage.evidence_storage_root,
            ),
            (
                "REDACTION_PSEUDONYM_KEY_PATH",
                "protection.pseudonym_key_path",
                settings.protection.pseudonym_key_path,
            ),
        ):
            if variable not in skipped:
                _assert_in_the_session_root(label, value)
    finally:
        reset_settings()
    return skipped


def test_every_data_default_resolves_in_the_session_root():
    skipped = _assert_every_data_default_in_the_session_root()
    if skipped:
        pytest.skip(
            f"the caller set {', '.join(sorted(skipped))} explicitly, to a path "
            "outside this session's root; the caller's value wins (#1772), so "
            "those fields were not checked (every other one passed). The "
            "emptied-environment test still checks their defaults."
        )


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
