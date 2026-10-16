"""M1's terminal clause: a cause is identified, concluded and resolved only at a
root its hypothesis marked terminal (#2036).

The engine used to identify the cause as soon as the deepest emitted root
validated. A validated root proves the cause holds and produces D (R7 clause
a), not that nothing the user controls produces it in turn (clause b), so the
model concluded at an expired credential. Separately, a resolution was READY on
any causal_absence row, even with no cause identified at all, so a renewed
issuer read as "the cause removed".

These pins hold the one definition ("the cause" = a validated root marked
terminal) at each place the engine decides it:

- identification (``cause_state``) and the backstop legs;
- the conclusion mirror;
- the cause leg of a resolution (a row counts only once the case's cause is
  posited, and none while the cause is held open);
- the confirmation stamp;
- the verdict's transport onto the root its entry named;
- what the model is told while held.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.causal_graph.rcc import (
    retract_stale_engine_rcc,
    synthesize_rcc_from_validated_root,
)
from faultmaven.core.investigation.cause_assurance import (
    cause_elimination_rows,
    confirm_root_from_resolution_absence,
)
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_cause_state_from_chain,
)
from faultmaven.core.investigation.milestone_engine.chain_emission import (
    _apply_chain_emission,
    record_root_verdict,
)
from faultmaven.core.investigation.milestone_engine.response_application import (
    WHY_OPEN_DOWNGRADE_REASON,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _withdraw_unlicensed_solution_offers,
)
from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
    _build_causal_graph_block,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
    cause_identification_leg,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CauseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    HypothesisState,
    InvestigationActionType,
    NodeType,
    ProblemStatus,
    ProposedAction,
    RootCauseConclusion,
    WorkingConclusion,
)
from tests.unit.core.investigation.test_chain_cause_state import (
    _FILE_ID,
    _chain_case,
    _root,
)

pytestmark = pytest.mark.unit


def _open_chain_case():
    """A root validated on two independent supports whose hypothesis has given
    no why-stopping verdict."""
    case, root, hyp = _chain_case()
    root.metadata.clear()
    return case, root, hyp


def _absence(category: EvidenceCategory, turn: int, summary: str) -> Evidence:
    return Evidence(
        summary=summary,
        primary_purpose="post-fix check",
        category=category,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=_FILE_ID,
        collected_by="user",
        collected_at_turn=turn,
        collected_at=datetime.now(UTC),
    )


def _renewal_rows(case, turn: int) -> None:
    """What the #2036 runs recorded after a renewed credential: the issuer and
    the certificate seen back, and the reported 503s gone."""
    case.evidence += [
        _absence(
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            turn,
            "vault-pki ClusterIssuer Ready=True again; client certificate renewed",
        ),
        _absence(
            EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
            turn,
            "api-gateway calls to user-service return 200; no certificate_expired",
        ),
    ]


# ---------------------------------------------------------------------------
# Identification
# ---------------------------------------------------------------------------


def test_a_validated_root_whose_why_is_open_is_held():
    case, root, hyp = _open_chain_case()
    _recompute_cause_state_from_chain(case)
    assert root.node_state.value == "validated"
    assert case.progress.cause_state == CauseState.CANDIDATES
    assert case.progress.cause_identification_held_open is True
    # No conclusion names an open root as the root cause...
    assert case.root_cause_conclusion is None
    # ...but the cause still holds: its hypothesis reads VALIDATED (#695).
    assert hyp.state == HypothesisState.VALIDATED


def test_marking_the_root_terminal_identifies_it():
    case, root, _hyp = _open_chain_case()
    root.record_root_terminal(True, "a one-off manual edit", case.current_turn)
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.progress.cause_identification_held_open is False
    assert case.root_cause_conclusion is not None


def test_the_mirror_names_only_a_terminal_root():
    """At the mirror's own boundary: it has callers besides the recompute, so
    the recompute's hold does not cover it."""
    case, root, _hyp = _open_chain_case()
    _recompute_cause_state_from_chain(case)
    assert synthesize_rcc_from_validated_root(case) is False
    assert case.root_cause_conclusion is None

    # A mirror minted while the root was marked terminal is retracted when the
    # verdict is withdrawn.
    root.record_root_terminal(True, "a one-off manual edit", case.current_turn)
    assert synthesize_rcc_from_validated_root(case) is True
    root.record_root_terminal(False, "nothing rotates it", case.current_turn)
    assert retract_stale_engine_rcc(case) is True
    assert case.root_cause_conclusion is None


