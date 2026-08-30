"""The 046 unique index, exercised through the real service.

``uq_conversion_drafts_org_runbook_id`` admits one LIVE draft per
``(organization_id, runbook_id)`` (#1230). That is a correctness win and a
liability at the same time: the mint is deterministic on ``(service, title)``,
so two ordinary user actions can reach the same id, and an unhandled
``IntegrityError`` there is a 500 that explains nothing.

Everything here runs against a real SQLAlchemy engine with the real schema —
``Base.metadata.create_all`` builds the partial unique index from the same
``Index`` declaration migration 046 creates — so the assertions are about what
the database does, not about a mock.

Four claims:

1. The index BITES through the service: a second live draft on the same key is
   refused rather than silently persisted (the #1230 defect).
2. It is refused as a ``ConflictError`` (409) naming the id, not as a bare
   ``IntegrityError`` (500).
3. The scan DEGRADES: one unmintable file is skipped with an error entry, and
   the walk over the rest completes.
4. And the case the index CANNOT see — two drafts in ONE job, where nothing is
   committed yet — is refused before either is written, as the losing failure
   mode's own error rather than as that bare ``IntegrityError`` (#1258).
"""

from __future__ import annotations

import pathlib
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.exceptions import ConflictError
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    OrganizationModel,
    UploadedFileModel,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionStatus,
    FailureModeAnalysis,
    PreprocessingResult,
    SourceAssessment,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionService,
)
from faultmaven.utils.runbook_id import RunbookPathEscape

pytestmark = pytest.mark.integration

ORG = "00000000-0000-0000-0000-000000000001"

TEMPLATE_ARGS = dict(
    domain="platform",
    symptom_class=["availability"],
    severity="high",
    scope="global",
    tags=[],
    difficulty="medium",
    symptom_recognition="Requests start failing with 503 across the fleet.",
    applicability="Any deployment of the affected service.",
    diagnostic_steps="### Step 1. Look at the logs",
    causes="### Cause A: Something\nStatement: it broke.",
    prevention="Watch the dashboard.",
    user_id="user_x",
)


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(
                enterprise_id=ORG, name="Default Enterprise", slug="default"
            )
        )
        session.add(
            OrganizationModel(
                organization_id=ORG,
                enterprise_id=ORG,
                name="Default Org",
                slug="default-org",
            )
        )
        await session.commit()
    return factory


@pytest.fixture
def service(session_factory, tmp_path, monkeypatch):
    """The real service, writing under ``tmp_path``.

    ``monkeypatch.chdir`` because ``knowledge_root()`` is deliberately
    relative; the ``_data_dir`` patch is what the sibling scan test uses.
    """
    monkeypatch.chdir(tmp_path)
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    svc = ConversionService(
        llm_router=MagicMock(),
        settings=settings,
        db_session_factory=session_factory,
    )
    with patch.object(
        type(svc), "_data_dir", new=property(lambda self: tmp_path / "data/knowledge")
    ):
        yield svc


async def _live_draft_count(session_factory, runbook_id: Optional[str] = None) -> int:
    stmt = select(func.count()).select_from(ConversionDraftModel)
    stmt = stmt.where(ConversionDraftModel.status != "discarded")
    if runbook_id is not None:
        stmt = stmt.where(ConversionDraftModel.runbook_id == runbook_id)
    async with session_factory() as session:
        return (await session.execute(stmt)).scalar_one()


