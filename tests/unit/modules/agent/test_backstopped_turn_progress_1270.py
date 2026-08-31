"""A backstopped turn reports the score the backstop actually took (#1270).

Three routes answer without reaching the engine's Step 6 turn bookkeeping —
GREETING, FILE_RECLASSIFICATION, and the engine's terminal short-circuit — and
all three hand back a metadata dict carrying a hardcoded ``progress_made:
False`` alongside the turn's upload keys. ``_backfill_consumed_turn`` (#1264)
then scores that dict with ``check_if_progress_made``, records a
``TurnProgress`` from the honest reading and resets
``turns_without_progress`` — but the score was never written BACK onto the
dict, so the three surfaces that report it disagreed about the same turn:

* ``TurnProgress.progress_made`` and the stall counter: the honest reading
* the emitted #1142 row and ``TurnResponse.progress_made``: the hardcoded False

which on an upload turn produces the row shape #1270 is about —
``arms.novel_files_uploaded: 1`` beside ``progress_made: false``, with
``turns_without_progress: 0`` in the same row.

The GREETING handler's comment justified the hardcoded flag as "self-consistent
— the flag says False and the counter is unchanged, which agree". That was true
when it was written and #1264 invalidated it: the counter IS touched now.
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from faultmaven.core.investigation.case_telemetry import (
    TELEMETRY_LOGGER_NAME,
    collect_progress_arms,
)
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.models.api import DataType
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
    _backfill_consumed_turn,
)
from faultmaven.modules.case.domain.models import CaseState

pytestmark = pytest.mark.unit

CONTENT = b"2026-08-28T10:00:00Z ERROR pod restart loop\n"
CONTENT_HASH = "e" * 64
#: Non-zero on purpose: a counter asserted at its default proves nothing.
STANDING_STALL = 4


class _PreprocessingDouble:
    async def classify_and_extract(self, content, filename, source_metadata=None):
        return SimpleNamespace(
            summary="Pod restart loop.",
            structural_index="ERROR x 42",
            detailed_data_type=DataType.LOGS_AND_ERRORS,
            content_hash=CONTENT_HASH,
            coverage_start_ts=None,
            coverage_end_ts=None,
            coverage_source=None,
            extraction_method="structure_extraction",
            extraction_metadata={},
        )


@pytest.fixture
def repo(mock_case_repository):
    mock_case_repository.find_uploaded_file_by_content_hash = AsyncMock(
        return_value=None
    )
    return mock_case_repository


@pytest.fixture
def engine():
    """The engine's TERMINAL short-circuit shape: no telemetry handoff, a
    hardcoded ``progress_made: False``, and the turn's upload keys — merged onto
    the working dict above the path fork, so they are there even though the
    short-circuit returns before Step 6.

    ``create_autospec`` rather than a bare ``Mock``: a bare Mock advertises
    ``(*args, **kwargs)`` and would accept a call shape the real engine rejects,
    which makes every assertion below unfailable.
    """
    double = create_autospec(MilestoneEngine, instance=True)
    double.llm_provider = MagicMock()

    async def terminal_shaped(
        *,
        case,
        user_message: str,
        attachments: Optional[list] = None,
        intent_type: Optional[str] = None,
        intent_data: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        case.updated_at = datetime.now(timezone.utc)
        return {
            "case_updated": case,
            "agent_response": "that case is closed",
            "metadata": {
                "milestones_completed": [],
                "progress_made": False,
                "files_uploaded": ["file_0123456789ab"],
                "novel_files_uploaded": ["file_0123456789ab"],
            },
        }

    double.process_turn = AsyncMock(side_effect=terminal_shaped)
    return double


@pytest.fixture
def service(engine, repo):
    svc = InvestigationService(milestone_engine=engine, case_repository=repo)
    svc.preprocessing_service = _PreprocessingDouble()
    return svc


@pytest.fixture
def case(sample_case, sample_user_id):
    sample_case.user_id = sample_user_id
    sample_case.inquiry.proposed_problem_statement = "etcd connectivity"
    sample_case.inquiry.problem_statement_confirmed = True
    sample_case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    sample_case.inquiry.decided_to_investigate = True
    sample_case.inquiry.decision_made_at = datetime.now(timezone.utc)
    sample_case.state = CaseState.INVESTIGATING
    sample_case.turns_without_progress = STANDING_STALL
    return sample_case


def _rows(caplog):
    return [r for r in caplog.records if r.name == TELEMETRY_LOGGER_NAME]


@pytest.mark.asyncio
async def test_a_backstopped_upload_turn_agrees_with_itself(
    service, repo, case, caplog
):
    """Every surface reporting the turn's progress reports the same decision."""
    await repo.save(case)
    with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
        response = await service.process_turn(
            case_id=case.case_id,
            user_id=case.user_id,
            payload=TurnPayload(
                query="any update?",
                attachments=[
                    Attachment(
                        content=CONTENT,
                        filename="app.log",
                        content_type="text/plain",
                    )
                ],
            ),
        )

    rows = _rows(caplog)
    assert (
        len(rows) == 1
    ), f"denominator: expected one row for one turn, got {len(rows)}"
    row = rows[0]
    recorded = case.turn_history[-1]

    # Positive controls: the backstop fired on a turn that really did carry a
    # novel upload, or nothing below is being tested.
    assert row.arms["novel_files_uploaded"] == 1
    assert recorded.turn_number == case.current_turn
    assert recorded.progress_made is True, (
        "the #1264 backstop scores the same dict with check_if_progress_made; "
        "a novel upload is one of its arms"
    )

    assert case.turns_without_progress == 0
    assert row.progress_made is True, (
        f"row reports progress_made={row.progress_made} while the turn record "
        f"it was emitted beside says {recorded.progress_made} and the counter "
        f"it carries reads {row.turns_without_progress}"
    )
    assert response.progress_made is True


