"""A ``team_*`` file the standalone scan imports is a personal runbook (#1986).

The scan (``POST /api/v1/knowledge/scan``) runs only on a single-tenant
deployment, and single-tenant builds no team service: there are no teams. A
team's visibility is a ``resource_shares`` row tied to a real team id, and a
directory name is not one. The scan used to mint ``scope="team"`` with
``team_id=None``; verifying that draft published a ``knowledge_items`` row
labelled ``team`` that no team could see and only its owner could.

The invariant these tests pin: no runbook the scan imports carries a team scope
it cannot have, and the scan's result tells the caller when a ``team_*`` file
was imported as personal. They drive the whole flow the label travels
(scan -> draft -> verify -> published row -> what its owner reads) against a
real schema, with only the vector half of ingestion stubbed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import frontmatter
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.exceptions import ConflictError
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    KnowledgeItemModel,
    ResourceShareModel,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    runbook_scan,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from tests.runbook_samples import valid_runbook
from tests.utils import regex_only_sanitizer

pytestmark = pytest.mark.integration

OWNER = "u1"


def _note(relative: str) -> str:
    return (
        f"{relative} imported as personal: this deployment has no teams to "
        f"share it with"
    )


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
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
def knowledge(session_factory) -> KnowledgeService:
    service = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=regex_only_sanitizer(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=session_factory,
    )
    service._index_document_in_vector_store = AsyncMock(return_value=3)
    return service


@pytest.fixture
def knowledge_dir(tmp_path: Path) -> Path:
    path = tmp_path / "knowledge"
    path.mkdir()
    return path


@pytest.fixture
def conversion(session_factory, knowledge, knowledge_dir):
    """The real service over the real schema; no team service, as on the only
    deployments the scan runs on."""
    service = ConversionService(
        sanitizer=regex_only_sanitizer(),
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=session_factory,
        knowledge_service=knowledge,
    )
    with patch.object(
        type(service), "_data_dir", new=property(lambda _: knowledge_dir)
    ):
        yield service


def _place(
    root: Path, relative: str, runbook_id: str, frontmatter_scope: str = "global"
) -> Path:
    """A valid runbook at ``relative``. Its own frontmatter ``scope`` is
    independent of the directory it is placed in: the sample says ``global``."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    text = valid_runbook().replace("id: sample-runbook", f"id: {runbook_id}")
    assert "scope: global\n" in text
    path.write_text(
        text.replace("scope: global\n", f"scope: {frontmatter_scope}\n", 1),
        encoding="utf-8",
    )
    return path


async def _scan(conversion: ConversionService, *, admin: bool = False) -> dict:
    return await conversion.scan_for_runbooks(
        user_id=OWNER, enterprise_id=STANDALONE_ENTERPRISE_ID, is_platform_admin=admin
    )


async def _job_of(session_factory, draft_id: str) -> ConversionJobModel:
    async with session_factory() as session:
        draft = await session.get(ConversionDraftModel, draft_id)
        return await session.get(ConversionJobModel, draft.conversion_id)