class TestTheIndexBitesThroughTheService:
    async def test_the_second_draft_with_the_same_title_is_refused(
        self, service, session_factory
    ):
        """#1230's general defect, driven through the real create path.

        Before 046 this persisted TWO rows sharing one ``runbook_id``, which
        verify/approve and every id-keyed lookup could not tell apart.
        """
        first = await service.create_runbook_from_template(
            title="Connection Pool Exhausted",
            service_name="checkout-api",
            **TEMPLATE_ARGS,
        )
        runbook_id = first["draft"].runbook_id
        assert runbook_id, first

        with pytest.raises(ConflictError) as excinfo:
            await service.create_runbook_from_template(
                title="Connection Pool Exhausted",
                service_name="checkout-api",
                **TEMPLATE_ARGS,
            )

        # A 409 that names the id, not a bare IntegrityError.
        assert not isinstance(excinfo.value, IntegrityError)
        assert runbook_id in str(excinfo.value)
        assert excinfo.value.conflict_reason == "duplicate_runbook_id"
        assert excinfo.value.resource_type == "conversion_draft"

        assert await _live_draft_count(session_factory, runbook_id) == 1

    async def test_the_refused_create_does_not_clobber_the_existing_file(
        self, service, tmp_path
    ):
        """A 409 must mean nothing happened.

        The draft file is named after ``runbook_id``, so the second create
        resolves to the SAME path. Writing first and rejecting at the INSERT
        would leave the first draft's row pointing at the second author's
        content — a worse state than the duplicate rows this index removes.
        """
        first = await service.create_runbook_from_template(
            title="Connection Pool Exhausted",
            service_name="checkout-api",
            **TEMPLATE_ARGS,
        )
        path = Path(first["draft"].file_path)
        before = path.read_text(encoding="utf-8")
        assert "Connection Pool Exhausted" in before

        with pytest.raises(ConflictError):
            await service.create_runbook_from_template(
                title="Connection Pool Exhausted",
                service_name="checkout-api",
                **{**TEMPLATE_ARGS, "prevention": "A DIFFERENT PREVENTION SECTION"},
            )

        assert path.read_text(encoding="utf-8") == before
        assert "A DIFFERENT PREVENTION SECTION" not in path.read_text(encoding="utf-8")

    async def test_a_punctuation_only_title_no_longer_collides_at_all(
        self, service, session_factory
    ):
        """#1230's reproducing case, end to end.

        Two runbooks whose ``(service, title)`` filters to nothing used to
        share ``runbook_id = ''``. With the mint fixed they get distinct ids,
        so BOTH persist — the conflict path is not even reached, which is the
        point: the index is the backstop, not the fix.
        """
        a = await service.create_runbook_from_template(
            title="???", service_name="...", **TEMPLATE_ARGS
        )
        b = await service.create_runbook_from_template(
            title="!!!", service_name="___", **TEMPLATE_ARGS
        )

        assert a["draft"].runbook_id
        assert b["draft"].runbook_id
        assert a["draft"].runbook_id != b["draft"].runbook_id
        assert await _live_draft_count(session_factory) == 2

    async def test_a_discarded_draft_does_not_block_its_own_title(
        self, service, session_factory
    ):
        """The partial predicate, executed through the service.

        Discard is a SOFT delete. Without ``WHERE status <> 'discarded'`` the
        tombstone would block re-authoring the same runbook forever.
        """
        first = await service.create_runbook_from_template(
            title="Connection Pool Exhausted",
            service_name="checkout-api",
            **TEMPLATE_ARGS,
        )
        runbook_id = first["draft"].runbook_id

        async with session_factory() as session:
            row = await session.get(ConversionDraftModel, first["draft"].draft_id)
            row.status = "discarded"
            await session.commit()

        again = await service.create_runbook_from_template(
            title="Connection Pool Exhausted",
            service_name="checkout-api",
            **TEMPLATE_ARGS,
        )
        assert again["draft"].runbook_id == runbook_id
        assert await _live_draft_count(session_factory, runbook_id) == 1


class TestTheScanDegradesRatherThanAborting:
    """One file the index will not take must not kill the whole walk.

    The scan already skips a path it cannot contain; a taken id is the same
    class of problem and gets the same treatment, because a scan that aborts
    on file 3 of 90 leaves the operator with no inventory and no filename.
    """

    @staticmethod
    def _runbook(runbook_id: str, title: str) -> str:
        return f"""---
id: {runbook_id}
title: {title}
domain: database
service: redis
severity: high
tags: [redis]
---

# {title}

## Problem Definition

Redis is hitting the maxmemory limit and operations begin failing with OOM
command not allowed errors, so service availability suffers badly.

## Diagnostic Steps

### Step 1. Check current memory usage
```bash
redis-cli INFO memory | grep used_memory_human
```

## Recovery Procedure

1. Increase maxmemory if RAM is available.
2. Configure an eviction policy to age out cold keys.
"""

    async def test_a_duplicate_id_on_disk_is_skipped_and_the_rest_are_minted(
        self, service, session_factory, tmp_path
    ):
        scope_dir: Path = tmp_path / "data/knowledge/global/database"
        scope_dir.mkdir(parents=True)
        # Two files carrying the SAME frontmatter id, plus one distinct file.
        (scope_dir / "a.md").write_text(self._runbook("redis-oom", "Redis OOM A"))
        (scope_dir / "b.md").write_text(self._runbook("redis-oom", "Redis OOM B"))
        (scope_dir / "c.md").write_text(self._runbook("redis-slow", "Redis Slow"))

        result = await service.scan_for_runbooks(
            user_id=None, organization_id=None, is_platform_admin=True
        )

        minted = {d["runbook_id"] for d in result["drafts"]}
        assert "redis-oom" in minted
        # The walk continued past the refusal — this is the whole claim.
        assert "redis-slow" in minted, result
        assert result["discovered"] == 2, result
        assert any("already held" in e for e in result["errors"]), result["errors"]
        assert await _live_draft_count(session_factory, "redis-oom") == 1


