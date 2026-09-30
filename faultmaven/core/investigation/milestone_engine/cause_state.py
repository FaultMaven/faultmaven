import logging
from typing import Any

from faultmaven.core.investigation.causal_graph.clusters import mece_contested_root_ids
from faultmaven.core.investigation.causal_graph.derivation import (
    derive_node_states,
    validate_by_exclusion,
)
from faultmaven.core.investigation.causal_graph.disconfirmation import (
    any_chain_root_inconclusive,
    demote_disconfirmed_cause_via_evidence,
)
from faultmaven.core.investigation.causal_graph.projection import (
    any_chain_root_validated,
    project_hypothesis_states_from_roots,
)
from faultmaven.core.investigation.causal_graph.rcc import (
    link_llm_rcc_to_cause,
    retract_disconfirmed_rcc,
    retract_stale_engine_rcc,
    synthesize_rcc_from_validated_root,
)
from faultmaven.core.investigation.cause_assurance import (
    CauseAssuranceGrade,
    conclusion_overclaims,
    grade_cause_assurance,
)
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.lifecycle_metrics import (
    cause_identification_held_mece_total,
    work_gate_crossed_total,
)
from faultmaven.core.investigation.verification_status import (
    assess_verification_status,
    is_progress_stalled,
    work_gate_passed,
)
from faultmaven.core.investigation.working_conclusion_generator import (
    is_early_stage_conclusion,
)
from faultmaven.modules.case.contracts import Case, CauseState

from .stage_gates import (
    _refresh_working_conclusion,
    _withdraw_unlicensed_solution_offers,
)
from .transition_consent import (
    TYPED_CONFIRMATION_LINE,
    gate1_offer_key,
    offer_intent_fields,
)

logger = logging.getLogger(__name__)


NO_ROOT_CAUSE_ESTABLISHED = "No root cause established"


def _get_root_cause_summary(case) -> str:
    """A brief root-cause description for confirmation prompts.

    Precedence: the recorded ``RootCauseConclusion`` → the working conclusion,
    but ONLY when it carries a real finding → the honest
    ``NO_ROOT_CAUSE_ESTABLISHED``.

    The working-conclusion leg is gated on
    ``is_early_stage_conclusion`` (#987): that fallback exists to surface a
    cause the engine holds outside the conclusion record, and the early-stage
    PLACEHOLDER is definitionally not that. Rendering it here told the user
    "Root cause: Investigating potential causes - awaiting hypothesis
    generation" in the resolution recap of a case whose preceding ten turns had
    identified, fixed, and verified the cause. Saying nothing was established
    is honest; saying the investigation has not begun is false.
    """
    if case.root_cause_conclusion and getattr(
        case.root_cause_conclusion, "root_cause", None
    ):
        cause = case.root_cause_conclusion.root_cause
        return cause[:200] + "..." if len(cause) > 200 else cause
    wc = case.working_conclusion
    if wc and getattr(wc, "statement", None) and not is_early_stage_conclusion(wc):
        stmt = wc.statement
        return stmt[:200] + "..." if len(stmt) > 200 else stmt
    return NO_ROOT_CAUSE_ESTABLISHED


def _get_solution_summary(case) -> str:
    """Extract a brief solution description from the case for confirmation prompts."""
    if case.solutions:
        sol = case.solutions[-1]  # Most recent solution
        # Try fields in order of specificity. Skip titles that look like
        # raw enum references (e.g., "Solution: SolutionType.CONFIG_CHANGE")
        # which indicate the LLM wrote a placeholder instead of a description.
        title = getattr(sol, "title", None)
        if title and "SolutionType." not in title:
            return title[:200] + "..." if len(title) > 200 else title
        longterm = getattr(sol, "longterm_fix", None)
        if longterm:
            return longterm[:200] + "..." if len(longterm) > 200 else longterm
        immediate = getattr(sol, "immediate_action", None)
        if immediate:
            return immediate[:200] + "..." if len(immediate) > 200 else immediate
        # Last resort: return title even if it has enum reference
        if title:
            return title[:200] + "..." if len(title) > 200 else title
    return "Not yet documented"