class TestTeamDirectoryFileIsImportedAsPersonal:
    async def test_the_draft_is_personal_and_the_result_says_so(
        self, conversion, session_factory, knowledge_dir
    ):
        _place(knowledge_dir, "team_x/foo.md", "team-foo")

        result = await _scan(conversion)

        assert result["discovered"] == 1
        assert result["errors"] == []
        assert result["notes"] == [_note("team_x/foo.md")]
        [draft] = result["drafts"]
        assert draft["scope"] == "personal"
        job = await _job_of(session_factory, draft["draft_id"])
        assert job.scope == "personal"
        async with session_factory() as session:
            shares = (await session.execute(select(ResourceShareModel))).scalars().all()
        # No team to share with: the scan does not invent one.
        assert shares == []

    async def test_a_nested_file_is_named_by_its_path_under_the_tree(
        self, conversion, knowledge_dir
    ):
        _place(knowledge_dir, "team_x/database/foo.md", "team-nested")

        result = await _scan(conversion)

        assert result["notes"] == [_note("team_x/database/foo.md")]

    async def test_verify_publishes_a_personal_row_its_owner_sees(
        self, conversion, session_factory, knowledge, knowledge_dir
    ):
        _place(knowledge_dir, "team_x/foo.md", "team-foo")
        result = await _scan(conversion)
        [draft] = result["drafts"]
        job = await _job_of(session_factory, draft["draft_id"])

        verified = await conversion.verify_draft(
            job.id, draft["draft_id"], user_id=OWNER
        )

        assert verified.status == "verified"
        async with session_factory() as session:
            item = (
                await session.execute(
                    select(KnowledgeItemModel).where(
                        KnowledgeItemModel.item_id == verified.knowledge_item_id
                    )
                )
            ).scalar_one()
            shares = (await session.execute(select(ResourceShareModel))).scalars().all()
        # The row's label is true: personal, owned by the importer, shared with
        # nobody (there is nobody it could be shared with).
        assert item.scope == "personal"
        assert item.owner_id == OWNER
        assert shares == []
        # And what the owner reads agrees with the label.
        owner = SimpleNamespace(user_id=OWNER, enterprise_id=STANDALONE_ENTERPRISE_ID)
        listed = await knowledge.list_documents(user=owner)
        mine = [d for d in listed["documents"] if d["document_id"] == item.item_id]
        assert [d["scope"] for d in mine] == ["personal"]
        assert await knowledge.get_document_visible(item.item_id, user=owner)
        assert listed["scope_counts"]["team"] == 0

    async def test_a_second_scan_changes_nothing_and_notes_nothing(
        self, conversion, knowledge_dir
    ):
        """A note describes an import. A file already imported is skipped, so
        the second scan has no import to describe."""
        _place(knowledge_dir, "team_x/foo.md", "team-foo")
        await _scan(conversion)

        second = await _scan(conversion)

        assert (second["discovered"], second["skipped"]) == (0, 1)
        assert second["notes"] == []

    async def test_a_team_file_that_is_not_imported_is_an_error_not_a_note(
        self, conversion, knowledge_dir
    ):
        (knowledge_dir / "team_x").mkdir()
        (knowledge_dir / "team_x" / "short.md").write_text(
            "too short", encoding="utf-8"
        )

        result = await _scan(conversion)

        assert result["discovered"] == 0
        assert result["notes"] == []
        assert len(result["errors"]) == 1
        assert result["errors"][0].startswith("short.md: too short")

    async def test_a_team_file_whose_id_is_taken_is_an_error_not_a_note(
        self, conversion, knowledge_dir
    ):
        """The id is claimed between the walk's read and the mint: the file is
        not imported, so there is no imported-as-personal to tell the caller."""
        _place(knowledge_dir, "team_x/foo.md", "team-foo")
        taken = AsyncMock(side_effect=ConflictError("the id is taken"))

        with patch.object(runbook_scan, "_persist_job", taken):
            result = await _scan(conversion)

        taken.assert_awaited_once()
        assert result["discovered"] == 0
        assert result["drafts"] == []
        assert result["notes"] == []
        assert len(result["errors"]) == 1
        assert result["errors"][0].startswith("foo.md: runbook id 'team-foo'")

    async def test_several_files_each_get_their_own_note(
        self, conversion, knowledge_dir
    ):
        _place(knowledge_dir, "team_x/a.md", "team-a")
        _place(knowledge_dir, "team_y/b.md", "team-b")

        result = await _scan(conversion)

        assert result["discovered"] == 2
        assert sorted(result["notes"]) == [_note("team_x/a.md"), _note("team_y/b.md")]
        assert {d["scope"] for d in result["drafts"]} == {"personal"}


async def _published(
    conversion: ConversionService, session_factory, relative: str, **verify_kwargs
):
    """Scan the one file placed at ``relative``, verify its draft, and return
    ``(published item row, verified draft row)``."""
    result = await _scan(
        conversion, admin=verify_kwargs.get("is_platform_admin", False)
    )
    [draft] = result["drafts"]
    job = await _job_of(session_factory, draft["draft_id"])
    verified = await conversion.verify_draft(
        job.id, draft["draft_id"], user_id=OWNER, **verify_kwargs
    )
    assert verified.status == "verified"
    async with session_factory() as session:
        item = (
            await session.execute(
                select(KnowledgeItemModel).where(
                    KnowledgeItemModel.item_id == verified.knowledge_item_id
                )
            )
        ).scalar_one()
        row = await session.get(ConversionDraftModel, draft["draft_id"])
    return item, row