class TestTheGuardCoversEveryNewDraftWritePath:
    """The clobber fix reached one write site; the review found the others.

    ``refuse_if_draft_slot_taken`` is the single guard, and these test it
    through the paths rather than by calling it — a guard wired into only one
    of two callers passes a direct-call test and fails a user.
    """

    async def test_the_guard_is_reached_before_every_new_draft_write(self):
        """Structural: every ``write_runbook_file`` on a NEW draft has the
        guard above it in the same function.

        Source-level on purpose. Driving the LLM path end to end needs a
        scripted provider and the whole analysis pipeline; what actually
        regressed was a missing CALL, and that is what this sees. ``update_draft``
        is exempt and named, so adding a third write site fails here until
        someone decides which kind it is.
        """
        import inspect

        from faultmaven.modules.knowledge.domain.services import conversion_service

        src = pathlib.Path(inspect.getfile(conversion_service)).read_text()
        # Split into top-level method bodies and check each that writes.
        methods = {}
        current = None
        for line in src.splitlines():
            if line.startswith("    def ") or line.startswith("    async def "):
                current = line.split("(")[0].split()[-1]
                methods[current] = []
            elif current is not None:
                methods[current].append(line)

        writers = {
            name: "\n".join(body)
            for name, body in methods.items()
            if "write_runbook_file(" in "\n".join(body)
        }
        assert writers, "no runbook write site found — this test has gone blind"

        # ``update_draft`` rewrites the file its OWN row already owns, so the
        # row it would conflict with is itself.
        exempt = {"update_draft"}
        unguarded = [
            name
            for name, body in writers.items()
            if name not in exempt and "refuse_if_draft_slot_taken(" not in body
        ]
        assert not unguarded, (
            f"these write a NEW draft file without checking the slot first: "
            f"{unguarded}. Either call refuse_if_draft_slot_taken before the "
            f"write, or add the method to `exempt` with the reason."
        )
        # And the exemption is not vacuous.
        assert exempt <= set(writers), (exempt, sorted(writers))

    async def test_the_guard_keys_on_the_PATH_not_only_the_id(
        self, service, session_factory
    ):
        """``draft_filename`` collapses a hyphen run, so a legacy row holding
        ``foo--bar`` owns ``foo-bar.md`` — the file a fresh ``foo-bar`` mint
        resolves to. An id-only lookup misses it and the write clobbers.

        Measured as 0 rows on the dev database, so this is a guard against a
        state that was not reproduced in the wild; it is cheap and the failure
        it prevents is silent data loss.
        """
        first = await service.create_runbook_from_template(
            title="Connection Pool Exhausted",
            service_name="checkout-api",
            **TEMPLATE_ARGS,
        )
        # Rewrite the row's id to the double-hyphen form a pre-#1243 mint could
        # have produced. Its file_path is unchanged, and that is the point.
        legacy_id = first["draft"].runbook_id.replace("-connection-", "--connection-")
        assert legacy_id != first["draft"].runbook_id
        async with session_factory() as session:
            row = await session.get(ConversionDraftModel, first["draft"].draft_id)
            row.runbook_id = legacy_id
            await session.commit()

        path = Path(first["draft"].file_path)
        before = path.read_text(encoding="utf-8")

        with pytest.raises(ConflictError):
            await service.create_runbook_from_template(
                title="Connection Pool Exhausted",
                service_name="checkout-api",
                **{**TEMPLATE_ARGS, "prevention": "SECOND AUTHOR CONTENT"},
            )
        assert path.read_text(encoding="utf-8") == before
        assert "SECOND AUTHOR CONTENT" not in before

    async def test_an_empty_runbook_id_still_classifies(self, service, session_factory):
        """The falsy filter dropped ``''`` from the lookup, so a collision on
        the one id shape #1230 reported never classified and the raw
        ``IntegrityError`` escaped every caller written to catch the typed
        refusal.

        Seeded directly: the mint cannot produce ``''`` any more, and a legacy
        row is exactly the case that matters.
        """
        async with session_factory() as session:
            session.add(
                UploadedFileModel(
                    file_id="file_legacy",
                    organization_id=ORG,
                    uploaded_by=None,
                    filename="legacy.md",
                    size_bytes=1,
                    content_type="text/markdown",
                    storage_ref="x",
                    upload_source="conversion_source",
                    uploaded_at_turn=0,
                )
            )
            await session.flush()
            session.add(
                ConversionJobModel(
                    id="conv_legacy",
                    organization_id=ORG,
                    scope="global",
                    status="completed",
                    source_file_id="file_legacy",
                    source_type="document",
                )
            )
            session.add(
                ConversionDraftModel(
                    id="draft_legacy",
                    organization_id=ORG,
                    conversion_id="conv_legacy",
                    runbook_id="",
                    title="Legacy",
                    file_path="data/knowledge/global/legacy.md",
                    status="draft",
                    source_type="document",
                )
            )
            await session.commit()

        with pytest.raises(ConflictError) as excinfo:
            await service._raise_if_runbook_id_taken(ORG, [""])
        assert excinfo.value.resource_id == "draft_legacy"
        # ... and a list of only ``None`` still short-circuits.
        await service._raise_if_runbook_id_taken(ORG, [None])


