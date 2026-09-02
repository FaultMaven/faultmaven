"""The startup bootstrap creates the directories this deployment READS (#936).

``ensure_data_directories`` derived all six of its paths from
``get_project_root()`` while the consumers read live settings —
``CHROMADB_KB_PERSIST_DIR``, ``CHROMADB_EVIDENCE_PERSIST_DIR``,
``EVIDENCE_STORAGE_ROOT``, and ``knowledge_root()``. A second spelling of a
configured path does not merely fail to find the store; it CREATES an empty
one beside it. That empty directory is then indistinguishable from a store to
anything that looks, which is how ``fm-reset-kb`` came to wipe a decoy, report
"Removed …", and exit 0 while the real vectors survived and the SQL rows did
not.

The external-ChromaDB case is the sharpest one: the deployment opens no local
tree at all, so any local directory created here is a decoy by construction.
"""

from __future__ import annotations

import pytest

from faultmaven.bootstrap.data_init import ensure_data_directories

pytestmark = pytest.mark.unit

_KNOBS = (
    "CHROMADB_KB_PERSIST_DIR",
    "CHROMADB_EVIDENCE_PERSIST_DIR",
    "EVIDENCE_STORAGE_ROOT",
    "CHROMADB_URL",
    "VECTOR_STORAGE_TYPE",
    "PROJECT_ROOT",
)


