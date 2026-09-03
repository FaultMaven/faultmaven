"""``fm-reset-kb`` acts on the store the SERVER opens, or on nothing (#936).

The command deletes every ``knowledge_items`` row and then ``rmtree``s a
ChromaDB directory. It used to pick that directory by re-deriving
``get_project_root()/data/chroma-kb`` — a second, independent answer to a
question the server answers from ``CHROMADB_KB_PERSIST_DIR``. Two answers to
one question are only ever correct by coincidence, and when they diverged
nothing said so, because the startup bootstrap had already created an EMPTY
directory at the project root. The wipe found that decoy, removed it, printed
"Removed …" and exited 0 while the server's vectors survived and the SQL rows
did not — and the DIVERGE warning written for exactly this shape could not
fire, because the decoy made the directory exist.

Four ways that goes wrong, all closed here:

* an explicit ``CHROMADB_KB_PERSIST_DIR`` that points somewhere else;
* a **remote** store — ``CHROMADB_URL`` (the container's client factory) or
  ``CHROMADB_HOST`` alone (``KnowledgeIngester``'s k8s opt-in) — where no local
  directory is the store in the first place;
* the unset default, which is relative and must be read against the working
  directory exactly as ``create_persistent_client`` reads it, with no
  project-root anchoring to reintroduce a second spelling; and
* a resolved path that is blank, absent, or not store-shaped — reading an
  operator-supplied knob made the ``rmtree`` argument operator-supplied too.

Every refusal lands BEFORE the SQL delete, because that half is not undoable
and a check after it can only describe damage.

Every test asserts the POSITIVE column as well as the negative one. "The wrong
directory survived" is satisfied by a command that refuses to do anything, so
each guard is paired with the case that must still run.
"""

from __future__ import annotations

import pytest

from faultmaven.cli import reset_kb

pytestmark = pytest.mark.unit

#: The external ChromaDB these tests configure. ``.invalid`` is the reserved
#: never-resolvable TLD, and the host carries a distinctive token so an
#: assertion can name what the command printed WITHOUT spelling a URL literal
#: inside an ``in`` check — CodeQL reads that shape as URL-validation
#: sanitization (``py/incomplete-url-substring-sanitization``, high severity).
#: It is a false positive here (the haystack is captured stdout, not a URL
#: being authorised), but the configured value stays a realistic URL either
#: way, so nothing is lost by asserting on a token within it.
_EXTERNAL_CHROMA_HOST = "chroma-9f3a2b"
_EXTERNAL_CHROMA_URL = f"http://{_EXTERNAL_CHROMA_HOST}.invalid:8000"

#: Every knob these tests speak about. Cleared before each configuration so a
#: value set by an earlier test (or by the developer's shell) cannot decide the
#: outcome of a later one.
_KNOBS = (
    "CHROMADB_KB_PERSIST_DIR",
    "CHROMADB_EVIDENCE_PERSIST_DIR",
    "EVIDENCE_STORAGE_ROOT",
    "CHROMADB_URL",
    "CHROMADB_HOST",
    "VECTOR_STORAGE_TYPE",
    "PROJECT_ROOT",
)


class _FakeResult:
    """Stands in for a DELETE result: the CLI reads ``.rowcount``."""

    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.deletes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def scalar(self, _stmt):
        return self._rows

    async def execute(self, _stmt):
        self.deletes += 1
        return _FakeResult(self._rows)

    async def commit(self):
        return None


@pytest.fixture
def stub_db(monkeypatch):
    """Replace the session factory the CLI imports at call time."""

    def _stub(rows=0):
        session = _FakeSession(rows)
        from faultmaven.infrastructure.persistence import database

        monkeypatch.setattr(database, "get_db_session", lambda: session)
        return session

    return _stub