class TestTheCaseRaceStillReturnsTheWinner:
    """F2. Migration 046 made the live-case race and a runbook_id duplicate
    indistinguishable at the commit, because two replicas converting one case
    mint the same ids — so the classifier reports a runbook_id duplicate for
    what is really the race, and ``convert_from_case`` would have handed the
    loser a 409 instead of the winner's conversion.
    """

    @staticmethod
    def _handler_source() -> str:
        import inspect

        from faultmaven.modules.knowledge.domain.services.conversion_service import (
            ConversionService,
        )

        return inspect.getsource(ConversionService._convert_from_case_impl)

    def test_the_handler_catches_both_typed_failures(self):
        src = self._handler_source()
        assert "except (IntegrityError, ConflictError):" in src, (
            "the live-case race now arrives as ConflictError as often as "
            "IntegrityError; catching only one hands the loser a 409"
        )
        # It must still RE-RAISE what its own re-read cannot confirm, or a
        # genuine duplicate from a different case would be swallowed.
        assert "return existing" in src and "raise" in src

    async def test_the_classifier_really_does_fire_on_the_race_shape(
        self, service, session_factory
    ):
        """Why the catch has to be widened, executed rather than argued.

        Seed the WINNER's state — a live draft holding the id both replicas
        mint — then ask the classifier what it makes of the loser's ids. It
        raises ConflictError, which is precisely why IntegrityError alone is no
        longer the whole story.
        """
        first = await service.create_runbook_from_template(
            title="Connection Pool Exhausted",
            service_name="checkout-api",
            **TEMPLATE_ARGS,
        )
        with pytest.raises(ConflictError):
            await service._raise_if_runbook_id_taken(ORG, [first["draft"].runbook_id])


# ---------------------------------------------------------------------------
# #1258 — the INTRA-job half: two drafts in ONE conversion minting one id
# ---------------------------------------------------------------------------


