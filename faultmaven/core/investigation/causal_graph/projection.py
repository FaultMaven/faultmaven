from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from faultmaven.modules.case.contracts import HypothesisState, NodeState

from .queries import is_chain_root_validated

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case


# A hypothesis is a STANDING cause only while ACTIVE or VALIDATED — a REFUTED,
# RETIRED (abandoned/decayed) or INCONCLUSIVE one is not, so a stale root under
# it must not keep grounding the case. One definition, used by every
# chain-mode cause_state query below.
_STANDING_HYP_STATES = {HypothesisState.ACTIVE, HypothesisState.VALIDATED}


def _standing_hypotheses(case: Case):
    """Yield the case's standing-cause hypotheses (ACTIVE/VALIDATED)."""
    return (
        h for h in case.hypotheses.values() if h and h.state in _STANDING_HYP_STATES
    )


def any_chain_root_validated(case: Case) -> bool:
    """§9.2: does some STANDING hypothesis's chain ROOT node read VALIDATED? This
    is the chain-mode ``cause_state=IDENTIFIED`` signal."""
    return any(
        is_chain_root_validated(h, case.causal_nodes)
        for h in _standing_hypotheses(case)
    )


class HypothesisProjection(NamedTuple):
    """What :func:`project_hypothesis_states_from_roots` settled this call.

    ``changed`` keeps its original meaning — either direction moved.
    ``newly_validated`` carries the RISING edge alone: the ids the turn's
    ``hypotheses_validated`` progress arm is written from (#1284).

    ‼ This is a tuple, so it is **always truthy**, including
    ``HypothesisProjection(False, [])``. ``if project_hypothesis_states_from_roots
    (case):`` was a valid boolean read before #1284 and is now silently always
    True — read ``.changed`` explicitly. Both production callers unpack or
    discard, but one of them reaches this through the untyped ``_GRAPH_HOOKS``
    registry, where mypy sees ``Any`` and would not catch the bare form.
    """

    changed: bool
    newly_validated: list[str]


def project_hypothesis_states_from_roots(case: Case) -> HypothesisProjection:
    """#695 Defect A — derive each hypothesis's VALIDATED state from its chain
    ROOT node's ``node_state``. This is the SOLE producer of a VALIDATED
    hypothesis (the flat likelihood-threshold transition was removed). Returns
    ``(changed, newly_validated_ids)`` — True if any state changed, plus the ids
    that crossed INTO ``VALIDATED`` on this call.

    The id list is what the caller records as the turn's ``hypotheses_validated``
    progress arm (#1284). Because this is the sole producer, an arm written from
    anywhere else would be a second opinion about the same event; because the
    list carries only the RISING edge, a hypothesis that merely stays VALIDATED
    across turns cannot re-report itself as progress — the ``novel_*`` rule
    #1136 applied to the other artifact arms, here by construction. The revert
    direction (VALIDATED -> ACTIVE) moves ``changed`` but is deliberately absent
    from the list: losing validation is not advancement.

    Scope of that guarantee: it covers a STANDING value, not a FLAPPING one.
    VALIDATED is not sticky — the revert below fires whenever the chain root
    loses validation — so a hypothesis that loses and regains it IS re-reported.
    That is deliberate, and it is not the restatement leak #1136 closed: a
    restatement is the model re-emitting an artifact the case already holds,
    whereas re-validation means the case actually lost the root and re-earned it
    on new evidence. The same applies to ``cause_state`` under the §7.1.2 MECE
    hold. If oscillation is ever observed holding the stall net open live, the
    remedy is a once-per-case latch like INV-39's ``work_gate_crossed``.

    Invariant: a hypothesis reads ``VALIDATED`` ⟺ its chain root node is
    ``VALIDATED``. Because ``grade_cause_assurance`` returns ``NO_ROOT`` ⟺ no
    validated root node, this makes the report's "Validated" bucket, the cause
    grade, the runbook gate, and ``cause_state`` resolve to ONE determination —
    they can no longer disagree (the #695 divergence).

    Scope is VALIDATED only:
      * a STANDING (ACTIVE/VALIDATED) hypothesis whose root node is VALIDATED
        becomes VALIDATED;
      * a VALIDATED hypothesis whose root is no longer VALIDATED reverts to
        ACTIVE (a stale projection — e.g. the root lost validation to an
        evidence tie or the restatement guard on a pre-guard persisted case).
    ``REFUTED``/``RETIRED`` are NEVER touched: refutation is owned by the
    explicit lifecycle (``refute_hypothesis`` / M6 ``_net_refuted`` / an
    explicit user-or-LLM refute), which is already evidence-grounded and can
    legitimately refute a hypothesis whose root node is not itself REFUTED. A
    hypothesis with no ``root_node_id`` can never be VALIDATED — the graph is
    emission-only, so there is no flat validation floor.

    Ordering (load-bearing): run AFTER node-state settling — ``derive_node_states``
    INCLUDING the ``validate_by_exclusion`` re-derive — and AFTER the M6 demotion,
    so it reads final node states and cannot resurrect a just-REFUTED hypothesis
    to ACTIVE. Called at both settle points: the per-turn assessment recompute and
    the terminal confirm-stamp (a case reaching CONFIRMED via
    ``confirm_root_from_resolution_absence`` never recomputes, so its hypothesis
    must be re-projected there or the report would show it un-validated beside a
    CONFIRMED grade).
    """
    changed = False
    newly_validated: list[str] = []
    for hyp in case.hypotheses.values():
        # Only ACTIVE/VALIDATED are projection targets — INCONCLUSIVE, REFUTED
        # and RETIRED are owned by other lifecycle paths and must never be
        # overwritten here (the REFUTED no-clobber rule).
        if hyp.state not in (HypothesisState.ACTIVE, HypothesisState.VALIDATED):
            continue
        root = case.causal_nodes.get(hyp.root_node_id) if hyp.root_node_id else None
        root_validated = root is not None and root.node_state == NodeState.VALIDATED
        if root_validated and hyp.state != HypothesisState.VALIDATED:
            hyp.state = HypothesisState.VALIDATED
            newly_validated.append(hyp.hypothesis_id)
            changed = True
        elif not root_validated and hyp.state == HypothesisState.VALIDATED:
            hyp.state = HypothesisState.ACTIVE
            # Re-entering the ACTIVE differential restarts the stagnation clock:
            # the age-based decay sweep skips non-ACTIVE hypotheses, so a
            # last_progress_at_turn left at the pre-validation touch would charge
            # the reverted candidate for the turns it spent VALIDATED and could
            # decay/retire it the instant it reverts, and a stagnation counter it
            # built up before validating would let anti-anchoring retire it on
            # the same turn. A just-devalidated theory gets the same fresh grace
            # as a newly-formed candidate: clock and counter both restart.
            hyp.last_progress_at_turn = case.current_turn
            hyp.last_updated_turn = case.current_turn
            hyp.iterations_without_progress = 0
            changed = True
    return HypothesisProjection(changed, newly_validated)