def _gate1_statement_presentation(case: "Case") -> str:
    """The engine's own presentation of the standing problem statement.

    Gate 1's affordances ask the user to confirm a problem statement, so the
    statement has to be ON SCREEN on the same turn — INV-01 requires the user
    to confirm a statement that was *presented*, and an affordance referring to
    text nobody can see is not a confirmable choice.

    Presentation is engine-owned for exactly the reason the affordance is: the
    prompt cannot be relied on to do it. Two production cases proved that — a
    statement written into ``state_updates`` while the prose answered an
    unrelated how-to question, and a dropdown turn whose prose correctly said
    no problem could be stated yet. In both, the buttons shipped and the
    statement did not.

    Rendered as a block quote so a multi-line statement stays visually one
    quoted unit rather than dissolving into the surrounding reply.
    """
    statement = (case.inquiry.proposed_problem_statement or "").strip()
    quoted = "\n".join(f"> {line}" if line else ">" for line in statement.split("\n"))
    # States the status and asks nothing. This block is re-composed on EVERY
    # Gate-1-pending turn, including the one right after the user clicks
    # "Not quite, let me clarify", so a question here is one the user may have
    # answered a message earlier. "Awaiting your confirmation" stays true on
    # the first presentation, after a decline, and on every repeat.
    #
    # The last line says what a typed confirmation must look like (#1814,
    # ruling (b)): Gate 1 commits only on its click or a bare consent token
    # (#1794). True on every pending turn, so it needs no turn-scoped flag.
    return (
        "Here is the problem statement awaiting your confirmation:\n\n"
        f"{quoted}\n\n"
        "Confirm it to start the focused investigation, or tell me what to "
        f"change.\n\n{TYPED_CONFIRMATION_LINE}"
    )


def _investigation_confirmation_suggestions(case) -> list:
    """Generate DECIDE follow-up suggestions for investigation confirmation.

    Used when the dropdown triggers INQUIRY → INVESTIGATING and a problem
    statement already exists. One positive (confirm) and one mild negative (refine).

    Both intents name the offer by the key of the statement shown,
    ``case.inquiry.proposed_problem_statement`` (#1812): the offer IS that
    wording, so a card from before a revision is refused rather than
    committing the revised text.
    """
    inquiry = getattr(case, "inquiry", None)
    statement = (getattr(inquiry, "proposed_problem_statement", None) or "").strip()
    offer = offer_intent_fields(
        gate1_offer_key(statement) if statement else None, case=case, gate="gate1"
    )
    return [
        {
            "label": "Yes, let's investigate",
            "action_type": "DECIDE",
            "payload": "Yes, that's correct. Let's investigate.",
            "body": "Confirm the problem statement and start the investigation.",
            "intent": {"type": "confirmation", "confirmation_value": True, **offer},
        },
        {
            "label": "Not quite, let me clarify",
            "action_type": "DECIDE",
            "payload": "Not quite — let me clarify the problem before we investigate.",
            "body": "Refine the problem statement before starting the investigation.",
            "intent": {"type": "confirmation", "confirmation_value": False, **offer},
        },
    ]


def is_identification_edge(
    prior_cause_state: "CauseState", current_cause_state: "CauseState"
) -> bool:
    """True on the turn ``cause_state`` NEWLY crosses to IDENTIFIED (INV-35).

    Since identification is engine-derived there is no milestone event to hang a
    per-turn reaction on, so the caller passes the pre-recompute and
    post-recompute values and this reports the rising edge. Two things react to
    it — the KB-remediation warm-up and the ``root_cause_identified`` milestone
    the turn records (#1284) — and they read ONE definition rather than two
    copies of the same comparison, so they cannot come to disagree about which
    turn the cause was identified on.
    """
    return (
        prior_cause_state != CauseState.IDENTIFIED
        and current_cause_state == CauseState.IDENTIFIED
    )


def _kb_prefetch_query_on_identification(
    prior_cause_state: "CauseState",
    current_cause_state: "CauseState",
    root_cause_conclusion,
    working_conclusion,
) -> "str | None":
    """The KB-remediation warm-up query for the ``cause_state``→IDENTIFIED edge.

    Returns the cause text to pre-fetch KB remediation for, but ONLY on the turn
    ``cause_state`` newly crosses to IDENTIFIED (INV-35) — since cause_state is
    engine-derived there is no milestone event to hang this on, so the caller
    passes the pre-recompute value and the post-recompute value and this detects
    the rising edge. Prefers the LLM conclusion's ``root_cause`` over the working
    conclusion's ``statement``. Returns None when it is not the edge, or no cause
    text is available yet.
    """
    if not is_identification_edge(prior_cause_state, current_cause_state):
        return None
    if root_cause_conclusion and getattr(root_cause_conclusion, "root_cause", None):
        return root_cause_conclusion.root_cause
    if working_conclusion and getattr(working_conclusion, "statement", None):
        return working_conclusion.statement
    return None


