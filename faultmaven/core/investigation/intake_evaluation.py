"""Process-owned intake-evaluation step (the supply half of the collect→validate
loop).

On each data submission, every new datum is run through the deterministic
evaluator (``differential_intake.evaluate_datum_against_differential`` — matcher
body) and each returned ``StanceVerdict`` is recorded as a typed
``NodeEvidenceLink`` on the relevant cause's ROOT node, carrying the verdict's
provenance. ``derive_node_states`` then counts those links — so cause validation
becomes a function of deterministic, authority-grounded judgments rather than raw
LLM-asserted stances.

OWNERSHIP / SEAM
----------------
This module is process-owned. It depends on two matcher-owned operations,
injected so the two sides build in parallel against the stub:

  - ``evaluate`` — ``evaluate_datum_against_differential`` (matcher body; the stub
    returns ``[]`` so this loop is inert until it lands).
  - ``resolve_root`` — ``(case, record, *, may_instantiate) -> Optional[str]``:
    the cause's ROOT node id, instantiating the chain (lazy promotion, via the
    matcher's ``instantiate_cause_chain`` + root resolution) when
    ``may_instantiate`` and it does not yet exist; ``None`` if it cannot be
    resolved/instantiated. This is the one NEW seam symbol the build surfaced — it
    is matcher-owned (only the matcher knows the CauseRecord→node mapping) and is
    flagged for ratification; injecting it here keeps it off the shared seam file
    until then.

Lazy promotion is on a SUPPORTS only (``may_instantiate=True``); a REFUTES verdict
for a cause that was never promoted has no node to attach to and is skipped —
we do not instantiate a chain just to mark it refuted.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable, Optional, Protocol

from faultmaven.core.investigation.differential_intake import (
    StanceVerdict,
    assemble_active_causes,
    evaluate_datum_against_differential,
)
from faultmaven.modules.case.contracts import (
    EvidenceNeed,
    EvidenceStance,
    NeedPurpose,
    NeedState,
    NodeEvidenceLink,
    NodeState,
    ValidationMethod,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from faultmaven.core.investigation.cause_schemas import CauseRecord
    from faultmaven.modules.case.contracts import Case, Evidence


class ActiveCauseLike(Protocol):
    """What the intake step consumes from each differential candidate — the
    matcher's ``ActiveCause`` satisfies this (``candidate_id`` + ``record``)."""

    candidate_id: str
    record: "CauseRecord"


# (case, record, *, may_instantiate) -> root node id or None
RootResolver = Callable[..., Optional[str]]


def _attach_verdict_link(
    node, evidence: "Evidence", verdict: StanceVerdict, current_turn: int
) -> bool:
    """Attach a ``NodeEvidenceLink`` for ``verdict`` to ``node``; return True if a
    link was added. Idempotent: a link for the same (evidence, stance) is not
    duplicated, so re-evaluating the same datum across turns does not inflate the
    tally."""
    for link in node.evidence_links:
        if link.evidence_id == evidence.evidence_id and link.stance == verdict.stance:
            return False
    node.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=evidence.evidence_id,
            stance=verdict.stance,
            reasoning=f"deterministic intake ({verdict.provenance}): predicate "
            f"{verdict.predicate} fired",
            provenance=verdict.provenance,
            linked_at_turn=current_turn,
        )
    )
    return True


