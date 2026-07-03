"""Differential-driven collect→validate seam (process layer ⇄ matcher).

FROZEN INTERFACE. This module is the single contract that the process-layer
intake-evaluation loop and the matcher's predicate evaluator both build against,
in parallel. Until the matcher body lands, the evaluator stubs return empty, so
the loop is inert (no behavior change) — that is the agreed "build against the
stub" model.

WHY
---
Today node/hypothesis truth values are computed from LLM-authored stance/category
links. To make validation self-mediating, each submitted datum must be evaluated
deterministically — by a criterion that originates OUTSIDE the in-loop LLM, on
the raw/normalized telemetry — to decide which causes it supports/refutes. That
judgment is what ``StanceVerdict`` carries.

OWNERSHIP (no overlap)
----------------------
Matcher (predicate evaluator) owns the BODIES of the two functions below:
  - content-addressed dispatch (one datum → all active candidates' predicates),
  - the ``data_type`` metadata pre-filter (fail-open),
  - telemetry resolution: read the normalized ``UploadedFile`` (preferred over
    ``Evidence.extract`` to drop the residual LLM-slice step); NEVER
    ``Evidence.summary``,
  - runbook ``match_predicates`` as the primary (sound) source; a constrained
    LLM-authored predicate as the labeled fallback,
  - the MECE-discriminating authoring bar on sibling predicates.

Process layer owns everything AROUND them:
  - WHEN they are called (the first-class intake-evaluation step, per submission),
  - turning each ``StanceVerdict`` into the typed evidence link that
    ``derive_node_states`` already consumes, copying ``provenance`` onto the link,
  - lazy promotion: a verdict's ``cause_id`` is a DIFFERENTIAL CANDIDATE, which
    may not yet be a graph node. On the first SUPPORTS, the process layer mints
    the chain via the matcher's existing entry point
    ``runbook_cause_matcher.instantiate_cause_chain(case, cause_record, turn)``
    and then attaches the link,
  - provenance is carried so it can be load-bearing downstream. WIRED today: the
    runbook-harvest gate withholds a fallback-only ``IDENTIFIED`` from KB
    harvesting. INTENDED, not yet wired: a provenance-aware validation threshold
    (a fallback-only root needs ≥2 independent causal rows, #573) and a read-time
    lower-assurance label on the current-case conclusion (#572). ``weight`` is
    deliberately NOT a field on the verdict either way,
  - the demand side: regenerating Evidence Needs from the active differential.

TWO TIERS, SAME OUTPUT, DIFFERENT INPUT SHAPE
---------------------------------------------
  - Runbook (primary) is a many-to-many content-addressed sweep — one datum
    against every active candidate's predicates → ``evaluate_datum_against_differential``.
  - LLM-fallback is a one-to-one re-check of the predicate the LLM proposed on its
    own emitted link → ``recheck_proposed_predicate``. Its predicate arrives via
    the turn's emitted link, NOT via ``active_causes``.
Both produce the same ``StanceVerdict`` type, keeping provenance unambiguous.

SEQUENCING
----------
1. Ratify this contract (countersign).  2. Build both sides against these stubs in
parallel (process tests inject fake verdicts; matcher tests its body in isolation).
3. Integrate: swap the stubs for the real bodies; run the joint acceptance tests
(headline: a confident-but-wrong LLM with no satisfying predicate against the raw
datum cannot drive ``cause_state→IDENTIFIED``).

OPEN BUILD-PHASE DETAILS (not blocking ratification)
----------------------------------------------------
  - Exact ``cause_id`` form for a stable, cross-runbook-unique candidate id
    (e.g. ``f"{runbook_id}:{cause_letter}"``).
  - How ``active_causes`` (the candidate differential carrying ``CauseRecord``s)
    is assembled from the matched ``CauseMatchResult`` and surfaced to the intake
    step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

from faultmaven.core.investigation.indicator_evaluator import (
    evaluate_predicate_against_text,
)
from faultmaven.core.investigation.runbook_cause_matcher import resolve_datum_text
from faultmaven.modules.case.contracts import EvidenceStance

if TYPE_CHECKING:
    from collections.abc import Iterable

    from faultmaven.core.investigation.cause_schemas import CauseRecord
    from faultmaven.modules.case.contracts import Case, Evidence

# Predicate verdict → the stance it implies. ``untested`` maps to nothing (the
# predicate is silent on this datum), so it is deliberately absent from the table.
_VERDICT_STANCE = {
    "matched": EvidenceStance.SUPPORTS,
    "refuted": EvidenceStance.REFUTES,
}


@dataclass(frozen=True)
class StanceVerdict:
    """One deterministic judgment that a datum bears on a cause.

    Emitted only for a predicate that FIRED (an untested predicate yields no
    verdict). ``provenance`` is set HERE — at the point of determination, the only
    place that knows whether the firing came from a runbook predicate (sound) or
    an LLM-fallback predicate (lower-assurance) — and is copied onto the link by
    the process layer.
    """

    cause_id: str
    """DIFFERENTIAL CANDIDATE id — NOT a graph node_id. The process layer maps it
    to a node, instantiating the chain on the first SUPPORTS."""

    stance: "EvidenceStance"  # SUPPORTS | REFUTES

    provenance: Literal["runbook", "llm_fallback"]
    """``runbook`` = expert-authored predicate against telemetry (sound);
    ``llm_fallback`` = the LLM's own predicate, re-checked (lower-assurance)."""

    predicate: dict
    """Round-trippable spec of what fired — e.g.
    ``{"predicate": "contains"|"absent"|"exit_code"|"threshold", "target": ...,
    "op"?: ..., "value"?: ..., "data_type"?: ...}`` — re-runnable for audit/repro,
    not a free-text description."""

    evidence_id: str | None = None
    """The datum that produced this verdict, stamped by the process layer when the
    verdict is recorded (the evaluator judges one datum at a time but does not carry
    its id). Threads the fulfilling evidence to the demand-side reconcile: a fired
    predicate marks its Evidence Need FULFILLED, and the model requires ≥1 fulfilling
    evidence id for that state. ``None`` only on an un-recorded verdict."""


