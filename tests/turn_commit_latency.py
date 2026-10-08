"""Timing a turn's ONE commit, to size ``TURN_COMMIT_RESERVE_SECONDS`` (#1882).

The reserve is the end of the turn budget kept back for the commit: the route's
deadline no longer covers the commit, so ``commit_turn`` refuses to start one
with less than the reserve left. It must cover the commit's real latency with
room to spare, so it is sized from measurement — the p99 of the commit, on
SQLite and on PostgreSQL, times a safety factor — and these helpers are the
measurement. ``tests/performance/test_turn_commit_latency.py`` runs it on
SQLite and judges the p99 against its ``budgets.py`` row;
``tests/integration/test_turn_rows_commit_with_case_postgres_1882.py`` runs it
on PostgreSQL under RLS and prints the numbers (integration tests judge no
clock, #1579).

The case grows one turn at a time, shaped as real turns are: a user message and
an agent reply of a realistic size, the turn record, an uploaded file and an
evidence row and a hypothesis every few turns, and on the last turn a terminal
close with its checkpoint and summary report, so the commit carries every kind
of row it ever carries. Each turn is committed through ``commit_turn_plan``,
the function the service's settlement calls.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import List
from uuid import uuid4

from faultmaven.core.investigation.checkpoint_service import CheckpointService
from faultmaven.core.investigation.milestone_engine.turn_commit import (
    TurnCommitPlan,
    commit_turn_plan,
)
from faultmaven.core.investigation.terminal_transitions import (
    confirm_pending_transition,
    propose_transition,
)
from faultmaven.modules.case.contracts import (
    MessageRowKind,
    TurnOutcome,
    append_message_row,
)
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.evidence import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    UploadedFile,
)
from faultmaven.modules.case.domain.models.hypothesis import (
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
)
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.progress import InvestigationProgress
from faultmaven.modules.case.domain.models.turn import TurnProgress
from faultmaven.modules.case.domain.owned_models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
)

#: Turns per measured case: past where a real investigation ends, so the last
#: commits upsert a case larger than most.
TURNS = 60

#: The safety factor the reserve was SIZED with over the measured p99 (the
#: worse of SQLite and PostgreSQL); the numbers are in the #1882 PR body.
SAFETY_FACTOR = 10

_USER_TEXT = (
    "We are still seeing intermittent 502s from the checkout API behind the "
    "ingress; the pool metrics show 64/64 connections in use at the peaks. "
) * 6
_AGENT_TEXT = (
    "The pool saturation lines up with the 502 bursts. Before we change the "
    "pool size, please share the connection-holding time histogram and the "
    "slow-query log for the same window, so we can tell a leak from load. "
) * 8


def investigating_case(enterprise_id: str) -> Case:
    """A case past Gate 1, before its first turn: what most turns commit onto."""
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        enterprise_id=enterprise_id,
        title="Checkout API 502s",
        description="Intermittent 502s from the checkout API",
        state=CaseState.INQUIRY,
    )
    case.inquiry.proposed_problem_statement = "Intermittent 502s from the checkout API"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    return case


def _grow_one_turn(case: Case, turn: int, *, terminal: bool) -> TurnCommitPlan:
    """Advance ``case`` by one turn, in memory, as a real turn does."""
    plan = TurnCommitPlan()
    case.current_turn = turn
    append_message_row(
        case,
        MessageRowKind.USER_TURN,
        f"[turn {turn}] {_USER_TEXT}",
        turn_number=turn,
    )
    if turn % 6 == 1:
        upload = UploadedFile(
            filename=f"app-{turn}.log",
            size_bytes=48_000,
            content_type="text/plain",
            uploaded_at_turn=turn,
            upload_source="file_upload",
            preprocessing_summary="3 errors observed",
        )
        case.uploaded_files.append(upload)
        case.evidence.append(
            Evidence(
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                primary_purpose="symptom_verified",
                summary=f"502 burst at turn {turn}",
                extract="ERROR 502 upstream connect error",
                source_type=EvidenceSourceType.LOGS,
                source_file_id=upload.file_id,
                collected_by="user_latency",
                collected_at_turn=turn,
            )
        )
        hypothesis = Hypothesis(
            statement=f"Pool exhaustion variant {turn}",
            category=HypothesisCategory.DATABASE,
            state=HypothesisState.ACTIVE,
            likelihood=0.5,
            initial_likelihood=0.5,
            generated_at_turn=turn,
            last_updated_turn=turn,
            last_progress_at_turn=turn,
            iterations_without_progress=0,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
            rationale="Matches the error pattern",
        )
        case.hypotheses[hypothesis.hypothesis_id] = hypothesis
    if terminal:
        plan.add_checkpoint(
            CheckpointService.capture(
                case,
                trigger="pre_case_action",
                metadata={"from_state": case.state.value, "to_state": "closed"},
            )
        )
        propose_transition(case, to_state="closed", summary="Close it?")
        assert confirm_pending_transition(case, "user_latency")
        plan.add_reports(
            [
                CaseReport(
                    case_id=case.case_id,
                    report_type=ReportType.CLOSURE_SUMMARY,
                    title=f"Closure Summary: {case.title}",
                    content="# Closure summary\n\n" + _AGENT_TEXT * 4,
                    generation_status=ReportStatus.COMPLETED,
                    generated_at=datetime.now(timezone.utc).isoformat(),
                    generation_time_ms=1,
                )
            ]
        )
    case.turn_history.append(
        TurnProgress(
            turn_number=turn,
            progress_made=True,
            outcome=TurnOutcome.CONVERSATION,
            user_message_summary=_USER_TEXT[:200],
            agent_response_summary=_AGENT_TEXT[:500],
        )
    )
    append_message_row(
        case,
        MessageRowKind.AGENT_ANSWER,
        f"[turn {turn}] {_AGENT_TEXT}",
        turn_number=turn,
        metadata={"progress_made": True, "milestones_completed": []},
    )
    case.message_count = len(case.messages)
    return plan


async def measure_turn_commits(
    repository, case: Case, *, turns: int = TURNS
) -> List[float]:
    """Commit ``turns`` turns of ``case`` one by one; return each commit's seconds.

    ``case`` must already be saved once (its row exists), as a case is before
    its first turn.
    """
    timings: List[float] = []
    for turn in range(1, turns + 1):
        plan = _grow_one_turn(case, turn, terminal=turn == turns)
        started = time.perf_counter()
        await commit_turn_plan(repository, case, plan)
        timings.append(time.perf_counter() - started)
    return timings


def percentile(samples: List[float], pct: float) -> float:
    """Nearest-rank percentile: the smallest sample with ``pct``% at or below it."""
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil, at least the first
    return ordered[int(rank) - 1]


def summarize(samples: List[float]) -> str:
    """One line for the PR body: count, p50, p99, max, in milliseconds."""
    return (
        f"n={len(samples)} p50={percentile(samples, 50) * 1000:.1f}ms "
        f"p99={percentile(samples, 99) * 1000:.1f}ms "
        f"max={max(samples) * 1000:.1f}ms"
    )
