"""#1880 review (#1900 F1): two cases about one failure get two drafts.

The case path mints its id from the title the model writes, not from the case
title, which names the incident. So two DIFFERENT cases about the same failure,
owned by different users, write the same title and mint the same id. The
draft slot (migration 046) is enterprise-wide, so the second conversion used to
FAIL on "A runbook draft with id ... already exists in this enterprise", and
the slot's holder could be another user's personal draft that the second user
can neither see nor discard.

``claim_case_draft_slot`` re-mints once with the case stem when the minted id
is held. Driven here through ``convert_from_case`` on a real SQLite schema,
with the knowledge model stubbed (no live LLM call):

1. two cases with one produced title give two COMPLETED drafts with distinct
   ids, neither of which carries either case title, and the second id carries
   the second case's stem;
2. the same holds for a title long enough that the stem survives only inside
   the over-length hash;
3. a re-minted id that is ALSO held is a collision within one case, and is
   refused as before, and so is a held case-stem id.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.exceptions import ConflictError
from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    EnterpriseModel,
)
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.knowledge.domain.case_authoring import (
    case_disambiguated_runbook_id,
    case_stem_runbook_id,
    mint_case_runbook_id,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    CaseConversionRequest,
    ConversionStatus,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.draft_slots import (
    claim_case_draft_slot,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.utils.runbook_id import draft_filename
from tests.runbook_samples import valid_runbook

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

ENTERPRISE_ID = "00000000-0000-0000-0000-000000001880"

#: Short enough that ``<minted>-case-case_<12 hex>`` fits the 60-character id.
SHORT_TITLE = "Redis OOM Kills"
#: The title the review measured the collision with: its minted id is 48
#: characters, so the stem only reaches the re-minted id through the hash.
LONG_TITLE = "PostgreSQL Connection Pool Exhaustion"

CASE_A = ("case_aa00000018a0", "user_a", "Checkout pool exhausted on prod-web-07")
CASE_B = ("case_bb00000018b0", "user_b", "INC-2211 billing-api timeouts for Contoso")


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(enterprise_id=ENTERPRISE_ID, name="E 1880", slug="e-1880")
        )
        await session.commit()
    yield factory
    await engine.dispose()


def _case(case_id: str, title: str):
    return SimpleNamespace(
        case_id=case_id,
        title=title,
        problem_verification=ProblemVerification(
            symptom_statement=f"{title}: requests fail", severity="HIGH"
        ),
        root_cause_conclusion=SimpleNamespace(
            root_cause="The pool is exhausted by leaked connections",
            mechanism=None,
            contributing_factors=[],
        ),
    )


def _service(session_factory, produced: str) -> ConversionService:
    router = AsyncMock()
    router.route.return_value = LLMResponse(
        content=produced,
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=100,
        response_time_ms=10,
        stop_reason=StopReason.STOP,
    )
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    return ConversionService(
        llm_router=router,
        settings=settings,
        db_session_factory=session_factory,
        knowledge_service=None,
    )


async def _convert_both(session_factory, tmp_path, produced: str):
    """Convert CASE_A then CASE_B, each as its own user, with one stubbed model
    answer. Returns each conversion's single draft."""
    drafts = []
    with patch.object(
        ConversionService,
        "_data_dir",
        new_callable=lambda: property(lambda self: tmp_path),
    ):
        for case_id, user_id, title in (CASE_A, CASE_B):
            response = await _service(session_factory, produced).convert_from_case(
                CaseConversionRequest.from_case(_case(case_id, title)),
                user_id=user_id,
                enterprise_id=ENTERPRISE_ID,
            )
            assert response.status == ConversionStatus.COMPLETED, response
            (draft,) = response.drafts
            drafts.append(draft)
    return drafts


def _assert_no_case_title_in(runbook_id: str):
    for _, _, title in (CASE_A, CASE_B):
        for word in ("checkout", "prod-web-07", "inc-2211", "billing", "contoso"):
            assert word not in runbook_id, (runbook_id, title)


async def _persisted_ids(session_factory) -> list[str]:
    async with session_factory() as session:
        rows = await session.execute(
            select(ConversionDraftModel.runbook_id).order_by(
                ConversionDraftModel.created_at
            )
        )
        return [r[0] for r in rows.all()]


async def test_two_cases_with_one_produced_title_get_two_drafts(
    session_factory, tmp_path
):
    produced = valid_runbook(SHORT_TITLE)
    first, second = await _convert_both(session_factory, tmp_path, produced)

    minted = mint_case_runbook_id(produced, CASE_A[0])
    assert first.runbook_id == minted
    assert second.runbook_id != first.runbook_id
    assert second.runbook_id == case_disambiguated_runbook_id(minted, CASE_B[0])
    assert case_stem_runbook_id(CASE_B[0]) in second.runbook_id
    for draft in (first, second):
        _assert_no_case_title_in(draft.runbook_id)
        # The file, the forced frontmatter id and the row all use the final id.
        assert f"\nid: {draft.runbook_id}\n" in draft.content
        assert draft.file_path.endswith(draft_filename(draft.runbook_id))
    assert await _persisted_ids(session_factory) == [
        first.runbook_id,
        second.runbook_id,
    ]


async def test_a_long_produced_title_is_separated_by_the_hash(
    session_factory, tmp_path
):
    first, second = await _convert_both(
        session_factory, tmp_path, valid_runbook(LONG_TITLE)
    )

    assert first.runbook_id == "postgresql-postgresql-connection-pool-exhaustion"
    assert second.runbook_id != first.runbook_id
    assert second.runbook_id == case_disambiguated_runbook_id(
        first.runbook_id, CASE_B[0]
    )
    for draft in (first, second):
        _assert_no_case_title_in(draft.runbook_id)


async def test_a_held_re_minted_id_is_still_refused(session_factory, tmp_path):
    """After both conversions, CASE_B's minted AND re-minted ids are held: a
    further claim for CASE_B is a collision within that one case."""
    produced = valid_runbook(SHORT_TITLE)
    await _convert_both(session_factory, tmp_path, produced)

    with pytest.raises(ConflictError):
        await claim_case_draft_slot(
            session_factory,
            ENTERPRISE_ID,
            mint_case_runbook_id(produced, CASE_B[0]),
            CASE_B[0],
            lambda rid: tmp_path / draft_filename(rid),
        )


async def test_a_held_case_stem_is_refused_not_re_minted(session_factory, tmp_path):
    """A draft with no usable title is named by the case stem, which only that
    case mints: a held stem is never re-minted into a second draft."""
    untitled = valid_runbook(SHORT_TITLE).replace(f"title: {SHORT_TITLE}\n", "")
    first, _ = await _convert_both(session_factory, tmp_path, untitled)
    assert first.runbook_id == case_stem_runbook_id(CASE_A[0])

    with pytest.raises(ConflictError):
        await claim_case_draft_slot(
            session_factory,
            ENTERPRISE_ID,
            case_stem_runbook_id(CASE_A[0]),
            CASE_A[0],
            lambda rid: tmp_path / draft_filename(rid),
        )