def _recompute_cause_state_from_chain(
    case: "Case", *, exclusion_survivors: "set[str] | frozenset[str]" = frozenset()
) -> list[str]:
    """Chain-derived ``cause_state`` (Option A, methodology §9.2; flag ON).

    ``exclusion_survivors`` are the ROOT nodes the LLM certified this turn as the
    sole survivor of an exhaustive differential (§7.1.1); each is validated by
    ``validate_by_exclusion`` iff its differential has genuinely collapsed. Empty on
    reload/terminal recomputes — a previously stamped DEDUCTIVE node survives on its
    own (``derive_node_states`` locks it), so no re-assertion is needed to keep it.

    ``IDENTIFIED`` iff some live hypothesis's chain ROOT is VALIDATED from real
    rung evidence (``derive_node_states`` + ``any_chain_root_validated``) **AND
    the symptom is verified** (the cause-identification anchor) **AND the
    validated root is UNCONTESTED** (§7.1.2 MECE arbitration: >1 simultaneously-
    validated distinct roots is a coherence violation — hold at CANDIDATES
    pending discrimination) — never from a flat assertion.
    The chain is load-bearing: a cause reaches IDENTIFIED only by emitting a chain,
    grounding its root, AND having established the evidence-grounded verified
    symptom that anchors it. A validated root without ``symptom_verified`` holds
    at CANDIDATES (never UNKNOWN).

    Pure structural grounding by design — there is deliberately NO flat fallback
    and NO separate disconfirmation guard. Disconfirmation is handled by the chain
    itself: M6 attaches a durable refutation to the root and ``derive_node_states``
    holds it REFUTED, so a disproven cause simply fails ``any_chain_root_validated``.
    ``cause_state`` is a SOFT signal — under-reporting (the LLM emits a chain +
    correct conclusion but does not attach the rung evidence) is backstopped for
    terminal soundness by the ``RootCauseConclusion`` (``terminal_transitions.
    _cause_identified`` reads cause_state OR the RCC OR the working conclusion), so
    it costs only prompt-focus accuracy, never a wrong terminal conclusion.

    Order matters:
      1. M6 (Option c): a counterfactually-disconfirmed grounded cause gets a
         DURABLE engine refutation attached to its root + the conclusion retracted
         — BEFORE derive, so derive refutes the root from that evidence this turn
         and every later turn (preventing the turn-28 resurrection that an
         imperative-only refutation would allow once stale support re-derives it).
      2. ``derive_node_states``: evidence → node states (validate/refute each rung).
      3. cause_state: IDENTIFIED if a live chain root is validated; else
         CANDIDATES (≥2 active hypotheses, OR a live root that is INCONCLUSIVE —
         the soft floor) / UNKNOWN. It follows the root's evidence-derived truth
         (M6 demotion drops it automatically), but a root that merely loses
         validation to an evidence TIE (INCONCLUSIVE, not REFUTED) holds the case
         at CANDIDATES rather than flapping to UNKNOWN (finding-5 / NO-COLLAPSE);
         only a counterfactual REFUTED drops it fully.
    """
    p = case.progress
    # §7.6 / INV-34 + §7.7 / INV-35: attribute an LLM-authored conclusion to the
    # standing hypothesis it names — authoritatively when it named its cause's root
    # node (names_root_node_id), else by lexical fallback. Runs BEFORE the M6
    # demotion, so M6 tracks the LLM's actual cause (not a max-likelihood proxy)
    # and retract_disconfirmed_rcc can reach a disconfirmed LLM conclusion.
    link_llm_rcc_to_cause(case)
    demote_disconfirmed_cause_via_evidence(case)
    derive_node_states(case)
    # Deductive validation (§7.1.1, proof-by-exclusion): stamp DEDUCTIVE on any
    # LLM-certified survivor whose differential has now collapsed to it. Runs AFTER
    # derive_node_states (so siblings have reached REFUTED and their exclusion
    # strength is set) and BEFORE any_chain_root_validated below, so a freshly
    # validated root promotes cause_state in this same pass. The asserted set is the
    # agent's exhaustiveness certification; validate_by_exclusion re-checks the
    # engine-computable guards (≥2 members, all-but-survivor absolutely refuted).
    # If it stamps anything, re-derive: a newly-DEDUCTIVE root can satisfy a
    # downstream effect's AND-gate (M7) that the empirical pass above missed because
    # it settled before the stamp. The demotion-guard preserves the DEDUCTIVE node on
    # the re-run, so this only ADDS downstream validations (no churn when nothing
    # stamped — the common case returns early).
    if validate_by_exclusion(case, exclusion_survivors):
        derive_node_states(case)
    # Source-of-truth retraction: clear a RootCauseConclusion whose named cause
    # (validated_hypothesis_id) is now disconfirmed, so no consumer asserts a
    # disproven cause. Covers the gap M6 misses when cause_state never reached
    # IDENTIFIED. Runs BEFORE the cause_state branch so a freshly-validated root
    # below re-synthesizes a correct RCC via synthesize_rcc_from_validated_root.
    retract_disconfirmed_rcc(case)
    # Engine-mirror coherence: an ENGINE-authored RCC whose grounding root no
    # longer stands validated (demoted by the restatement guard on a pre-guard
    # persisted case, or by an evidence tie) is cleared here — the readiness/
    # report readers key on RCC presence, and a mirror must not outlive its
    # chain. Runs on EVERY recompute (the IDENTIFIED branch below re-mints via
    # synthesize when a validated root stands; the demotion path otherwise had
    # no owner for the stale mirror). LLM-authored conclusions are untouched.
    # §7.1.2 MECE arbitration (#656): >1 simultaneously-validated DISTINCT
    # standing roots is a coherence violation (S2 — at most one origin can be
    # the cause), so identification is HELD at CANDIDATES pending
    # discrimination — the forward mirror of the §7.1.1 exclusion collapse.
    # Node states are untouched (each root's evidence rules it); duplicates and
    # same-LIVE-causal-line roots collapse to one cause; a counterfactually
    # confirmed root settles the contest. Computed ONCE here (post-derive, the
    # graph is settled) and threaded into every same-frame consumer.
    contested_ids = mece_contested_root_ids(case)
    retract_stale_engine_rcc(case, contested_ids=contested_ids)
    root_validated = any_chain_root_validated(case)
    # The persisted flag records CONTEST EXISTENCE — the same predicate every
    # behavioral consumer acts on (the IDENTIFIED gate below, the mirror
    # retraction above, the context-builder discrimination ask) — NOT the
    # symptom-anchored sub-case: a contest whose symptom is still unverified
    # already retracts the mirror and renders the ask, so the queryable flag
    # and the metric must see it too (behavior and observability keyed apart
    # is how holds go invisible).
    contested = bool(contested_ids)
    if contested and not p.cause_identification_contested:
        # Block-event semantics (one increment per transition INTO the
        # contest), edge-triggered on the persisted flag like the M2
        # over-claim seam.
        cause_identification_held_mece_total.inc()
        logger.warning(
            "MECE arbitration hold: case=%s turn=%s — %d simultaneously-"
            "validated roots across competing causes (%s); cause "
            "identification held at CANDIDATES pending discriminating "
            "evidence",
            case.case_id,
            case.current_turn,
            len(contested_ids),
            sorted(contested_ids),
            extra={
                "event": "cause_identification_mece_hold",
                "case_id": case.case_id,
                "turn": case.current_turn,
                "contested_root_ids": sorted(contested_ids),
            },
        )
    p.cause_identification_contested = contested
    # The evidence-grounded VERIFIED SYMPTOM is the anchor for cause
    # identification: IDENTIFIED requires ``symptom_verified``. A validated chain
    # root WITHOUT a verified symptom is held at CANDIDATES (never flapped to
    # UNKNOWN), pending verification; it is not promoted to IDENTIFIED and no
    # RootCauseConclusion is synthesized. This gates CAUSE IDENTIFICATION only —
    # not runbook retrieval / early triage, which engage before the symptom is
    # verified.
    if root_validated and p.symptom_verified and not contested_ids:
        p.cause_state = CauseState.IDENTIFIED
        # Case invariant: IDENTIFIED requires a positive likelihood + a method.
        # Floor them (the LLM's own higher confidence still wins where applied).
        if not p.root_cause_likelihood or p.root_cause_likelihood <= 0:
            p.root_cause_likelihood = 0.8
        if not p.root_cause_method:
            p.root_cause_method = "hypothesis_validation"
        # §9.3/§7.7: the validated root IS the cause, so mirror it into the
        # RootCauseConclusion the disposition/report layer reads. The mirror
        # outranks an LLM-authored conclusion and replaces it here — this runs
        # after this turn's LLM conclusion has been applied, so the surfaced text
        # is rendered from the chain whenever one stands. With no validated root
        # this branch is not reached at all and the LLM's conclusion stands as the
        # explicit fallback.
        synthesize_rcc_from_validated_root(case)
    elif (
        root_validated
        or HypothesisManager.count_active_hypotheses(case) >= 2
        or any_chain_root_inconclusive(case)
    ):
        # CANDIDATES covers: ≥2 active hypotheses, an INCONCLUSIVE live root (the
        # soft floor), a validated root still awaiting symptom verification —
        # the anchor exists structurally but is not yet grounded — AND the
        # §7.1.2 MECE-contested hold (several validated roots, none arbitrated:
        # honest state is "several candidates", not "identified").
        p.cause_state = CauseState.CANDIDATES
    else:
        p.cause_state = CauseState.UNKNOWN

    # #695 Defect A: derive hypothesis VALIDATED from its chain root's final
    # node_state — the sole producer of a VALIDATED hypothesis. Runs LAST, after
    # the whole node-state settling (derive_node_states + the validate_by_exclusion
    # re-derive above) AND the M6 demotion, so it reads final node states and
    # cannot resurrect a just-REFUTED hypothesis. Keeps hypothesis.state, the
    # report bucket, the grade, and cause_state on ONE determination.
    # The ids returned here are the turn's ``hypotheses_validated`` arm. The
    # projection is the sole producer of a VALIDATED hypothesis; every consumer
    # of the arm read an empty list for as long as the value was dropped on the
    # floor (#1284).
    #
    # ‼ The projection has a SECOND caller — the terminal confirm-stamp, via
    # ``_GRAPH_HOOKS["project_hyp_states"]`` in
    # ``terminal_transitions.finalize_resolution_truth_surface`` — which still
    # discards the ids and sets ``cause_state = IDENTIFIED`` without recording a
    # milestone. A hypothesis first validated AT the confirm-stamp therefore
    # reaches no turn's arm. Out of scope here rather than covered: that path
    # runs during RESOLVED execution, where the turn already scores
    # ``status_transitioned`` through ``confirmed_transition_arms`` and where
    # progress transparency does not apply (INVESTIGATING-only). A real residue,
    # tracked on #1284 — not a claim that this covers every path.
    return project_hypothesis_states_from_roots(case).newly_validated