def test_a_root_marked_not_terminal_stays_held():
    case, root, _hyp = _open_chain_case()
    root.record_root_terminal(False, "nothing rotates the secret_id", 5)
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.CANDIDATES
    assert case.progress.cause_identification_held_open is True


def test_no_backstop_names_a_held_cause_as_known():
    """A conclusion or a likelihood naming the open root would assert as the
    root what the chain says is not yet the root."""
    case, _root_node, hyp = _open_chain_case()
    _recompute_cause_state_from_chain(case)
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="the AppRole secret_id expired",
        mechanism="the issuer cannot log in to Vault",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
    )
    case.working_conclusion = WorkingConclusion(
        statement=hyp.statement, reasoning="r", likelihood=0.9
    )
    assert cause_identification_leg(case) is None


# ---------------------------------------------------------------------------
# The cause leg of a resolution
# ---------------------------------------------------------------------------


def test_a_held_case_does_not_resolve_on_a_renewal():
    case, _root_node, _hyp = _open_chain_case()
    _recompute_cause_state_from_chain(case)
    _renewal_rows(case, turn=case.current_turn)
    assert cause_elimination_rows(case) == []
    assert assess_resolution_readiness(case).verdict != ResolutionReadiness.READY


def test_the_same_rows_resolve_a_terminal_root():
    """The contrast: with the root marked terminal, the same rows confirm."""
    case, root, _hyp = _open_chain_case()
    root.record_root_terminal(True, "a one-off manual edit", case.current_turn)
    _recompute_cause_state_from_chain(case)
    _renewal_rows(case, turn=case.current_turn)
    assert cause_elimination_rows(case)
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


def test_rows_from_before_the_cause_was_posited_do_not_confirm_it():
    """The deepened chain: a renewal at turn 5, under the expiry, then the
    hypothesis re-rooted at turn 7 onto what produced it. The turn-5 rows
    observed the renewal, not the deeper cause removed."""
    case, old_root, hyp = _chain_case()
    _renewal_rows(case, turn=5)
    deeper = _root(
        "cn_000000000002",
        support_labels=["ev_root_support", "ev_root_support_b"],
        statement="nothing rotates the secret_id",
    )
    deeper.metadata["root_attached_turn"] = 7
    case.causal_nodes[deeper.node_id] = deeper
    case.causal_edges.append(
        CausalEdge(cause_node_id=deeper.node_id, effect_node_id=old_root.node_id)
    )
    hyp.root_node_id = deeper.node_id
    case.current_turn = 8
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert cause_elimination_rows(case) == []

    _renewal_rows(case, turn=8)
    assert [e.collected_at_turn for e in cause_elimination_rows(case)] == [8]


def test_with_no_rooted_hypothesis_every_row_counts():
    """Unchanged where the case has no chain at all: an out-of-band fix still
    resolves on the output of the checks."""
    case, _root_node, hyp = _chain_case()
    hyp.root_node_id = None
    case.progress.cause_identification_held_open = False
    _renewal_rows(case, turn=2)
    assert cause_elimination_rows(case)


# ---------------------------------------------------------------------------
# The confirmation stamp
# ---------------------------------------------------------------------------


def test_the_stamp_never_confirms_an_open_root():
    case, root, _hyp = _open_chain_case()
    _recompute_cause_state_from_chain(case)
    case.progress.cause_identification_held_open = False  # stamp's own check
    _renewal_rows(case, turn=case.current_turn)
    assert confirm_root_from_resolution_absence(case) is False

    root.record_root_terminal(True, "a one-off manual edit", case.current_turn)
    assert confirm_root_from_resolution_absence(case) is True


# ---------------------------------------------------------------------------
# The verdict lands on the root its entry named
# ---------------------------------------------------------------------------


def _emission_case():
    from tests.unit.core.investigation.test_chain_emission_wiring import (
        _case as emission_case,
    )
    from tests.unit.core.investigation.test_chain_emission_wiring import _hyp

    case = emission_case()
    hyp = _hyp()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    return case, hyp


def _emit(case, hyp, *, nodes, ref, entry, created):
    metadata = {
        "hypotheses_generated": [hyp.hypothesis_id] if created else [],
        "hyp_root_refs": {hyp.hypothesis_id: ref} if ref else {},
    }
    record_root_verdict(metadata, hyp.hypothesis_id, entry)
    _apply_chain_emission(
        case,
        SimpleNamespace(
            causal_nodes_to_add=nodes, causal_edges_to_add=[], node_evidence_links=[]
        ),
        metadata,
    )


def _node(statement, produces):
    return SimpleNamespace(
        statement=statement, node_type=NodeType.ROOT, produces=produces, and_group=None
    )


