"""Outcome telemetry for lifecycle invariants.

Counters defined here measure **outcomes** — what the system actually
achieved — not just rule fires. The distinction matters because the
class of regression these metrics catch is *dynamic drift*: behaviors
that look locally correct (rule fires logged, code paths exercised)
but where the system as a whole has silently stopped achieving its
design goal.

See ``docs/architecture/investigation-engine/investigation-lifecycle-
logic.md`` §1.3.1 "Dynamic drift" for the framework. See
``docs/operations/lifecycle-metrics.md`` for query and alerting
guidance.

Adding a metric here:

- Pair every "rule-fire" counter with a "rule-outcome" counter. The
  ratio is the load-bearing signal; a single counter is a leading
  indicator at best.
- Document the metric inline so future maintainers know what it
  proves and what shape of regression it surfaces.
- Counters are no-ops unless ``ENABLE_METRICS=true`` and
  ``prometheus_client`` is installed — see ``shims/metrics.py`` for
  the graceful-degradation policy.
"""

from faultmaven.infrastructure.shims.metrics import Counter

# INV-01 outcome telemetry (composition seam between the same-turn-
# confirmation guard and the recovery-turn affordances). The ratio
# ``handshake_recovered_total / handshake_deferred_total`` measures
# whether deferred handshakes are actually recovering in production.
# A ratio that drops far below 1.0 (e.g., < 0.7) signals dynamic drift
# — most likely the recovery prompt or the deterministic suggestion
# emission has weakened without anyone noticing.
#
# Captures the failure shape observed on case_bb917dcd5bb2: guard fires,
# case persists, no transition ever happens. In a healthy system the
# two counters should track each other closely (recovery may lag by a
# turn or two but should eventually catch up).
inquiry_handshake_deferred_total = Counter(
    "faultmaven_inquiry_handshake_deferred_total",
    "INV-01: same-turn-confirmation guard fires (LLM attempted to "
    "collapse INQUIRY→INVESTIGATING handshake into one turn).",
)

inquiry_handshake_recovered_total = Counter(
    "faultmaven_inquiry_handshake_recovered_total",
    "INV-01: cases that transitioned INQUIRY→INVESTIGATING after a "
    "prior same-turn-confirmation guard fire. Divide by "
    "faultmaven_inquiry_handshake_deferred_total to get the recovery "
    "ratio; sustained ratio drops indicate the recovery path is "
    "broken even though the guard is firing as expected.",
)


# Engine-owned-affordance telemetry. Fires every turn where the response
# builder substituted the canonical gate-affordance pair for the LLM's own
# suggestions. The label identifies which gate; collectively the counter
# answers "are engine-owned affordances actually firing across the gate
# vocabulary?" — the observability companion to the architectural
# commitment made in the engine_owned_affordances consolidation (step 2 of
# the intent-on-suggestions redesign).
#
# Healthy-system expectations (rough, subject to scenario mix):
#   gate1 — fires on the first INQUIRY turn after a problem statement is
#           proposed, repeats until Gate 1 closes
#   disposition — fires whenever propose_transition emits override_suggestions
#
# Failure-mode signal: a sustained zero rate on `gate1` while INQUIRY turn
# volume is non-zero indicates the consolidator or the Gate 1 predicate has
# regressed silently. Pair this with the existing recovery-ratio query for
# a complete INV-01 picture.
engine_owned_affordance_served_total = Counter(
    "faultmaven_engine_owned_affordance_served_total",
    "Engine-owned gate affordances served. Labels: gate "
    "(disposition | gate1 | insufficient_evidence | not_yet_productive). "
    "Counts turns where the engine "
    "substituted the canonical clickable affordance pair for a pending "
    "state-machine gate or mid-investigation reading, regardless of LLM "
    "compliance with the prompt's suggestion-emission directives.",
    ["gate"],
)


# Evidence-needs lifecycle telemetry (Phase 3 of the evidence-needs
# rollout). The pool model surfaces three things worth observing:
#   1. Need creation by purpose — sanity check that symptom and causal
#      needs are both being emitted in expected proportions
#   2. Status transitions — measure how often needs reach FULFILLED vs
#      SUPERSEDED vs stay PENDING; high SUPERSEDED rates may flag a
#      prompt drift or anchoring problem
#   3. Path-conditional rejection — counts causal-purpose updates the
#      engine had to reject because the LLM tried to emit them in a
#      restricted state. Healthy systems should trend toward zero as
#      prompt updates improve compliance; a sustained nonzero signal
#      means prompt-side guidance is weakening.
evidence_need_created_total = Counter(
    "faultmaven_evidence_need_created_total",
    "EvidenceNeed rows created by purpose. Labels: purpose "
    "(symptom_verification | causal_verification). Pairs with "
    "``evidence_need_status_changed_total`` to track full lifecycle.",
    ["purpose"],
)