class TestThePublishedTextNamesTheTierItWasPublishedAt:
    """``scope`` in a runbook's frontmatter is the tier its knowledge item is
    published at (``taxonomy.KnowledgeScope``). The tier is the job's, so verify
    writes it into the stored text; a file's own claim does not survive."""

    @pytest.mark.parametrize(
        ("relative", "file_says", "published_at", "admin"),
        [
            ("team_x/foo.md", "team", "personal", False),
            ("personal_u1/foo.md", "global", "personal", False),
            ("user_u1/foo.md", "team", "personal", False),
            ("global/foo.md", "personal", "global", True),
        ],
        ids=["team-dir", "personal-dir-claims-global", "user-dir", "global-dir"],
    )
    async def test_verify_writes_the_published_tier_into_the_text(
        self,
        relative,
        file_says,
        published_at,
        admin,
        conversion,
        session_factory,
        knowledge_dir,
    ):
        _place(knowledge_dir, relative, "scoped-foo", frontmatter_scope=file_says)

        item, row = await _published(
            conversion, session_factory, relative, is_platform_admin=admin
        )

        assert item.scope == published_at
        assert frontmatter.loads(item.content).metadata["scope"] == published_at
        # The draft row holds the same text as its item (#1950).
        assert row.content == item.content

    async def test_a_draft_whose_frontmatter_already_agrees_verifies_as_before(
        self, conversion, session_factory, knowledge_dir
    ):
        """Verify's text is the draft's, re-read with ``status`` and
        ``verified_by`` set. A draft that already names its tier gains nothing
        else: the expected text is computed here without any ``scope`` write."""
        path = _place(
            knowledge_dir,
            "personal_u1/foo.md",
            "agree-foo",
            frontmatter_scope="personal",
        )
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
        post.metadata["status"] = "verified"
        post.metadata["verified_by"] = ""
        expected = frontmatter.dumps(post)

        item, row = await _published(conversion, session_factory, "personal_u1/foo.md")

        assert item.content == expected
        assert row.content == expected


class TestOtherDirectoriesAreUnchanged:
    @pytest.mark.parametrize("directory", ["personal_u1", "user_u1"])
    async def test_a_personal_directory_is_personal_with_no_note(
        self, directory, conversion, session_factory, knowledge_dir
    ):
        _place(knowledge_dir, f"{directory}/foo.md", "personal-foo")

        result = await _scan(conversion)

        assert result["notes"] == []
        [draft] = result["drafts"]
        assert draft["scope"] == "personal"
        assert (await _job_of(session_factory, draft["draft_id"])).scope == "personal"

    async def test_a_global_directory_is_global_for_an_operator_with_no_note(
        self, conversion, session_factory, knowledge_dir
    ):
        _place(knowledge_dir, "global/foo.md", "global-foo")

        result = await _scan(conversion, admin=True)

        assert result["notes"] == []
        [draft] = result["drafts"]
        assert draft["scope"] == "global"
        assert (await _job_of(session_factory, draft["draft_id"])).scope == "global"

    async def test_a_global_file_is_still_skipped_for_a_non_operator(
        self, conversion, knowledge_dir
    ):
        _place(knowledge_dir, "global/foo.md", "global-foo")
        _place(knowledge_dir, "team_x/bar.md", "team-bar")

        result = await _scan(conversion, admin=False)

        assert (result["discovered"], result["skipped"]) == (1, 1)
        assert result["notes"] == [_note("team_x/bar.md")]

    async def test_a_missing_directory_returns_the_same_shape(self, conversion):
        with patch.object(
            type(conversion),
            "_data_dir",
            new=property(lambda _: Path("/nonexistent/knowledge-1986")),
        ):
            result = await _scan(conversion)

        assert result == {
            "discovered": 0,
            "skipped": 0,
            "errors": [],
            "notes": [],
            "drafts": [],
        }
