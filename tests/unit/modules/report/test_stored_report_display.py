"""#1097 follow-up — the STORED report is the surface a user reads.

#1097 normalized the conclusion's fields at the read, on the premise that
terminal cases never recompute. That is true of the fields and false of the
report: a resolution summary is generated ONCE at the terminal transition and
persisted as markdown, and every read path serves that column verbatim. So the
Dashboard's Report tab kept showing the engine notation on every case resolved
before the fix — including the case the issue was filed on.
"""

import pytest

from faultmaven.modules.case.contracts import CONFIRMED_ESTABLISHED_BY
from faultmaven.modules.report.domain.services.report_display import (
    normalize_stored_report_content,
)

pytestmark = pytest.mark.unit

# The stored report for case_eb44251de48f, as persisted (abridged).
_LEGACY_REPORT = """# Resolution Summary: Checkout-api Degraded

## Root Cause

checkout-api v2.14.0 retains an unbounded orderSummaryCache

_Established by: engine: user-confirmed resolution at turn 8 — causal-absence \
ev_a9f662e1c86f bears on root cn_984e2337cbda (M2 gone⇒gone)._

**How it produced the symptom:** JVM heap pressure causes prolonged GC pauses \
and readiness failure before container OOM termination → the problem

## Confirming Evidence

- **[causal evidence]** heap reached 380MB of 384MB — _02-container.log_
"""


def test_a_stored_legacy_report_serves_without_engine_notation():
    out = normalize_stored_report_content(_LEGACY_REPORT)

    for leaked in ("ev_a9f662e1c86f", "cn_984e2337cbda", "gone⇒gone", "→ the problem"):
        assert leaked not in out
    assert f"_Established by: {CONFIRMED_ESTABLISHED_BY}._" in out
    # The provenance is restated, not deleted.
    assert "Established by:" in out


def test_the_rest_of_the_report_is_untouched():
    """Only the two generator-written lines are eligible; everything else —
    including evidence prose that may itself mention ids — passes through."""
    out = normalize_stored_report_content(_LEGACY_REPORT)

    assert "# Resolution Summary: Checkout-api Degraded" in out
    assert "- **[causal evidence]** heap reached 380MB of 384MB" in out
    assert "checkout-api v2.14.0 retains an unbounded orderSummaryCache" in out
    assert out.count("\n") == _LEGACY_REPORT.count("\n")


def test_a_current_report_round_trips_byte_identical():
    """The rewrite is self-limiting: a report generated after #1097 can never
    match, so this only ever touches legacy rows."""
    current = (
        "## Root Cause\n\n"
        f"_Established by: {CONFIRMED_ESTABLISHED_BY}._\n\n"
        "**How it produced the symptom:** heap grows until the container dies\n"
    )

    assert normalize_stored_report_content(current) is current


def test_evidence_prose_naming_an_id_is_not_rewritten():
    """The rewrite is anchored on the generator's own labels, so an id inside a
    user's evidence — or an LLM's prose — is not eligible."""
    report = (
        "## Confirming Evidence\n\n"
        "- **[causal evidence]** operator cited ev_a9f662e1c86f in the ticket\n"
    )

    assert normalize_stored_report_content(report) is report


def test_empty_and_missing_content_pass_through():
    assert normalize_stored_report_content(None) is None
    assert normalize_stored_report_content("") == ""


def test_the_read_path_applies_it():
    """All three report read endpoints funnel through ReportResponse.from_domain."""
    from faultmaven.modules.case.domain.owned_models.report import (
        CaseReport,
        ReportStatus,
        ReportType,
    )
    from faultmaven.modules.report.api.routes import ReportResponse

    report = CaseReport(
        case_id="case_eb44251de48f",
        report_type=ReportType.RESOLUTION_SUMMARY,
        title="Resolution Summary",
        content=_LEGACY_REPORT,
        generation_status=ReportStatus.COMPLETED,
        generation_time_ms=0,
    )

    assert "ev_a9f662e1c86f" not in ReportResponse.from_domain(report).content