class TestTwoDraftsInOneJobCannotMintOneId:
    """The half #1230 left open, driven through ``convert_document``.

    ``refuse_if_draft_slot_taken`` and ``_raise_if_runbook_id_taken`` both work
    by reading COMMITTED rows. A multi-failure-mode conversion commits nothing
    until ``_persist_job``, so two modes whose ``(service, title)`` slug
    identically were invisible to both: they wrote both runbooks to one file and
    then failed the commit with a bare ``IntegrityError`` — a 500, on a document
    the user has every reason to expect a partial result from.

    Everything here runs against the real engine and the real 046 index, so the
    ``IntegrityError`` these assertions exclude is the one the database actually
    raises.
    """

    #: A body that passes every gate in ``_convert_single_failure_mode``:
    #: >100 chars, frontmatter delimiters, and a required section heading.
    RUNBOOK = """---
id: placeholder
title: "Generated"
domain: platform
service: redis
symptom_class: [saturation]
scope: global
tags: []
difficulty: medium
severity: high
version: "1.0.0"
last_updated: "2026-01-01"
verified_by: ""
status: draft
---

# Runbook: Generated

## Symptom Recognition
Redis begins evicting keys and clients see OOM errors on every write path.

## Applicability
Any Redis deployment running with a maxmemory bound.

## Diagnostic Steps
### Step 1. Read INFO memory

## Causes
### Cause A: maxmemory is below the working set
Statement: the instance reached its configured cap.

## Prevention
Alert on the used_memory to maxmemory ratio.
"""

    @staticmethod
    def _mode(fm_id: str, title: str, symptom_class: list[str]) -> FailureModeAnalysis:
        return FailureModeAnalysis(
            id=fm_id,
            title=title,
            domain="platform",
            service="redis",
            # DIFFERENT symptom classes on purpose: the pre-existing dedup in
            # ``_convert_all_failure_modes`` keys on ``(service, symptom_class)``,
            # so identical classes would be dropped by that and this test would
            # pass without exercising the id collision at all.
            symptom_class=symptom_class,
            severity="high",
            symptoms_summary="Writes fail.",
            resolution_summary="Raise the cap.",
        )

    async def _convert(self, service, tmp_path, titles):
        """Drive the real ``convert_document`` over ``titles`` as failure modes.

        Only the two LLM calls are stubbed — preprocessing (which needs a real
        file parser) and analysis (which needs a model that emits failure-mode
        JSON). Everything the defect lives in — the batch walk, the per-mode
        conversion, the disk write, and the single ``_persist_job`` commit — is
        the production path.
        """
        modes = [
            self._mode(f"fm-{i}", t, [c])
            for i, (t, c) in enumerate(zip(titles, ["saturation", "latency", "errors"]))
        ]
        source = tmp_path / "doc.md"
        source.write_text("source material", encoding="utf-8")

        service._preprocessor.preprocess = AsyncMock(
            return_value=PreprocessingResult(
                extracted_text="source material", source_metadata={}
            )
        )
        service._llm_router.route = AsyncMock(
            return_value=SimpleNamespace(content=self.RUNBOOK, is_truncated=False)
        )
        with patch.object(
            ConversionService,
            "_analyze_document",
            AsyncMock(
                return_value=AnalysisResult(
                    is_actionable=True,
                    failure_modes=modes,
                    source_assessment=SourceAssessment(
                        content_type="doc",
                        actionability_rating="high",
                        missing_information=[],
                    ),
                )
            ),
        ):
            return await service.convert_document(
                file_path=source,
                content_type="text/markdown",
                original_filename="doc.md",
                scope="global",
                user_id="user_x",
                organization_id=ORG,
            )

    async def test_the_colliding_mode_is_refused_not_raised_as_IntegrityError(
        self, service, session_factory, tmp_path
    ):
        """The issue's own measurement: ``"Redis OOM"`` and ``"redis oom"``
        under service ``"redis"`` both mint ``redis-redis-oom``.

        Pre-fix this call raises ``sqlalchemy.exc.IntegrityError`` out of
        ``_persist_job``. The assertion is that it RETURNS, with the collision
        reported as the losing mode's own failure.
        """
        response = await self._convert(service, tmp_path, ["Redis OOM", "redis oom"])

        # One runbook, not two, and not a 500.
        assert [d.runbook_id for d in response.drafts] == ["redis-redis-oom"]
        assert response.status == ConversionStatus.PARTIAL
        assert await _live_draft_count(session_factory, "redis-redis-oom") == 1

        # The refusal names BOTH failure modes, so the user can act on it.
        assert len(response.warnings) == 1, response.warnings
        warning = response.warnings[0]
        assert "fm-0" in warning and "fm-1" in warning, warning
        assert "Redis OOM" in warning and "redis oom" in warning, warning
        assert "redis-redis-oom" in warning, warning

    async def test_the_loser_never_writes_over_the_winners_file(
        self, service, tmp_path
    ):
        """The half that is worse than the 500 and invisible without this.

        Both ids resolve to one ``draft_filename``, so pre-fix the second mode's
        content replaced the first mode's on disk BEFORE the commit failed. The
        refusal happens before any conversion runs, so exactly one file exists
        and it belongs to the surviving draft.
        """
        response = await self._convert(service, tmp_path, ["Redis OOM", "redis oom"])

        scope_dir = tmp_path / "data/knowledge/global"
        written = sorted(p.name for p in scope_dir.glob("*.md"))
        assert written == ["redis-redis-oom.md"], written
        assert Path(response.drafts[0].file_path).read_text(encoding="utf-8")

    async def test_the_non_colliding_modes_in_the_same_job_still_yield(
        self, service, session_factory, tmp_path
    ):
        """The degradation contract this path inherits, stated as a claim.

        A document analysed into three failure modes, two of which collide, must
        still produce the two distinct runbooks. Collapsing to a whole-job
        refusal would take the LLM path's per-mode degradation and make it the
        manual path's outright one.
        """
        response = await self._convert(
            service, tmp_path, ["Redis OOM", "redis oom", "Redis Slow"]
        )

        assert sorted(d.runbook_id for d in response.drafts) == [
            "redis-redis-oom",
            "redis-redis-slow",
        ]
        assert response.status == ConversionStatus.PARTIAL
        assert len(response.warnings) == 1, response.warnings
        assert await _live_draft_count(session_factory) == 2

    async def test_a_job_with_no_collision_is_unchanged(
        self, service, session_factory, tmp_path
    ):
        """The negative control. Without it every assertion above is satisfied
        by a guard that refuses everything.
        """
        response = await self._convert(service, tmp_path, ["Redis OOM", "Redis Slow"])

        assert sorted(d.runbook_id for d in response.drafts) == [
            "redis-redis-oom",
            "redis-redis-slow",
        ]
        assert response.status == ConversionStatus.COMPLETED
        assert response.warnings == []
        assert await _live_draft_count(session_factory) == 2

    async def test_the_manual_path_still_refuses_OUTRIGHT(
        self, service, session_factory, tmp_path
    ):
        """The two write paths degrade differently BY DESIGN, and #1258 must not
        collapse them.

        The LLM path turns a taken id into that one mode's ``ConversionError``
        (above). A manual create is one runbook, so refusing it IS the answer
        and the caller gets the 409 — unchanged.
        """
        await service.create_runbook_from_template(
            title="Connection Pool Exhausted",
            service_name="checkout-api",
            **TEMPLATE_ARGS,
        )
        with pytest.raises(ConflictError):
            await service.create_runbook_from_template(
                title="connection pool exhausted!",
                service_name="Checkout-API",
                **TEMPLATE_ARGS,
            )
        assert await _live_draft_count(session_factory) == 1


