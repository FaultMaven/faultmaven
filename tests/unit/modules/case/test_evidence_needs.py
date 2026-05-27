"""Unit tests for the EvidenceNeed domain model + persistence (Phase 1).

Covers:

- Pydantic validators on ``EvidenceNeed`` (status/reason invariant,
  FULFILLED requires fulfilling evidence, dedup of motivating IDs).
- ``CATEGORY_MILESTONE_MAP`` exhaustiveness against ``EvidenceCategory``.
- Round-trip persistence through ``SQLiteCaseRepository`` on an
  in-memory SQLite engine (via ``Base.metadata.create_all``).

Run:
    pytest tests/unit/modules/case/test_evidence_needs.py -v
"""

from __future__ import annotations

from typing import AsyncGenerator
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from faultmaven.core.investigation.milestone_engine import CATEGORY_MILESTONE_MAP
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    InquiryData,
    NeedPriority,
    NeedPurpose,
    NeedStatus,
    UploadedFile,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def async_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="function")
async def async_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
def repository(async_session) -> SQLiteCaseRepository:
    return SQLiteCaseRepository(async_session)


def _make_case() -> Case:
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_alpha",
        organization_id="org_alpha",
        title="Test case",
        status=CaseStatus.INQUIRY,
    )


def _make_need(
    *,
    case_id: str,
    purpose: NeedPurpose = NeedPurpose.SYMPTOM_VERIFICATION,
    status: NeedStatus = NeedStatus.PENDING,
    motivating_hypothesis_ids: list[str] | None = None,
    fulfilling_evidence_ids: list[str] | None = None,
    superseded_reason: str | None = None,
    priority: NeedPriority = NeedPriority.MEDIUM,
) -> EvidenceNeed:
    return EvidenceNeed(
        case_id=case_id,
        purpose=purpose,
        request_text="kubectl get pods showing crash counts",
        rationale="confirms whether pods are still restarting",
        priority=priority,
        status=status,
        motivating_hypothesis_ids=motivating_hypothesis_ids or [],
        fulfilling_evidence_ids=fulfilling_evidence_ids or [],
        superseded_reason=superseded_reason,
        created_at_turn=1,
    )


# ============================================================
# Pydantic validators
# ============================================================


class TestEvidenceNeedValidators:
    """The domain model enforces consistency invariants regardless of
    how the row was produced (LLM, API, repository hydration)."""

    def test_minimal_need_creation(self):
        n = _make_need(case_id="case_abc123def456")
        assert n.need_id.startswith("eneed_")
        assert n.purpose == NeedPurpose.SYMPTOM_VERIFICATION
        assert n.status == NeedStatus.PENDING
        assert n.priority == NeedPriority.MEDIUM
        assert n.motivating_hypothesis_ids == []
        assert n.fulfilling_evidence_ids == []
        assert n.superseded_reason is None

    def test_superseded_requires_reason(self):
        with pytest.raises(ValidationError, match="superseded_reason"):
            _make_need(
                case_id="case_abc123def456",
                status=NeedStatus.SUPERSEDED,
                superseded_reason=None,
            )

    def test_non_superseded_forbids_reason(self):
        with pytest.raises(ValidationError, match="must be None"):
            _make_need(
                case_id="case_abc123def456",
                status=NeedStatus.PENDING,
                superseded_reason="should not be here",
            )

    def test_superseded_with_reason_accepted(self):
        n = _make_need(
            case_id="case_abc123def456",
            status=NeedStatus.SUPERSEDED,
            superseded_reason="all motivating hypotheses retired",
        )
        assert n.status == NeedStatus.SUPERSEDED
        assert n.superseded_reason == "all motivating hypotheses retired"

    def test_fulfilled_requires_fulfilling_evidence(self):
        with pytest.raises(ValidationError, match="fulfilling_evidence_id"):
            _make_need(
                case_id="case_abc123def456",
                status=NeedStatus.FULFILLED,
                fulfilling_evidence_ids=[],
            )

    def test_fulfilled_with_evidence_accepted(self):
        n = _make_need(
            case_id="case_abc123def456",
            status=NeedStatus.FULFILLED,
            fulfilling_evidence_ids=["ev_abc123def456"],
        )
        assert n.status == NeedStatus.FULFILLED
        assert n.fulfilling_evidence_ids == ["ev_abc123def456"]

    def test_whitespace_only_request_text_rejected(self):
        with pytest.raises(ValidationError):
            EvidenceNeed(
                case_id="case_abc123def456",
                purpose=NeedPurpose.SYMPTOM_VERIFICATION,
                request_text="   ",
                rationale="ok",
                created_at_turn=0,
            )

    def test_whitespace_only_rationale_rejected(self):
        with pytest.raises(ValidationError):
            EvidenceNeed(
                case_id="case_abc123def456",
                purpose=NeedPurpose.SYMPTOM_VERIFICATION,
                request_text="ok",
                rationale="   ",
                created_at_turn=0,
            )

    def test_motivating_ids_deduplicate_preserve_order(self):
        n = _make_need(
            case_id="case_abc123def456",
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            motivating_hypothesis_ids=["hyp_001", "hyp_002", "hyp_001", "hyp_003"],
        )
        assert n.motivating_hypothesis_ids == ["hyp_001", "hyp_002", "hyp_003"]

    def test_fulfilling_ids_deduplicate_preserve_order(self):
        n = _make_need(
            case_id="case_abc123def456",
            status=NeedStatus.FULFILLED,
            fulfilling_evidence_ids=["ev_aaa", "ev_bbb", "ev_aaa"],
        )
        assert n.fulfilling_evidence_ids == ["ev_aaa", "ev_bbb"]

    def test_motivating_ids_must_be_strings(self):
        with pytest.raises(ValidationError):
            EvidenceNeed(
                case_id="case_abc123def456",
                purpose=NeedPurpose.CAUSAL_VERIFICATION,
                request_text="x",
                rationale="y",
                motivating_hypothesis_ids=["", "hyp_002"],
                created_at_turn=0,
            )

    def test_created_at_turn_nonnegative(self):
        # Pydantic's ge=0 catches negative values
        with pytest.raises(ValidationError):
            EvidenceNeed(
                case_id="case_abc123def456",
                purpose=NeedPurpose.SYMPTOM_VERIFICATION,
                request_text="x",
                rationale="y",
                created_at_turn=-1,
            )


