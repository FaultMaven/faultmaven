"""Investigation Routing Service

This module is responsible for determining the optimal investigation path
(MITIGATION_FIRST vs ROOT_CAUSE) based on urgency and temporal state.

The router returns a *recommendation*; the user confirms or overrides at
Gate 2. The router is a pure helper — it does NOT mutate the case. The
engine renders the recommendation into the Gate 2 affordance, and only on
the user's Gate 2 click does ``case.path_selection`` get created. Existence
of ``case.path_selection`` IS the commit (see ``engine_owned_affordances``
and ``_gate2_is_pending`` in milestone_engine.py).

Ambiguous cases default to ROOT_CAUSE with auto_selected=False and an honest
rationale; the user can switch to MITIGATION_FIRST in Gate 2 if they have
out-of-band context (e.g., mitigation already applied elsewhere, telemetry
doesn't capture the full impact).

Note: The rca_infeasible advisory signal on ProblemVerification does NOT affect
path selection. It influences post-mitigation agent behavior only (see §2.4 of
investigation-lifecycle-logic.md). Path selection remains purely urgency × temporal.

Design Reference:
- docs/architecture/investigation-engine/investigation-lifecycle-logic.md
"""

import logging
from typing import TYPE_CHECKING, Optional

from faultmaven.modules.case.contracts import (
    InvestigationPath,
    PathSelection,
    ProblemVerification,
    TemporalState,
    UrgencyLevel,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

logger = logging.getLogger(__name__)


def determine_investigation_path(verification: ProblemVerification) -> PathSelection:
    """
    Determine investigation path recommendation from the Urgency × Temporal matrix.

    Returns a PathSelection populated with the recommended path,
    ``auto_selected`` indicating whether the matrix matched a row (True) or
    the router fell back to the safe default (False), and a rationale string
    surfaced to the user in Gate 2.

    Matrix outcomes:
    - ONGOING + CRITICAL/HIGH   -> MITIGATION_FIRST (auto)
    - ONGOING + MEDIUM/LOW      -> ROOT_CAUSE (auto)
    - HISTORICAL + any urgency  -> ROOT_CAUSE (auto)
    - missing/UNKNOWN signals   -> ROOT_CAUSE (default, not auto-selected)

    All outcomes are recommendations — the caller is responsible for surfacing
    them at Gate 2 and only writing ``case.path_selection`` once the user
    clicks (existence == commit, see milestone_engine).

    Args:
        verification: Consolidated problem verification data

    Returns:
        PathSelection with path, auto_selected, rationale, alternate_path.
        Pure helper — does not mutate any case.
    """
    temporal = verification.temporal_state
    urgency = verification.urgency_level

    logger.info(f"Determining path for Temporal:{temporal} Urgency:{urgency}")

    # Ambiguous: missing temporal or unknown urgency.
    # Default to ROOT_CAUSE (safer for non-emergency); user overrides via Gate 2
    # if they have context the data doesn't capture.
    if not temporal or urgency == UrgencyLevel.UNKNOWN:
        return PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=False,
            rationale="Urgency or temporal signal missing — defaulting to root-cause "
            "analysis. Switch to mitigation-first if you have active impact "
            "the data doesn't capture.",
            alternate_path=InvestigationPath.MITIGATION_FIRST,
        )

    # AUTO: Ongoing + High/Critical Urgency -> MITIGATION_FIRST (then RCA)
    # Stop the bleeding before finding root cause.
    if temporal == TemporalState.ONGOING and urgency in [
        UrgencyLevel.CRITICAL,
        UrgencyLevel.HIGH,
    ]:
        return PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale=(
                f"Ongoing {urgency.value} impact — recommend mitigating first, "
                "RCA after stabilization."
            ),
            alternate_path=InvestigationPath.ROOT_CAUSE,
        )

    # AUTO: Ongoing + Low/Medium -> ROOT_CAUSE
    # Active issue but not urgent — can afford a thorough investigation.
    if temporal == TemporalState.ONGOING and urgency in [
        UrgencyLevel.LOW,
        UrgencyLevel.MEDIUM,
    ]:
        return PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=True,
            rationale=(
                f"Ongoing but {urgency.value} urgency — recommend root-cause "
                "analysis for a permanent fix."
            ),
            alternate_path=InvestigationPath.MITIGATION_FIRST,
        )

    # AUTO: Historical + any urgency -> ROOT_CAUSE
    # Past issue — immediate impact has subsided, focus on permanent fix.
    if temporal == TemporalState.HISTORICAL:
        return PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=True,
            rationale=(
                f"Historical {urgency.value} issue — recommend root-cause analysis "
                "since immediate impact has subsided."
            ),
            alternate_path=InvestigationPath.MITIGATION_FIRST,
        )

    # Defensive fallback. The matrix above covers every combination of
    # (TemporalState, UrgencyLevel) modulo the missing-signal branch, so this
    # is unreachable today; if a new enum value is added to either field the
    # router falls back to ROOT_CAUSE rather than crashing.
    logger.warning(
        f"Path matrix did not match (temporal={temporal}, urgency={urgency}); "
        "defaulting to ROOT_CAUSE"
    )
    return PathSelection(
        path=InvestigationPath.ROOT_CAUSE,
        auto_selected=False,
        rationale=(
            f"Unmatched combination ({temporal.value} + {urgency.value}) — "
            "defaulting to root-cause analysis."
        ),
        alternate_path=InvestigationPath.MITIGATION_FIRST,
    )


def recommend_investigation_path_for_case(case: "Case") -> Optional[PathSelection]:
    """Pure recommendation helper — returns a ``PathSelection`` without mutating case.

    Used at Gate 2 button-render time to determine which option to label
    "Recommended", and inside the Gate 2 click handler to construct the
    committed ``PathSelection`` from the user's choice + the recommendation
    context (rationale, alternate_path).

    Returns ``None`` if recommendation prerequisites aren't met:
      - Gate 1 not yet passed (no problem_statement_confirmed)
      - preliminary_urgency not populated

    Does NOT write to ``case.path_selection`` — caller chooses when (and if)
    to commit. The path-selection commit happens exclusively in the Gate 2
    click handler; this function is read-only.
    """
    if not case.inquiry or not case.inquiry.problem_statement_confirmed:
        return None
    pu = case.inquiry.preliminary_urgency
    if pu is None or pu.level is None:
        return None

    temporal = TemporalState.ONGOING if pu.is_ongoing else TemporalState.HISTORICAL
    severity = pu.level.value.upper() if pu.level != UrgencyLevel.UNKNOWN else "MEDIUM"

    # Build a transient ProblemVerification for the router. The case-level
    # case.problem_verification is populated lazily in
    # _transition_to_investigating; constructing one here avoids prematurely
    # materializing case state that the transition path owns.
    verification = ProblemVerification(
        symptom_statement=case.description
        or case.inquiry.proposed_problem_statement
        or "Unspecified issue",
        severity=severity,
        temporal_state=temporal,
        urgency_level=pu.level,
    )
    return determine_investigation_path(verification)