evidence_need_status_changed_total = Counter(
    "faultmaven_evidence_need_status_changed_total",
    "EvidenceNeed status transitions. Labels: from_state, to_state. "
    "Healthy patterns: PENDING→FULFILLED, PENDING→PARTIALLY_MET→FULFILLED, "
    "PENDING→SUPERSEDED. Sustained PENDING (no transitions to FULFILLED) "
    "suggests the LLM is emitting needs but not matching uploads against "
    "them at file-processing time.",
    ["from_state", "to_state"],
)

# NOTE: the former ``evidence_need_rejected_total`` counter was removed with
# the path-conditional emission backstop (unified opportunistic flow): the
# apply-layer no longer rejects EvidenceNeedUpdate emissions by investigation
# state, so there is nothing to count.


# Phase 6 response-flattening seam. Counts ``SuggestedFollowUp.evidence_need_id``
# values that ``_flatten_follow_ups`` drops because the ``new_index_N``
# placeholder didn't resolve against this turn's
# ``metadata["evidence_needs_updated"]`` list. Observable as a ratio so drops
# along the evidence-needs suggestion seam surface without log greps.
#
# Healthy-system expectation: near zero. A sustained nonzero rate signals
# the LLM is emitting stale or mis-indexed ``new_index_N`` references on
# the suggestion side — a Phase 5 prompt-quality concern (the same-turn
# ID rule in ``_EVIDENCE_NEEDS_LIFECYCLE_BLOCK`` may need sharpening, or
# the LLM is referencing needs created in earlier turns by index).
# Labels:
#   - ``out_of_range`` — N exceeded the length of
#     ``evidence_needs_updated`` (most likely shape)
#   - ``missing_metadata`` — the key was absent entirely (defensive
#     default fired; should be near-zero in production paths)
evidence_need_id_dropped_total = Counter(
    "faultmaven_evidence_need_id_dropped_total",
    "SuggestedFollowUp.evidence_need_id values dropped at the response-"
    "flattening seam because the new_index_N placeholder didn't resolve. "
    "A sustained nonzero rate means the LLM is emitting stale same-turn ID "
    "references on the suggestion side.",
    ["reason"],
)


# §7.1 restatement guard (#656 turn-6 class): counts BLOCK EVENTS — the state
# transition where a ROOT that would otherwise have validated (supported,
# net-positive, AND-gate satisfied) is held at INCONCLUSIVE because its
# statement restates the problem anchor. One increment per event, never per
# fixpoint pass or per re-derive of an already-held node, and an AND-gate
# block is never misattributed here. Healthy systems sit near zero; a
# sustained rate means the model keeps emitting NEW symptom-as-cause roots
# (an elicitation problem, not a truth one — the guard is holding). NOTE the
# check is lexical: a near-zero counter does not prove the #656 class closed
# (synonym paraphrases score 0) — the layered defenses are tracked on #656.
root_validation_blocked_restatement_total = Counter(
    "faultmaven_root_validation_blocked_restatement_total",
    "A ROOT that would otherwise have validated was held at INCONCLUSIVE "
    "because its statement restates the problem anchor (no explanatory "
    "depth) — one increment per block event.",
)

# INV-29 (#573) calibration lever, LABELED BY BLOCK REASON so the metric
# measures the same populations the interventions act on (an unlabeled mix of
# count-blocked / mirror-collapsed / hedged-only slices reads as one number
# with three different recovery actions). Healthy shape: transient blips that
# clear as the reason-specific recovery arrives — a second independent
# observation for count/mirror_collapse, a CONFIDENT link for hedged_only. A
# sustained rate on cases that never validate means the bar is too high for
# real traffic — judge against grounded-resolution outcomes, not this counter
# alone.
root_validation_blocked_support_count_total = Counter(
    "faultmaven_root_validation_blocked_support_count_total",
    "A ROOT with real causal-category support that cleared the generic "
    "validation bar was held at INCONCLUSIVE by the independent-support "
    "bar — one increment per block event, labeled by reason: 'count' "
    "(fewer qualifying supports than the bar), 'mirror_collapse' (enough "
    "rows but mutual restatements), 'hedged_only' (causal links exist but "
    "all below the stance_confidence bar).",
    labelnames=["reason"],
)

