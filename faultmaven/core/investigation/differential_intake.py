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
  - provenance is load-bearing: a fallback-only ``IDENTIFIED`` is treated
    differently downstream (held from auto-resolution / lower surfaced
    confidence). ``provenance → weight`` (runbook ≤ 0.5, fallback ≤ 0.2) is a
    process-side mapping; it is deliberately NOT a field on the verdict,
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
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from faultmaven.core.investigation.cause_schemas import CauseRecord
    from faultmaven.modules.case.contracts import Case, Evidence, EvidenceStance


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

    STUB: returns ``[]`` until the matcher body lands (loop stays inert).
    """
    return []


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

    STUB: returns ``None`` until the matcher body lands.
    """
    return None
