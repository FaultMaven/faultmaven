"""How CURRENT the problem verification is — a pure derived read.

``progress.symptom_verified`` records that the problem was shown to exist. It
does not record *when*, and it never falls. So a symptom established from any
observation of the past — yesterday's log excerpt, a screenshot from last week,
a notification captured hours ago, a post-mortem attachment — satisfies it
identically to a live measurement, and the investigation proceeds as though the
problem is happening now.

This module answers the question the flag omits: **when was the symptom last
actually observed?** That instant is the investigation's window anchor. The
prompt has always said so — "the first occurrence timestamp ... becomes the
anchor for all Zone 2 searches; every evidence request in Zone 2 references
this window" — but immediately added that it is "an extracted fact, not a
tracked variable", so it lived in prose for one turn and then vanished. Nothing
downstream could reference a window it no longer had.

The cost is concrete: with the anchor lost, evidence requests default to the
present. An investigation of a symptom observed two hours ago asks for
``--since=30m``, looks at a window the problem was never in, finds nothing, and
reads that emptiness as though it meant something.

WHAT THIS IS NOT. It is not a test of whether the problem is still happening,
and staleness is not a reason to stop. FaultMaven investigates a problem that
EXISTS — evidence still collectible, root cause unidentified, solution unknown
— whether or not it is currently firing. Currency changes *how* you work it (an
active incident with user impact can take a mitigation insert; a quiet one goes
straight down the causal chain) and *where you look*, never *whether* you look.

SCOPING — keyed on the case being under investigation, not on the kind of
evidence and not on ``temporal_state``:

- Not on evidence shape. Treating some source types as inherently suspect would
  both miss the stale-log case and nag on legitimate ones; what matters is when
  the observation was made, whatever produced it.
- Not on ``temporal_state``. A window anchor is exactly as necessary for an
  inactive incident as for a live one — arguably more, since there is no
  current state to fall back on. And that field is populated only when the LLM
  happened to emit ``preliminary_urgency`` during INQUIRY, so keying on it
  would drop the anchor for reasons unrelated to the problem.

Compute-only: reads durable symptom-evidence coverage timestamps, changes no
state, blocks no transition.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Optional

from faultmaven.core.investigation.coverage_trust import is_vouched
from faultmaven.modules.case.contracts import CaseState, EvidenceCategory

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

__all__ = [
    "SymptomCurrency",
    "STALE_AFTER",
    "assess_symptom_currency",
    "newest_symptom_observation",
]

# How far back the newest symptom observation may sit before "now" stops being
# a usable proxy for the investigation window.
#
# A policy constant, not a measurement. Inside it, current-state diagnostics
# still land on the period the problem was seen in, so defaulting to the
# present is harmless. Beyond it, "now" and "when the symptom happened" are
# different windows, and an evidence request aimed at the wrong one comes back
# empty for reasons that have nothing to do with the problem.
#
# Set generously: too tight and it fires constantly mid-incident and gets
# tuned out; thirty minutes is longer than the gap between observations in an
# actively-worked incident, and well short of the two hours that had an
# investigation querying ``--since=30m`` for a symptom seen at 17:36.
STALE_AFTER = timedelta(minutes=30)


class SymptomCurrency(str, Enum):
    """How far the symptom's observation window sits from the present.

    A reading about WHERE TO LOOK, not about whether the problem still counts.
    A problem is investigable while it EXISTS — evidence collectible, cause
    unidentified, solution unknown — regardless of currency.
    """

    CURRENT = "current"
    """Observed within ``STALE_AFTER``. Present-tense diagnostics still land on
    the window the symptom was seen in."""

    STALE = "stale"
    """Observed longer ago than ``STALE_AFTER``. The investigation window is
    back THERE, not now — evidence requests must target it, and a clean
    current-state reading is not counter-evidence, it is a look at a different
    window. Says nothing about whether the problem is still worth
    investigating; it usually is."""

    UNDATED = "undated"
    """Symptom evidence carries no observation time, so the window is unknown
    and there is nothing to anchor to. NOT the same as CURRENT — it is the
    absence of an answer, and must never be read as an assurance that the
    present is the right place to look. Common and often benign (configs,
    screenshots, short pastes have nothing to parse), which is why it informs
    rather than warns."""

    NOT_APPLICABLE = "not_applicable"
    """No window to anchor: the symptom is not verified yet, or the case is not
    under investigation."""


def newest_symptom_observation(case: "Case") -> Optional[datetime]:
    """The most recent instant at which the symptom was observed present.

    Reads ``coverage_end_ts`` (the end of the span the evidence's CONTENT
    covers) across ``symptom_evidence`` rows — never ``collected_at`` /
    ``collected_at_turn``, which record when the AGENT looked and would make
    any freshly-pasted history look current.

    Absence rows are excluded by construction: they evidence that the symptom
    is GONE, so counting one would invert the reading — the strongest possible
    proof the problem stopped would register as proof it is present.

    Returns None when no symptom evidence carries a coverage timestamp.

    VOUCHED provenance only — stricter than the prompt, deliberately. The
    prompt can render an INFERRED instant with ``observed_basis`` and let the
    model weigh it; this function feeds a binary CURRENT/STALE classification
    with nowhere to put a caveat. ``syslog_bsd_noyear``'s year comes from the
    wall clock, so its age can be wrong by a whole year — the one error size
    that flips that classification. Unrecorded provenance is likewise not
    counted: a span nobody vouched for cannot establish the symptom was seen
    recently.
    """
    observations = [
        ev.coverage_end_ts
        for ev in case.evidence
        if ev.category == EvidenceCategory.SYMPTOM_EVIDENCE
        and getattr(ev, "coverage_end_ts", None) is not None
        and is_vouched(getattr(ev, "coverage_source", None))
    ]
    if not observations:
        return None
    # SQLite round-trips these columns without tzinfo; normalise so a mixed
    # set cannot raise on comparison during prompt assembly.
    normalised = [
        ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        for ts in observations
    ]
    return max(normalised)


def assess_symptom_currency(
    case: "Case", *, now: Optional[datetime] = None
) -> SymptomCurrency:
    """Classify how current the problem verification is. Pure; no side effects.

    ``now`` is injectable so callers can pin one instant across a turn (and so
    tests need no clock patching); it defaults to the wall clock.
    """
    progress = getattr(case, "progress", None)
    if progress is None or not progress.symptom_verified:
        return SymptomCurrency.NOT_APPLICABLE

    # Keyed on the case being under investigation — see the module docstring.
    # An anchor is as necessary for an inactive incident as for a live one, and
    # ``temporal_state`` is populated only when the LLM happened to emit
    # ``preliminary_urgency`` during INQUIRY, so keying on it would drop the
    # anchor for reasons unrelated to the problem.
    if getattr(case, "state", None) != CaseState.INVESTIGATING:
        return SymptomCurrency.NOT_APPLICABLE

    observed = newest_symptom_observation(case)
    if observed is None:
        return SymptomCurrency.UNDATED

    now = now or datetime.now(timezone.utc)
    if now - observed > STALE_AFTER:
        return SymptomCurrency.STALE
    return SymptomCurrency.CURRENT