hypothesis_likelihood_capped_no_evidence_total = Counter(
    "faultmaven_hypothesis_likelihood_capped_no_evidence_total",
    "An LLM likelihood update on a hypothesis with NO supporting evidence "
    "links was capped at NEW_HYPOTHESIS_MAX_PRIOR — belief above the prior "
    "cap requires linked case evidence (#573 B1).",
)

absence_confirmation_link_stripped_total = Counter(
    "faultmaven_absence_confirmation_link_stripped_total",
    "An LLM-emitted SUPPORTS link on a causal_absence evidence row was "
    "stripped at chain-emission ingest — counterfactual CONFIRMATION is "
    "engine-reserved (the resolution confirm-stamp), so a self-claimed "
    "confirmation must never mint the CONFIRMED grade. One increment per "
    "stripped link.",
)

hypothesis_support_mirrored_to_root_total = Counter(
    "faultmaven_hypothesis_support_mirrored_to_root_total",
    "A hypothesis's flat causal_evidence SUPPORTS link was mirrored onto its "
    "chain ROOT node's evidence links at chain-emission ingest (#695 B1) — the "
    "flat hypothesis_evidence and causal_node_evidence axes are otherwise "
    "disjoint, so grounding recorded only on the hypothesis left its root node "
    "uncertifiable. One increment per mirrored link; the link is a CANDIDATE "
    "only (derive_node_states still applies the independence/restatement/"
    "AND-gate filters).",
)

# §7.1.2 MECE arbitration (#656). One increment per transition INTO the
# contest — the recompute where >=2 simultaneously-validated DISTINCT
# standing roots (duplicates and same-live-causal-line roots collapsed; a
# counterfactually confirmed root settles the contest) first stand
# unarbitrated. While the contest stands, cause identification is held at
# CANDIDATES and the engine conclusion mirror is withheld. Edge-triggered on
# the persisted progress flag, never per turn while the contest stands.
# Healthy systems sit near zero; a sustained rate means the model keeps
# validating competing exclusive causes without discriminating between them
# (an elicitation/search problem — the hold is doing its job; the context
# builder renders the discrimination ask on the contested roots).
cause_identification_held_mece_total = Counter(
    "faultmaven_cause_identification_held_mece_total",
    "Cause identification became MECE-contested — >=2 simultaneously-"
    "validated DISTINCT chain roots with no discriminating evidence "
    "(§7.1.2), holding identification at CANDIDATES; one increment per "
    "transition into the contest.",
)

# §7.2 refute-side confidence discipline (#656). A hedged counterfactual
# is ingested and kept as ORDINARY refuting evidence — this counts how often
# the model itself hedges the strongest evidence grade. A sustained rate is an
# elicitation signal (the model reports failed fixes it does not trust), not a
# truth problem: the decisive-power gate is what protects the conclusion.
counterfactual_refute_hedged_total = Counter(
    "faultmaven_counterfactual_refute_hedged_total",
    "A REFUTES link on a causal_absence evidence row arrived below the "
    "stance-confidence bar at chain-emission ingest — kept as ordinary "
    "refuting evidence but denied decisive (§7.2) force. One increment per "
    "link created.",
)

# #656 bearing check at the resolution confirm-stamp. One increment per
# candidate row refused because its content bears on a DIFFERENT chain than
# the root being confirmed (frame-shared tokens below the bar, elsewhere-shared
# at/above it). Rate is bounded by refused-rows-per-stamp × stamp evaluations
# (a retried RESOLVED execution re-evaluates and re-counts — refusal leaves no
# marker on the case); any sustained rate means confirmations are being
# recorded against the wrong candidate cause — inspect before trusting
# CONFIRMED-grade harvests from affected deployments.
absence_confirmation_bearing_rejected_total = Counter(
    "faultmaven_absence_confirmation_bearing_rejected_total",
    "A qualifying causal_absence row was refused as the resolution "
    "confirmation because its content bears on a different chain than the "
    "root being confirmed — one increment per refused row.",
)