def _resolve_chat_provider_name(llm_provider: "Any") -> str:
    """Best-effort name of the CHAT provider driving the investigation, for the
    DF-6 provider-floor metric (INV-39).

    In the real deployment ``self.llm_provider`` is the ``LLMRouter``, which has
    no ``provider_name`` — so fall back to its configured chat provider
    (``settings.llm.provider``, the ``CHAT_PROVIDER`` the router routes through).
    A raw provider (unit tests, non-router deployments) exposes ``provider_name``
    directly. ``settings.llm.provider`` is an ``LLMProvider`` enum (``.value``).
    Returns ``"unknown"`` when neither resolves — the metric labels the crossing
    rather than dropping it."""
    name = getattr(llm_provider, "provider_name", None)
    if isinstance(name, str) and name:
        return name
    chat = getattr(
        getattr(getattr(llm_provider, "settings", None), "llm", None),
        "provider",
        None,
    )
    if chat is not None:
        return getattr(chat, "value", None) or str(chat)
    return "unknown"


def _recompute_assessment_state(
    case: "Case",
    *,
    exclusion_survivors: "set[str] | frozenset[str]" = frozenset(),
    rcc_authored_this_turn: bool = False,
    metadata: "dict[str, Any] | None" = None,
    provider_name: "str | None" = None,
) -> "CauseState":
    """Recompute the engine-owned assessment variables each INVESTIGATING turn.

    Returns the PRE-recompute ``cause_state``. Two things react to the
    identification edge — the milestone recorded below and the caller's
    KB-remediation warm-up — and both need the same "before" value. Returning it
    keeps ONE capture: a second read in the caller could drift from this one if
    anything were ever inserted between them, and the two reactions would then
    disagree about which turn the cause was identified on.

    Assessment variables are TRUTH signals the engine derives — never
    path-stripped (redesign R1). Called at the end of
    ``_apply_investigation_updates`` so hypotheses/solutions added this turn
    are reflected.

    - ``cause_state`` is chain-derived (Option A, §9.2) via
      ``_recompute_cause_state_from_chain`` (documented at its definition):
      ``IDENTIFIED`` iff a standing chain root is VALIDATED; NOT sticky (it
      follows the root's evidence-derived truth, so M6 drops it on its own).
    - ``solution_proposed`` / ``solution_state`` are DERIVED from live
      SOLUTION offers each recompute (INV-32, #656 DF-3 — the write-once
      latch dissolved): first the M5 license is re-checked (pending offers
      whose established-cause license fell this turn are withdrawn), then
      the pair is derived by the SHARED
      ``terminal_transitions.derive_solution_surface`` (also called by the
      resolution finalizer and the CLOSED executor — one definition, no
      terminal drift).
    - ``verification_status``: the grounding × progress join
      (``assess_verification_status``), computed LAST so it reads the grade the
      cause_state recompute (including its deductive-exclusion stamp) just
      settled — the #593 recompute-after-stamp ordering. Persisted in the
      progress blob so the model-declared obtainability signal it reads survives
      across turns (Phase 3).
    """
    p = case.progress

    # NOTE: the M2 confirm-side stamp (confirm_root_from_resolution_absence)
    # deliberately does NOT run here. An absence row's mere appearance is an
    # LLM self-claim — a premature "it's stable now" row emitted mid-rollout
    # must not confirm anything (observed live in the gate sims). The stamp
    # fires only at RESOLVED transition execution, on the user's explicit
    # confirmation (terminal_transitions._execute_resolved_transition).
    prior_cause_state = p.cause_state
    newly_validated = _recompute_cause_state_from_chain(
        case, exclusion_survivors=exclusion_survivors
    )
    # The cause_state rising edge, recorded as this turn's milestone (#1284).
    # ``record_turn`` models the per-turn list as a before/after diff of
    # ``case.progress`` (lifecycle §3.2), and cause identification is a progress
    # indicator on that model — but #675/INV-35 made it engine-derived and no
    # writer replaced the retired LLM-claimed boolean. Every CASE-level reader
    # was rewired to the derived ``completed_milestones`` property; the PER-TURN
    # readers were left keyed on a name that stopped arriving. The loudest is the
    # transparency counter (progress_monitor, and its copy in the UI adapter),
    # which breaks on ``turn.milestones_completed`` — so identifying the cause did
    # not turn the light off, contradicting progress-transparency.md §Transitions
    # and its Turn 11 example. Measured before this: 30 of 49 identification
    # turns carried no milestone and did not reset.
    #
    # Written HERE, beside the validated-hypothesis arm, because this is the one
    # function that owns the derivation: both engine-derived progress signals
    # leave from the same place, under the SAME name the derived map already uses
    # (one vocabulary per turn and per case). It lands after step 2b's
    # ``validate_milestone_claims`` review, which exists to revert LLM CLAIMS —
    # this is the engine's own recompute, not a claim, and INV-35 keeps
    # identification off the LLM's authority in both directions.
    if metadata is not None and is_identification_edge(
        prior_cause_state, p.cause_state
    ):
        milestones = metadata.setdefault("milestones_completed", [])
        if "root_cause_identified" not in milestones:
            milestones.append("root_cause_identified")
    # #1284: ``hypotheses_validated`` is one of the nine progress arms and a
    # field on every persisted turn record, and nothing wrote it — five
    # consumers read a permanently-empty list (the momentum bands summed three
    # inputs of which one was always 0, the loop fingerprint carried a constant
    # component, the context-builder line could never render). This is the
    # writer, at the sole producer of the event. Extends rather than assigns so
    # a second recompute in the same turn cannot drop the first one's ids.
    if metadata is not None and newly_validated:
        arm = metadata.setdefault("hypotheses_validated", [])
        arm.extend(h for h in newly_validated if h not in arm)

    # INV-32 (#656 DF-3): solution_proposed is DERIVED, not latched. Runs
    # AFTER the cause recompute so the license re-check reads this turn's
    # settled truth (a root demoted or a conclusion retracted above withdraws
    # the pending offer in the same turn).
    from faultmaven.core.investigation.terminal_transitions import (
        derive_solution_surface,
    )

    # The re-check reads the working-conclusion leg too, so rebuild it here:
    # after this turn's likelihood updates AND the cause recompute above, whose
    # M6 demotion can refute the very hypothesis a license rests on (fm#1679).
    _refresh_working_conclusion(case)
    _withdraw_unlicensed_solution_offers(case, metadata)
    derive_solution_surface(case)

    # Assurance grade + verification status LAST — after the cause_state
    # recompute above has run derive_node_states + the deductive-exclusion
    # stamp, so both read a fresh graph rather than pre-empting the deductive
    # arm. The grade is persisted (progress blob, like verification_status) so
    # the grade × conclusion-confidence seam is queryable per turn (#656);
    # the join reads the just-persisted grade rather than recomputing, so both
    # persisted signals derive from the same graph snapshot.
    p.cause_assurance = grade_cause_assurance(case)
    p.verification_status = assess_verification_status(case, grade=p.cause_assurance)

    # DF-6 provider-floor metric (§5.2, INV-39): count the FIRST time this case
    # crosses the work gate, per CHAT provider. ``work_gate_passed`` is the
    # documented observability primitive; the ``work_gate_crossed`` latch makes
    # the count exactly once-per-case (a later drop below the gate never
    # re-counts, and re-emitting the same hypotheses next turn does not
    # double-count). ``provider_name`` is supplied by the caller (where the
    # provider is in scope). Metric-only; it never changes engine behavior.
    if not p.work_gate_crossed and work_gate_passed(case):
        p.work_gate_crossed = True
        provider = provider_name or "unknown"
        work_gate_crossed_total.labels(provider=provider).inc()
        logger.info(
            "work_gate_crossed case=%s turn=%s provider=%s",
            case.case_id,
            case.current_turn,
            provider,
            extra={
                "event": "work_gate_crossed",
                "case_id": case.case_id,
                "turn": case.current_turn,
                "provider": provider,
            },
        )

    # M2 over-claim seam (#656 turn-6 shape): a recorded conclusion claims
    # "verified" while the graph grade lacks counterfactual confirmation. The
    # engine mirror can no longer produce this (its confidence is grade-derived),
    # so a hit here is an LLM-authored conclusion over-claiming — and, since a
    # standing validated root takes the conclusion over (§7.7), specifically a
    # FALLBACK conclusion over-claiming with no such root behind it. Surfaced at
    # WARNING (prod-visible, unlike the DEBUG grounding trace). Edge-triggered via the persisted
    # flag so a standing over-claim warns once, not once per turn (alert
    # hygiene); the per-turn state stays visible in the DEBUG grounding trace
    # and the persisted flag itself. The under-claim polarity lives in
    # ``_log_grounding_assessment``.
    rcc = case.root_cause_conclusion
    overclaims = conclusion_overclaims(rcc, p.cause_assurance)
    # Edge-triggered on the persisted flag, RE-ARMED when a conclusion was
    # (re)authored this turn: a NEW over-claiming conclusion replacing a
    # retracted one while the flag is still True is a distinct over-claim event
    # and must get its own WARNING, not be absorbed as "standing".
    if overclaims and (rcc_authored_this_turn or not p.cause_overclaim):
        logger.warning(
            "M2 over-claim seam: case=%s turn=%s conclusion claims verified "
            "(likelihood=%.2f, determined_by=%s) but cause_assurance=%s",
            case.case_id,
            case.current_turn,
            rcc.likelihood,
            getattr(rcc, "determined_by", None),
            p.cause_assurance.value,
            extra={
                "event": "cause_confidence_overclaim",
                "case_id": case.case_id,
                "turn": case.current_turn,
                "rcc_likelihood": rcc.likelihood,
                "rcc_determined_by": getattr(rcc, "determined_by", None),
                "cause_assurance": p.cause_assurance.value,
            },
        )
    p.cause_overclaim = overclaims

    _log_grounding_assessment(case)

    # The caller's KB-remediation warm-up reacts to the same edge as the
    # milestone above, so it reads this rather than re-snapshotting a field this
    # function has since rewritten.
    return prior_cause_state


