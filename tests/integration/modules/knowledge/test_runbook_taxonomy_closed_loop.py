"""Every value the runbook taxonomy allows can be validated, stored and verified (#1886).

A runbook whose ``severity`` was ``info`` passed validation and could never be
verified: verify writes the frontmatter severity into
``conversion_drafts.severity``, and that column's CHECK — a hand copy of the
vocabulary — had lost ``info``. The vocabularies now have one owner
(``faultmaven.modules.knowledge.taxonomy``) and every reader derives from it;
this module closes the loop by driving each allowed value through the real
manual-create path:

    ``create_runbook_from_template`` (writes the file, validates, persists the
    draft) → ``verify_draft`` (rewrites the frontmatter, writes the draft row's
    metadata columns, ingests the knowledge item) → the rows as stored.

Each vocabulary that reaches a CONSTRAINED column goes through: ``severity``
(``conversion_drafts_severity_check``) and ``scope`` (``conversion_jobs`` and
``knowledge_items`` scope CHECKs). The others (domain, symptom_class,
difficulty) are driven through the same path, since an allowed value must also
survive validation and verify even where no column constrains it.

Both schemas a deployment can have are exercised: the ORM's ``create_all`` and
the alembic chain migrated to head, because the constraint text of each is a
separate copy and either could be the one that drifts. The vector half of
ingestion is stubbed — the property here is the relational write.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.settings import set_env_var
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    KnowledgeItemModel,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.taxonomy import (
    KnowledgeScope,
    RunbookDifficulty,
    RunbookDomain,
    RunbookSeverity,
    SymptomClass,
    vocabulary,
)
from tests.runbook_samples import valid_runbook

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[4]

#: The five free-text sections of the Create form, shaped to pass the validator.
SECTIONS = {
    "symptom_recognition": '- "ERROR: something failed" in the application log',
    "applicability": "PostgreSQL 14+. Requires pg_monitor role. Tools: psql.",
    "diagnostic_steps": (
        "### Step 1: Check state\n"
        "```bash\n"
        'psql -c "SELECT 1"\n'
        "```\n"
        "Look for a non-empty result."
    ),
    "causes": (
        "### Cause A: Example root cause\n"
        "**Statement:** The single root cause of the failure.\n"
        "**Indicators:**\n"
        "- root: [Step 1] the observable that confirms the root\n"
        "**Interventions:**\n"
        "- **remediation** (root): apply the durable fix.\n"
        "  **Verification:** Re-run Step 1; the result is non-empty.\n"
        "\n"
        "### Cause Z: Unidentified\n"
        "**Statement:** None of the documented causes match the observed evidence.\n"
        "**Indicators:**\n"
        "- [Default]\n"
        "**Interventions:**\n"
        "- **mitigation** (D): Capture full diagnostic output and consult an SME.\n"
        "  **Risk:** Diagnostic only. **Duration:** Until SME review. "
        "**Verification:** N/A."
    ),
    "prevention": "- Add an alert on the failing metric.",
}

#: One field value per case. The baseline is an all-defaults runbook; each
#: case moves ONE field to one allowed value, so a failure names the value.
BASELINE = {
    "domain": RunbookDomain.DATABASE.value,
    "symptom_class": [SymptomClass.LATENCY.value],
    "severity": RunbookSeverity.HIGH.value,
    "scope": KnowledgeScope.PERSONAL.value,
    "difficulty": RunbookDifficulty.INTERMEDIATE.value,
}
CASES = (
    [("severity", value) for value in vocabulary(RunbookSeverity)]
    + [("domain", value) for value in vocabulary(RunbookDomain)]
    + [("symptom_class", [value]) for value in vocabulary(SymptomClass)]
    + [("difficulty", value) for value in vocabulary(RunbookDifficulty)]
    # ``team`` needs a team service and a membership — another test's subject;
    # its value reaches the same two CHECKs as these.
    + [
        ("scope", value)
        for value in vocabulary(KnowledgeScope)
        if value != KnowledgeScope.TEAM.value
    ]
)


def _case_id(case) -> str:
    field, value = case
    return f"{field}={value[0] if isinstance(value, list) else value}"


def _migrate(db_path: Path) -> None:
    """``alembic upgrade head`` on a fresh SQLite file, as a deployment does."""
    env = os.environ.copy()
    set_env_var(env, "DATABASE_URL", f"sqlite:///{db_path}")
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(PROJECT_ROOT), env.get("PYTHONPATH")) if p
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory) -> Path:
    """One migrated database, copied per test (migrating costs seconds)."""
    path = tmp_path_factory.mktemp("migrated") / "head.db"
    _migrate(path)
    return path


#: A PostgreSQL to run the loop against too, when the suite is pointed at one
#: (CI's PostgreSQL job, or a local container): ``DATABASE_URL=postgresql...``.
POSTGRES_URL = os.environ.get("DATABASE_URL", "")
ON_POSTGRES = POSTGRES_URL.startswith("postgresql")
_postgres = pytest.param(
    "postgres",
    marks=[
        pytest.mark.postgres,
        pytest.mark.skipif(
            not ON_POSTGRES,
            reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
        ),
    ],
)

#: What one test writes, cleared before the next on the shared PG database.
_WRITTEN_TABLES = (
    "resource_shares, knowledge_items, conversion_drafts, conversion_jobs, "
    "uploaded_files"
)


def _async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
        "postgresql+psycopg2://", "postgresql+asyncpg://", 1
    )


@pytest.fixture(scope="module")
def migrated_postgres():
    """Migrate the PostgreSQL database to head once for the module."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(PROJECT_ROOT), env.get("PYTHONPATH")) if p
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return _async_url(POSTGRES_URL)