# INV-32 (#656 DF-3) offer-liveness discipline. ``solution_proposed`` is derived
# from live SOLUTION offers each recompute, so an offer leaving liveness is the
# event that can move the prompt frame back out of "awaiting execution".
# Labeled by supersession reason so the two populations stay separately
# actionable: 'reproposal' (a newer SOLUTION offer replaced a standing pending
# one — routine; a sustained high rate means the model churns fixes without
# the user executing any) and 'license_lost' (the established-cause license
# that admitted the offer fell — M6 failed-fix demotion, conclusion
# retraction, a MECE hold, or the working-conclusion proxy dropping below
# its bar — and the engine withdrew the now-unlicensed offer rather than
# keep presenting it as awaiting execution). A sustained 'license_lost' rate
# means established causes keep falling after fixes are on the table — a
# validation-bar admission problem (INV-27/INV-29/INV-31 telemetry) or
# working-conclusion decay under stagnation; interpretation and PromQL:
# docs/operations/monitoring/lifecycle-metrics.md § INV-32.
solution_offer_superseded_total = Counter(
    "faultmaven_solution_offer_superseded_total",
    "A pending SOLUTION ProposedAction was superseded — one increment per "
    "offer, labeled by reason: 'reproposal' (replaced by a newer SOLUTION "
    "offer) or 'license_lost' (the established-cause license that admitted "
    "it fell; the engine withdrew the offer).",
    labelnames=["reason"],
)


# INV-33 (#656): a shadowed DIAGNOSTIC pending ask was retired when the SOLUTION
# it predated left pending state — WITHDRAWN on license loss or ACCEPTED into
# TREATMENT — so it cannot resurface as the <pending_action> the LLM reads for
# compliance. One increment per retired action. A sustained rate means
# investigations keep proposing fixes with stale diagnostic asks still open
# underneath. MITIGATION is cause-independent (INV-32) and never retired this way.
# Interpretation: docs/operations/monitoring/lifecycle-metrics.md § INV-33.
pending_action_superseded_stale_total = Counter(
    "faultmaven_pending_action_superseded_stale_total",
    "A DIAGNOSTIC pending ProposedAction shadowed by a SOLUTION offer was "
    "retired ('stale_pending') when that offer left pending state (withdrawn or "
    "accepted) so it cannot resurface in <pending_action>. One per retired action.",
)


# INV-34 (#656): an LLM-authored RootCauseConclusion's named cause was
# conservatively linked (validated_hypothesis_id) to a standing hypothesis at
# the per-turn recompute — the link that lets the link-based retraction
# lifecycle (retract_disconfirmed_rcc, the M6 demotion) reach an LLM conclusion
# whose cause is later disconfirmed. Only a SINGLE unambiguous STRONG lexical
# match links (the orphan-chain T1 discipline); a sustained near-zero rate is
# expected once a case's cause is named. One increment per link written.
llm_rcc_cause_linked_total = Counter(
    "faultmaven_llm_rcc_cause_linked_total",
    "An LLM-authored RootCauseConclusion was linked to a standing hypothesis "
    "(validated_hypothesis_id) by conservative lexical match at recompute, so "
    "its disconfirmation retraction lifecycle can reach it (§7.6 / INV-34). "
    "One increment per link written.",
)


# INV-35 (#656): an LLM-authored RootCauseConclusion was linked to its cause
# AUTHORITATIVELY — the LLM named the cause's cn_ root node (names_root_node_id)
# and the engine matched it to the hypothesis whose root_node_id equals it, with
# no lexical guess. This is the primary link; llm_rcc_cause_linked_total is the
# lexical fallback for id-less conclusions. A healthy ratio (named >> linked)
# means models are naming their cause node as instructed. One increment per link.
llm_rcc_cause_named_total = Counter(
    "faultmaven_llm_rcc_cause_named_total",
    "An LLM-authored RootCauseConclusion was linked to its cause authoritatively "
    "via names_root_node_id → root_node_id match at recompute (§7.7 / INV-35). "
    "One increment per link written.",
)


# INV-34 (#656): an LLM-authored RootCauseConclusion was RETRACTED at
# source because its named cause was disconfirmed (counterfactual refute /
# net-refutation) — the engine never re-authors the conclusion, it only clears a
# proven-wrong one so no consumer (terminal gate, report, UI, KB harvest)
# asserts a disproven cause. Distinct from the MECE-contest READ-suppression
# (reversible, no mutation, counted by cause_identification_held_mece_total). A
# sustained rate means models keep concluding causes that fixes then disconfirm.
llm_rcc_retracted_disconfirmed_total = Counter(
    "faultmaven_llm_rcc_retracted_disconfirmed_total",
    "An LLM-authored RootCauseConclusion was retracted at source because its "
    "named cause was disconfirmed (§7.6 / INV-34). One increment per retraction.",
)