class TestATakenIdCostsNoGeneration:
    """The cost half of "refuse where the duplicate is produced".

    #1258 hoisted the INTRA-job check ahead of the LLM calls on the argument
    that the batch is knowable up front. The identical argument applies to a
    duplicate of an id some COMMITTED draft already holds — and that check ran
    inside the per-mode coroutine, after generation. Re-converting a document
    whose modes all already have live drafts burned one full generation per
    mode before refusing each one.
    """

    async def _seed_live_draft_for(self, service, title):
        """Create a live draft holding the id ``title`` mints, via the real
        manual-create path."""
        return await service.create_runbook_from_template(
            title=title, service_name="redis", **TEMPLATE_ARGS
        )

    async def test_no_llm_call_is_made_when_every_id_is_already_taken(
        self, service, tmp_path
    ):
        first = await self._seed_live_draft_for(service, "Redis OOM")
        taken_id = first["draft"].runbook_id

        modes = [
            FailureModeAnalysis(
                id="fm-0",
                title="Redis OOM",
                domain="platform",
                service="redis",
                symptom_class=["saturation"],
                severity="high",
                symptoms_summary="x",
                resolution_summary="y",
            )
        ]
        route = AsyncMock(
            return_value=SimpleNamespace(content="unused", is_truncated=False)
        )
        service._llm_router.route = route

        drafts, errors = await service._convert_all_failure_modes(
            "source text",
            modes,
            "global",
            "doc.md",
            "conv_probe",
            "user_x",
            None,
            organization_id=ORG,
        )

        assert drafts == []
        assert [e.failure_mode_id for e in errors] == ["fm-0"]
        assert taken_id in errors[0].error
        # The claim. Pre-fix this was 1: the generation was paid for and then
        # thrown away by refuse_if_draft_slot_taken inside the coroutine.
        assert route.await_count == 0, (
            f"a mode whose id is already held must not spend a generation; "
            f"made {route.await_count} LLM call(s)"
        )

    async def test_only_the_taken_modes_are_refused(self, service, tmp_path):
        """The negative control: a free id in the same batch still converts, so
        the pre-filter is not simply refusing everything."""
        await self._seed_live_draft_for(service, "Redis OOM")

        modes = [
            FailureModeAnalysis(
                id="fm-taken",
                title="Redis OOM",
                domain="platform",
                service="redis",
                symptom_class=["saturation"],
                severity="high",
                symptoms_summary="x",
                resolution_summary="y",
            ),
            FailureModeAnalysis(
                id="fm-free",
                title="Redis Slow",
                domain="platform",
                service="redis",
                symptom_class=["latency"],
                severity="high",
                symptoms_summary="x",
                resolution_summary="y",
            ),
        ]
        route = AsyncMock(
            return_value=SimpleNamespace(
                content=TestTwoDraftsInOneJobCannotMintOneId.RUNBOOK,
                is_truncated=False,
            )
        )
        service._llm_router.route = route

        drafts, errors = await service._convert_all_failure_modes(
            "source text",
            modes,
            "global",
            "doc.md",
            "conv_probe2",
            "user_x",
            None,
            organization_id=ORG,
        )

        assert [d.runbook_id for d in drafts] == ["redis-redis-slow"]
        assert [e.failure_mode_id for e in errors] == ["fm-taken"]
        # Exactly one generation: the free mode's. The taken one cost nothing.
        assert route.await_count == 1, route.await_count


