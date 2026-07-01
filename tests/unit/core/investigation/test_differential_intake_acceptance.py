"""Acceptance test for the integrated differential-intake loop (§7 headline).

The contract the whole campaign exists to enforce: a confident LLM with **no
satisfying runbook predicate against the raw telemetry** cannot turn a cause into
a soundly-validated (runbook-grounded) one. These exercise the REAL wiring
end-to-end — ``run_differential_intake_turn`` → the real
``evaluate_datum_against_differential`` (predicate vs. the datum's trusted digest)
→ ``resolve_root`` lazy promotion → ``derive_node_states`` →
``cause_validation_is_fallback_only`` — not injected fakes.

The discrimination of the evaluate body in isolation lives in
``test_differential_intake.py``; this proves it stays load-bearing once wired
through links and node-state derivation.
"""

from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph import derive_node_states
from faultmaven.core.investigation.cause_assurance import (
    cause_is_runbook_grounded,
    cause_validation_is_fallback_only,
)
from faultmaven.core.investigation.cause_schemas import CauseRecord
from faultmaven.core.investigation.intake_evaluation import run_differential_intake_turn
from faultmaven.core.investigation.runbook_cause_matcher import resolve_root
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)
from faultmaven.modules.case.domain.models import UploadedFile
from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult

pytestmark = pytest.mark.unit

OOM_PREDICATE = {"predicate": "contains", "target": "OOMKilled"}


def _case() -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="Pod crash-loops",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="Pod crash-loops",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="Pod crash-loops", severity=CaseSeverity.HIGH
        ),
    )
    case.current_turn = 3
    return case


def _datum(
    case: Case,
    file_extract: str,
    *,
    category: EvidenceCategory = EvidenceCategory.CAUSAL_EVIDENCE,
) -> Evidence:
    """Append a datum whose backing file's trusted digest carries ``file_extract``
    (the only thing the runbook predicate is allowed to read). ``category`` is the
    LLM's filing of the datum; pass a non-causal category to prove that runbook
    provenance grounds a cause independent of it (#590 A2)."""
    file_id = f"file_{uuid4().hex[:12]}"
    case.uploaded_files.append(
        UploadedFile(
            file_id=file_id,
            filename="kubelet.log",
            size_bytes=1,
            uploaded_at_turn=case.current_turn,
            structural_index=ExtractResult(file_extract=file_extract).to_json(),
        )
    )
    ev = Evidence(
        evidence_id="ev_" + uuid4().hex[:12],
        summary="LLM's prose interpretation (NEVER read by predicates)",
        primary_purpose="diagnosis",
        category=category,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=file_id,
        collected_by="user",
        collected_at_turn=case.current_turn,
    )
    case.evidence.append(ev)
    return ev


def _cause(
    letter: str,
    name: str,
    statement: str,
    root_statement: str,
    predicate: dict,
) -> CauseRecord:
    """A runbook cause with a discriminating predicate AND an instantiable chain
    (root → D, whose problem statement matches the case) so a SUPPORTS lazily
    promotes it. The root→problem shape is the load-bearing promotion precondition,
    named once here rather than hand-copied per cause."""
    return CauseRecord(
        cause_letter=letter,
        cause_name=name,
        cause_statement=statement,
        chain_nodes=[
            {"ref": "root", "node_type": "root", "statement": root_statement},
            {"ref": "D", "node_type": "problem", "statement": "Pod crash-loops"},
        ],
        chain_edges=[{"cause_ref": "root", "effect_ref": "D"}],
        match_predicates=[predicate],
    )


def _oom_cause() -> CauseRecord:
    return _cause(
        "A", "oom", "container OOM-killed", "container OOM-killed", OOM_PREDICATE
    )


async def _resolve_causes(_rid):
    return [{"opaque": "raw cause dict — build_records returns the real record"}]


async def _run(
    case: Case, evidence: Evidence, *, causes: list[CauseRecord] | None = None
):
    """Drive one real intake turn. ``causes`` seeds the differential (defaults to a
    single OOM cause); the REAL ``evaluate_datum_against_differential`` is used, not
    an injected fake — the invariant this file exists to protect."""

    def _build(_rid, _raw):
        return list(causes) if causes is not None else [_oom_cause()]

    return await run_differential_intake_turn(
        case,
        [evidence],
        case.current_turn,
        runbook_ids=["rb1"],
        resolve_causes=_resolve_causes,
        build_records=_build,
        resolve_root=resolve_root,
    )


@pytest.mark.asyncio
async def test_unsatisfied_predicate_grants_no_validation():
    # The raw telemetry does NOT contain OOMKilled — the predicate is untested, so
    # the loop emits no verdict, promotes nothing, and attaches no link. There is
    # no cause to drive IDENTIFIED.
    case = _case()
    ev = _datum(case, "connection refused to db:5432")
    recorded = await _run(case, ev)

    assert recorded == []
    roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
    assert roots == []  # never lazily promoted


@pytest.mark.asyncio
async def test_satisfied_predicate_grants_runbook_grounded_validation():
    # The raw telemetry contains OOMKilled — the runbook predicate fires, the cause
    # is lazily promoted, and a runbook-provenance link validates its root. This is
    # the by-design floor: a predicate satisfied by real telemetry SOUNDLY validates.
    case = _case()
    ev = _datum(case, "memory cgroup out of memory: Killed process 1 (OOMKilled)")
    recorded = await _run(case, ev)

    assert len(recorded) == 1
    roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
    assert len(roots) == 1
    link = next(
        link
        for link in roots[0].evidence_links
        if link.stance == EvidenceStance.SUPPORTS
    )
    assert link.provenance == "runbook"  # authority-grounded, not LLM-asserted

    derive_node_states(case)
    assert roots[0].node_state == NodeState.VALIDATED
    assert cause_validation_is_fallback_only(case) is False  # sound tier