# INV-36 (#656): an LLM-emitted hypotheses_to_add item was NOT minted
# because its statement duplicates a standing (non-terminal) or same-batch
# hypothesis (hypothesis_statements_duplicate — a fail-open 0.8 mutual mirror
# with a negation-polarity guard, stricter than the §7.1.2 fold).
# This protects the ≥2-active work gate — the axis that separates
# INSUFFICIENT_EVIDENCE from NOT_YET_PRODUCTIVE — from duplicate inflation
# (observed live: an identical DNS hypothesis minted twice, turns 10/11).
# Near-zero is the healthy state; a sustained-nonzero rate means the model is
# re-emitting causes it already posited (prompt drift, or it is not being shown
# the standing hypotheses) — a signal to strengthen the standing-hypothesis
# context rather than a soundness alarm (the dedup already held the gate).
hypothesis_dedup_skipped_total = Counter(
    "faultmaven_hypothesis_dedup_skipped_total",
    "An LLM-emitted hypotheses_to_add item was skipped as a duplicate of an "
    "existing or same-batch hypothesis (§7.8 / INV-36), protecting the work "
    "gate from duplicate inflation. One increment per skipped item.",
)

# INV-37 outcome telemetry (closed-gate resolve-preservation). A pending
# CLOSE was about to terminally commit, but the case had become resolvable
# (assess_closure_readiness → SUGGEST_RESOLVE) since the close was proposed;
# the confirm-time guard pivoted it to a RESOLVED proposal instead of
# recording a resolvable case as closed-unresolved. One increment per
# confirm-time pivot. A non-zero value proves the proposal-time SUGGEST_RESOLVE
# pivot has a late-arriving gap that the terminal-boundary guard is catching;
# a rising trend means resolvable cases are routinely reaching the close
# handshake (worth investigating the proposal-time path), not that the guard
# is unsound.
close_pivoted_to_resolve_total = Counter(
    "faultmaven_close_pivoted_to_resolve_total",
    "A pending CLOSE was pivoted to a RESOLVED proposal at confirm time "
    "because the case qualified for resolution (§ closed-gate resolve-"
    "preservation / INV-37). One increment per confirm-time pivot.",
)

# DF-6 provider-floor telemetry (§5.2, INV-39). ``work_gate_passed`` is the
# documented "observability primitive for the per-provider gate-crossing
# metric": whether a configured provider gets real diagnostic work across the
# §5.2 work gate (>=2 hypotheses across >=2 categories with >=2 evidence). Until
# now it had no metric surface — only the status join and one DEBUG log field —
# so a mis-provisioned model that never crosses (the #656 shape:
# gemini-3.1-flash-lite spun 13 turns without crossing) produced no fleet
# signal. This counter is that signal: once per case (latched on
# ``InvestigationProgress.work_gate_crossed``), labeled by the CHAT provider
# driving the investigation. Compare its rate against per-provider case volume —
# a provider with sustained cases but near-zero crossings is at or below the
# provider floor. It is a provider-HEALTH fact (a mis-provisioning signal,
# surfaced beside the boot-time tool-calling capability gate in
# ``config/investigation_capability.py``), NOT a per-case verdict: a single
# honest UNRESOLVED case that never crosses is normal.
work_gate_crossed_total = Counter(
    "faultmaven_work_gate_crossed_total",
    "A case crossed the §5.2 work gate (>=2 hypotheses across >=2 categories "
    "with >=2 evidence items) for the first time, labeled by the CHAT provider "
    "(``provider``) driving the investigation (DF-6 provider floor / INV-39). "
    "One increment per case — latched, so subsequent turns and a later drop "
    "below the gate never re-count. A provider with sustained case volume but "
    "near-zero crossings is mis-provisioned; this is a provider-health fact, "
    "not a per-case verdict. (Accepted rollout residual: the latch is "
    "migration-free, so a case that had already crossed before this shipped "
    "counts its crossing on the next turn it is touched — attributed to that "
    "turn's provider, not the original one. Bounded to one count per case and "
    "self-clearing; read the metric as a trend, not to the turn.)",
    ["provider"],
)


