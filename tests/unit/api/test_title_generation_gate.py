"""Tests for the single titleability gate on ``POST /cases/{case_id}/title``.

Titleability is decided by **substance** and nothing else: a confirmed/proposed
problem statement, or ``MIN_CONTENT_LENGTH_FOR_TITLE`` characters of evidence,
file summaries and user chat. There is deliberately no second, turn-count gate —
see the constants block in ``modules/case/api/routes.py`` for why the two ANDed
gates that used to sit here were residue of an incomplete replacement rather
than a policy.

The property these tests exist to hold is the one fm#1069 reported broken: **the
gate must key on content, not on how many times the user typed.** A case driven
by a log dump or a page capture is substantive after ONE turn, and a case where
someone said "hi" five times is not substantive after five.

The gate raises ``ValidationException`` (mapped to HTTP 422 by the global handler
in ``api/exception_handlers.py`` — see
``docs/architecture/specifications/exception-contract.md``). Tests assert on the
raised type and ``str(exc)`` rather than a wrapped HTTP response shape; HTTP
translation happens at the app boundary.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes import (
    MIN_CONTENT_LENGTH_FOR_TITLE,
    generate_case_title,
)
from faultmaven.modules.case.contracts import CaseState
from faultmaven.modules.case.domain.models import (
    Case,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
)

# The Case model enforces created_at <= resolved_at, so a terminal fixture needs
# two ordered instants rather than two calls to "now".
_EARLIER = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
_LATER = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def _make_case(**overrides) -> Case:
    """A real Case — the gate reads nested attributes a MagicMock would fake."""
    defaults = {
        "case_id": f"case_{uuid4().hex[:12]}",
        "title": "Case-260101-1",
        "description": "",
        "user_id": "user_123",
        "organization_id": "org_test123",
        "state": CaseState.INQUIRY,
    }
    defaults.update(overrides)
    return Case(**defaults)


def _conversation(user_turns: int, words_per_turn: int = 3) -> str:
    """Render ``user_turns`` user messages in the format the extractor parses."""
    lines = []
    for i in range(user_turns):
        lines.append(f"User: {' '.join(f'word{i}x{w}' for w in range(words_per_turn))}")
        lines.append(f"Assistant: ack {i}")
    return "\n".join(lines)


@pytest.fixture
def mock_request():
    request = MagicMock()
    request.app.state.llm_provider = AsyncMock()
    request.app.state.llm_provider.generate = AsyncMock(
        return_value=LLMResponse(
            content="Database Connection Issues",
            confidence=0.95,
            provider="test",
            model="test-model",
            tokens_used=10,
            response_time_ms=100,
        )
    )
    return request


@pytest.fixture
def mock_response():
    response = MagicMock()
    response.headers = {}
    return response


@pytest.fixture
def mock_user():
    return UserDTO(
        user_id="user_123",
        username="testuser",
        email="test@example.com",
        display_name="Test User",
        is_active=True,
    )


def _service_for(case: Case, context_text: str = "") -> AsyncMock:
    """A case service that serves ``case`` and ``context_text``, and accepts writes.

    ``update_case`` really mutates the case: the endpoint re-reads and compares
    after persisting, so a write-accepting stub that keeps serving the old title
    would fail that verification and mask what the test is actually asserting.
    """
    service = AsyncMock()
    service.get_case = AsyncMock(return_value=case)
    service.get_case_conversation_context = AsyncMock(return_value=context_text)

    async def _update(case_id, updates, user_id=None):
        if "title" in updates:
            case.title = updates["title"]
        return True

    service.update_case = AsyncMock(side_effect=_update)
    return service


async def _generate(case_service, mock_request, mock_response, mock_user, case_id):
    return await generate_case_title(
        case_id=case_id,
        request=mock_request,
        response=mock_response,
        request_body=None,
        force=False,
        case_service=case_service,
        current_user=mock_user,
    )


@pytest.mark.unit
class TestSubstanceGateIgnoresTurnCount:
    """The regression fm#1069 reported: turn count must not gate titling."""

    @pytest.mark.asyncio
    async def test_single_turn_with_problem_statement_is_titleable(
        self, mock_request, mock_response, mock_user
    ):
        """ONE user turn + a confirmed problem statement → titled, not refused.

        This is the upload/capture-driven case the substance gate was written for
        (#477) and that the turn gate in front of it made unreachable. A problem
        statement is by itself a title-grade summary.
        """
        case = _make_case()
        case.inquiry.proposed_problem_statement = (
            "Checkout API returns 502 for 30% of requests since the v2.1.3 deploy"
        )
        service = _service_for(case, _conversation(user_turns=1))

        result = await _generate(
            service, mock_request, mock_response, mock_user, case.case_id
        )

        assert result.title
        service.update_case.assert_awaited_once()
        assert service.update_case.await_args.args[1] == {"title": result.title}

    @pytest.mark.asyncio
    async def test_single_turn_with_bulk_evidence_is_titleable(
        self, mock_request, mock_response, mock_user
    ):
        """ONE user turn + evidence summaries past the threshold → titled.

        The uploader typed almost nothing; the case still carries kilobytes of
        analysed data. Substance, not turns.
        """
        case = _make_case()
        case.evidence = [
            Evidence(
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                primary_purpose="Establish the saturation symptom",
                summary="Postgres connection pool exhausted; "
                + ("saturation observed across every app pod. " * 6),
                source_type=EvidenceSourceType.LOGS,
                source_file_id=f"file_{uuid4().hex[:12]}",
                collected_by="preprocessing",
                collected_at_turn=1,
            )
        ]
        service = _service_for(case, _conversation(user_turns=1))

        result = await _generate(
            service, mock_request, mock_response, mock_user, case.case_id
        )

        assert result.title
        service.update_case.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_many_turns_without_substance_is_still_refused(
        self, mock_request, mock_response, mock_user
    ):
        """Five turns of small talk → refused. Turns are not a licence to title.

        This is the other half of the property: dropping the turn gate must not
        let a thin case through just because the user typed a lot of nothing.
        """
        case = _make_case()
        service = _service_for(case, _conversation(user_turns=5, words_per_turn=3))

        with pytest.raises(ValidationException) as exc_info:
            await _generate(
                service, mock_request, mock_response, mock_user, case.case_id
            )

        assert f"at least {MIN_CONTENT_LENGTH_FOR_TITLE} characters" in str(
            exc_info.value
        )
        service.update_case.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_case_is_refused(self, mock_request, mock_response, mock_user):
        """No statement, no evidence, no files, no chat → refused.

        The 0-turn case the removed turn gate used to catch. It is still caught,
        by the gate that measures what the case actually contains.
        """
        case = _make_case()
        service = _service_for(case, "")

        with pytest.raises(ValidationException) as exc_info:
            await _generate(
                service, mock_request, mock_response, mock_user, case.case_id
            )

        assert "currently 0 characters" in str(exc_info.value)
        service.update_case.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_fetch_failure_does_not_pass_the_gate(
        self, mock_request, mock_response, mock_user
    ):
        """A failed context read falls back to case metadata — and is still gated.

        The fallback text is built from the case's own title/description, so a
        case with neither must not become titleable by way of the error path.
        """
        case = _make_case()
        service = _service_for(case)
        service.get_case_conversation_context = AsyncMock(
            side_effect=RuntimeError("db")
        )

        with pytest.raises(ValidationException):
            await _generate(
                service, mock_request, mock_response, mock_user, case.case_id
            )

        service.update_case.assert_not_awaited()


