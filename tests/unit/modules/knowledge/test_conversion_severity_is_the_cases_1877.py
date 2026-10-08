"""#1877 — a runbook harvested from a case carries the severity the case
recorded, and none when it recorded none (no invented "medium")."""

import pytest

from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.problem import (
    InquiryData,
    ProblemVerification,
)
from faultmaven.modules.knowledge.domain.models.conversion import CaseConversionRequest

pytestmark = pytest.mark.unit


def _case(**pv_fields) -> Case:
    return Case(
        case_id="case_aa0000000004",
        user_id="u",
        enterprise_id="o",
        title="Checkout OOM kills",
        description="checkout-api crash-loops after v2.14.0.",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="checkout-api crash-loops",
            problem_statement_confirmed=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout-api crash-loops", **pv_fields
        ),
    )


def test_a_case_with_no_severity_converts_with_none():
    assert CaseConversionRequest.from_case(_case()).severity is None


def test_an_assessed_severity_is_carried_lowercase():
    assert CaseConversionRequest.from_case(_case(severity="HIGH")).severity == "high"


def test_a_case_with_no_record_converts_with_none():
    case = _case()
    case.problem_verification = None
    assert CaseConversionRequest.from_case(case).severity is None


# --- what the model is told -------------------------------------------------

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from faultmaven.modules.knowledge.domain.models.conversion import (  # noqa: E402
    FailureModeAnalysis,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (  # noqa: E402
    ConversionService,
)
from faultmaven.modules.knowledge.taxonomy import (  # noqa: E402
    RunbookSeverity,
    render_vocabulary,
)


async def _user_message_for(severity) -> str:
    """Drive the runbook prompt build for a case-sourced failure mode and
    return the user message handed to the (mocked) router."""
    request = CaseConversionRequest.from_case(
        _case(**({"severity": severity} if severity else {}))
    )
    failure_mode = FailureModeAnalysis(
        id=f"case-{request.case_id}",
        title=request.title,
        domain=request.domain,
        service=request.service,
        symptom_class=[],
        severity=request.severity,
        symptoms_summary=request.description,
        resolution_summary="root cause",
    )
    router = AsyncMock()
    router.route.side_effect = RuntimeError("stop after capture")
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    service = ConversionService(
        llm_router=router,
        settings=settings,
        db_session_factory=None,
        knowledge_service=None,
    )
    await service._convert_single_failure_mode(
        text="source",
        failure_mode=failure_mode,
        scope="personal",
        filename="case.md",
        conversion_id="conv_1",
        user_id="u",
        enterprise_id="o",
    )
    messages = router.route.await_args.kwargs["messages"]
    return next(m["content"] for m in messages if m["role"] == "user")


async def test_an_unassessed_severity_names_the_allowed_vocabulary():
    """The runbook's vocabulary, rendered from its owner (#1886) — ``info``
    included, which a hand-written list here had left out."""
    message = await _user_message_for(None)
    assert (
        "SEVERITY: (not assessed — choose one of critical, high, medium, low, "
        "info from the source material)"
    ) in message
    assert render_vocabulary(RunbookSeverity) in message


async def test_an_assessed_severity_is_stated():
    assert "SEVERITY: high\n" in await _user_message_for("HIGH")