@pytest.fixture(params=["orm", "migrated", _postgres])
async def session_factory(request, tmp_path, migrated_template):
    if request.param == "postgres":
        from sqlalchemy import text

        engine = create_async_engine(request.getfixturevalue("migrated_postgres"))
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {_WRITTEN_TABLES} CASCADE"))
            # PostgreSQL enforces the foreign keys SQLite leaves off: the
            # author the drafts and items name must exist.
            await conn.execute(
                text(
                    "INSERT INTO users (user_id, enterprise_id, username, email, "
                    "display_name, created_at, updated_at) VALUES ('u1', :e, "
                    "'author', 'author@example.com', 'Author', now(), now()) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"e": STANDALONE_ENTERPRISE_ID},
            )
    elif request.param == "migrated":
        db = tmp_path / "db.sqlite"
        db.write_bytes(migrated_template.read_bytes())
        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    if request.param == "orm":
        async with factory() as session:
            session.add(
                EnterpriseModel(
                    enterprise_id=STANDALONE_ENTERPRISE_ID,
                    name="Default Enterprise",
                    slug="default",
                )
            )
            await session.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
def service(session_factory, tmp_path):
    knowledge = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=session_factory,
    )
    knowledge._index_document_in_vector_store = AsyncMock(return_value=3)
    conversion = ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=session_factory,
        knowledge_service=knowledge,
    )
    data_dir = tmp_path / "knowledge"
    with patch.object(type(conversion), "_data_dir", new=property(lambda _: data_dir)):
        yield conversion


async def _create_and_verify(service: ConversionService, fields: dict):
    created = await service.create_runbook_from_template(
        title="Connection Pool Exhausted On The API",
        service_name="postgresql",
        tags=["pgbouncer"],
        user_id="u1",
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        **fields,
        **SECTIONS,
    )
    draft = created["draft"]
    verified = await service.verify_draft(
        created["conversion_id"],
        draft.draft_id,
        user_id="u1",
        username="author",
        is_platform_admin=True,
    )
    return created, draft, verified


@pytest.mark.parametrize("case", CASES, ids=_case_id)
async def test_every_allowed_value_is_validated_stored_and_verified(
    case, service, session_factory
):
    field, value = case
    fields = {**BASELINE, field: value}

    created, draft, verified = await _create_and_verify(service, fields)

    assert draft.validation.passed, draft.validation.errors
    assert verified is not None and verified.status == "verified"

    async with session_factory() as session:
        row = await session.get(ConversionDraftModel, draft.draft_id)
        job = await session.get(ConversionJobModel, created["conversion_id"])
        item = (
            await session.execute(
                select(KnowledgeItemModel).where(
                    KnowledgeItemModel.item_id == verified.knowledge_item_id
                )
            )
        ).scalar_one()
    assert row.status == "verified"
    assert row.severity == fields["severity"]
    assert row.domain == fields["domain"]
    assert job.scope == fields["scope"]
    assert item.scope == fields["scope"]