@pytest.mark.asyncio
async def test_no_backstopped_row_carries_a_fired_arm_beside_progress_false(
    service, repo, case, caplog
):
    """The same arm-generic invariant the engine path is held to.

    Named by no arm: a row carrying ANY fired arm alongside
    ``progress_made: false`` is self-contradictory, whichever arm it is.

    **The denominator is asserted before the universal.** "No emitted row
    violates X" is trivially true when NO row was emitted — a wrong logger
    name, telemetry disabled in the test config, or a turn that never reaches
    the emit site all produce that, and none of them error. So the row count is
    pinned at exactly one (one turn was driven), and at least one arm must have
    fired, before the verdict is checked.
    """
    await repo.save(case)
    with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
        await service.process_turn(
            case_id=case.case_id,
            user_id=case.user_id,
            payload=TurnPayload(
                query="any update?",
                attachments=[
                    Attachment(
                        content=CONTENT,
                        filename="app.log",
                        content_type="text/plain",
                    )
                ],
            ),
        )

    rows = _rows(caplog)
    assert len(rows) == 1, (
        f"denominator: one turn was driven, so exactly one row must exist for "
        f"the invariant to quantify over; got {len(rows)}"
    )
    row = rows[0]
    fired = {k: v for k, v in row.arms.items() if v}
    assert fired, (
        "denominator: no arm fired on this row, so the universal has nothing "
        "to quantify over and would pass on unfixed code"
    )
    assert (
        row.progress_made is True
    ), f"row claims progress_made=false while these arms fired: {fired}"


def test_the_backfill_writes_its_score_back_onto_the_dict(sample_case):
    """The unit seam, covering all three backstopped routes at once.

    GREETING and FILE_RECLASSIFICATION build their own metadata dicts in the
    service; the terminal short-circuit returns the engine's. All three reach
    the same backfill, so pinning the write here covers routes added later too.
    """
    sample_case.current_turn = 7
    sample_case.turn_history = []
    sample_case.turns_without_progress = STANDING_STALL
    metadata: dict[str, Any] = {
        "progress_made": False,
        "milestones_completed": [],
        "files_uploaded": ["file_0123456789ab"],
        "novel_files_uploaded": ["file_0123456789ab"],
    }

    _backfill_consumed_turn(
        sample_case,
        user_message="here is the log",
        agent_response="ack",
        metadata=metadata,
    )

    assert sample_case.turn_history[-1].progress_made is True
    assert sample_case.turns_without_progress == 0
    assert metadata["progress_made"] is True
    assert collect_progress_arms(metadata)["novel_files_uploaded"] == 1


