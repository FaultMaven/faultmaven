"""How many report/runbook regenerations a case has left, read from the report service and repository the owner passes at call time."""

from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
)


async def _remaining_regens_for(report_service, repository, case: "Case") -> int:
    """How many regenerations the user has left for this case's
    canonical terminal summary (RESOLUTION_SUMMARY for RESOLVED,
    CLOSURE_SUMMARY for CLOSED).

    Drives both the label suffix on the regen affordance and the
    "hide when exhausted" gate. Returns ``MAX_REGENERATIONS`` (the
    cap) when the repository or report service is unavailable
    (test/degraded paths) — preserves the legacy behaviour of always
    showing the affordance when the count cannot be checked.

    Counted from the persisted ``reports`` table (each generation
    writes a new row). See ICaseRepository.count_reports.
    """
    from faultmaven.modules.case.contracts import ReportType

    if report_service is None or repository is None:
        return getattr(report_service, "MAX_REGENERATIONS", 5)
    if case.state == CaseState.RESOLVED:
        report_type = ReportType.RESOLUTION_SUMMARY
    elif case.state == CaseState.CLOSED:
        report_type = ReportType.CLOSURE_SUMMARY
    else:
        # Non-terminal cases have no regen affordance at all; the
        # value is unused by callers but keep it self-consistent.
        return getattr(report_service, "MAX_REGENERATIONS", 5)
    try:
        count = await repository.count_reports(case.case_id, report_type)
    except Exception:
        # Best-effort: if counting fails, don't strand the user
        # without an affordance. Fall back to the cap.
        return getattr(report_service, "MAX_REGENERATIONS", 5)
    max_regens = getattr(report_service, "MAX_REGENERATIONS", 5)
    return max(0, max_regens - count)