def run_intake_evaluation(
    case: "Case",
    new_evidence: "list[Evidence]",
    active_causes: "list[ActiveCauseLike]",
    current_turn: int,
    *,
    resolve_root: RootResolver,
    evaluate: Callable[..., list[StanceVerdict]] = evaluate_datum_against_differential,
) -> list[StanceVerdict]:
    """Evaluate each new datum against the differential and record the resulting
    verdicts as node-evidence links (with provenance).

    Returns the verdicts that were recorded (for turn-progress accounting). A
    verdict is recorded iff its candidate is known, its root node resolves
    (instantiated on a first SUPPORTS), the root is not an already-REFUTED node a
    SUPPORTS would resurrect, and the link is not a duplicate.
    """
    records_by_id = {ac.candidate_id: ac.record for ac in active_causes}
    recorded: list[StanceVerdict] = []

    for evidence in new_evidence:
        for verdict in evaluate(
            evidence=evidence, active_causes=active_causes, case=case
        ):
            record = records_by_id.get(verdict.cause_id)
            if record is None:
                continue  # verdict for a candidate not in the differential
            root_id = resolve_root(
                case,
                record,
                may_instantiate=verdict.stance == EvidenceStance.SUPPORTS,
            )
            if root_id is None:
                continue  # REFUTES on an un-promoted cause, or no instantiable root
            node = case.causal_nodes.get(root_id)
            if node is None:
                continue
            if (
                node.node_state == NodeState.REFUTED
                and verdict.stance == EvidenceStance.SUPPORTS
            ):
                # A REFUTED root is settled — don't let a runbook predicate
                # re-support it back to life turn after turn (the standing-bias
                # harm). The cause stays refuted; the runbook stays in the
                # differential, so its other (untested) causes remain testable.
                continue
            if _attach_verdict_link(node, evidence, verdict, current_turn):
                recorded.append(verdict)

    return recorded


async def run_differential_intake_turn(
    case: "Case",
    new_evidence: "list[Evidence]",
    current_turn: int,
    *,
    runbook_ids: "list[str]",
    resolve_causes: "Callable[[str], Awaitable[list[dict] | None]]",
    build_records: "Callable[[str, list[dict]], list[CauseRecord]]",
    resolve_root: RootResolver,
    evaluate: Callable[..., list[StanceVerdict]] = evaluate_datum_against_differential,
) -> list[StanceVerdict]:
    """Per-turn driver: rebuild the standing differential from its persisted
    runbook source and validate this turn's new data against it.

    The matcher establishes WHICH runbook(s) are the differential once per case
    (one-shot); this runs EVERY turn so each newly submitted datum is checked
    against the candidates' predicates — that is what keeps a confident-but-wrong
    LLM from driving an unsupported cause to IDENTIFIED on a later turn. The
    candidates are re-resolved each turn (``resolve_causes`` is a cheap DB read,
    not an LLM call), so nothing heavyweight is persisted — only the runbook
    id(s).

    No new data, or no runbook source yet (the matcher has not fired), ⇒ a no-op
    returning ``[]`` — the loop is inert until the differential is established.
    """
    if not new_evidence or not runbook_ids:
        return []
    matched: list[tuple[str, list[CauseRecord]]] = []
    for runbook_id in runbook_ids:
        raw = await resolve_causes(runbook_id)
        if not raw:
            continue
        try:
            # ``build_records`` is tolerant per entry (a malformed cause is logged
            # and skipped, not raised) — so this only guards a wholesale failure.
            records = build_records(runbook_id, raw)
        except Exception:  # noqa: BLE001 — a prior must never break the turn
            continue
        if records:
            matched.append((runbook_id, records))

    active_causes = assemble_active_causes(matched)
    if not active_causes:
        return []
    recorded = run_intake_evaluation(
        case,
        new_evidence,
        active_causes,
        current_turn,
        resolve_root=resolve_root,
        evaluate=evaluate,
    )
    # Demand half of the loop: keep the case's causal Evidence Needs in sync with
    # the differential — one need per still-unsatisfied predicate, fulfilled as
    # predicates fire, and superseded for a cause whose root is now SETTLED (REFUTED,
    # or DEDUCTIVELY VALIDATED) so its open need can no longer change the outcome.
    # One pass: resolve each cause's root once and classify by its settled state
    # (an EMPIRICAL validation is NOT settled — its needs stay open). The engine owns
    # these; the LLM keeps authoring symptom needs and any non-differential ones.
    refuted: set[str] = set()
    validated: set[str] = set()
    for ac in active_causes:
        settled = _cause_root_settled_state(case, ac.record, resolve_root)
        if settled == NodeState.REFUTED:
            refuted.add(ac.candidate_id)
        elif settled == NodeState.VALIDATED:
            validated.add(ac.candidate_id)
    _regen_differential_evidence_needs(
        case, active_causes, recorded, current_turn, refuted, validated
    )
    return recorded


