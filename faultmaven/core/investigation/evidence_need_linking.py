"""Give every EVIDENCE ask a durable identity in the needs pool (#1079).

The problem this closes
=======================

FaultMaven has two mechanisms for not nagging a user about data they cannot
supply, and both key on the same field:

1. **The obtainability wall** — a ``causal_verification`` need the model declares
   ``UNOBTAINABLE`` yields its surface slot and stops rotating back in
   (``evidence_need_surfacing.select_surfaced_causal_needs``).
2. **Mention decay** — the prompt's "first mention: full request; second: brief
   reminder; third+: stop surfacing" rule.

Both act on an ``EvidenceNeed``. Neither can act on an ask that is not one.

Nothing required an EVIDENCE-type ``SuggestedFollowUp`` to carry an
``evidence_need_id``; the prompt asked for it and the engine dropped the field
silently when it was absent. Across 19 recorded simulator runs — six scenarios,
138 EVIDENCE suggestions — the field was populated **zero** times. So every ask
FaultMaven put to a user was a free-floating string that existed only in that
turn's response: nothing to declare a wall on, nothing to count mentions of,
nothing for the surface cap to bound. On fm#1079 the agent asked for the same
target-account IAM record on ten consecutive turns while the user declined six
times, and no engine mechanism could see a repeat, because from the pool's point
of view no ask had ever been made.

This module reconciles the two: before the turn is saved, every EVIDENCE
suggestion is attached to a need — an existing one where the ask matches, a
newly created one otherwise — and the turn is recorded on that need's ask
history. Downstream, the pool becomes the single record of what has been asked
and how often, which is what both mechanisms above were always meant to read.

Why the engine and not the prompt
=================================

The prompt-side instruction is kept (it is still the model's job to author needs
deliberately, with a real rationale and motivating hypotheses), but it cannot be
the *guarantee*: it was in place for the whole of the run above and produced
nothing. Backfill is the floor, not the intended path — a need created here
carries ``engine_inferred=True`` so readers can tell an inferred ask from one the
model reasoned about, and so the two can be treated differently where the
difference matters (``_awaiting_recent_evidence`` ignores inferred needs;
``sweep_silent_inferred_needs`` garbage-collects them).

An inferred need is deliberately thinner than an authored one: no real rationale,
and no ``motivating_hypothesis_ids``, because the engine knows an ask was made but
not which candidate it separates. That thinness has two consequences worth
knowing, both handled rather than hidden — the orphan needs its own GC (see
``_INFERRED_NEED_SILENCE_TURNS``), and it cannot satisfy
``verification_status._candidate_unresolvable``, so declaring one UNOBTAINABLE
stops the nagging without carrying a case to INSUFFICIENT_EVIDENCE.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from faultmaven.modules.case.contracts import (
    CaseState,
    EvidenceNeed,
    NeedPriority,
    NeedPurpose,
    NeedState,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

logger = logging.getLogger(__name__)


#: Overlap at or above which an ask is treated as a repeat of an existing
#: outstanding need rather than a new one.
#:
#: CALIBRATED ON THE RECORDED RUNS, not chosen by intuition. An earlier version
#: used Jaccard at 0.55 and folded NOTHING: replaying the fm#1079 transcript's
#: own 14 EVIDENCE asks produced 14 separate needs, every one reading "asked
#: once", so mention decay still never fired. Jaccard divides by the UNION, which
#: penalises the length asymmetry these asks actually have — a terse "Share
#: target-account IAM output" against a paragraph naming the same data scores
#: 0.40 purely because the paragraph has more words.
#:
#: The overlap coefficient (intersection over the SMALLER token set) measures
#: what matters here — "is the short ask contained in the long one?" — and at
#: 0.40 the same transcript collapses to 5 needs with the target-account request
#: correctly reading 8 asks across turns 8–15. Checked across 8 recorded runs on
#: four scenarios: pools land at 4–7 needs with max repeat counts of 3–8, i.e.
#: repeats are detected without collapsing the pool to one entry.
#:
#: Deliberately permissive: the failure this module exists to prevent is treating
#: a repeat as new, which is worse than occasionally folding two distinct asks
#: together — a folded pair still surfaces, it just shares one counter.
_MATCH_THRESHOLD = 0.40

#: Minimum shared content words before an overlap score counts at all.
#:
#: The overlap coefficient divides by the SMALLER token set, which is what makes
#: it robust to the length asymmetry these asks have — but it also means a tiny
#: token set scores high on almost nothing. A two-token need ("Share pod
#: status", minted from the ``label`` fallback when a suggestion carries no
#: ``body``) reaches 0.5 on ONE shared generic word, so "Share the latest status
#: of all PVCs in the production namespace" folds into it on ``status`` alone.
#: Observed on ``k8s-pvc-pending_20260605_224649``.
#:
#: That fold is not the benign kind the threshold comment accepts: the two asks
#: then share one decay counter (a live ask is told "asked 3×, stop surfacing"
#: for mentions it never had), one ``obtainability`` declaration walls both, and
#: the pool's ``request_text`` never records the second ask at all.
#:
#: A floor of two removes it without touching the calibration — the fm#1079
#: transcript still yields 5 needs with the target-account ask at 8 repeats,
#: identical to a floor of one.
_MIN_SHARED_TOKENS = 2

#: Cap on needs CREATED per turn by backfill. A well-behaved turn emits one to
#: three EVIDENCE suggestions; this only bounds a pathological response, and the
#: overflow is logged rather than dropped silently.
_MAX_BACKFILLED_PER_TURN = 5

#: Turns of silence after which an engine-inferred need is superseded.
#:
#: Inferred needs need their own garbage collection because they are, by the
#: engine's own definition, malformed: ``_apply_evidence_need_updates`` REJECTS a
#: model-emitted causal create with no motivating hypothesis precisely because
#: "a need born with no motivator at all has none to key on, so it would never
#: be auto-cleaned" — and ``_sweep_needs_for_terminal_hypotheses`` does key off a
#: terminal motivator, so it skips these forever. Backfill has to create that
#: shape anyway (it knows an ask was made, not which candidate it separates), so
#: it owes the pool a different cleanup rule.
#:
#: Silence is the right signal: every inferred need records its creating turn in
#: ``surfaced_turns``, so "the agent has not repeated this ask for N turns" is
#: always answerable, and it means the agent moved on. Left alone these
#: accumulate for the life of the case, occupy the three rotating surface slots
#: ahead of model-authored discriminators (the surfacing sort key is
#: rarity-then-id, NOT priority), inflate the "…and N more not shown" notice, and
#: are reported as unmet discriminating data in the insufficient-evidence close
#: record.
_INFERRED_NEED_SILENCE_TURNS = 5

#: Rationale carried by an engine-inferred need. Provenance is recorded
#: structurally on ``EvidenceNeed.engine_inferred``, not here — the
#: ``<evidence_needs>`` block never renders ``rationale``, so this string is for
#: readers of the pool (API, DB, tests), not for the prompt.
_BACKFILL_RATIONALE = (
    "Recorded from an EVIDENCE suggestion the agent raised without declaring a "
    "need, so the ask has a durable identity (repeat count, obtainability)."
)

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Words carried by nearly every evidence ask; they inflate the overlap score
#: between two unrelated requests without discriminating between them.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "could",
        "data",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "output",
        "please",
        "provide",
        "share",
        "show",
        "that",
        "the",
        "then",
        "this",
        "to",
        "with",
        "you",
        "your",
    }
)


def _tokens(text: str) -> frozenset[str]:
    """Content words of ``text``, lowercased, stopwords removed."""
    return frozenset(
        w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1
    )


def _similarity(left: str, right: str) -> float:
    """Overlap coefficient of the two strings' content words (0.0 – 1.0).

    Token-set rather than sequence-based: the same ask reworded across turns
    keeps its content words and loses its order, so order-sensitive similarity
    scores every repeat as new.

    Intersection over the SMALLER set, not over the union. These asks vary
    wildly in length — the model states a request tersely on one turn and
    expands it into a paragraph on the next — and dividing by the union charges
    that expansion as dissimilarity. On the fm#1079 transcript the union form
    scored consecutive re-asks for the same record at 0.40–0.53 and folded none
    of them; the containment form scores them well above threshold. See
    ``_MATCH_THRESHOLD`` for the calibration.
    """
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    shared = len(a & b)
    if shared < _MIN_SHARED_TOKENS:
        # One word in common is coincidence, not a repeat — see
        # ``_MIN_SHARED_TOKENS``. Scored 0.0 rather than returned raw so a short
        # candidate cannot clear the threshold on a single generic noun.
        return 0.0
    return shared / min(len(a), len(b))


def _best_match(
    ask_text: str, candidates: Iterable[EvidenceNeed]
) -> EvidenceNeed | None:
    """The outstanding need whose ``request_text`` best matches ``ask_text``,
    or ``None`` when nothing clears ``_MATCH_THRESHOLD``."""
    best: EvidenceNeed | None = None
    best_score = _MATCH_THRESHOLD
    for need in candidates:
        score = _similarity(ask_text, need.request_text)
        if score >= best_score:
            best, best_score = need, score
    return best


def _ask_text(follow_up: Any) -> str:
    """The user-visible text of an EVIDENCE suggestion.

    ``body`` carries the actual request; ``label`` is a short button caption
    ("Share target provider details") that is often identical across turns for
    different asks, so it is only a fallback.
    """
    body = (getattr(follow_up, "body", None) or "").strip()
    if body:
        return body
    return (getattr(follow_up, "label", None) or "").strip()


#: Turn-metadata flags whose branches REPLACE ``follow_ups`` wholesale further
#: down ``_process_turn_impl``. An ask on one of these turns is never rendered,
#: so it must not be recorded — see ``suggestions_are_engine_replaced``.
_REPLACEMENT_METADATA_FLAGS = (
    "resolution_ready_for_confirmation",
    "resolution_suggest_close",
    "resolution_needs_info_first_pass",
    "close_pivoted_to_resolve",
    "rca_infeasible_closure_message",
    "override_suggestions",
)


def suggestions_are_engine_replaced(
    case: Case,
    metadata: dict[str, Any],
    gate_pending: bool,
) -> bool:
    """Whether the engine will discard the LLM's suggestions on this turn.

    Linking has to run before ``repository.save`` (needs must persist with the
    turn), but the branches that swap ``follow_ups`` for an engine-owned list run
    *after* the save. So the decision has to be predicted here rather than
    observed.

    It matters because recording is what drives mention decay. On a gate turn the
    engine always substitutes the confirm/refine pair, so an EVIDENCE suggestion
    the model emitted is never rendered; counting it anyway would push a need
    toward "third+: stop surfacing" for asks the user never saw — retiring a live
    request precisely because it kept being suppressed.

    NOT conservative by construction — the drift runs the unsafe way. This is a
    hand-maintained mirror of ``_process_turn_impl``: a renamed flag is caught by
    a test, but a NEW replacement branch keyed on a new flag is not, and its
    effect is OVER-recording (decaying asks the user never saw). Any change to
    the suggestion-replacement branches has to be reflected here.
    """
    if case.is_terminal or case.state != CaseState.INVESTIGATING:
        # Terminal turns carry engine-owned regen/runbook cards. INQUIRY is
        # treated as replaced too, though the reason is weaker than it looks:
        # Gate 1 substitutes the confirm/refine pair only once a problem
        # statement has been PROPOSED (``_gate1_is_pending``), so on a
        # pre-proposal turn the model's EVIDENCE suggestions do reach the user
        # and go unrecorded. That is the safe direction — the ask starts its
        # history a turn late rather than decaying early — and INQUIRY asks are
        # scene-setting rather than the repeated discriminator demands this
        # mechanism exists to bound.
        return True
    if gate_pending:
        return True
    return any(metadata.get(flag) for flag in _REPLACEMENT_METADATA_FLAGS)


def link_evidence_suggestions_to_needs(
    case: Case,
    follow_ups: Iterable[Any],
    metadata: dict[str, Any],
    current_turn: int,
    resolve_id_ref: Callable[[str, list[str], str], str],
) -> None:
    """Attach every EVIDENCE suggestion to a need and record the ask.

    Mutates ``case.evidence_needs`` (creating needs where an ask has none) and
    the ``SuggestedFollowUp`` objects themselves (setting ``evidence_need_id``),
    then records ``current_turn`` on each need's ``surfaced_turns``.

    Must run BEFORE the turn's ``repository.save`` so created needs and the ask
    history land in the same turn's persisted state, and before
    ``_flatten_follow_ups`` so the wire response carries the resolved IDs.

    Only call this when the suggestions will actually reach the user — see
    ``suggestions_are_engine_replaced`` at the call site. Recording an ask the
    engine then discards would decay a request the user never saw.

    Args:
        case: the case being updated (needs are appended to its pool).
        follow_ups: this turn's ``SuggestedFollowUp`` objects.
        metadata: turn metadata; ``evidence_needs_updated`` is read to resolve
            the model's own ``new_index_N`` refs. Needs created here are NOT
            appended to it: linking assigns a real ``eneed_`` id, so the flatten
            seam never has to resolve them, and growing that list would shift
            the index space out from under a model-emitted ``new_index_N``,
            silently resolving it to an unrelated engine-created need instead of
            dropping it.
        current_turn: the turn being processed.
        resolve_id_ref: the engine's ``_resolve_id_ref``, passed in so this
            module stays free of a ``MilestoneEngine`` import.
    """
    created_this_turn = 0

    for follow_up in follow_ups or []:
        if getattr(follow_up, "action_type", None) != "EVIDENCE":
            continue

        ask_text = _ask_text(follow_up)
        if not ask_text:
            continue

        target: EvidenceNeed | None = None
        # True only when the MODEL's own declaration resolved to a real need.
        # Everything else means the engine picked the need, which is what the
        # unlinked-suggestion metric observes.
        linked_by_model = False

        # 1. The model declared the link itself — the intended path.
        declared = getattr(follow_up, "evidence_need_id", None)
        if declared:
            resolved = resolve_id_ref(
                declared, metadata.get("evidence_needs_updated", []), "eneed"
            )
            # Outstanding only, matching the fallback path below. A model
            # declaring a FULFILLED/SUPERSEDED need's id would otherwise record
            # the ask on a dead need — which the `<evidence_needs>` block never
            # renders, so the count would be invisible while the live ask it
            # belongs to counted nowhere at all.
            target = next(
                (
                    n
                    for n in case.evidence_needs
                    if n.need_id == resolved and n.is_outstanding
                ),
                None,
            )
            if target is None:
                logger.warning(
                    "EVIDENCE suggestion declared evidence_need_id %r which "
                    "resolves to no need on case %s; falling back to matching",
                    declared,
                    case.case_id,
                )
            else:
                linked_by_model = True

        # 2. Match the ask against what is already outstanding — this is what
        #    turns a re-ask into a second mention instead of a new need.
        if target is None:
            target = _best_match(
                ask_text, [n for n in case.evidence_needs if n.is_outstanding]
            )

        # 3. Genuinely new ask: record it so the next repeat has something to
        #    match against.
        if target is None:
            if created_this_turn >= _MAX_BACKFILLED_PER_TURN:
                logger.warning(
                    "Backfill cap reached on case %s turn %s; EVIDENCE "
                    "suggestion left unlinked: %.80r",
                    case.case_id,
                    current_turn,
                    ask_text,
                )
                continue
            target = EvidenceNeed(
                case_id=case.case_id,
                # Always CAUSAL_VERIFICATION. An EVIDENCE suggestion is a
                # request for data that would decide something, and the two
                # mechanisms this need has to reach are both causal-scoped: the
                # surface cap (``select_surfaced_causal_needs``) and the
                # obtainability wall — ``_apply_evidence_need_updates`` applies
                # a model-declared ``obtainability`` ONLY when the need is
                # causal, and the model validator coerces it back to UNKNOWN
                # otherwise. Filing an inferred need as SYMPTOM would silently
                # discard the very declaration the prompt's "a refused ask is a
                # wall" rule tells the model to make.
                purpose=NeedPurpose.CAUSAL_VERIFICATION,
                request_text=ask_text[:500],
                rationale=_BACKFILL_RATIONALE,
                priority=NeedPriority.MEDIUM,
                created_at_turn=current_turn,
                # Provenance, load-bearing rather than decorative — see the
                # field docstring and ``_awaiting_recent_evidence``.
                engine_inferred=True,
            )
            # motivating_hypothesis_ids stays empty: the engine knows an ask was
            # made, not which candidate it separates. Inventing a motivator
            # would let the terminal-hypothesis sweep supersede an ask still
            # being shown, and would let one refusal declare a wall on a
            # candidate the data never spoke to. KNOWN LIMIT of that choice:
            # ``verification_status._candidate_unresolvable`` only counts a need
            # as a discriminator when the candidate is in its motivating list,
            # so declaring an inferred need UNOBTAINABLE stops the nagging
            # (surfacing) but cannot on its own carry a case to
            # INSUFFICIENT_EVIDENCE. Reaching that arm needs a model-authored
            # need, which is what the prompt still asks for.
            case.evidence_needs.append(target)
            created_this_turn += 1
            _count(backfilled=True)
            logger.info(
                "Backfilled EvidenceNeed %s on case %s from an unlinked "
                "EVIDENCE suggestion",
                target.need_id,
                case.case_id,
            )
        elif not linked_by_model:
            # The engine — not the model — decided which need this ask belongs
            # to. Includes the dangling-declared case (the model named an id
            # that resolved to nothing and we matched instead), which is exactly
            # the mis-linking `resolution="matched"` exists to observe; gating
            # on "the model sent no id at all" missed it.
            _count(backfilled=False)

        follow_up.evidence_need_id = target.need_id
        target.record_surfaced(current_turn)


def _count(*, backfilled: bool) -> None:
    """Record that an ask arrived without a model-declared need.

    Best-effort: metrics never block a turn. Split created/matched because they
    read differently — sustained ``created`` means the model is authoring asks
    it never declares; sustained ``matched`` means it is re-asking for something
    already in the pool without linking to it.
    """
    try:
        from faultmaven.core.investigation.lifecycle_metrics import (
            evidence_suggestion_unlinked_total,
        )

        evidence_suggestion_unlinked_total.labels(
            resolution="created" if backfilled else "matched"
        ).inc()
    except Exception:
        pass


def sweep_silent_inferred_needs(case: Case, current_turn: int) -> int:
    """Supersede engine-inferred needs the agent has stopped asking for.

    Returns the number superseded.

    The garbage collector inferred needs would otherwise never get. A causal
    need with no motivating hypothesis is the orphan shape
    ``_sweep_needs_for_terminal_hypotheses`` cannot reach — it keys off a
    terminal motivator id, and these have none — which is exactly why
    ``_apply_evidence_need_updates`` refuses to let the MODEL create one. The
    engine has to create them anyway (it knows an ask was made, not which
    candidate it separates), so this is the cleanup that makes that safe.

    Only inferred needs are swept, and only OUTSTANDING ones. A model-authored
    need is the model's deliberate demand and is not the engine's to retire; a
    FULFILLED or SUPERSEDED need is already terminal.

    Silence, not age, is the trigger — a need the agent is still repeating stays,
    however old, and one it dropped goes, however recently created. Every
    inferred need carries its creating turn in ``surfaced_turns`` (backfill
    records the surfacing in the same breath as the creation), so
    ``last_surfaced_turn`` is always set and the rule is always answerable.

    Run BEFORE linking each turn, so a need that IS re-asked this turn is
    refreshed by the linking pass rather than swept a moment earlier.
    """
    swept = 0
    for need in case.evidence_needs:
        if not need.engine_inferred or not need.is_outstanding:
            continue
        last = need.last_surfaced_turn
        if last is None or current_turn - last < _INFERRED_NEED_SILENCE_TURNS:
            continue
        prior_state = need.state
        need.state = NeedState.SUPERSEDED
        need.superseded_reason = (
            f"engine-inferred ask not repeated for "
            f"{_INFERRED_NEED_SILENCE_TURNS} turns"
        )
        need.revoke_obtainability_if_terminal()
        swept += 1
        try:
            from faultmaven.core.investigation.lifecycle_metrics import (
                evidence_need_status_changed_total,
            )

            evidence_need_status_changed_total.labels(
                from_state=prior_state.value,
                to_state=NeedState.SUPERSEDED.value,
            ).inc()
        except Exception:
            pass
    if swept:
        logger.info(
            "Superseded %d silent engine-inferred need(s) on case %s at turn %s",
            swept,
            case.case_id,
            current_turn,
        )
    return swept