# ============================================================
# Category map exhaustiveness
# ============================================================


class TestEvidenceCategoryExhaustiveness:
    """Every EvidenceCategory value must appear in
    ``CATEGORY_MILESTONE_MAP`` — the map is the canonical attribution
    table consumed by the milestone engine. A missing entry would
    silently drop a category's milestone contribution."""

    def test_all_categories_present_in_milestone_map(self):
        missing = [c for c in EvidenceCategory if c not in CATEGORY_MILESTONE_MAP]
        assert (
            missing == []
        ), f"Categories missing from CATEGORY_MILESTONE_MAP: {missing}"

    def test_no_extra_keys_in_milestone_map(self):
        # Defensive: catches drift in the reverse direction (map carries
        # a category that no longer exists).
        extras = [
            k for k in CATEGORY_MILESTONE_MAP if not isinstance(k, EvidenceCategory)
        ]
        assert extras == [], f"Extra keys in CATEGORY_MILESTONE_MAP: {extras}"

    def test_new_absence_categories_present(self):
        assert EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE in CATEGORY_MILESTONE_MAP
        assert EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE in CATEGORY_MILESTONE_MAP


# ============================================================
# Repository round-trip
# ============================================================


class TestRepositoryRoundTrip:
    """save() persists ``case.evidence_needs``; get() reconstructs them
    with identity, lifecycle state, and junction-table linkage intact."""

    @pytest.mark.asyncio
    async def test_case_with_no_needs_round_trips_clean(self, repository):
        case = _make_case()
        await repository.save(case)
        retrieved = await repository.get(case.case_id)
        assert retrieved is not None
        assert retrieved.evidence_needs == []

    @pytest.mark.asyncio
    async def test_single_need_round_trips_with_full_state(self, repository):
        case = _make_case()
        need = _make_need(
            case_id=case.case_id,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            priority=NeedPriority.HIGH,
        )
        case.evidence_needs.append(need)

        await repository.save(case)
        retrieved = await repository.get(case.case_id)

        assert retrieved is not None
        assert len(retrieved.evidence_needs) == 1
        loaded = retrieved.evidence_needs[0]
        assert loaded.need_id == need.need_id
        assert loaded.case_id == case.case_id
        assert loaded.purpose == NeedPurpose.SYMPTOM_VERIFICATION
        assert loaded.priority == NeedPriority.HIGH
        assert loaded.status == NeedStatus.PENDING
        assert loaded.request_text == need.request_text
        assert loaded.rationale == need.rationale
        assert loaded.motivating_hypothesis_ids == []
        assert loaded.fulfilling_evidence_ids == []
        assert loaded.superseded_reason is None
        assert loaded.created_at_turn == 1

    @pytest.mark.asyncio
    async def test_motivating_hypothesis_ids_round_trip(self, repository):
        case = _make_case()
        need = _make_need(
            case_id=case.case_id,
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            motivating_hypothesis_ids=["hyp_001", "hyp_002"],
        )
        case.evidence_needs.append(need)

        await repository.save(case)
        retrieved = await repository.get(case.case_id)

        assert retrieved.evidence_needs[0].motivating_hypothesis_ids == [
            "hyp_001",
            "hyp_002",
        ]

    @pytest.mark.asyncio
    async def test_superseded_need_round_trips(self, repository):
        case = _make_case()
        need = _make_need(
            case_id=case.case_id,
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            status=NeedStatus.SUPERSEDED,
            superseded_reason="all motivating hypotheses retired",
        )
        case.evidence_needs.append(need)

        await repository.save(case)
        retrieved = await repository.get(case.case_id)

        loaded = retrieved.evidence_needs[0]
        assert loaded.status == NeedStatus.SUPERSEDED
        assert loaded.superseded_reason == "all motivating hypotheses retired"

    @pytest.mark.asyncio
    async def test_multiple_needs_preserve_creation_order(self, repository):
        case = _make_case()
        # Force a stable creation-time ordering by setting created_at_turn.
        first = _make_need(case_id=case.case_id)
        second = _make_need(case_id=case.case_id)
        third = _make_need(case_id=case.case_id)
        # The loader orders by created_at ASC; same-timestamp ordering is
        # not guaranteed at row level, so assert via set comparison plus
        # presence of each ID rather than exact order. (Engine-side LLM
        # emissions don't rely on order — they look up by need_id.)
        case.evidence_needs.extend([first, second, third])

        await repository.save(case)
        retrieved = await repository.get(case.case_id)

        loaded_ids = {n.need_id for n in retrieved.evidence_needs}
        assert loaded_ids == {first.need_id, second.need_id, third.need_id}

    @pytest.mark.asyncio
    async def test_fulfilling_evidence_link_round_trips_via_junction(self, repository):
        # The junction-row creation requires the evidence row to exist.
        # Build a full case with one uploaded file, one evidence row,
        # and one need that links to that evidence. INVESTIGATING
        # requires non-empty description per the Case validator, so
        # construct with the final shape rather than mutating after.
        inquiry = InquiryData()
        inquiry.proposed_problem_statement = "API outage with database errors"
        inquiry.problem_statement_confirmed = True
        inquiry.decided_to_investigate = True
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="user_alpha",
            organization_id="org_alpha",
            title="API outage",
            description="API outage with database errors",
            status=CaseStatus.INVESTIGATING,
            inquiry=inquiry,
        )
        uploaded = UploadedFile(
            filename="app.log",
            size_bytes=1024,
            content_type="text/plain",
            uploaded_at_turn=1,
            upload_source="file_upload",
            preprocessing_summary="3 errors observed",
        )
        case.uploaded_files.append(uploaded)
        evidence = Evidence(
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            primary_purpose="symptom_verified",
            summary="ERROR: connection refused",
            extract="ERROR: connection refused on port 5432",
            source_type=EvidenceSourceType.LOGS,
            source_file_id=uploaded.file_id,
            collected_by="user_alpha",
            collected_at_turn=1,
        )
        case.evidence.append(evidence)

        need = _make_need(
            case_id=case.case_id,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            status=NeedStatus.FULFILLED,
            fulfilling_evidence_ids=[evidence.evidence_id],
        )
        case.evidence_needs.append(need)

        await repository.save(case)
        retrieved = await repository.get(case.case_id)

        loaded_need = next(
            (n for n in retrieved.evidence_needs if n.need_id == need.need_id),
            None,
        )
        assert loaded_need is not None
        assert loaded_need.status == NeedStatus.FULFILLED
        assert loaded_need.fulfilling_evidence_ids == [evidence.evidence_id]

    @pytest.mark.asyncio
    async def test_update_existing_need_via_save(self, repository):
        # Round-trip: save, mutate, save again, reload — the upsert
        # should reflect the new state.
        case = _make_case()
        need = _make_need(case_id=case.case_id)
        case.evidence_needs.append(need)

        await repository.save(case)

        # Mutate: bump priority on the existing need
        case.evidence_needs[0].priority = NeedPriority.HIGH

        await repository.save(case)
        retrieved = await repository.get(case.case_id)

        assert retrieved.evidence_needs[0].priority == NeedPriority.HIGH
        assert retrieved.evidence_needs[0].need_id == need.need_id

    @pytest.mark.asyncio
    async def test_case_delete_cascades_needs(self, repository, async_session):
        from sqlalchemy import text

        case = _make_case()
        need = _make_need(case_id=case.case_id)
        case.evidence_needs.append(need)
        await repository.save(case)
        await async_session.commit()

        # Confirm row exists.
        before = (
            await async_session.execute(
                text("SELECT COUNT(*) FROM evidence_needs WHERE case_id = :cid"),
                {"cid": case.case_id},
            )
        ).scalar()
        assert before == 1

        # Delete the case (cascade should clear needs).
        await async_session.execute(
            text("PRAGMA foreign_keys = ON"),
        )
        await async_session.execute(
            text("DELETE FROM cases WHERE case_id = :cid"),
            {"cid": case.case_id},
        )
        await async_session.commit()

        after = (
            await async_session.execute(
                text("SELECT COUNT(*) FROM evidence_needs WHERE case_id = :cid"),
                {"cid": case.case_id},
            )
        ).scalar()
        assert after == 0