def _cause_root_settled_state(
    case: "Case", record: "CauseRecord", resolve_root: RootResolver
) -> "NodeState | None":
    """The SETTLED node_state of a candidate cause's instantiated root, or None.

    "Settled" means a state whose open needs can be retired *terminally* — the
    cause cannot return to "in play, needs more telemetry":

      - ``REFUTED`` — durable: the #580 re-support guard keeps a refuted root
        refuted (``run_intake_evaluation`` won't re-support it).
      - ``VALIDATED`` **only when** ``validation_method == DEDUCTIVE`` — the one
        validation ``derive_node_states`` locks against demotion to
        INCONCLUSIVE/CANDIDATE (``elif deductively_valid: continue``). A deductive
        node can still go REFUTED, where a superseded need stays correct, but it
        never returns to undecided. An **EMPIRICAL** validation is deliberately
        NOT settled: ``derive_node_states`` recomputes it each turn and reverts it
        to INCONCLUSIVE/CANDIDATE when support net-disappears, so its discriminating
        needs must stay OPEN (retiring them would strand a cause that later
        un-validates).

    ``may_instantiate=False`` — never seeds a node just to check. None for an
    un-instantiated cause or any non-settled state. One root resolution per call,
    so callers classify in a single pass.
    """
    root_id = resolve_root(case, record, may_instantiate=False)
    if not root_id:
        return None
    node = case.causal_nodes.get(root_id)
    if node is None:
        return None
    if node.node_state == NodeState.REFUTED:
        return NodeState.REFUTED
    if (
        node.node_state == NodeState.VALIDATED
        and node.validation_method == ValidationMethod.DEDUCTIVE
    ):
        return NodeState.VALIDATED
    return None


def _supersede_open_need(need: EvidenceNeed, reason: str) -> None:
    """Retire an OPEN (PENDING / PARTIALLY_MET) need; no-op on FULFILLED or
    SUPERSEDED. FULFILLED is preserved — the audit trail of what was collected."""
    if need.state in (NeedState.PENDING, NeedState.PARTIALLY_MET):
        need.state = NeedState.SUPERSEDED
        need.superseded_reason = reason
        need.updated_at = datetime.now(UTC)


def _predicate_signature(predicate: dict) -> str:
    """A stable, order-independent key for a predicate dict, so the same
    predicate maps to the same need across turns (idempotent regen)."""
    return json.dumps(predicate, sort_keys=True, separators=(",", ":"))


# The id prefix shared by ALL engine EvidenceNeeds — the model's ``need_id``
# default_factory uses ``eneed_`` too (models.py), so this is NOT a differential-only
# marker: symptom needs and LLM-emitted causal needs carry it as well. Differential
# needs are identified by their DETERMINISTIC id (``_differential_need_id``); the open
# pool for the budget below is tallied over the CURRENT differential's (cause, predicate)
# needs directly (Pass 1), never by prefix-scanning the whole need list.
_DIFFERENTIAL_NEED_ID_PREFIX = "eneed_"

# SURFACE cap (not an existence cap): at most this many CAUSAL_VERIFICATION needs are
# SHOWN at once (``select_surfaced_causal_needs``). All needs stay PENDING in the pool;
# capping at render — not emission — keeps the full pool available so the surfaced window
# can rotate, which is what prevents the demand-side deadlock (#604): an existence cap
# lets 3 unanswerable asks lock the slots forever; a surface cap rotates past them.
_SURFACED_CAUSAL_CAP = 3

# Advance the surfaced window every this-many turns-of-non-progress
# (``case.turns_without_progress``), so a stuck investigation cycles through all
# discriminators rather than re-showing the same unanswerable 3. On progress the counter
# resets and the window returns to the most-discriminating asks.
_ROTATE_EVERY_K = 2


def _differential_need_id(candidate_id: str, predicate_sig: str) -> str:
    """Deterministic ``need_id`` for a (candidate, predicate) — recomputable each
    turn, so regeneration finds the existing need instead of duplicating it, and
    the engine's needs never collide with the LLM's randomly-id'd ones."""
    digest = hashlib.md5(
        f"{candidate_id}|{predicate_sig}".encode(), usedforsecurity=False
    ).hexdigest()[:12]
    return f"{_DIFFERENTIAL_NEED_ID_PREFIX}{digest}"