@dataclass(frozen=True)
class ActiveCause:
    """One candidate in the differential: a stable id + its full ``CauseRecord``.

    A bare ``CauseRecord`` is NOT cross-runbook-unique (every runbook has a
    "Cause A"), so it cannot mint the ``StanceVerdict.cause_id`` the process layer
    keys on. This wrapper pairs the cross-runbook-unique ``candidate_id`` with the
    record. Both fields are load-bearing:

      - ``candidate_id`` → becomes ``StanceVerdict.cause_id``; the process layer's
        verdict→link / lazy-promotion lookup keys on it.
      - ``record`` → the matcher instantiates its chain on the first SUPPORTS
        (``resolve_root(case, record, may_instantiate=True)``), and reads its
        ``match_predicates`` to evaluate.
    """

    candidate_id: str
    """Cross-runbook-unique candidate id — ``f"{runbook_id}:{cause_letter}"``
    (matcher-minted/normalized). Mirrors ``StanceVerdict.cause_id``."""

    record: "CauseRecord"
    """The full cause record — chain (for lazy instantiation) + match_predicates
    (for evaluation)."""


def assemble_active_causes(
    matched: "Iterable[tuple[str, Iterable[CauseRecord]]]",
) -> "list[ActiveCause]":
    """Build the candidate differential from matched runbooks' resolved causes.

    ``matched`` is ``(runbook_id, cause_records)`` per matched runbook — the same
    ``CauseRecord``s that go INTO the matcher's evaluator, carrying the chain (for
    lazy instantiation) and ``match_predicates`` (for evaluation) the differential
    needs. ``CauseMatchResult`` itself can't be the source: it keeps a full record
    only for the single *selected* cause (``selected_record``), whereas the
    differential holds EVERY candidate as a soft prior, pruned later by evidence.

    Each non-fallback cause becomes an ``ActiveCause`` with a cross-runbook-unique
    ``candidate_id`` ``f"{runbook_id}:{cause_letter}"``. The fallback Cause
    (``is_fallback_cause`` — its only indicator is ``[Default]``, which never
    matches) is excluded; it is a routing default, not a differential candidate.
    Duplicate candidate ids (same runbook + letter) keep the first.
    """
    active: list[ActiveCause] = []
    seen: set[str] = set()
    for runbook_id, records in matched:
        for record in records:
            if getattr(record, "is_fallback_cause", False):
                continue
            candidate_id = f"{runbook_id}:{record.cause_letter}"
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            active.append(ActiveCause(candidate_id=candidate_id, record=record))
    return active


