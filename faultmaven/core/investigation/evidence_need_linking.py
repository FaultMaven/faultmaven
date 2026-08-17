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
nothing. Backfill is the floor, not the intended path — a need created here is
marked as such in its rationale so a reader can tell an engine-inferred ask from
one the model reasoned about.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from faultmaven.modules.case.contracts import (
    EvidenceNeed,
    NeedPriority,
    NeedPurpose,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

logger = logging.getLogger(__name__)


#: Token-set overlap at or above which an ask is treated as a repeat of an
#: existing outstanding need rather than a new one. Deliberately permissive:
#: the failure this module exists to prevent is treating a repeat as new (the
#: pool fills with near-duplicates and the ask count never rises), which is
#: worse than occasionally folding two genuinely distinct asks together — a
#: folded pair still surfaces, it just shares one counter.
_MATCH_THRESHOLD = 0.55

#: Cap on needs CREATED per turn by backfill. A well-behaved turn emits one to
#: three EVIDENCE suggestions; this only bounds a pathological response, and the
#: overflow is logged rather than dropped silently.
_MAX_BACKFILLED_PER_TURN = 5

#: Marks a need the engine inferred from a suggestion rather than one the model
#: authored through ``evidence_need_updates``. Rendered nowhere special — it is
#: the need's rationale — but it makes provenance legible in the pool and in the
#: ``<evidence_needs>`` block.
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
    """Jaccard overlap of the two strings' content words (0.0 – 1.0).

    Token-set rather than sequence-based: the same ask reworded across turns
    ("provide the target-account provider record" / "have the analytics team
    return the provider record for the target account") keeps its content words
    and loses its order, so order-sensitive similarity scores it as new.
    """
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


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

    Args:
        case: the case being updated (needs are appended to its pool).
        follow_ups: this turn's ``SuggestedFollowUp`` objects.
        metadata: turn metadata; ``evidence_needs_updated`` is read to resolve
            ``new_index_N`` refs and appended to for needs created here.
        current_turn: the turn being processed.
        resolve_id_ref: the engine's ``_resolve_id_ref``, passed in so this
            module stays free of a ``MilestoneEngine`` import.
    """
    metadata.setdefault("evidence_needs_updated", [])
    created_this_turn = 0

    # Purpose for a backfilled need. An ask raised before any hypothesis exists
    # is symptom-shaped (it is establishing what is happening); once a
    # differential exists, an ask is discriminating between its branches. The
    # motivating hypothesis is deliberately left empty — the engine knows the
    # ask was made, not which candidate it was meant to separate, and inventing
    # a motivator would let the terminal-hypothesis sweep supersede an ask the
    # user was still being shown.
    inferred_purpose = (
        NeedPurpose.CAUSAL_VERIFICATION
        if case.hypotheses
        else NeedPurpose.SYMPTOM_VERIFICATION
    )

    for follow_up in follow_ups or []:
        if getattr(follow_up, "action_type", None) != "EVIDENCE":
            continue

        ask_text = _ask_text(follow_up)
        if not ask_text:
            continue

        target: EvidenceNeed | None = None

        # 1. The model declared the link itself — the intended path.
        declared = getattr(follow_up, "evidence_need_id", None)
        if declared:
            resolved = resolve_id_ref(
                declared, metadata.get("evidence_needs_updated", []), "eneed"
            )
            target = next(
                (n for n in case.evidence_needs if n.need_id == resolved), None
            )
            if target is None:
                logger.warning(
                    "EVIDENCE suggestion declared evidence_need_id %r which "
                    "resolves to no need on case %s; falling back to matching",
                    declared,
                    case.case_id,
                )

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
                purpose=inferred_purpose,
                request_text=ask_text[:500],
                rationale=_BACKFILL_RATIONALE,
                priority=NeedPriority.MEDIUM,
                created_at_turn=current_turn,
            )
            case.evidence_needs.append(target)
            metadata["evidence_needs_updated"].append(target.need_id)
            created_this_turn += 1
            _count(backfilled=True)
            logger.info(
                "Backfilled EvidenceNeed %s (purpose=%s) on case %s from an "
                "unlinked EVIDENCE suggestion",
                target.need_id,
                target.purpose.value,
                case.case_id,
            )
        elif not declared:
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
