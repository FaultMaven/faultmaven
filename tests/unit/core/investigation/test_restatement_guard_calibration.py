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


def test_known_limit_filler_padding_escapes():
    """KNOWN LIMIT (documented, not endorsed): novelty is lexical, so padding a
    pure restatement with generic filler tokens crosses the bar — the incident
    root scores 0.11 (blocked) but the same text + 'in the production
    environment cluster' scores ~0.33 and passes. No lexical bar closes this
    (filler tokens ARE novel); the semantic layers (multi-support validation,
    grade caps) tracked on #656 own it. Pinned so the escape is a conscious,
    visible property rather than a surprise."""
    case = _case(
        "Intermittent 502 errors under load",
        hyp_statements=[
            "Transient network congestion",
            "Resource contention on the backend",
        ],
    )
    padded = _root(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors in the production environment cluster"
    )
    assert root_restates_case_frame(padded, case) is False  # escapes — known limit


def test_dilution_fp_bounded_under_realistic_sibling_frames():
    """Frame-dilution bound: with the HARSHEST realistic frame — every sibling
    cause of the same runbook standing simultaneously as unattached hypotheses
    — the corpus FP rate stays ≤ 2% (measured 1.1%, dense-vocabulary IAM/RBAC
    tails). Under the entry-bar semantics this is an upper bound on validation
    DELAY, not a permanent block: an already-validated root is ruled by
    evidence alone, and the frame shrinks as siblings are refuted/retired."""
    from faultmaven.core.investigation.causal_graph import (
        _content_tokens,
        _node_restates,
    )

    pack = json.loads(PACK_JSON.read_text(encoding="utf-8"))
    blocked = checked = 0
    for rb in pack["runbooks"]:
        anchors = _content_tokens(rb["title"])
        causes = [
            c
            for c in rb.get("causes", [])
            if not c.get("is_fallback_cause") and c.get("cause_statement")
        ]
        for i, cause in enumerate(causes):
            st = _content_tokens(cause["cause_statement"])
            if not st:
                continue
            siblings = [
                (None, _content_tokens(o["cause_statement"]))
                for j, o in enumerate(causes)
                if j != i
            ]
            checked += 1
            if _node_restates(st, "cn_under_test", anchors, siblings):
                blocked += 1
    assert checked > 400
    assert (
        blocked / checked <= 0.02
    ), f"dilution FP regressed: {blocked}/{checked} ({blocked/checked:.1%})"


# ---------------------------------------------------------------------------
# fm#1137 — the guard's known limit, and the #656 TP that bounds any fix
# ---------------------------------------------------------------------------


def test_known_limit_unattached_duplicate_frames_its_own_root():
    """KNOWN LIMIT (documented, not endorsed — fm#1137). A hypothesis that
    DUPLICATES this root's own hypothesis, left standing and unattached, enters
    the root's frame and holds it. In the live incident that cost nine turns on
    a root carrying three confident independent causal supports: novelty 1/9
    against a 0.30 bar, permanently, because a true duplicate is never refuted
    or retired.

    Pinned as CURRENT behaviour rather than fixed, because every lexical
    separator tried releases the #656 shape too (next test). The recovery is
    elicited from the model instead — see the restatement recovery note in
    context_builder and ``restatement_held_root_ids``."""
    case = _case(
        "The production payment-processor deployment in the `payments` "
        "namespace is currently unavailable or unstable because its v2.1.4 "
        "pods enter CrashLoopBackOff after 2-3 minutes, causing customer "
        "payment failures.",
        hyp_statements=[
            "The v2.1.4 JVM configuration sets a 512MB maximum heap inside a "
            "400Mi container, leaving insufficient headroom for JVM "
            "native/non-heap memory; total RSS reaches the cgroup limit, the "
            "kernel kills the process with SIGKILL/exit 137, and Kubernetes "
            "restarts it into CrashLoopBackOff."
        ],
    )
    root = _root(
        "JVM heap and native/non-heap memory exceed the 400Mi container " "cgroup limit"
    )
    assert root_restates_case_frame(root, case) is True  # the limit


def test_656_disjunction_root_stays_blocked_against_verbose_siblings():
    """REGRESSION PIN — the bound on any fm#1137 fix. The #656 TP must survive
    siblings written at the length real hypotheses are written at. A
    disjunction root is CONTAINED IN each verbose sibling (0.667 here) exactly
    as each terse sibling is contained in the root, so a one-way-containment
    ownership arm in EITHER direction releases the incident shape. An earlier
    cut of fm#1137 shipped one and this fixture is what caught it."""
    case = _case(
        "Intermittent 502 errors under load",
        hyp_statements=[
            "Transient network congestion between the ingress controller and "
            "the backend pods causes intermittent 502 errors whenever request "
            "volume rises under load",
            "Resource contention on the backend host - CPU and memory pressure "
            "from co-tenant workloads - causes intermittent 502 errors under "
            "load",
        ],
    )
    root = _root(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors"
    )
    from faultmaven.core.investigation.causal_graph import _content_tokens

    st = _content_tokens(root.statement)
    covers = [
        len(st & _content_tokens(h.statement)) / len(st)
        for h in case.hypotheses.values()
    ]
    assert max(covers) >= 0.6, f"fixture no longer exercises containment: {covers}"
    assert root_restates_case_frame(root, case) is True
