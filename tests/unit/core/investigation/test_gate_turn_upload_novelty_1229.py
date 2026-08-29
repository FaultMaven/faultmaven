"""#1229 — the turn's uploads must be reported on EVERY path, not just generation.

#1224 made ``novel_files_uploaded`` live, but derived it inside
``_process_response_structured`` — the generation path. The deterministic
early-return branches of ``_process_turn_impl`` (pending resolve/close gates,
the status-transition dropdown handlers) build their own metadata dict and
returned before that block ever ran, so a genuinely new file arriving on a gate
turn was invisible: no ``files_uploaded``, no ``novel_files_uploaded``, and
``progress_made: False`` on the API response. The two #1224 degradation
warnings lived in the same unreachable block, so the degradation was
unobservable there as well.

**The gate-semantics answer pinned here: a gate turn DOES count upload
progress.** ``_check_if_progress_made`` defines progress as *advancement, not
activity* — "an artifact the case did not already hold" — and a file that
survived content-hash dedup is exactly that. Whether the user accepted a
mitigation is orthogonal to whether new data arrived.

**And the accounting is one-directional.** ``test_the_freeze_on_the_increment_
side_is_real`` below is the measurement behind that: a deterministic branch
returns above Step 5.8, so it never reached the ``turns_without_progress += 1``
either. The counter was FROZEN on these paths, not advanced — the issue
reported it as incrementing, and it does not. So a novel upload resets it and
nothing here increments it; both arms err against a stall net firing on a turn
the engine did no investigative work on.

Service-side half (the three intent handlers that passed ``attachments=None``):
``tests/unit/modules/agent/test_intent_handler_attachments_1229.py``.
"""

import logging
from datetime import UTC, datetime
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InvestigationProgress,
    ProblemVerification,
)

pytestmark = pytest.mark.unit

_ENGINE_LOGGER = "faultmaven.core.investigation.milestone_engine"

# The counter's value going into every turn below. Non-zero on purpose: it is
# what makes "reset to 0" and "left where it was" distinguishable outcomes.
STANDING_STALL = 3


class _SeamReached(Exception):
    """Raised by the patched LLM seam — proves the turn fell THROUGH the
    deterministic branch instead of short-circuiting on it."""


def _novel(file_id: str = "file_aaaaaaaaaaaa") -> dict[str, Any]:
    """An attachment the dedup lookup ran on and found nothing for."""
    return {
        "file_id": file_id,
        "filename": "fresh.log",
        "data_type": "logs_and_errors",
        "size": 1024,
        "source_type": "file_upload",
        "summary": "",
        "storage_ref": "ref/fresh.log",
        "is_novel": True,
    }


def _duplicate(file_id: str = "file_bbbbbbbbbbbb") -> dict[str, Any]:
    """The tri-state's other arm: dedup ran and found the bytes already held."""
    return {**_novel(file_id), "filename": "again.log", "is_novel": False}


def _undetermined(file_id: str = "file_cccccccccccc") -> dict[str, Any]:
    """Dedup never ran, so nobody knows — scored conservatively as NOT novel."""
    return {**_novel(file_id), "filename": "unknown.log", "is_novel": None}


def _no_file_id() -> dict[str, Any]:
    """Metadata with nothing to report the upload BY."""
    return {**_novel(), "file_id": None}


def _engine() -> MilestoneEngine:
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine._generate_structured_output = AsyncMock(side_effect=_SeamReached())
    return engine