def evaluate_datum_against_differential(
    *,
    evidence: "Evidence",
    active_causes: "list[ActiveCause]",
    case: "Case",
) -> list[StanceVerdict]:
    """Runbook (content-addressed) tier — BODY OWNED BY THE MATCHER.

    Evaluate one submitted datum against the predicates of every active candidate
    in the differential, returning one ``StanceVerdict`` per FIRING predicate
    (``provenance="runbook"``).

    Args:
        evidence: handle to the submitted datum; the body resolves the
            normalized/raw telemetry from it (never ``Evidence.summary``).
        active_causes: the candidate differential — a list of ``ActiveCause``,
            each pairing a cross-runbook-unique ``candidate_id`` (→ the verdict's
            ``cause_id``) with its full ``CauseRecord`` (``chain_nodes``/
            ``chain_edges`` for lazy instantiation on a SUPPORTS, and
            ``match_predicates`` to evaluate).
        case: for ``data_type`` classification + telemetry resolution.

    Resolves the datum's trusted digest once, then evaluates each candidate's
    ``match_predicates`` against it under subset-trust and aggregates to AT MOST
    ONE verdict per (datum, cause) — the junction PK is ``(node_id, evidence_id)``,
    so a second stance for the same pair would not survive persistence. No trusted
    content (no backing file / no index) ⇒ ``[]`` (the loop abstains, never
    refutes on missing data).
    """
    text = resolve_datum_text(evidence, case)
    if text is None:
        return []
    verdicts: list[StanceVerdict] = []
    for candidate in active_causes:
        predicates = getattr(candidate.record, "match_predicates", None) or []
        if not predicates:
            continue
        stance, fired = _aggregate_predicates(predicates, text)
        if stance is None:
            continue
        verdicts.append(
            StanceVerdict(
                cause_id=candidate.candidate_id,
                stance=stance,
                provenance="runbook",
                predicate=fired,
            )
        )
    return verdicts


def _aggregate_predicates(
    predicates: list, text: str
) -> "tuple[Optional[EvidenceStance], Optional[dict]]":
    """Collapse a cause's predicates (vs one datum's digest) into ONE stance.

    Each predicate is evaluated under subset-trust (``complete=False``):
    ``matched`` → SUPPORTS, ``refuted`` → REFUTES, untested/unknown is silent.
    The outcome enforces the one-verdict-per-(datum, cause) invariant:

      - exactly one stance fired → that stance, plus the FIRST predicate that
        produced it (a real, re-runnable spec for ``StanceVerdict.predicate``),
      - both SUPPORTS and REFUTES fired (the cause's own predicates disagree about
        this datum) → abstain ``(None, None)`` — we never pick a winner between a
        cause's conflicting predicates,
      - nothing fired → ``(None, None)``.
    """
    fired: "dict[EvidenceStance, dict]" = {}
    for pred in predicates:
        if not isinstance(pred, dict):
            continue
        try:
            verdict = evaluate_predicate_against_text(pred, text, complete=False)
        except ValueError:
            continue  # unknown predicate name → treat as untested
        stance = _VERDICT_STANCE.get(verdict)
        if stance is not None:
            fired.setdefault(stance, pred)
    if len(fired) == 1:
        stance, pred = next(iter(fired.items()))
        return stance, pred
    return None, None


def recheck_proposed_predicate(
    *,
    evidence: "Evidence",
    cause_id: str,
    proposed_predicate: dict,
    case: "Case",
) -> "StanceVerdict | None":
    """LLM-fallback (one-to-one) tier — BODY OWNED BY THE MATCHER.

    Re-check a single LLM-proposed predicate against the datum it was emitted for.
    Returns a ``StanceVerdict`` (``provenance="llm_fallback"``) if it fires, else
    ``None``. Catches an incoherent LLM (a predicate its own cited datum does not
    satisfy); it does NOT make an LLM-authored predicate "sound".

    Same subset-trust evaluation as the runbook tier, against the same trusted
    digest — only the provenance and the one-to-one shape differ. ``None`` when
    there is no trusted content, the predicate is malformed/unknown, or it is
    untested against the digest.
    """
    if not isinstance(proposed_predicate, dict):
        return None
    text = resolve_datum_text(evidence, case)
    if text is None:
        return None
    try:
        verdict = evaluate_predicate_against_text(
            proposed_predicate, text, complete=False
        )
    except ValueError:
        return None
    stance = _VERDICT_STANCE.get(verdict)
    if stance is None:
        return None
    return StanceVerdict(
        cause_id=cause_id,
        stance=stance,
        provenance="llm_fallback",
        predicate=proposed_predicate,
    )
