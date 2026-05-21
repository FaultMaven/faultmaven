"""
Self-correction failure fallback behavior.

When diagnostic reasoning validation fails on an LLM response, the engine
attempts a single self-correction retry. If that retry also fails (either
the retried prose still doesn't validate, or the retry call raises), the
old behavior silently shipped validator-rejected prose to the user.

The new behavior substitutes ``agent_response`` with a brief honest
fallback message while preserving the response's ``state_updates`` (the
LLM's structured output — hypotheses, evidence categorization,
milestones — typically remains usable even when prose form failed
validation).

These tests pin both failure modes against the substitution behavior so a
future refactor can't silently revert to shipping validator-rejected
content.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    ProblemVerification,
)


class _MockLLM(ILLMProvider):
    async def generate(self, prompt, **kwargs):
        return "{}"

    async def generate_stream(self, prompt, **kwargs):
        yield "mock"

    async def generate_with_history(self, messages, **kwargs):
        return "{}"

    def get_structured_output_strategy(self, schema):
        return StructuredOutputStrategy(
            capability=StructuredOutputCapability.STRICT,
            mode=StructuredOutputMode.JSON_SCHEMA_STRICT,
            include_schema_in_prompt=False,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "TestSchema",
                    "strict": True,
                    "schema": schema,
                },
            },
        )


@pytest.fixture
def mock_llm():
    llm = _MockLLM()
    llm.generate = AsyncMock()
    return llm


@pytest.fixture
def mock_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock()
    return repo


@pytest.fixture
def investigating_case():
    """An INVESTIGATING case with one evidence row.

    The reasoning validator only fires on INVESTIGATING (or later) cases
    where the response contains suggestion-like content — see
    diagnostic_reasoning_validator._is_non_diagnostic_response.
    """
    case = Case(
        case_id="case_abcdef012345",
        title="Self-correction test",
        status=CaseStatus.INVESTIGATING,
        user_id="user_test",
        organization_id="org_test",
        description="Test case for self-correction fallback",
        problem_verification=ProblemVerification(
            symptom_statement="Pods OOMKill",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_test",
            proposed_problem_statement="Pods OOMKill",
        ),
    )
    case.evidence.append(
        Evidence(
            evidence_id="ev_001122334455",
            summary="OOMKill events",
            content_ref="kubectl-events.log",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at=datetime.now(UTC),
            collected_by="user_test",
            primary_purpose="Symptom data",
            preprocessed_content="OOMKill events visible in cluster",
            content_size_bytes=100,
            preprocessing_method="manual",
            source_file_id=None,
            collected_at_turn=1,
        )
    )
    return case


def _llm_response(*, agent_response: str, milestone: str = "symptom_verified") -> str:
    """Build a JSON LLM response with one milestone update.

    The reasoning validator considers a response "contains a suggestion"
    when state_updates set a milestone — so this shape reliably triggers
    the validator in INVESTIGATING tests.
    """
    return json.dumps(
        {
            "agent_response": agent_response,
            "internal_reasoning": {
                "evidence_analyzed": ["ev_001122334455"],
                "conclusions": [
                    {
                        "observation": "OOMKill in events",
                        "inference": "Memory pressure",
                        "confidence": 0.8,
                    }
                ],
                "milestone_justifications": {
                    milestone: "Verified from kubectl-events.log",
                },
                "uncertainties": [],
            },
            "state_updates": {
                "milestones": {milestone: True},
                "evidence_to_add": [],
                "hypotheses_to_add": [],
                "outcome": "milestone_completed",
            },
        }
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestSelfCorrectionFallback:
    """When self-correction fails, agent_response must be the honest
    fallback message and metadata must flag the failure."""

    FALLBACK = MilestoneEngine._SELF_CORRECTION_FALLBACK_MESSAGE

    def _force_validator_to_fail(self, monkeypatch):
        """Pin the diagnostic-reasoning validator to a known-invalid
        result so the self-correction retry block always runs.

        validate_diagnostic_reasoning is locally imported inside
        _process_turn_impl, so we must patch the module-level binding
        (not an attribute on milestone_engine).
        """
        import faultmaven.core.investigation.diagnostic_reasoning_validator as v

        monkeypatch.setattr(
            v,
            "validate_diagnostic_reasoning",
            lambda case, agent_response, contains_suggestion=None: (
                False,
                ["Missing OBSERVATION/ANALYSIS structure"],
            ),
        )

    async def test_retry_still_invalid_substitutes_fallback(
        self, mock_llm, mock_repo, investigating_case, monkeypatch
    ):
        """When the retried prose ALSO fails validation, agent_response
        must be substituted with the fallback message — not the
        validator-rejected retry text."""
        self._force_validator_to_fail(monkeypatch)

        # Two LLM calls: original (also-invalid prose) + retry (still-invalid prose).
        mock_llm.generate.side_effect = [
            _llm_response(agent_response="I think it might be memory."),
            _llm_response(agent_response="Yeah I'm sure it's memory pressure."),
        ]

        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        result = await engine.process_turn(investigating_case, "Investigate")

        assert result["agent_response"] == self.FALLBACK
        assert result["metadata"]["self_correction_failed"] is True
        # Both calls happened (original + retry).
        assert mock_llm.generate.call_count == 2

    async def test_retry_errors_substitutes_fallback(
        self, mock_llm, mock_repo, investigating_case, monkeypatch
    ):
        """When the retry call ITSELF errors (e.g., context overflow,
        provider timeout), agent_response must be substituted — not the
        original uncorrected prose."""
        self._force_validator_to_fail(monkeypatch)

        # Original LLM call succeeds; retry raises.
        mock_llm.generate.side_effect = [
            _llm_response(agent_response="The cause is probably memory."),
            RuntimeError("Context window exhausted during retry"),
        ]

        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        result = await engine.process_turn(investigating_case, "Investigate")

        assert result["agent_response"] == self.FALLBACK
        assert result["metadata"]["self_correction_failed"] is True

    async def test_retry_succeeds_uses_corrected_response(
        self, mock_llm, mock_repo, investigating_case, monkeypatch
    ):
        """Sanity-check the success path: when retry validates cleanly,
        the corrected response ships (NOT the fallback), and no
        self_correction_failed flag is set.

        Implemented by patching the validator to fail the first call and
        pass the second — exercising the same branch as a real retry win.
        """
        import faultmaven.core.investigation.diagnostic_reasoning_validator as v

        call_count = {"n": 0}

        def _validator(case, agent_response, contains_suggestion=None):
            call_count["n"] += 1
            # First call (original) fails; second call (retry) passes.
            if call_count["n"] == 1:
                return (False, ["Missing OBSERVATION/ANALYSIS structure"])
            return (True, [])

        monkeypatch.setattr(v, "validate_diagnostic_reasoning", _validator)

        corrected_prose = "OBSERVATION: kubectl-events.log shows OOMKill. ANALYSIS: this indicates memory pressure."
        mock_llm.generate.side_effect = [
            _llm_response(agent_response="I think it might be memory."),
            _llm_response(agent_response=corrected_prose),
        ]

        engine = MilestoneEngine(
            mock_llm,
            mock_repo,
            investigation_tools=MagicMock(),
        )

        result = await engine.process_turn(investigating_case, "Investigate")

        # Retry's corrected prose ships; fallback NOT used.
        assert result["agent_response"] == corrected_prose
        assert result["metadata"]["self_correction_failed"] is False
