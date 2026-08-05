"""How CURRENT the problem verification is — a pure derived read.

``progress.symptom_verified`` records that the problem was shown to exist. It
does not record *when*, and it never falls. So a symptom established from any
observation of the past — yesterday's log excerpt, a screenshot from last week,
a notification captured hours ago, a post-mortem attachment — satisfies it
identically to a live measurement, and the investigation proceeds as though the
problem is happening now.

This module answers the question the flag omits: **as of now, how old is the
newest observation that the symptom was actually present?** It is compute-only
and reads existing durable state (symptom-evidence coverage timestamps +
``ProblemVerification.temporal_state``). It changes no state and blocks no
transition; the assessment is surfaced to the model, which remains the
authority over the milestone.

SCOPING — this is deliberately keyed on the case's OWN temporal claim, not on
the kind of evidence. For a problem the case records as HISTORICAL (a
post-mortem, a retrospective), old symptom evidence is exactly right and there
is nothing to flag; the currency question only means something when the case
claims the problem is ONGOING. Keying on evidence shape instead — treating some
source types as inherently suspect — would both miss the stale-log case and
nag on legitimately retrospective ones.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Optional

from faultmaven.modules.case.contracts import EvidenceCategory, TemporalState

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

__all__ = [
    "SymptomCurrency",
    "STALE_AFTER",
    "assess_symptom_currency",
    "newest_symptom_observation",
]

# How old the newest symptom observation may be before an ONGOING problem's
# verification stops counting as a statement about the present.
#
# This is a policy constant, not a measurement. It is set generously on
# purpose: the cost of flagging a live problem as stale (one wasted
# re-verification) is far lower than the cost of missing a stale one (an entire
# investigation into a condition that already ended), but a threshold tight
# enough to fire mid-incident would train the model to ignore it. Thirty
# minutes is longer than the gap between observations in any actively-worked
# incident, and shorter than the gap that let a two-hour-old signal drive a
# full root-cause hunt.
STALE_AFTER = timedelta(minutes=30)


class SymptomCurrency(str, Enum):
    """How well the symptom verification speaks to the PRESENT."""

    CURRENT = "current"
    """An ONGOING problem with a symptom observation inside ``STALE_AFTER``."""

    STALE = "stale"
    """An ONGOING problem whose newest symptom observation predates
    ``STALE_AFTER``. The problem was verified to have existed; nothing
    establishes that it exists now."""

    UNDATED = "undated"
    """An ONGOING problem whose symptom evidence carries no observation time at
    all. NOT the same as CURRENT — it is the absence of an answer, and must
    never be read as an assurance. Common and often benign (configs,
    screenshots, short pastes have nothing to parse), which is why it informs
    rather than warns."""

    NOT_APPLICABLE = "not_applicable"
    """The question does not arise: the symptom is not verified yet, or the
    case records the problem as HISTORICAL, where old evidence is correct."""


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
    """
    observations = [
        ev.coverage_end_ts
        for ev in case.evidence
        if ev.category == EvidenceCategory.SYMPTOM_EVIDENCE
        and getattr(ev, "coverage_end_ts", None) is not None
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

    verification = getattr(case, "problem_verification", None)
    # Only an ONGOING claim makes currency meaningful. A case with no recorded
    # temporal state is left alone rather than assumed ongoing: inventing the
    # claim would put a re-verification demand on cases that never made one.
    if (
        verification is None
        or getattr(verification, "temporal_state", None) != TemporalState.ONGOING
    ):
        return SymptomCurrency.NOT_APPLICABLE

    observed = newest_symptom_observation(case)
    if observed is None:
        return SymptomCurrency.UNDATED

    now = now or datetime.now(timezone.utc)
    if now - observed > STALE_AFTER:
        return SymptomCurrency.STALE
    return SymptomCurrency.CURRENT