@pytest.fixture
def configure(monkeypatch):
    """Configure the deployment through the environment, as the server is.

    Deliberately NOT a settings mock. A ``MagicMock`` answers plausibly to
    every attribute, so the predicates that decide these outcomes
    (``is_external_chroma_configured`` and friends) would return whatever the
    mock felt like and the whole file could pass against the unfixed code.
    """
    from faultmaven.config import settings as settings_module

    def _configure(**env):
        for knob in _KNOBS:
            monkeypatch.delenv(knob, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        settings_module.reset_settings()
        return settings_module.get_settings()

    yield _configure
    settings_module.reset_settings()


def _store(path):
    """A ChromaDB directory with a file in it, so its loss is observable."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "chroma.sqlite3").write_bytes(b"vectors")
    return path


async def test_the_configured_store_is_what_gets_wiped_not_the_project_root_one(
    tmp_path, capsys, configure, stub_db
):
    """The override variant. Both columns matter: the configured store must be
    GONE (a command that refuses everything would satisfy "the decoy survived"
    on its own), and the project-root look-alike must be untouched."""
    configured = _store(tmp_path / "elsewhere" / "chroma-kb")
    decoy = _store(tmp_path / "data" / "chroma-kb")
    configure(PROJECT_ROOT=tmp_path, CHROMADB_KB_PERSIST_DIR=configured)
    stub_db(rows=5)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 0
    assert not configured.exists(), "the store the server opens must be wiped"
    assert decoy.exists(), "the project-root look-alike must be left alone"
    out = capsys.readouterr().out
    assert str(configured) in out
    assert f"Removed {configured}" in out


async def test_an_unset_persist_dir_resolves_the_way_the_consumer_does(
    tmp_path, monkeypatch, capsys, configure, stub_db
):
    """The shipped default is RELATIVE (``./data/chroma-kb``), and
    ``create_persistent_client`` hands that string straight to ``os.makedirs``
    — so the consumer reads it against the process's working directory, and so
    must this. There is no project-root anchoring, not even here: that would be
    a second spelling for the one configuration nobody overrides, which is
    exactly where the decoy came from.

    ``PROJECT_ROOT`` is set to somewhere else entirely, and a store is planted
    under it. Anchoring on the project root would wipe that one.
    """
    cwd = tmp_path / "server-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    store = _store(cwd / "data" / "chroma-kb")
    project_root_store = _store(tmp_path / "project-root" / "data" / "chroma-kb")
    configure(PROJECT_ROOT=tmp_path / "project-root")
    stub_db(rows=1)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 0
    assert not store.exists(), "the cwd reading is what the consumer opens"
    assert project_root_store.exists(), "PROJECT_ROOT does not name a data store"
    assert f"Removed {store}" in capsys.readouterr().out


async def test_an_external_chromadb_is_refused_before_any_row_is_deleted(
    tmp_path, capsys, configure, stub_db
):
    """The production-reachable variant, with ZERO overrides. No local tree is
    the store, so there is nothing here this command can wipe — and the SQL
    DELETE is not undoable, so the check has to run BEFORE it, not after."""
    decoy = _store(tmp_path / "data" / "chroma-kb")
    configure(
        PROJECT_ROOT=tmp_path,
        CHROMADB_URL=_EXTERNAL_CHROMA_URL,
        VECTOR_STORAGE_TYPE="chromadb",
    )
    session = stub_db(rows=9)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 1
    assert session.deletes == 0, "nothing may be written before the refusal"
    assert decoy.exists(), "and nothing may be removed"
    out = capsys.readouterr().out
    assert "refusing" in out.lower()
    assert _EXTERNAL_CHROMA_HOST in out
    assert "Removed" not in out


async def test_the_same_configuration_without_a_remote_knob_is_not_refused(
    tmp_path, monkeypatch, capsys, configure, stub_db
):
    """The positive control for the refusal above. A guard that refused
    everything would pass that test too — this one fails unless the refusal
    keys specifically on the external store."""
    monkeypatch.chdir(tmp_path)
    store = _store(tmp_path / "data" / "chroma-kb")
    configure(VECTOR_STORAGE_TYPE="chromadb")
    session = stub_db(rows=9)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 0
    assert session.deletes == 1
    assert not store.exists()
    assert "refusing" not in capsys.readouterr().out.lower()


async def test_keep_chroma_is_the_opt_out_and_still_wipes_the_sql_rows(
    tmp_path, capsys, configure, stub_db
):
    """``--keep-chroma`` already meant "wipe SQL, leave the vectors" — a
    deliberate divergence. It is therefore the opt-out for the refusal too: the
    operator has explicitly said not to touch the vector store, so there is
    nothing left for the command to get wrong. It must say what it kept."""
    decoy = _store(tmp_path / "data" / "chroma-kb")
    configure(
        PROJECT_ROOT=tmp_path,
        CHROMADB_URL=_EXTERNAL_CHROMA_URL,
        VECTOR_STORAGE_TYPE="chromadb",
    )
    session = stub_db(rows=4)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=True
    )

    assert code == 0
    assert session.deletes == 1, "the SQL half must still run"
    assert decoy.exists(), "the local tree is not the store and is not touched"
    out = capsys.readouterr().out
    assert _EXTERNAL_CHROMA_HOST in out
    assert "DIVERGE" in out, "the operator opted into a divergence; say so"


async def test_a_dry_run_under_an_external_store_previews_the_refusal(
    tmp_path, capsys, configure, stub_db
):
    """A dry run writes nothing, so it is not refused — but it must preview the
    refusal, or an operator rehearses a run that will not happen."""
    _store(tmp_path / "data" / "chroma-kb")
    configure(
        PROJECT_ROOT=tmp_path,
        CHROMADB_URL=_EXTERNAL_CHROMA_URL,
        VECTOR_STORAGE_TYPE="chromadb",
    )
    session = stub_db(rows=3)

    code = await reset_kb.reset_kb(
        dry_run=True, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 0
    assert session.deletes == 0
    out = capsys.readouterr().out
    assert "refusing" in out.lower()
    assert "(dry-run) No changes made." in out
    # The banner must not offer a local path as "the ChromaDB path" when the
    # store is a server — that is precisely how an operator concludes the
    # local tree is what the wipe would target.
    assert str(tmp_path / "data" / "chroma-kb") not in out
    assert f"remote server (CHROMADB_URL={_EXTERNAL_CHROMA_URL})" in out


async def test_a_blank_persist_dir_is_refused_not_defaulted(
    tmp_path, monkeypatch, capsys, configure, stub_db
):
    """``Path("") == Path(".")``, and pydantic-settings runs with
    ``env_ignore_empty`` OFF — so ``CHROMADB_KB_PERSIST_DIR=`` (an unpopulated
    ConfigMap key, a trailing ``=`` in a .env) reaches the resolver as a SET
    field holding "". Taken literally, that aims the ``rmtree`` at the working
    directory.

    Substituting the documented default is not the fix either, and that is the
    subtle half: no consumer does it. ``getattr(db, "chromadb_kb_persist_dir",
    "./data/chroma-kb")`` returns ``""`` because the attribute exists, and
    ``create_persistent_client("")`` dies in ``os.makedirs``. A resolver that
    answered ``./data/chroma-kb`` here would hand this command a tree the
    container never opens — the decoy, rebuilt inside its own fix.
    """
    cwd = tmp_path / "cwd"
    (cwd / "precious").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    default_spelling = _store(cwd / "data" / "chroma-kb")
    configure(CHROMADB_KB_PERSIST_DIR="")
    session = stub_db(rows=1)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 1
    assert session.deletes == 0
    assert (cwd / "precious").is_dir(), "the working directory is not a store"
    assert default_spelling.exists(), "and the default is not a substitute for it"
    out = capsys.readouterr().out
    assert "refusing" in out.lower()
    assert "CHROMADB_KB_PERSIST_DIR" in out


async def test_keep_chroma_still_wipes_sql_under_a_blank_persist_dir(
    tmp_path, monkeypatch, capsys, configure, stub_db
):
    """The positive control for the refusal above. ``--keep-chroma`` removes
    nothing, so an unresolvable path decides nothing, and refusing there would
    be refusing over a question that was never asked."""
    monkeypatch.chdir(tmp_path)
    configure(CHROMADB_KB_PERSIST_DIR="")
    session = stub_db(rows=6)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=True
    )

    assert code == 0
    assert session.deletes == 1
    assert "refusing" not in capsys.readouterr().out.lower()


async def test_a_resolved_path_that_is_not_a_store_is_refused_before_the_sql_wipe(
    tmp_path, capsys, configure, stub_db
):
    """The wipe target became operator-supplied with #936, so a mistyped or
    over-broad ``CHROMADB_KB_PERSIST_DIR`` is now an ``rmtree`` argument. A
    directory that is neither empty nor a ChromaDB store is not the KB, and
    the refusal has to land before the irreversible half."""
    not_a_store = tmp_path / "home"
    (not_a_store / "documents").mkdir(parents=True)
    configure(PROJECT_ROOT=tmp_path, CHROMADB_KB_PERSIST_DIR=not_a_store)
    session = stub_db(rows=6)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 1
    assert session.deletes == 0
    assert (not_a_store / "documents").is_dir()
    out = capsys.readouterr().out
    assert "refusing" in out.lower()
    assert str(not_a_store) in out


@pytest.mark.parametrize("populated", [True, False])
async def test_a_real_store_is_wiped_whether_or_not_it_has_been_written_to(
    tmp_path, capsys, configure, stub_db, populated
):
    """The positive control for the guard above, in both admissible shapes.
    A check that refused an *empty* directory would break the ordinary case:
    the bootstrap creates the tree, and a KB that has never been ingested is
    still a KB an operator may reset."""
    store = tmp_path / "vol" / "kb"
    store.mkdir(parents=True)
    if populated:
        (store / "chroma.sqlite3").write_bytes(b"vectors")
    configure(PROJECT_ROOT=tmp_path, CHROMADB_KB_PERSIST_DIR=store)
    session = stub_db(rows=6)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 0
    assert session.deletes == 1
    assert not store.exists()
    assert f"Removed {store}" in capsys.readouterr().out


async def test_a_host_only_chromadb_wipes_the_local_store_and_warns(
    tmp_path, monkeypatch, capsys, configure, stub_db
):
    """``CHROMADB_HOST`` alone is NOT the same answer as ``CHROMADB_URL``.

    ``create_kb_chromadb_client`` dispatches on the URL, so under the host-only
    opt-in it still returns a local ``PersistentClient`` (measured:
    ``chroma_server_host`` is None) — the local tree really is the store this
    deployment searches. Only ``KnowledgeIngester`` goes remote. Refusing here
    would leave the searched store with no way to be reset, on the strength of
    a second store this command has never claimed to touch. So it wipes, and it
    says the other one exists.
    """
    monkeypatch.chdir(tmp_path)
    store = _store(tmp_path / "data" / "chroma-kb")
    configure(CHROMADB_HOST="chromadb.faultmaven.svc")
    session = stub_db(rows=7)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 0
    assert session.deletes == 1
    assert not store.exists(), "the store the client factory opens IS reset"
    out = capsys.readouterr().out
    assert "refusing" not in out.lower()
    # …and the operator is told the reset was not total.
    assert "CHROMADB_HOST=chromadb.faultmaven.svc" in out
    assert "REMOTE" in out
    assert "two places" in out


async def test_a_blank_chromadb_host_is_not_a_remote_store(
    tmp_path, monkeypatch, capsys, configure, stub_db
):
    """The same ``env_ignore_empty`` shape, one knob over. ``CHROMADB_HOST=``
    arrives as a set field holding "", and a bare ``!= "localhost"`` reads that
    as a configured server — which refused this command on an ordinary embedded
    deployment whose store was right there, naming a knob with no value."""
    monkeypatch.chdir(tmp_path)
    store = _store(tmp_path / "data" / "chroma-kb")
    configure(CHROMADB_HOST="")
    session = stub_db(rows=7)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 0
    assert session.deletes == 1
    assert not store.exists()
    out = capsys.readouterr().out
    assert "refusing" not in out.lower()
    assert "REMOTE" not in out, "nothing remote is configured"


async def test_the_default_chromadb_host_is_not_treated_as_remote(
    tmp_path, monkeypatch, capsys, configure, stub_db
):
    """The positive control for the guard above. ``localhost`` is the shipped
    default and means the embedded PersistentClient, so a check that read any
    ``CHROMADB_HOST`` as remote would refuse every ordinary deployment."""
    monkeypatch.chdir(tmp_path)
    store = _store(tmp_path / "data" / "chroma-kb")
    configure(CHROMADB_HOST="localhost")
    session = stub_db(rows=7)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 0
    assert session.deletes == 1
    assert not store.exists()
    assert "refusing" not in capsys.readouterr().out.lower()


async def test_a_store_that_is_not_there_is_refused_not_warned_about_afterwards(
    tmp_path, monkeypatch, capsys, configure, stub_db
):
    """This used to delete every row and then print a DIVERGE warning — a
    description of damage, arriving after the irreversible half. The resolved
    path is read exactly as the consumer reads it, so "not there" means this
    process is not looking where the server looked, which is precisely when it
    must not proceed."""
    cwd = tmp_path / "wrong-directory"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    configure()
    session = stub_db(rows=11)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 1
    assert session.deletes == 0, "the SQL half must not run"
    out = capsys.readouterr().out
    assert "refusing" in out.lower()
    assert str(cwd / "data" / "chroma-kb") in out


async def test_keep_chroma_still_wipes_sql_when_there_is_no_local_store(
    tmp_path, monkeypatch, capsys, configure, stub_db
):
    """The positive control for the refusal above, and the escape hatch it
    names. An operator whose KB genuinely has no vector store yet must still be
    able to clear the rows — otherwise the guard has replaced a data-loss bug
    with a dead command."""
    monkeypatch.chdir(tmp_path)
    configure()
    session = stub_db(rows=11)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=True
    )

    assert code == 0
    assert session.deletes == 1
    assert "refusing" not in capsys.readouterr().out.lower()


async def test_an_unremovable_store_is_reported_not_raised(
    tmp_path, monkeypatch, capsys, configure, stub_db
):
    """The pre-flight says the path exists and is store-shaped; ``rmtree`` can
    still refuse it. A symlinked persist directory is the reachable case — the
    shape checks follow the link, ``shutil.rmtree`` will not — and by then the
    rows are gone, so a bare traceback would leave the operator with a diverged
    KB and no statement of it."""
    monkeypatch.chdir(tmp_path)
    real = _store(tmp_path / "real-store")
    link = tmp_path / "data" / "chroma-kb"
    link.parent.mkdir(parents=True)
    link.symlink_to(real, target_is_directory=True)
    configure()
    session = stub_db(rows=4)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    # The LITERAL, first and on its own. Asserting only ``EXIT_DIVERGED`` would
    # make this fail with AttributeError against code that lacks the constant —
    # a symbol failure that proves the name is new, not that the behaviour is
    # fixed. The exit code is the whole finding: the old path printed every one
    # of the strings below and then exited 0.
    assert code != 0, (
        "reporting success after leaving the KB diverged is the defect; a "
        "runbook chaining `fm-reset-kb --yes && kubectl … --replicas=1` "
        "brings the API back up onto the un-wiped store"
    )
    assert code == 3
    assert code == reset_kb.EXIT_DIVERGED, "and 3 is the code that names it"
    assert session.deletes == 1
    assert (real / "chroma.sqlite3").exists(), "nothing was removed"
    out = capsys.readouterr().out
    assert "DIVERGE" in out
    assert "symlink" in out.lower()
    # The old fall-through printed this and exited 0, so a runbook chaining
    # `fm-reset-kb --yes && kubectl … scale --replicas=1` brought the API back
    # up onto the un-wiped store.
    assert "Next step: restart the API server" not in out


async def test_the_directory_the_bootstrap_creates_is_the_one_the_reset_wipes(
    tmp_path, monkeypatch, capsys, configure, stub_db
):
    """The seam, end to end. The decoy was not something the operator left
    lying around — the server's own startup manufactured it. Run the real
    bootstrap, then the real reset, and require that they name the same tree."""
    from faultmaven.bootstrap.data_init import ensure_data_directories

    monkeypatch.chdir(tmp_path)  # knowledge_root() is cwd-relative
    configured = tmp_path / "elsewhere" / "chroma-kb"
    configure(PROJECT_ROOT=tmp_path, CHROMADB_KB_PERSIST_DIR=configured)

    ensure_data_directories()
    assert configured.exists(), "the bootstrap must create the configured tree"
    assert not (
        tmp_path / "data" / "chroma-kb"
    ).exists(), "and must not manufacture a project-root decoy beside it"

    stub_db(rows=2)
    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 0
    assert not configured.exists()
    assert f"Removed {configured}" in capsys.readouterr().out