def _investigating_case(*, pending: Optional[str] = None) -> Case:
    case = Case(
        case_id="case_5db5417fe445",
        title="Gate turn carrying a new file",
        state=CaseState.INQUIRY,
        user_id="user_test",
        organization_id="org_test",
        description="etcdInsufficientMembers alerts",
        problem_verification=ProblemVerification(
            symptom_statement="recurring etcdInsufficientMembers alerts",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "etcd connectivity"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.inquiry.decided_to_investigate = True
    case.inquiry.decision_made_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 7
    case.turns_without_progress = STANDING_STALL
    if pending:
        case.pending_transition = {
            "to_state": pending,
            "summary": "You can **close** the case instead.",
            "evidence_ids": [],
            "proposed_at": datetime.now(UTC).isoformat(),
        }
    return case


async def _gate_turn(engine: MilestoneEngine, case: Case, attachments) -> dict:
    """A short, question-free non-answer to a pending gate: the re-present
    branch, which returns without ever reaching the LLM."""
    result = await engine.process_turn(
        case=case, user_message="hmm", attachments=attachments
    )
    assert not engine._generate_structured_output.called, (
        "this turn must short-circuit on the deterministic gate branch — if it "
        "reached the LLM, the test is no longer exercising the #1229 path"
    )
    return result["metadata"]


class TestAGateTurnCarryingANovelUpload:
    """ "not yet — here's the new log", answered while a close is pending."""

    async def test_the_upload_is_reported(self):
        case = _investigating_case(pending="closed")

        metadata = await _gate_turn(_engine(), case, [_novel()])

        assert metadata["files_uploaded"] == ["file_aaaaaaaaaaaa"]
        assert metadata["novel_files_uploaded"] == ["file_aaaaaaaaaaaa"]

    async def test_it_counts_as_progress(self):
        """The gate-semantics answer. ``progress_made`` is API-visible
        (``TurnResponse.progress_made``), so this is what the client is told."""
        case = _investigating_case(pending="closed")

        metadata = await _gate_turn(_engine(), case, [_novel()])

        assert metadata["progress_made"] is True

    async def test_it_resets_the_stall_counter(self):
        """And the persisted counter agrees with the boolean above — the
        deterministic branches save the case, so this is what is stored."""
        case = _investigating_case(pending="closed")

        await _gate_turn(_engine(), case, [_novel()])

        assert case.turns_without_progress == 0

    async def test_the_reset_is_saved(self):
        """The reset is applied above the fork, so it precedes the branch's own
        ``save`` rather than being computed after it."""
        engine = _engine()
        case = _investigating_case(pending="closed")
        saved: list[int] = []
        engine.repository.save = AsyncMock(
            side_effect=lambda c: saved.append(c.turns_without_progress) or c
        )

        await _gate_turn(engine, case, [_novel()])

        assert saved == [0], f"counter at save time: {saved}"


class TestAGateTurnCarryingADuplicateUpload:
    """The tri-state's other arm. A re-submission of bytes the case already
    holds is activity, not advancement, and must not arm the stall net."""

    async def test_the_upload_is_still_reported(self):
        case = _investigating_case(pending="closed")

        metadata = await _gate_turn(_engine(), case, [_duplicate()])

        assert metadata["files_uploaded"] == ["file_bbbbbbbbbbbb"]

    async def test_it_is_not_novel(self):
        case = _investigating_case(pending="closed")

        metadata = await _gate_turn(_engine(), case, [_duplicate()])

        assert "novel_files_uploaded" not in metadata

    async def test_it_does_not_count_as_progress(self):
        case = _investigating_case(pending="closed")

        metadata = await _gate_turn(_engine(), case, [_duplicate()])

        assert metadata["progress_made"] is False

    async def test_the_freeze_on_the_increment_side_is_real(self):
        """The measurement behind the one-directional rule (#1229).

        A deterministic branch returns above Step 5.8, so the
        ``turns_without_progress += 1`` there is unreachable from it: a gate
        turn that delivered nothing leaves the counter exactly where it was. It
        neither advances toward exhaustion nor retreats.

        This is also the half of the issue's stated symptom that does NOT
        reproduce — it asserted the counter increments on such a turn.
        """
        case = _investigating_case(pending="closed")

        await _gate_turn(_engine(), case, [_duplicate()])

        assert case.turns_without_progress == STANDING_STALL


class TestAnUndeterminedNoveltySignal:
    async def test_it_is_scored_as_not_novel(self):
        case = _investigating_case(pending="closed")

        metadata = await _gate_turn(_engine(), case, [_undetermined()])

        assert metadata["files_uploaded"] == ["file_cccccccccccc"]
        assert "novel_files_uploaded" not in metadata
        assert case.turns_without_progress == STANDING_STALL

    async def test_the_degradation_is_observable(self, caplog):
        """#1224's warning lived inside the block these paths never run, so
        this silence was total. An undetermined signal on a gate turn now says
        so."""
        case = _investigating_case(pending="closed")

        with caplog.at_level(logging.WARNING, logger=_ENGINE_LOGGER):
            await _gate_turn(_engine(), case, [_undetermined()])

        assert any(
            "carries no novelty signal" in r.getMessage()
            and "file_cccccccccccc" in r.getMessage()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]


class TestAnAttachmentWithNoFileId:
    async def test_it_is_not_reported_and_says_so(self, caplog):
        case = _investigating_case(pending="closed")

        with caplog.at_level(logging.WARNING, logger=_ENGINE_LOGGER):
            metadata = await _gate_turn(_engine(), case, [_no_file_id()])

        assert "files_uploaded" not in metadata
        assert metadata["progress_made"] is False
        assert any("carries no file_id" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]


class TestTheDropdownTransitionBranch:
    """The other family of deterministic returns: an explicit
    ``status_transition`` intent that proposes a terminal transition and
    answers without an LLM call."""

    async def test_a_novel_upload_riding_a_close_click_is_reported(self):
        engine = _engine()
        case = _investigating_case()

        result = await engine.process_turn(
            case=case,
            user_message="closing this out",
            attachments=[_novel()],
            intent_type="status_transition",
            intent_data={"to_state": "closed"},
        )

        assert not engine._generate_structured_output.called
        assert result["metadata"]["novel_files_uploaded"] == ["file_aaaaaaaaaaaa"]
        assert result["metadata"]["progress_made"] is True
        assert case.turns_without_progress == 0

    async def test_a_duplicate_riding_a_close_click_does_not_count(self):
        engine = _engine()
        case = _investigating_case()

        result = await engine.process_turn(
            case=case,
            user_message="closing this out",
            attachments=[_duplicate()],
            intent_type="status_transition",
            intent_data={"to_state": "closed"},
        )

        assert result["metadata"]["files_uploaded"] == ["file_bbbbbbbbbbbb"]
        assert result["metadata"]["progress_made"] is False
        assert case.turns_without_progress == STANDING_STALL


class TestTheTerminalShortCircuit:
    """A terminal case is immutable and its stall counter is inert, so the
    reset is deliberately NOT applied — but the upload is still reported, so
    the terminal path is not a second silent hole."""

    @staticmethod
    def _terminal_case() -> Case:
        case = _investigating_case()
        # Bypass the assignment validators, which enforce a state/closed_at
        # ordering neither order satisfies from INVESTIGATING (the same
        # construction the lifecycle-invariant tests use).
        object.__setattr__(case, "state", CaseState.CLOSED)
        object.__setattr__(case, "closed_at", datetime.now(UTC))
        # A CLOSED case that would survive re-validation, so the carve-out
        # below is pinned by the ENGINE declining to write the counter — not by
        # a write blowing up on a half-built model.
        object.__setattr__(case, "closure_reason", "abandoned")
        return case

    async def test_the_report_reaches_the_terminal_path(self):
        engine = _engine()
        case = self._terminal_case()
        seen: dict = {}

        async def spy(
            case: Case,
            user_message: str,
            metadata: dict[str, Any],
            user_id: Optional[str] = None,
        ) -> dict[str, Any]:
            seen.update(metadata)
            return {"agent_response": "", "case_updated": case, "metadata": metadata}

        engine._process_terminal_turn = spy

        await engine.process_turn(
            case=case, user_message="what happened here?", attachments=[_novel()]
        )

        assert seen["files_uploaded"] == ["file_aaaaaaaaaaaa"]
        assert seen["novel_files_uploaded"] == ["file_aaaaaaaaaaaa"]

    async def test_the_counter_is_left_alone(self):
        engine = _engine()
        case = self._terminal_case()

        async def spy(case, user_message, metadata, user_id=None):
            return {"agent_response": "", "case_updated": case, "metadata": metadata}

        engine._process_terminal_turn = spy

        await engine.process_turn(
            case=case, user_message="what happened here?", attachments=[_novel()]
        )

        assert case.turns_without_progress == STANDING_STALL
