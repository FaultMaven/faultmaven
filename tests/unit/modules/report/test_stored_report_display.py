"""#1097 follow-up — the STORED report is the surface a user reads.

#1097 normalized the conclusion's fields at the read, on the premise that
terminal cases never recompute. That is true of the fields and false of the
report: a resolution summary is generated ONCE at the terminal transition and
persisted as markdown, and every read path serves that column verbatim. So the
Dashboard's Report tab kept showing the engine notation on every case resolved
before the fix — including the case the issue was filed on.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from faultmaven.modules.case.contracts import (
    CONFIRMED_ESTABLISHED_BY,
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


class _Row:
    """The stored columns ``_row_to_report`` reads, duck-typed."""

    report_id = "rep_1"
    case_id = "case_eb44251de48f"
    report_type = "resolution_summary"
    title = "Resolution Summary"
    content = _LEGACY_REPORT
    format = "markdown"
    generation_status = "completed"
    generation_time_ms = 0
    version = 1
    is_current = True
    linked_to_closure = False
    generated_by = "engine"
    generated_at = datetime(2026, 8, 19, tzinfo=timezone.utc)
    updated_at = None
    report_metadata = None
    metadata = None


@pytest.mark.parametrize(
    "repo_module",
    [
        "faultmaven.modules.case.infrastructure.sqlite_case_repository",
        "faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository",
    ],
)
def test_every_repository_normalizes_where_a_row_becomes_a_report(repo_module):
    """The boundary, not the presentation site.

    Applied per-reader this is a discipline every future consumer must opt into,
    and the first attempt had already missed one — the report DOWNLOAD endpoint,
    in a different module, served the raw column while the Report tab was clean.
    Applied here it is a property of any report loaded from storage, which is
    what makes the download path correct without knowing about it.
    """
    import importlib

    module = importlib.import_module(repo_module)
    repo_cls = next(
        obj
        for name, obj in vars(module).items()
        if name.endswith("CaseRepository") and hasattr(obj, "_row_to_report")
    )

    report = repo_cls._row_to_report(repo_cls.__new__(repo_cls), _Row())

    assert "ev_a9f662e1c86f" not in report.content
    assert "gone⇒gone" not in report.content
    assert "→ the problem" not in report.content
    assert CONFIRMED_ESTABLISHED_BY in report.content


@pytest.mark.asyncio
async def test_the_download_endpoint_serves_normalized_bytes():
    """The surface the first attempt missed, and the worse one to leave leaking:
    a download is the copy most likely to leave the product — attached to a
    ticket, pasted into a postmortem — and unlike the tab it keeps its content
    indefinitely.

    Driven through the endpoint with a repository that builds its report the
    real way (``_row_to_report``), so this asserts the BYTES a user receives
    rather than that some function was called.
    """
    from unittest.mock import AsyncMock

    from faultmaven.modules.case.api.routes import download_case_report
    from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
        SQLiteCaseRepository,
    )

    report = SQLiteCaseRepository._row_to_report(
        SQLiteCaseRepository.__new__(SQLiteCaseRepository), _Row()
    )

    case_service = AsyncMock()
    case_service.get_case = AsyncMock(return_value=object())
    case_repository = AsyncMock()
    case_repository.get_report = AsyncMock(return_value=report)
    user = SimpleNamespace(user_id="u")

    response = await download_case_report(
        case_id="case_eb44251de48f",
        report_id="rep_1",
        format="markdown",
        case_service=case_service,
        case_repository=case_repository,
        current_user=user,
    )

    body = response.body.decode("utf-8")
    for leaked in ("ev_a9f662e1c86f", "cn_984e2337cbda", "gone⇒gone", "→ the problem"):
        assert leaked not in body
    assert CONFIRMED_ESTABLISHED_BY in body
