"""Calibration pin for the §7.1 restatement guard (INV-27).

Runs the REAL guard predicate over the shipped 91-runbook corpus: every
expert-authored (non-fallback) cause Statement, anchored on its own runbook's
title-as-symptom, must PASS the guard — a guard that blocks real mechanism
statements trades the #656 false-positive for false-negatives on correct
grounding. The pin is FP == 0 on the whole corpus, so any change to the
novelty bar, the tokenizer, or the corpus that starts blocking real causes
fails CI loudly instead of silently degrading grounding.

The positive side pins the #656 incident shape (the disjunction root against
its real case frame) and verbatim symptom-as-cause.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph import root_restates_case_frame
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeType,
    ProblemVerification,
)

pytestmark = pytest.mark.unit

PACK_JSON = (
    Path(__file__).resolve().parents[4]
    / "resources"
    / "knowledge"
    / "pack"
    / "pack.json"
)


def _case(symptom: str, *, hyp_statements: list[str] | None = None) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement=symptom,
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement=symptom, severity=CaseSeverity.HIGH
        ),
    )
    for stmt in hyp_statements or []:
        h = Hypothesis(
            statement=stmt,
            category=HypothesisCategory.OTHER,
            state=HypothesisState.ACTIVE,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="posited",
            generated_at_turn=1,
        )
        case.hypotheses[h.hypothesis_id] = h
    return case


def _root(statement: str) -> CausalNode:
    return CausalNode(
        statement=statement,
        node_type=NodeType.ROOT,
        generated_at_turn=1,
    )


def test_no_shipped_mechanism_statement_is_blocked():
    """FP pin: every non-fallback cause Statement in the shipped corpus passes
    the guard against its own runbook's symptom anchor."""
    pack = json.loads(PACK_JSON.read_text(encoding="utf-8"))
    blocked: list[str] = []
    checked = 0
    for rb in pack["runbooks"]:
        case = _case(rb["title"])
        for cause in rb.get("causes", []):
            if cause.get("is_fallback_cause"):
                continue
            stmt = cause.get("cause_statement") or ""
            if not stmt:
                continue
            checked += 1
            if root_restates_case_frame(_root(stmt), case):
                blocked.append(f"[{rb['title'][:40]}] {stmt[:80]}")
    assert checked > 300  # the corpus is really being swept
    assert not blocked, (
        f"{len(blocked)}/{checked} real mechanism statements blocked "
        f"(guard FP regression):\n" + "\n".join(blocked[:10])
    )


def test_incident_disjunction_root_is_blocked_against_its_frame():
    """TP pin: the #656 turn-6 root — a disjunction of the case's two ACTIVE
    hypotheses restating the symptom — is blocked against its real frame."""
    case = _case(
        "Intermittent 502 errors under load",
        hyp_statements=[
            "Transient network congestion",
            "Resource contention on the backend",
        ],
    )
    root = _root(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors"
    )
    assert root_restates_case_frame(root, case) is True


def test_verbatim_symptom_as_cause_is_blocked():
    case = _case("Kafka consumer group lag keeps increasing")
    root = _root("Kafka consumer group lag increasing")
    assert root_restates_case_frame(root, case) is True


def test_known_boundary_cause_contaminated_anchor_blocks_prenamed_cause():
    """KNOWN BOUNDARY (documented, not endorsed — #661 gate, pg-postmortem run):
    when the PROBLEM framing itself embeds the cause (a postmortem user opens
    with 'the audit_events table is missing an index, causing ...'), the case
    frame contains the true cause's tokens, so a root restating that
    user-pre-named cause has ~zero novelty and the guard holds it INCONCLUSIVE
    even when real evidence supports it. This pins the CURRENT behavior so any
    future change is conscious. If live runs show this shape actually reaching
    would-validate and being blocked, the anchor definition (symptom-shaped
    text only) is the tuning lever — tracked on #656/#661."""
    case = _case(
        "The audit_events table is missing an index on created_at, causing "
        "pool exhaustion and request timeouts"
    )
    root = _root("audit_events table missing index on created_at")
    assert root_restates_case_frame(root, case) is True  # blocked — the boundary