def test_a_verdict_lands_on_the_root_its_entry_named():
    case, hyp = _emission_case()
    _emit(
        case,
        hyp,
        nodes=[_node("the AppRole secret_id expired", "D")],
        ref="new_index_0",
        entry=SimpleNamespace(root_is_terminal=False, root_terminal_basis=" ttl "),
        created=True,
    )
    root = case.causal_nodes[hyp.root_node_id]
    assert (root.root_terminal, root.root_terminal_basis) == (False, "ttl")

    # A verdict-only update applies to the hypothesis's current root.
    _emit(
        case,
        hyp,
        nodes=[],
        ref=None,
        entry=SimpleNamespace(root_is_terminal=True, root_terminal_basis=None),
        created=False,
    )
    assert root.root_terminal is True


def test_a_verdict_sent_with_a_declined_re_root_is_dropped():
    """The engine keeps the old root when the new chain does not reach D; the
    verdict described the new root, so it must not land on the old one."""
    case, hyp = _emission_case()
    _emit(
        case,
        hyp,
        nodes=[_node("the AppRole secret_id expired", "D")],
        ref="new_index_0",
        entry=SimpleNamespace(root_is_terminal=None),
        created=True,
    )
    old_root = case.causal_nodes[hyp.root_node_id]
    _emit(
        case,
        hyp,
        nodes=[_node("nothing rotates the secret_id", None)],  # reaches nothing
        ref="new_index_0",
        entry=SimpleNamespace(root_is_terminal=True, root_terminal_basis="b"),
        created=False,
    )
    assert hyp.root_node_id == old_root.node_id
    assert old_root.root_terminal is None


def test_re_rooting_stamps_when_the_cause_was_posited():
    """Re-rooting onto an OLDER node (a standing orphan) dates the cause from
    the re-root, not from when the node was first emitted."""
    case, hyp = _emission_case()
    _emit(
        case,
        hyp,
        nodes=[_node("the AppRole secret_id expired", "D")],
        ref="new_index_0",
        entry=SimpleNamespace(root_is_terminal=None),
        created=True,
    )
    old_root_id = hyp.root_node_id
    orphan = _root("cn_00000000000f", statement="nothing rotates the secret_id")
    orphan.generated_at_turn = 2
    case.causal_nodes[orphan.node_id] = orphan
    case.causal_edges.append(
        CausalEdge(cause_node_id=orphan.node_id, effect_node_id=old_root_id)
    )
    case.current_turn = 7
    _emit(
        case, hyp, nodes=[], ref=orphan.node_id, entry=SimpleNamespace(), created=False
    )
    assert hyp.root_node_id == orphan.node_id
    assert orphan.root_attached_turn == 7


def test_a_verdict_is_cause_work():
    """Refused with the rest of the chain before the problem is verified."""
    case, hyp = _emission_case()
    case.progress.problem_status = ProblemStatus.UNVERIFIED
    _emit(
        case,
        hyp,
        nodes=[_node("the AppRole secret_id expired", "D")],
        ref="new_index_0",
        entry=SimpleNamespace(root_is_terminal=True, root_terminal_basis="b"),
        created=True,
    )
    assert all(n.root_terminal is None for n in case.causal_nodes.values())


# ---------------------------------------------------------------------------
# What the model is told while held
# ---------------------------------------------------------------------------


def test_the_graph_asks_the_why_on_a_held_root():
    case, root, _hyp = _open_chain_case()
    _recompute_cause_state_from_chain(case)
    line = next(
        line
        for line in _build_causal_graph_block(case).splitlines()
        if root.node_id in line
    )
    assert "its WHY is open (ROOT-CAUSE TEST, clause b)" in line
    assert "root_is_terminal true" in line


def test_m5_recovery_names_the_why_when_the_cause_is_held():
    assert len(WHY_OPEN_DOWNGRADE_REASON) <= 500  # ProposedAction.downgrade_reason
    assert "ROOT-CAUSE TEST, clause b" in WHY_OPEN_DOWNGRADE_REASON
    assert "WORKAROUND" in WHY_OPEN_DOWNGRADE_REASON

    case, _root_node, hyp = _open_chain_case()
    _recompute_cause_state_from_chain(case)
    case.working_conclusion = WorkingConclusion(
        statement=hyp.statement, reasoning="r", likelihood=0.9
    )
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description="generate a fresh secret_id",
            proposed_in_turn=case.current_turn,
        )
    )
    metadata: dict = {}
    assert _withdraw_unlicensed_solution_offers(case, metadata=metadata) == 1
    assert "its why is open (ROOT-CAUSE TEST" in metadata["system_feedback"]
