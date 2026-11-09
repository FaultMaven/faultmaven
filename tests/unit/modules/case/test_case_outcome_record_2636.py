"""The stored shape of the case outcome record (#2636, #2615 stage S1).

``CaseOutcome`` lives in the ``progress`` blob as ``progress.outcome``, with no
migration. Two things keep every stored case loadable:

* each enum the record stores is APPEND-ONLY. A stored case names members by
  value, so a member renamed or removed would fail every load of that case.
  ``PINNED`` holds every member in declaration order, as
  ``alembic/revision_fingerprints.txt`` holds every applied revision: a new
  member is appended here with its enum, and nothing here is ever edited;
* the record round-trips through both repositories' row codecs, and a row
  stored without it loads with ``outcome`` None.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.contracts import (
    AttemptOutcome,
    CaseOutcome,
    CauseOutcome,
    ClosureEligibility,
    ClosureOutcome,
    ClosureReason,
    ConfirmationGap,
    DisconfirmationPair,
    FixWindowRecord,
    FixWindowState,
    MitigationOutcome,
    MitigationRefusalReason,
    PoolEmptyReason,
    ProblemOutcome,
    ProblemStatus,
    ResolutionOutcome,
    ResolutionReason,
    ResolutionVerdict,
    UnboundRecheck,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.loading import (
    _row_to_case,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.repository import (
    PostgreSQLHybridCaseRepository,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.rows import (
    _case_record_params,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
    SQLiteCaseRepository,
)
from tests.case_builders import make_case

pytestmark = pytest.mark.unit

#: Every member of every enum the record stores, in declaration order.
#: APPEND-ONLY: a stored case names these values.
PINNED = {
    FixWindowState: ["unknown", "succeeded", "failed"],
    PoolEmptyReason: [
        "held_open",
        "identification_contested",
        "fallen_with_other_standing",
        "fix_targets_other_cause",
        "rootless_validated_only",
        "no_cause",
    ],
    ResolutionVerdict: ["ready", "needs_info", "suggest_close"],
    ResolutionReason: [
        "false_alarm",
        "pending_check",
        "confirmed",
        "nothing_on_record",
        "fix_off_cause",
        "fix_failed",
        "documentation_gaps",
    ],
    ConfirmationGap: [
        "relief_without_cause_recheck",
        "problem_unrechecked",
        "no_symptom_row",
    ],
    MitigationRefusalReason: [
        "attempt_undated",
        "revision_pending",
        "problem_unverified",
        "no_bound_relief",
    ],
    ClosureEligibility: ["has_substance", "trivial", "suggest_resolve"],
    # Defined elsewhere, and stored in the record too.
    ProblemStatus: ["unverified", "verified", "revision_pending", "invalidated"],
    ClosureReason: [
        "inquiry_only",
        "closed_false_alarm",
        "resolution_declined",
        "solution_deferred",
        "closed_rca_infeasible",
        "mitigation_sufficient",
        "closed_restatement_held",
        "closed_insufficient_evidence",
    ],
}


@pytest.mark.parametrize("enum", list(PINNED), ids=lambda e: e.__name__)
def test_every_stored_enum_member_is_pinned(enum):
    assert [m.value for m in enum] == PINNED[enum], (
        f"{enum.__name__} changed. Its members are append-only: a stored case "
        "names them by value, so renaming or removing one fails every load of "
        "that case. Append a new member at the end, here and on the enum."
    )


def test_the_pin_covers_every_enum_the_record_declares():
    from faultmaven.modules.case.domain.models import outcome

    declared = {
        value
        for value in vars(outcome).values()
        if isinstance(value, type)
        and issubclass(value, outcome.StrEnum)
        and value is not outcome.StrEnum
        and value.__module__ == outcome.__name__
    }
    assert declared <= set(PINNED)


def test_a_reason_is_required_exactly_when_the_pool_is_empty():
    with pytest.raises(ValidationError, match="pool_empty_reason is required"):
        CauseOutcome(confirmation_root_ids=[])
    with pytest.raises(ValidationError, match="pool_empty_reason is required"):
        CauseOutcome(
            confirmation_root_ids=["cn_1"], pool_empty_reason=PoolEmptyReason.NO_CAUSE
        )
    assert CauseOutcome(confirmation_root_ids=["cn_1"]).pool_empty_reason is None


def test_the_resolution_reason_has_no_default():
    with pytest.raises(ValidationError):
        ResolutionOutcome(verdict=ResolutionVerdict.READY)


def _outcome() -> CaseOutcome:
    """A record with every section filled, enums and all."""
    window = FixWindowRecord(
        fix_turn=6,
        state=FixWindowState.FAILED,
        relief_turn=8,
        failure_turn=9,
        cause_row_ids=["ev_1"],
        cited_basis={"cn_1": ["ev_0"]},
        disconfirmation=DisconfirmationPair(
            cause_row_id="ev_1", problem_row_id="ev_2", cause_turn=7, problem_turn=9
        ),
        read_under="checkout returns 503",
    )
    return CaseOutcome(
        problem=ProblemOutcome(
            status=ProblemStatus.VERIFIED,
            verifying_row_ids=["ev_s"],
            statement="checkout returns 503",
        ),
        cause=CauseOutcome(
            fallen_root_ids=["cn_1"],
            cited_basis={"cn_1": ["ev_0"]},
            pool_empty_reason=PoolEmptyReason.FALLEN_WITH_OTHER_STANDING,
        ),
        attempts=[
            AttemptOutcome(window=window),
            AttemptOutcome(
                action_id="act_1",
                kind="remediation",
                target_ids=["cn_1"],
                accepted_in_turn=6,
                window=window,
            ),
        ],
        resolution=ResolutionOutcome(
            verdict=ResolutionVerdict.NEEDS_INFO,
            reason=ResolutionReason.DOCUMENTATION_GAPS,
            missing=["solution"],
            confirmation_gaps=[
                ConfirmationGap.PROBLEM_UNRECHECKED,
                ConfirmationGap.NO_SYMPTOM_ROW,
            ],
        ),
        unbound_rechecks=[
            UnboundRecheck(evidence_id="ev_3", observed_turn=7, uncited=True)
        ],
        mitigation=MitigationOutcome(
            attempt_turn=5,
            span_seconds=7200.0,
            disposition="declared_stable",
            refusal_reason=MitigationRefusalReason.NO_BOUND_RELIEF,
        ),
        milestones_shown=["symptom_verified"],
        closure=ClosureOutcome(
            eligibility=ClosureEligibility.HAS_SUBSTANCE,
            reason=ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE,
            statement="a mitigation was applied (turn 5)",
        ),
    )


def _case(**fields):
    return make_case(user_id="user_a", enterprise_id="ent_a", title="t", **fields)


@pytest.fixture
async def repository():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield SQLiteCaseRepository(session)
    await engine.dispose()


async def test_the_record_round_trips_through_sqlite(repository):
    case = _case()
    case.progress.outcome = _outcome()
    await repository.save(case)
    assert (await repository.get(case.case_id)).progress.outcome == _outcome()


async def test_a_sqlite_case_without_the_record_loads_with_none(repository):
    case = _case()
    await repository.save(case)
    assert (await repository.get(case.case_id)).progress.outcome is None


def _pg_row(progress) -> SimpleNamespace:
    """A stored PostgreSQL row for ``_row_to_case``: every JSON column empty
    but ``progress``."""
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_a",
        driver_id=None,
        enterprise_id="ent_a",
        organization_id=None,
        source="copilot",
        title="t",
        description="",
        state="inquiry",
        closure_reason=None,
        disposition_eligibility=None,
        investigation_strategy=None,
        current_turn=3,
        turns_without_progress=0,
        inquiry=None,
        problem_verification=None,
        working_conclusion=None,
        root_cause_conclusion=None,
        escalation_state=None,
        documentation=None,
        progress=progress,
        hypotheses_data=None,
        solutions_data=None,
        uploaded_files_data=None,
        messages_data=None,
        metadata=json.dumps({}),
        created_at=now,
        updated_at=now,
        version=1,
        last_activity_at=None,
        resolved_at=None,
        closed_at=None,
    )


async def _load_pg(progress):
    session = MagicMock()
    session.bind.dialect.name = "postgresql"
    repo = PostgreSQLHybridCaseRepository(session)
    with patch(
        "faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository"
        ".loading._load_case_actions",
        AsyncMock(return_value=[]),
    ):
        return await _row_to_case(repo.db, _pg_row(progress))


async def test_the_record_round_trips_through_the_postgresql_codecs():
    case = _case()
    case.progress.outcome = _outcome()
    params = _case_record_params(case, datetime.now(timezone.utc))
    loaded = await _load_pg(params["progress"])
    assert loaded.progress.outcome == _outcome()


async def test_a_postgresql_row_without_the_record_loads_with_none():
    params = _case_record_params(_case(), datetime.now(timezone.utc))
    stored = json.loads(params["progress"])
    del stored["outcome"]
    loaded = await _load_pg(json.dumps(stored))
    assert loaded.progress.outcome is None