# --- The differential holds two competing candidates from the same runbook; the
# --- submitted telemetry bears out one of them, and grounding follows the
# --- telemetry, not which candidate reads as more plausible.
#
# Two candidates sit in the differential:
#   * "limit too low" — a `threshold memory_pct > 0.95` predicate, the cheap,
#     obvious reading of OOMKilled + a modest limit, and
#   * "memory leak" — a `contains OutOfMemoryError` predicate, the truth.
# One datum arrives: a JVM log carrying `OutOfMemoryError` but NO `memory_pct`
# metric. Under subset-trust the leak predicate fires (SUPPORTS) and the limit
# predicate abstains (untested — the metric is simply absent, never a false
# refute). The deterministic loop therefore grounds ONLY the leak cause. This is
# the node-level basis for a runbook predicate overruling a confident-but-wrong
# disposition: which cause grounds is decided by what the telemetry bears out.
# NB: no LLM disposition is modeled here — the two candidates are symmetric except
# for their predicates; the test proves predicate-driven discrimination, which is
# the mechanism that WOULD overrule a favored-but-unsupported cause.

LEAK_PREDICATE = {"predicate": "contains", "target": "OutOfMemoryError"}
LIMIT_PREDICATE = {
    "predicate": "threshold",
    "target": "memory_pct",
    "op": ">",
    "value": 0.95,
}

# A JVM heap-exhaustion log: contains the leak signature, contains NO memory_pct
# metric (so the limit cause's threshold predicate cannot decide and abstains).
LEAK_LOG = (
    "ERROR Uncaught exception in request handler\n"
    "java.lang.OutOfMemoryError: Java heap space\n"
    "    at com.faultmaven.payments.LedgerCache.retain(LedgerCache.java:142)\n"
    "working set climbed 210Mi -> 505Mi over 36h with no traffic increase"
)


def _leak_cause() -> CauseRecord:
    return _cause(
        "B",
        "memory leak",
        "application memory leak driving unbounded growth",
        "application memory leak",
        LEAK_PREDICATE,
    )


def _limit_cause() -> CauseRecord:
    return _cause(
        "A",
        "limit too low",
        "memory limit set below the application's working set",
        "memory limit set too low",
        LIMIT_PREDICATE,
    )


@pytest.mark.asyncio
async def test_masquerade_grounds_telemetry_supported_cause_over_competing_candidate():
    case = _case()
    # SYMPTOM_EVIDENCE (non-causal): the runbook predicate firing is the ONLY path
    # to grounding, so every assertion below isolates runbook provenance (#590 A2)
    # rather than passing via the CAUSAL_EVIDENCE arm of node validation.
    ev = _datum(case, LEAK_LOG, category=EvidenceCategory.SYMPTOM_EVIDENCE)

    recorded = await _run(case, ev, causes=[_limit_cause(), _leak_cause()])

    # Exactly one cause grounded, and it is the leak candidate (rb1:B) — asserted
    # by candidate id, the load-bearing identity, not an English substring. The
    # limit candidate (rb1:A) abstained: its memory_pct threshold has no datum.
    assert [v.cause_id for v in recorded] == ["rb1:B"]

    roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
    assert len(roots) == 1  # only the grounded leak cause was lazily promoted
    leak_root = roots[0]

    derive_node_states(case)
    # Grounded through the real §7 grader (runbook provenance, non-dangling guard) —
    # and since the datum is non-causal, VALIDATED is reachable ONLY via that
    # provenance, so these assertions detect a broken provenance stamp.
    assert leak_root.node_state == NodeState.VALIDATED
    assert cause_is_runbook_grounded(case)
    assert cause_validation_is_fallback_only(case) is False  # grounded, not fallback


@pytest.mark.asyncio
async def test_confident_llm_assertion_without_predicate_match_stays_fallback_only():
    # THE headline: the LLM confidently asserts the cause itself (seeds the root +
    # its OWN, lower-assurance support link), but the raw telemetry does not satisfy
    # the runbook predicate. The deterministic loop adds NO runbook-grounded link,
    # so the cause is validated ONLY by the LLM's own assertion — it stays
    # fallback-only and is therefore held back from auto-seeding reusable knowledge.
    case = _case()
    # The LLM's self-authored grounding: a CAUSAL_EVIDENCE SUPPORTS link the model
    # emitted for itself. The real emitted-chain path leaves provenance=None (it
    # never tags it) — which is exactly what must NOT count as sound.
    llm_ev = Evidence(
        evidence_id="ev_" + uuid4().hex[:12],
        summary="the model is sure it is OOM",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=case.current_turn,
    )
    case.evidence.append(llm_ev)
    root = CausalNode(
        node_id="cn_" + uuid4().hex[:12],
        statement="container OOM-killed",
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        generated_at_turn=case.current_turn,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=llm_ev.evidence_id,
                stance=EvidenceStance.SUPPORTS,
                reasoning="LLM asserts OOM",
                provenance=None,  # the real LLM-emitted link is untagged
                linked_at_turn=case.current_turn,
            )
        ],
    )
    case.causal_nodes = {root.node_id: root}

    # New telemetry arrives that does NOT satisfy the runbook predicate.
    ev = _datum(case, "connection refused to db:5432")
    await _run(case, ev)

    derive_node_states(case)
    assert root.node_state == NodeState.VALIDATED  # the LLM's own support stands
    # ...but it is NOT sound: no runbook predicate ever bore it out.
    assert cause_validation_is_fallback_only(case) is True