def _log_grounding_assessment(case: "Case") -> None:
    """Debug-level structured trace of the grounding assessment, emitted at the
    one point where the grade × progress join is computed each turn.

    Permanent observability (not throwaway): it traces the join AND its inputs so
    a **grade ↔ cause_state divergence** — the composition-seam drift the design
    flags in §4.1 — is visible per turn in any case, not only in a debugger. Both
    polarities are flagged: ``seam_divergence`` is the UNDER-claim (a
    counterfactually CONFIRMED root with no identified cause_state / unverified
    symptom — the join reads healthier than the progress signals, masking a stuck
    investigation); ``seam_overclaim`` is the OVER-claim (#656 turn 6 — a
    conclusion claiming "verified" while the grade lacks counterfactual
    confirmation; also emitted at WARNING by the caller so prod sees it).

    Guarded by the level check so the payload construction (the
    node/hypothesis summaries; the grade is read from the field the caller just
    persisted) costs nothing above DEBUG, and
    the whole body is failure-isolated: a diagnostic trace must never break the
    turn pipeline it runs inside, whatever shape the case is in.

    **Not the progress ledger.** This runs inside response application — before
    the turn's progress decision and before the counter update at Step 5.8 — so
    its ``turns_without_progress`` / ``is_progress_stalled`` are the PREVIOUS
    turn's values, and it fires only on the generation path. Those fields are
    kept because they are useful context for the grounding readings around them,
    not because they are authoritative. Anything asking "did the engine stall
    this case?" wants the always-on per-turn stream in
    ``core/investigation/case_telemetry.py`` (#1142), which is emitted after the
    counter update, on every path, and carries the per-arm counts this trace
    does not.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return

    try:
        p = case.progress
        grade = p.cause_assurance  # persisted fresh by the caller this turn
        hyp_states: dict[str, int] = {}
        for h in case.hypotheses.values():
            hyp_states[h.state.value] = hyp_states.get(h.state.value, 0) + 1
        nodes = [
            {
                "type": n.node_type.value,
                "state": n.node_state.value,
                "method": (n.validation_method.value if n.validation_method else None),
            }
            for n in case.causal_nodes.values()
        ]
        seam_divergence = grade == CauseAssuranceGrade.CONFIRMED and (
            p.cause_state != CauseState.IDENTIFIED or not p.symptom_verified
        )
        seam_overclaim = conclusion_overclaims(case.root_cause_conclusion, grade)
        logger.debug(
            "grounding-assessment case=%s turn=%s verification_status=%s grade=%s "
            "cause_state=%s symptom_verified=%s hyps=%s nodes=%s seam_divergence=%s",
            case.case_id,
            case.current_turn,
            p.verification_status.value,
            grade.value,
            p.cause_state.value,
            p.symptom_verified,
            len(case.hypotheses),
            len(nodes),
            seam_divergence,
            extra={
                "event": "grounding_assessment",
                "case_id": case.case_id,
                "turn": case.current_turn,
                "verification_status": p.verification_status.value,
                "grade": grade.value,
                "cause_state": p.cause_state.value,
                "symptom_verified": p.symptom_verified,
                "work_gate_passed": work_gate_passed(case),
                "is_progress_stalled": is_progress_stalled(case),
                "turns_without_progress": case.turns_without_progress,
                "hypothesis_count": len(case.hypotheses),
                "hypothesis_states": hyp_states,
                "causal_nodes": nodes,
                "seam_divergence": seam_divergence,
                "seam_overclaim": seam_overclaim,
                "mece_contested": p.cause_identification_contested,
            },
        )
    except Exception:  # noqa: BLE001 - observability must never break the turn
        logger.debug("grounding-assessment trace failed", exc_info=True)