@pytest.mark.unit
class TestTerminalCases:
    """A terminal case's title is final — unless it never got one."""

    @pytest.mark.asyncio
    async def test_terminal_case_with_real_title_is_returned_unchanged(
        self, mock_request, mock_response, mock_user
    ):
        case = _make_case(
            title="Postgres pool exhaustion",
            state=CaseState.RESOLVED,
            # A RESOLVED case is required by the model to carry a non-empty
            # description — the invariant is that a terminal case has stated its
            # problem. Terminal cases are therefore never substance-less.
            description=(
                "Checkout API returned 502 for 30% of requests after the v2.1.3 "
                "deploy; the Postgres connection pool was exhausted."
            ),
            created_at=_EARLIER,
            resolved_at=_LATER,
            closed_at=_LATER,
        )
        service = _service_for(case)

        result = await _generate(
            service, mock_request, mock_response, mock_user, case.case_id
        )

        assert result.title == "Postgres pool exhaustion"
        service.update_case.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_terminal_case_still_holding_the_placeholder_is_titled(
        self, mock_request, mock_response, mock_user
    ):
        """RESOLVED but never named — there is no final title to protect.

        A case that reached a disposition still called ``Case-YYMMDD-N`` is the
        one a user most needs named in their history.
        """
        case = _make_case(
            title="Case-260101-7",
            state=CaseState.RESOLVED,
            # A RESOLVED case is required by the model to carry a non-empty
            # description — the invariant is that a terminal case has stated its
            # problem. Terminal cases are therefore never substance-less.
            description=(
                "Checkout API returned 502 for 30% of requests after the v2.1.3 "
                "deploy; the Postgres connection pool was exhausted."
            ),
            created_at=_EARLIER,
            resolved_at=_LATER,
            closed_at=_LATER,
        )
        case.inquiry.proposed_problem_statement = (
            "Checkout API returns 502 for 30% of requests since the v2.1.3 deploy"
        )
        service = _service_for(case, _conversation(user_turns=2))

        result = await _generate(
            service, mock_request, mock_response, mock_user, case.case_id
        )

        assert result.title != "Case-260101-7"
        service.update_case.assert_awaited_once()