def _predicate_request_text(predicate: dict) -> str:
    """A user-facing description of the telemetry that would satisfy a predicate."""
    op = predicate.get("predicate")
    target = predicate.get("target")
    if op == "contains":
        return f"telemetry (logs / command output) showing '{target}'"
    if op == "absent":
        return f"telemetry confirming '{target}' is absent"
    if op == "exit_code":
        return f"command output with exit code {predicate.get('value', target)}"
    if op == "threshold":
        return (
            f"a metric for '{target}' "
            f"{predicate.get('op', 'crossing')} {predicate.get('value', '')}".strip()
        )
    return f"telemetry matching {predicate}"


def select_surfaced_causal_needs(case: "Case") -> "list[EvidenceNeed]":
    """The ≤``_SURFACED_CAUSAL_CAP`` causal evidence-needs to SHOW this turn.

    A SURFACE cap, not an existence cap: every need stays PENDING in
    ``case.evidence_needs`` (so a datum that arrives still grounds regardless of what is
    shown). This is a pure render-time VIEW — it writes nothing and supersedes nothing
    (superseding is terminal and the differential need_id is deterministic, so a yielded
    slot could never re-open; #604). The prompt block is the single choke point that reads
    this; the copilot's EVIDENCE suggestions are downstream of what the prompt showed, so
    they inherit the cap by construction.

    Applies to ALL outstanding ``CAUSAL_VERIFICATION`` needs (engine-differential AND
    LLM-emitted causal — they can't be told apart at render, and "≤N causal asks, whatever
    the source" is the right user-facing invariant). SYMPTOM needs are untouched.

    Ranking — most-discriminating first: rarity is read from the pool itself as the number
    of outstanding causal needs sharing a ``request_text`` (each (cause, predicate) is one
    need, so a widely-shared predicate = more needs = less discriminating). To keep the
    demand side LIVE under pressure, the ≤N window ROTATES by ``turns_without_progress //
    _ROTATE_EVERY_K``: while the case is stuck the window slides through the whole ranked
    pool, so every need — including low-rarity ones — surfaces within a bounded number of
    non-progress turns; on progress the counter resets to the most-discriminating asks.
    """
    causal = [
        n
        for n in case.evidence_needs
        if n.is_outstanding and n.purpose == NeedPurpose.CAUSAL_VERIFICATION
    ]
    if len(causal) <= _SURFACED_CAUSAL_CAP:
        return causal
    shared = Counter(n.request_text for n in causal)  # carrier count per datum
    ranked = sorted(causal, key=lambda n: (shared[n.request_text], n.need_id))
    n = len(ranked)
    stuck = getattr(case, "turns_without_progress", 0) or 0
    offset = (stuck // _ROTATE_EVERY_K) % n
    return [ranked[(offset + i) % n] for i in range(_SURFACED_CAUSAL_CAP)]


def _regen_differential_evidence_needs(
    case: "Case",
    active_causes: "list[ActiveCauseLike]",
    recorded: list[StanceVerdict],
    current_turn: int,
    refuted_cause_ids: "set[str]",
    validated_cause_ids: "set[str]",
) -> None:
    """Reconcile the case's engine-owned causal Evidence Needs with the active
    differential: open ONE PENDING need per unsatisfied predicate, flip a need to
    FULFILLED once its predicate fires this turn, and SUPERSEDE the still-open needs of a
    cause whose root is now SETTLED — passed in as ``refuted_cause_ids`` and
    ``validated_cause_ids`` (the caller classifies; "settled validated" means *deductively*
    validated, the only durable kind — see ``_cause_root_settled_state``).

    Emission is UNCAPPED: a retrieval-seeded differential (Part A) can span many candidate
    causes with several untested predicates each, but the flood is bounded at RENDER by a
    surface cap (``select_surfaced_causal_needs`` shows ≤ ``_SURFACED_CAUSAL_CAP``), NOT by
    capping emission here. Capping emission would be an *existence* cap: the first N
    unanswerable asks would lock the surface with no pool left to rotate through — the
    demand-side deadlock #604. Keeping the full pool PENDING lets the render window rotate
    under pressure, so an answerable discriminator is never permanently hidden behind
    unanswerable ones. (The prior emission-cap approach + its eviction/departed-cause
    limitations are superseded by this surface cap.)

    Both supersessions close a demand↔validate inconsistency, and both are sound
    only because the state is terminal. REFUTED: ``run_intake_evaluation`` skips
    re-supporting a refuted root, so a predicate that keeps firing on it never
    reaches the ``fired`` set. DEDUCTIVELY VALIDATED: the root validated on
    proof-by-exclusion without *every* predicate firing, so the un-fired ones would
    otherwise keep asking for telemetry to discriminate a cause already proven.
    Either way the open need would hang PENDING forever, asking for telemetry that
    can no longer change the outcome.

    Idempotent (deterministic need ids) and safe from the hypothesis-retirement
    supersession rule (these needs carry no motivating hypotheses, like symptom
    needs). A need already FULFILLED stays FULFILLED — the audit trail of what was
    collected before the cause settled (whether refuted or validated); and because
    the fired branch runs BEFORE the validated branch, a predicate that DID fire
    this turn is recorded as FULFILLED rather than retired.
    """
    fired = {
        (v.cause_id, _predicate_signature(v.predicate))
        for v in recorded
        if v.stance == EvidenceStance.SUPPORTS
    }
    by_id = {n.need_id: n for n in case.evidence_needs}
    # Reconcile every current (cause, predicate), then create ONE PENDING need per
    # still-unsatisfied predicate. Emission is UNCAPPED — the ≤N user-facing cap is a
    # render-time surface cap (``select_surfaced_causal_needs``), not an existence cap
    # here, so the full pool stays available for the surfaced window to rotate over (#604).
    seen_need_ids: set[str] = set()  # dedup a predicate repeated within a cause
    for ac in active_causes:
        cause_refuted = ac.candidate_id in refuted_cause_ids
        cause_validated = ac.candidate_id in validated_cause_ids
        predicates = getattr(ac.record, "match_predicates", None) or []
        for predicate in predicates:
            sig = _predicate_signature(predicate)
            need_id = _differential_need_id(ac.candidate_id, sig)
            existing = by_id.get(need_id)
            if cause_refuted:
                # The cause this need discriminates is refuted — retire the open
                # need instead of asking for telemetry that can no longer matter.
                # TERMINAL: a SUPERSEDED need never re-opens (a later re-derivation
                # finds it neither re-created below nor re-pended). Sound because a
                # REFUTED root is settled — the #580 re-support guard keeps it
                # refuted. Note this runs BEFORE the fired branch: a refuted cause's
                # need is retired even if a predicate fired (the hit is moot).
                if existing is not None:
                    _supersede_open_need(
                        existing,
                        "the runbook cause this need discriminated is refuted",
                    )
                continue
            if (ac.candidate_id, sig) in fired:
                if existing is not None and existing.state not in (
                    NeedState.FULFILLED,
                    NeedState.SUPERSEDED,
                ):
                    existing.state = NeedState.FULFILLED
                    existing.updated_at = datetime.now(UTC)
                continue
            if cause_validated:
                # The cause is DEDUCTIVELY validated (the caller already excluded
                # EMPIRICAL validation, which can revert). Any predicate that fired
                # this turn was FULFILLED above; the REMAINING un-fired needs would
                # otherwise hang PENDING, asking the user to discriminate a cause
                # already proven — so retire them. TERMINAL is sound here because a
                # deductive validation is locked against demotion to undecided by
                # derive_node_states (it can only go REFUTED, where a superseded need
                # stays correct), unlike an empirical one.
                if existing is not None:
                    _supersede_open_need(
                        existing,
                        "the runbook cause this need discriminated is already "
                        "validated (deductively)",
                    )
                continue
            # Live cause, unsatisfied predicate → ensure exactly ONE PENDING need.
            # Dedup by deterministic need_id: match_predicates is free-form and may repeat
            # a predicate; the copies share one need_id (a PK), so create it once.
            if existing is not None or need_id in seen_need_ids:
                continue
            seen_need_ids.add(need_id)
            case.evidence_needs.append(
                EvidenceNeed(
                    need_id=need_id,
                    case_id=case.case_id,
                    purpose=NeedPurpose.CAUSAL_VERIFICATION,
                    request_text=_predicate_request_text(predicate)[:500],
                    rationale=(
                        f"Discriminates runbook cause {ac.candidate_id} against the "
                        "submitted telemetry."
                    ),
                    state=NeedState.PENDING,
                    created_at_turn=current_turn,
                )
            )