# INV-40 narration-truth coherence (#668, §7.9). Fires when a turn's finalized
# ``agent_response`` contains a completion phrase (the existing narrow
# ``_completion_phrases`` scan, INV-15) while the engine's state contradicts it
# — the case is not terminal AND no terminal transition executed this turn — so
# the engine appended a corrective notice below the LLM's prose. Before INV-40
# this over-claim polarity had no metric surface: the boolean died in the
# ``transition_compliance`` log field (DF-6 lesson — the over-claim polarity must
# be watched). Labeled by the CHAT provider driving the investigation so the
# picture composes with the INV-39 provider floor: a false "case resolved"
# narration is exactly the kind of failure a mis-provisioned / weak model emits
# under long context (the #668 incident: claude-haiku-4-5, 3/3). Healthy systems
# sit near zero; a sustained rate on one provider means that model keeps
# narrating resolution the engine refused — an elicitation/model-capability
# signal (the guard is holding; the append fired every time), not a soundness
# alarm. It never blocks a turn or mutates state; interpret against per-provider
# case volume, never to the individual turn.
narration_overclaim_total = Counter(
    "faultmaven_narration_overclaim_total",
    "A turn's finalized agent_response asserted an unqualified disposition claim "
    "(completion phrase) the engine's non-terminal state contradicted, so the "
    "engine appended a corrective notice (§7.9 / INV-40). One increment per turn, "
    "labeled by the CHAT provider (``provider``) driving the investigation. The "
    "guard is append-only — a nonzero rate means the model over-claims, not that "
    "a wrong disposition reached a terminal surface.",
    ["provider"],
)


# INV-41 (#673 dual-authoring retirement gate, §7.7). #673 — retire the LLM
# free-text conclusion and derive it from the validated chain — is the design's
# recorded endpoint, GATED on "reliable chain-grounding". That gate was prose
# with no instrument. This metric makes it measurable: at each RESOLVED
# transition, which leg of ``_cause_identified`` licensed the resolution.
# Labels:
#   - ``chain`` — cause_state=IDENTIFIED (a validated chain root): healthy, NOT
#     backstop-reliant.
#   - ``rcc`` — the LLM free-text ``root_cause_conclusion`` backstop, with no
#     chain-validated root.
#   - ``working_conclusion`` — the engine working-conclusion proxy backstop.
#   - ``none`` — resolved with no identified cause (causal-absence alone).
# The **backstop-reliance rate** ``(rcc + working_conclusion) / all`` per
# provider is the #673 retirement gate: while it is materially non-zero at the
# INV-39 provider floor, retiring the free-text backstop would strand exactly
# those resolutions (a NO-COLLAPSE regression for the weakest supported
# provider). Fires ONCE per resolution (RESOLVED is terminal/one-way — no latch
# needed). Metric-only: it never changes engine behavior; it is the
# observability half of the #673 decision, as INV-39 is for the §5.2 floor.
# Read as a trend against per-provider resolution volume, never to the single
# case — one backstop-reliant resolution is normal; a SUSTAINED rate at the
# provider floor is the retirement blocker.
resolution_cause_leg_total = Counter(
    "faultmaven_resolution_cause_leg_total",
    "Which leg of cause-identification licensed a RESOLVED transition, labeled by "
    "CHAT ``provider`` and ``leg`` (chain | rcc | working_conclusion | none). The "
    "backstop-reliance rate (rcc+working_conclusion)/all per provider is the #673 "
    "dual-authoring retirement gate (INV-41); it must clear at the INV-39 provider "
    "floor before the free-text conclusion can be retired. One increment per "
    "resolution.",
    ["provider", "leg"],
)


# A turn that hit a RECOVERABLE token failure and answered from the minimal
# fallback prompt instead of failing (#662, the NO-COLLAPSE guarantee). Labeled
# by ``reason`` (input_overflow | output_truncation | unclassified) because the
# recovery targets input overflow and serves truncation only incidentally — a
# rising truncation share is the signal to reach for the max_tokens escalation
# instead. One increment per degraded turn.
#
# Read as a RATE, never per case: an occasional degrade is the guarantee working.
# A sustained rate means turns are routinely too large for the window, which
# points at the prompt-sizing work (#610-#614), not at this recovery. Pairs with
# the ``prompt_context_error_recovered`` log line, which carries the case id.
prompt_context_recovery_total = Counter(
    "faultmaven_prompt_context_recovery_total",
    "Turns that degraded to the minimal fallback prompt (tools dropped) after a "
    "recoverable context/token failure rather than failing the turn, labeled by "
    "``reason`` (input_overflow | output_truncation | unclassified).",
    ["reason"],
)