class TestAPathEscapeIsNeverLaunderedIntoAResponse:
    """``RunbookPathEscape`` carries RESOLVED SERVER PATHS in its message.

    ``_convert_single_failure_mode`` re-raises it bare, deliberately, so it can
    never become a ``ConversionError`` — whose ``error`` string
    ``convert_document`` appends to ``response.warnings``, i.e. a 201 body
    (#866). The parallel branch dispatches with
    ``asyncio.gather(return_exceptions=True)``, which turns that re-raise into a
    RETURNED value, so the escape was being caught by the generic
    ``isinstance(result, Exception)`` arm and laundered into exactly the channel
    the re-raise exists to avoid. The sequential branch propagated it. Identical
    events behaved differently purely on how many failure modes the document had.
    """

    @staticmethod
    def _escaping_mode(fm_id, title, symptom_class):
        return FailureModeAnalysis(
            id=fm_id,
            title=title,
            domain="platform",
            service="redis",
            symptom_class=[symptom_class],
            severity="high",
            symptoms_summary="x",
            resolution_summary="y",
        )

    async def _run(self, service, n_modes):
        modes = [
            self._escaping_mode(f"fm-{i}", f"Redis Failure {i}", c)
            for i, c in enumerate(
                ["saturation", "latency", "errors", "other", "x", "y"][:n_modes]
            )
        ]
        service._llm_router.route = AsyncMock(
            return_value=SimpleNamespace(
                content=TestTwoDraftsInOneJobCannotMintOneId.RUNBOOK,
                is_truncated=False,
            )
        )
        with patch.object(
            ConversionService,
            "refuse_if_draft_slot_taken",
            AsyncMock(
                side_effect=RunbookPathEscape(
                    "/srv/secret/etc/passwd is outside /srv/kb"
                )
            ),
        ):
            return await service._convert_all_failure_modes(
                "source text",
                modes,
                "global",
                "doc.md",
                f"conv_esc_{n_modes}",
                "user_x",
                None,
                organization_id=ORG,
            )

    async def test_the_parallel_branch_propagates_instead_of_warning(self, service):
        """2 modes -> parallel branch (below PARALLEL_THRESHOLD)."""
        with pytest.raises(RunbookPathEscape):
            await self._run(service, 2)

    async def test_the_sequential_branch_propagates_too(self, service):
        """6 modes -> sequential branch. The two branches must agree."""
        with pytest.raises(RunbookPathEscape):
            await self._run(service, 6)

    async def test_the_resolved_path_never_reaches_a_conversion_error(self, service):
        """The property that actually matters, stated over the message text."""
        for n in (2, 6):
            try:
                drafts, errors = await self._run(service, n)
            except RunbookPathEscape:
                continue
            raise AssertionError(
                f"{n} modes: escape was swallowed into "
                f"{[e.error for e in errors]} instead of propagating"
            )
