"""The stored shape of the case outcome record (#2636, #2615 stage S1).

``CaseOutcome`` lives in the ``progress`` blob as ``progress.outcome``, with no
migration. Two things keep every stored case loadable:

* each enum and Literal the record stores is APPEND-ONLY. A stored case names
  members by value, so a member renamed or removed would fail every load of
  that case. ``PINNED`` holds every member in declaration order, as
  ``alembic/revision_fingerprints.txt`` holds every applied revision: a new
  member is appended here with its type, and nothing here is ever edited.
  Every such type is found by walking ``CaseOutcome``'s field annotations,
  so a new field's type is held to the pin too;
* the record round-trips through both repositories' row codecs, and a row
  stored without it loads with ``outcome`` None.
"""

import enum
import json
import typing
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.contracts import (
    AttemptOutcome,
    BindableRows,
    CaseOutcome,
    CaseState,
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

#: Every member of every enum and Literal the record stores, in declaration
#: order: an enum by its type, a Literal by the field that declares it.
#: APPEND-ONLY: a stored case names these values.
PINNED: dict = {
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
    CaseState: ["inquiry", "investigating", "resolved", "closed"],
    "AttemptOutcome.kind": ["remediation", "interception"],
    "CauseOutcome.identification_leg": ["chain", "rcc", "working_conclusion"],
}


def _stored_types(model: type, found: dict | None = None) -> dict:
    """Every Enum and Literal ``model`` stores, through every nested model,
    container and union, with its members: an Enum keyed by its type, a
    Literal by ``Model.field``."""
    found = {} if found is None else found

    def walk(annotation, where: str) -> None:
        if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
            found[annotation] = [m.value for m in annotation]
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation not in found:
                found[annotation] = None
                for name, field in annotation.model_fields.items():
                    walk(field.annotation, f"{annotation.__name__}.{name}")
        elif typing.get_origin(annotation) is typing.Literal:
            found[where] = list(typing.get_args(annotation))
        else:
            for arg in typing.get_args(annotation):
                walk(arg, where)

    walk(model, model.__name__)
    return {k: v for k, v in found.items() if v is not None}


def _unpinned(model: type) -> list[str]:
    """Each stored Enum or Literal whose members ``PINNED`` does not hold
    exactly."""
    return [
        getattr(key, "__name__", key)
        for key, members in _stored_types(model).items()
        if PINNED.get(key) != members
    ]


def test_every_type_the_record_stores_is_pinned_member_for_member():
    assert _unpinned(CaseOutcome) == [], (
        "A type the record stores changed or is new. Its members are "
        "append-only: a stored case names them by value, so renaming or "
        "removing one fails every load of that case. Append a new member at "
        "the end, here and on the type; pin a new type here."
    )
    assert set(_stored_types(CaseOutcome)) == set(PINNED), "a stale pin"


def test_the_walk_finds_a_foreign_enum_and_a_literal_in_a_nested_field():
    class _Foreign(enum.Enum):
        A = "a"

    class _Inner(BaseModel):
        state: typing.Optional[_Foreign] = None
        kinds: typing.List[typing.Literal["x", "y"]] = []

    class _Probe(BaseModel):
        inner: typing.Dict[str, _Inner] = {}
        verdict: FixWindowState = FixWindowState.UNKNOWN

    assert _stored_types(_Probe) == {
        _Foreign: ["a"],
        "_Inner.kinds": ["x", "y"],
        FixWindowState: ["unknown", "succeeded", "failed"],
    }
    assert _unpinned(_Probe) == ["_Foreign", "_Inner.kinds"]


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
        folded_under=CaseState.CLOSED,
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
        bindable=BindableRows(
            problem_row_ids=["ev_s"],
            cause_row_ids=["ev_0"],
            why="it re-checks a row the root's verdict does not cite",
        ),
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
        disagreements={"attempts.window:act_1": 9},
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


async def test_the_in_memory_progress_write_is_versioned():
    """``InMemoryCaseRepository.save_progress``: the version bump, the stale
    refusal and a case that is not stored. Written through a copy, as a load
    hands one to a caller, so the store is read back, not the argument."""
    from faultmaven.modules.case.exceptions import StaleCaseException
    from faultmaven.modules.case.infrastructure.case_repository import (
        InMemoryCaseRepository,
    )

    repo = InMemoryCaseRepository()
    case = _case()
    await repo.save(case)
    version = case.version
    stale = case.model_copy(deep=True)
    fresh = case.model_copy(deep=True)
    fresh.progress.outcome = _outcome()
    assert await repo.save_progress(fresh) is fresh
    stored = await repo.get(case.case_id)
    assert stored is not fresh
    assert (fresh.version, stored.version) == (version + 1, version + 1)
    assert stored.progress.outcome == _outcome()

    with pytest.raises(StaleCaseException) as refused:
        await repo.save_progress(stale)
    assert (refused.value.expected_version, refused.value.actual_version) == (
        version,
        version + 1,
    )
    with pytest.raises(StaleCaseException) as missing:
        await repo.save_progress(_case())
    assert missing.value.actual_version == 0