@pytest.fixture
def configure(monkeypatch, tmp_path):
    """Configure the deployment through the environment, as the server is.

    A settings ``MagicMock`` would be worse than useless here: the resolver
    reads the field and asks whether it is blank, and a ``MagicMock``
    attribute is a truthy object whose ``str()`` is a ``<MagicMock id=…>``
    repr — so every knob would read as *configured*, to a path made of that
    repr, and the assertions would be measuring the mock.

    ``chdir`` because ``knowledge_root()`` is relative on purpose — the scan
    matches persisted ``data/knowledge/...`` strings against a walk of that
    same relative root — so an uncontrolled cwd would write into the checkout.
    """
    from faultmaven.config import settings as settings_module

    monkeypatch.chdir(tmp_path)

    def _configure(**env):
        for knob in _KNOBS:
            monkeypatch.delenv(knob, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        settings_module.reset_settings()
        return settings_module.get_settings()

    yield _configure
    settings_module.reset_settings()


def test_the_configured_vector_directories_are_what_get_created(tmp_path, configure):
    """Settings-first, and the project-root spellings are NOT created — a
    bootstrap that created both would still manufacture the decoy."""
    kb = tmp_path / "vol" / "kb"
    evidence_vectors = tmp_path / "vol" / "ev"
    configure(
        PROJECT_ROOT=tmp_path,
        CHROMADB_KB_PERSIST_DIR=kb,
        CHROMADB_EVIDENCE_PERSIST_DIR=evidence_vectors,
    )

    ensure_data_directories()

    assert kb.is_dir()
    assert evidence_vectors.is_dir()
    assert not (tmp_path / "data" / "chroma-kb").exists()
    assert not (tmp_path / "data" / "chroma-evidence").exists()


def test_the_evidence_storage_root_follows_its_setting(tmp_path, configure):
    """Same knob-vs-project-root split, for the tree the storage factory opens
    (``settings.evidence_storage.evidence_storage_root``)."""
    uploads = tmp_path / "vol" / "uploads"
    configure(PROJECT_ROOT=tmp_path, EVIDENCE_STORAGE_ROOT=uploads)

    ensure_data_directories()

    assert uploads.is_dir()
    assert not (tmp_path / "data" / "evidence").exists()


def test_no_local_vector_directory_is_created_beside_an_external_chromadb(
    tmp_path, configure
):
    """The on-prem shape, with no overrides. The deployment reads its vectors
    from a server, so a local tree here is a decoy and nothing more —
    ``create_persistent_client`` does its own ``makedirs`` on whatever path it
    genuinely opens, so nothing needed the pre-creation anyway.

    The evidence assertion is the positive control: a bootstrap that had simply
    stopped creating anything would satisfy the two negative assertions.
    """
    configure(
        PROJECT_ROOT=tmp_path,
        CHROMADB_URL="http://chromadb.faultmaven.svc:8000",
        VECTOR_STORAGE_TYPE="chromadb",
    )

    ensure_data_directories()

    assert not (tmp_path / "data" / "chroma-kb").exists()
    assert not (tmp_path / "data" / "chroma-evidence").exists()
    assert (tmp_path / "data" / "evidence").is_dir(), "the rest must still be created"
    assert (tmp_path / "data" / "knowledge" / "global").is_dir()


def test_unconfigured_directories_resolve_the_way_their_consumers_do(
    tmp_path, configure
):
    """The shipped defaults are relative (``./data/chroma-kb``), and every
    consumer reads them against the process's working directory —
    ``create_persistent_client`` does ``os.makedirs`` on the raw string. So the
    unset case is cwd-relative too. Anchoring it on ``get_project_root()``
    instead would be a second spelling for exactly the configuration nobody
    overrides, which is where the decoy came from.

    ``PROJECT_ROOT`` is deliberately NOT the working directory here: with the
    two coincident the readings are the same path and this could not tell them
    apart.
    """
    configure(PROJECT_ROOT=tmp_path / "project-root")

    ensure_data_directories()

    assert (tmp_path / "data" / "chroma-kb").is_dir()
    assert (tmp_path / "data" / "chroma-evidence").is_dir()
    assert (tmp_path / "data" / "evidence").is_dir()
    # PROJECT_ROOT names repo-layout artifacts (alembic.ini, the KB pack), not
    # runtime data. Nothing may appear under it.
    assert not (tmp_path / "project-root").exists()


def test_a_relative_override_is_read_the_way_the_server_reads_it(
    tmp_path, monkeypatch, configure
):
    """chromadb hands the configured string straight to the filesystem, so a
    relative override is relative to the process's working directory. Anchoring
    it on the project root instead would reintroduce the divergence in the one
    case an operator is most likely to write by hand."""
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    configure(
        PROJECT_ROOT=tmp_path / "elsewhere-entirely",
        CHROMADB_KB_PERSIST_DIR="./local-kb",
    )
    monkeypatch.chdir(elsewhere)

    ensure_data_directories()

    assert (elsewhere / "local-kb").is_dir()
    assert not (tmp_path / "elsewhere-entirely" / "data" / "chroma-kb").exists()


def test_a_configured_value_is_passed_through_unstripped(tmp_path, configure):
    """Surrounding whitespace is stripped only to decide whether the value is
    EMPTY; the value itself goes through as configured, because
    ``create_persistent_client`` does not tidy it either. A caller that
    normalised it would open ``/vol/kb`` while the server opened ``/vol/kb ``
    — this bug, one layer in."""
    padded = f" {tmp_path / 'vol' / 'kb'} "
    configure(PROJECT_ROOT=tmp_path, CHROMADB_KB_PERSIST_DIR=padded)

    ensure_data_directories()

    from pathlib import Path

    assert Path(padded).is_dir(), "the configured spelling is what gets created"
    assert not (tmp_path / "vol" / "kb").exists(), "the tidied spelling is not it"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_vector_dir_creates_nothing_and_does_not_fault(
    tmp_path, caplog, configure, blank
):
    """``Path("")`` is the working directory and ``Path("   ")`` is a directory
    named with three spaces; nothing opens either on purpose. Substituting the
    documented default is not the answer — no consumer does that, so the tree
    would be one nothing opens (the decoy, rebuilt inside the fix). Create
    nothing, say why, and let the rest of startup proceed.

    The evidence assertion is the positive control: skipping ONE directory must
    not become skipping all of them.
    """
    import logging

    configure(PROJECT_ROOT=tmp_path, CHROMADB_KB_PERSIST_DIR=blank)

    with caplog.at_level(logging.ERROR):
        ensure_data_directories()

    assert not (tmp_path / "data" / "chroma-kb").exists(), "no default substitute"
    assert not (tmp_path / blank.strip()).exists() if blank.strip() else True
    assert "CHROMADB_KB_PERSIST_DIR" in caplog.text
    assert (tmp_path / "data" / "evidence").is_dir(), "the rest is still created"


def test_a_directory_that_cannot_be_created_does_not_kill_startup(
    tmp_path, caplog, configure
):
    """These paths are operator-supplied now, so a read-only mount or a PVC
    that did not attach turns a bad knob into a CrashLoopBackOff — where before
    the bootstrap only touched the writable project root. Every consumer
    creates its own tree lazily anyway, so the honest response is to log and
    carry on."""
    import logging

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("I am a file")
    configure(PROJECT_ROOT=tmp_path, CHROMADB_KB_PERSIST_DIR=str(blocker / "kb"))

    with caplog.at_level(logging.ERROR):
        ensure_data_directories()  # must not raise

    assert "Could not create data directory" in caplog.text
    assert (tmp_path / "data" / "evidence").is_dir(), "the rest is still created"


def test_the_knowledge_tree_is_the_one_the_scan_and_upload_paths_walk(
    tmp_path, configure
):
    """``knowledge_root()`` is the single anchor every runbook path resolves
    against, and it is relative by design. Creating a project-root-absolute
    copy of it is the same defect in a quieter place: under a ``PROJECT_ROOT``
    that is not the working directory, the bootstrap made one tree and the scan
    walked another."""
    from faultmaven.utils.runbook_id import knowledge_root

    configure(PROJECT_ROOT=tmp_path / "not-the-cwd")

    ensure_data_directories()

    assert knowledge_root().is_dir()
    assert (knowledge_root() / "global").is_dir()
    assert not (tmp_path / "not-the-cwd" / "data" / "knowledge").exists()


def test_it_is_idempotent_on_the_configured_directories(tmp_path, configure):
    """Called on every startup, so the second call must be a no-op rather than
    an error on an existing tree — including on the configured paths, which
    are the ones this change newly creates."""
    kb = tmp_path / "vol" / "kb"
    configure(PROJECT_ROOT=tmp_path, CHROMADB_KB_PERSIST_DIR=kb)

    ensure_data_directories()
    (kb / "chroma.sqlite3").write_bytes(b"vectors")
    ensure_data_directories()

    assert kb.is_dir()
    assert (kb / "chroma.sqlite3").exists(), "a re-run must not disturb the store"
