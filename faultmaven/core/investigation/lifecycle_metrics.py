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
    "(gate1 | disposition). Counts turns where the engine "
    "substituted the canonical clickable affordance pair for a pending "
    "state-machine gate, regardless of LLM compliance with the prompt's "
    "suggestion-emission directives.",
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