@pytest.mark.parametrize("schema_case", [("severity", "info")], ids=["info"])
async def test_info_severity_is_verified(schema_case, service, session_factory):
    """The reported defect, named: ``severity: info`` through the manual
    Create path, validated, verified, and stored. Red before #1886 on both
    schemas — verify raised ``IntegrityError`` on
    ``conversion_drafts_severity_check``."""
    fields = {**BASELINE, "severity": RunbookSeverity.INFO.value}
    _, draft, verified = await _create_and_verify(service, fields)
    assert draft.validation.passed, draft.validation.errors
    assert verified.status == "verified"
    async with session_factory() as session:
        row = await session.get(ConversionDraftModel, draft.draft_id)
    assert (row.status, row.severity) == ("verified", "info")


# ---------------------------------------------------------------------------
# The migrated schema's CHECKs are the vocabulary
# ---------------------------------------------------------------------------

#: Every CHECK a vocabulary owns: (table, constraint, column, enum).
CONSTRAINED_COLUMNS = [
    (
        "conversion_drafts",
        "conversion_drafts_severity_check",
        "severity",
        RunbookSeverity,
    ),
    ("knowledge_items", "knowledge_items_scope_check", "scope", KnowledgeScope),
    ("conversion_jobs", "conversion_jobs_scope_check", "scope", KnowledgeScope),
]


@pytest.mark.parametrize(
    "table,name,column,enum_cls", CONSTRAINED_COLUMNS, ids=lambda x: str(x)
)
def test_migrated_check_admits_exactly_the_vocabulary(
    migrated_template, table, name, column, enum_cls
):
    """The migration chain's copy of each CHECK, read back from the migrated
    database, holds the enum's set. A change to a vocabulary fails this until
    a revision moves the constraint (revisions freeze their DDL, so the enum
    cannot reach the chain any other way). Mutation: drop ``info`` from 008's
    constraint text and this fails."""
    import sqlite3

    conn = sqlite3.connect(migrated_template)
    try:
        (ddl,) = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    finally:
        conn.close()
    in_list = re.search(
        rf"CONSTRAINT {name} CHECK \([^,]*?\b{column} IN \(([^)]*)\)", ddl
    )
    assert in_list, ddl
    assert set(re.findall(r"'([^']*)'", in_list.group(1))) == set(vocabulary(enum_cls))


# ---------------------------------------------------------------------------
# An unvalidated writer cannot abort on an off-vocabulary value
# ---------------------------------------------------------------------------


async def test_a_scanned_file_with_an_off_vocabulary_severity_is_recorded(
    service, session_factory, tmp_path
):
    """The disk scan records every file whatever its validation verdict, and
    copied the raw frontmatter severity into the CHECKed column — so one file
    declaring ``severity: urgent`` failed the commit and aborted the scan. It
    is recorded now, failing validation, with no severity in the column."""
    bad = valid_runbook("Connection Pool Exhausted On The API").replace(
        "severity: high", "severity: urgent"
    )
    good = valid_runbook("Replica Lag Grows Past The Threshold").replace(
        "id: sample-runbook", "id: other-runbook"
    )
    root = tmp_path / "knowledge" / "global"
    root.mkdir(parents=True)
    (root / "bad.md").write_text(bad, encoding="utf-8")
    (root / "good.md").write_text(good, encoding="utf-8")

    result = await service.scan_for_runbooks(
        "u1", STANDALONE_ENTERPRISE_ID, is_platform_admin=True
    )

    assert result["discovered"] == 2, result
    async with session_factory() as session:
        rows = {
            row.runbook_id: row
            for row in (await session.execute(select(ConversionDraftModel))).scalars()
        }
    assert rows["sample-runbook"].severity is None
    assert rows["sample-runbook"].validation_passed is False
    assert rows["other-runbook"].severity == "high"


@pytest.mark.postgres
@pytest.mark.skipif(
    not ON_POSTGRES,
    reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
)
@pytest.mark.parametrize(
    "table,name,column,enum_cls", CONSTRAINED_COLUMNS, ids=lambda x: str(x)
)
async def test_postgres_check_admits_exactly_the_vocabulary(
    migrated_postgres, table, name, column, enum_cls
):
    """The PostgreSQL half of the migrated-CHECK pin: 008 alters the
    constraint in place there, so its definition is read back from the
    catalogue rather than from DDL text."""
    from sqlalchemy import text

    engine = create_async_engine(migrated_postgres)
    try:
        async with engine.connect() as conn:
            definition = (
                await conn.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = :name"
                    ),
                    {"name": name},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert set(re.findall(r"'([^']*)'::", definition)) == set(
        vocabulary(enum_cls)
    ), definition