def test_the_backfill_does_not_take_back_a_progress_true(sample_case):
    """Monotone, like the engine's own scorer — and NOT an endorsement.

    ``check_if_progress_made`` reads the arms, never the ``progress_made`` key,
    so an unguarded write-back would clobber a ``True`` a caller had already
    decided on. That is the rule being pinned.

    **What is deliberately NOT pinned here:** that a caller-supplied ``True``
    with every arm zero is a legitimate turn. It is not — it is the mirror of
    the shape ``docs/reference/case-telemetry-stream.md`` calls unemittable
    (``progress_made: true`` with every arm 0 reads as a lying counter). This
    asserts only that the DECISION survives the write-back, so the invariant can
    be enforced at this seam later without deleting the test. The consequences
    of an unsupported ``True`` — the ``TurnProgress`` it records and the counter
    reset it triggers — are left unasserted on purpose. That omission is
    deliberate, not an oversight: the counter consequence of a SUPPORTED
    reading is asserted in ``test_the_backfill_writes_its_score_back_onto_the
    _dict``, which drives a real novel upload and pins
    ``turns_without_progress == 0``. Covered where it is legitimate, left open
    where it is not.

    Unreachable at this seam today, and pinned as such by
    ``test_no_backstopped_route_seeds_an_unsupported_progress_true``: all three
    routes seed ``False``.
    """
    sample_case.current_turn = 3
    sample_case.turn_history = []
    metadata: dict[str, Any] = {"progress_made": True, "milestones_completed": []}

    # Positive control: the arms alone say False, so a non-monotone write-back
    # would visibly clobber and this test would not be measuring monotonicity.
    from faultmaven.core.investigation.milestone_engine import check_if_progress_made

    assert check_if_progress_made(metadata) is False

    _backfill_consumed_turn(
        sample_case,
        user_message="hello",
        agent_response="hi",
        metadata=metadata,
    )

    assert metadata["progress_made"] is True


def test_no_backstopped_route_seeds_an_unsupported_progress_true():
    """The reason the monotone arm above cannot fire in production.

    Monotonicity means the backfill honours a caller-supplied ``True``. If a
    route ever seeded one with no arm behind it, the result would be a
    ``TurnProgress(progress_made=True)``, a counter reset, and a row with every
    arm 0 — the lying-counter shape. None do: all three engine-bypassing routes
    hand over ``progress_made: False`` and let the backfill decide.

    Read off the source of the handlers themselves rather than a list written
    here, so a fourth route that seeds ``True`` fails this rather than shipping.
    """
    import inspect
    import re

    from faultmaven.modules.agent.domain.services.investigation_service import (
        InvestigationService,
    )

    src = inspect.getsource(InvestigationService)
    seeds = re.findall(r'"progress_made":\s*(True|False)', src)
    assert seeds, "denominator: no ``progress_made`` seed found in the service at all"
    assert set(seeds) == {"False"}, (
        f"a service route seeds progress_made={set(seeds)}; a True with no arm "
        "behind it becomes a counter reset and an all-zero-arms row"
    )


def test_an_already_recorded_turn_is_left_alone(sample_case):
    """The backfill is a no-op on every route that reached Step 6.

    Its early return is what keeps this write off the engine paths, where
    ``progress_made`` is already the engine's authoritative reading.
    """
    from faultmaven.core.investigation.turn_outcome import TurnOutcome
    from faultmaven.modules.case.domain.models import TurnProgress

    sample_case.current_turn = 2
    sample_case.turn_history = [
        TurnProgress(
            turn_number=1,
            timestamp=datetime.now(timezone.utc),
            progress_made=False,
            outcome=TurnOutcome.CONVERSATION,
        ),
        TurnProgress(
            turn_number=2,
            timestamp=datetime.now(timezone.utc),
            progress_made=False,
            outcome=TurnOutcome.CONVERSATION,
        ),
    ]
    sample_case.turns_without_progress = STANDING_STALL
    metadata: dict[str, Any] = {
        "progress_made": False,
        "novel_files_uploaded": ["file_0123456789ab"],
    }

    _backfill_consumed_turn(
        sample_case,
        user_message="x",
        agent_response="y",
        metadata=metadata,
    )

    assert len(sample_case.turn_history) == 2
    assert metadata["progress_made"] is False
    assert sample_case.turns_without_progress == STANDING_STALL
