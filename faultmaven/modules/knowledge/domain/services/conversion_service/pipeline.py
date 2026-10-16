"""Document analysis (the LLM pass that identifies failure modes) and
the route helper it and the conversion pass share."""

import logging

from faultmaven.exceptions import RawModelContentRefused
from faultmaven.infrastructure.llm.dispatch import route_model_call
from faultmaven.infrastructure.llm.json_response import loads_llm_json
from faultmaven.infrastructure.llm.truncation import generate_with_truncation_retry
from faultmaven.infrastructure.security.model_content import model_boundary_redacted
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionErrorCode,
    FailureModeAnalysis,
    SourceAssessment,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.errors import (
    ConversionRejectedError,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.prompts import (
    ANALYSIS_MAX_TOKENS,
    ANALYSIS_MAX_TOKENS_CEILING,
    ANALYSIS_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)


def _knowledge_route_kwargs(settings) -> dict:
    """``{"provider_override": <name>}`` when KNOWLEDGE_PROVIDER is set,
    else ``{}`` — the kwarg is added only when the role provider is
    explicitly configured, so the unset case is byte-identical to before
    role routing and duck-typed routers without the parameter keep
    working."""
    override = settings.llm.explicit_role_provider("knowledge")
    return {"provider_override": override} if override else {}


async def _analyze_document(
    llm_router,
    settings,
    text: str,
    filename: str,
    *,
    sanitizer,
) -> AnalysisResult:
    """Analyze document for failure modes using KNOWLEDGE_PROVIDER.

    Everything sent is minted through the model-boundary redaction over
    ``sanitizer`` (the DI sanitizer; ``None`` means redaction is disabled at
    DI level), once, before the truncation retry's closure, so the retry
    resends the redacted text (#1901). Keyword-only with no default so a new
    caller cannot send without deciding. A ``RedactionUnavailableError``
    propagates: nothing is sent.
    """
    knowledge_model = settings.llm.get_knowledge_model()

    messages = await model_boundary_redacted(
        [
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": f"Analyze this document:\n\n{text}"},
        ],
        sanitizer,
    )

    async def _analyze(cap: int):
        return await route_model_call(
            llm_router,
            messages=messages,
            model=knowledge_model,
            max_tokens=cap,
            temperature=0.2,
            response_format={"type": "json_object"},
            # Land on KNOWLEDGE_PROVIDER when the operator set one — the
            # model alone doesn't route (kwarg only when set, so
            # duck-typed routers keep working).
            **_knowledge_route_kwargs(settings),
        )

    # A document with many failure modes can genuinely outgrow the budget.
    # This path already failed LOUDLY on a cut body — the JSON does not
    # parse — which was the right shape but the wrong recovery: it reported
    # "could not be parsed" for a document that simply needed more room, and
    # a retry with the same cap could never differ. Raise the cap once, and
    # say what actually happened if it is still cut (#1094).
    response = await generate_with_truncation_retry(
        _analyze,
        max_tokens=ANALYSIS_MAX_TOKENS,
        ceiling=ANALYSIS_MAX_TOKENS_CEILING,
        label=f"document analysis ({filename})",
    )

    if response.is_truncated:
        raise ConversionRejectedError(
            "LLM analysis response was truncated at the output limit "
            f"({ANALYSIS_MAX_TOKENS_CEILING} tokens); the document may "
            "contain too many failure modes for a single analysis pass",
            error_code=ConversionErrorCode.LLM_PARSE_ERROR,
        )

    try:
        # Tolerant of a markdown fence: `response_format` is an
        # OpenAI-shaped parameter and a provider that cannot express it
        # drops it, so the body arrives as fenced prose-JSON. A bare
        # A bare `json.loads` made every conversion fail under such a provider
        # (#1380) with LLM_PARSE_ERROR, whose user-facing advice is "try a
        # different document" — advice that can never work, because the
        # document was never the problem.
        data = loads_llm_json(response.content, strict=True)
        return AnalysisResult(
            is_actionable=data.get("is_actionable", False),
            failure_modes=[
                FailureModeAnalysis(**fm) for fm in data.get("failure_modes", [])
            ],
            source_assessment=SourceAssessment(
                **data.get(
                    "source_assessment",
                    {
                        "content_type": "unknown",
                        "actionability_rating": "low",
                        "missing_information": [],
                    },
                )
            ),
        )
    except RawModelContentRefused:
        # A refusal is a programming error, never a fallback (#1998).
        raise
    except Exception as e:
        # The exception's CLASS only, as `_convert_single_failure_mode` does
        # (#836). A pydantic ValidationError quotes the failing failure-mode dict
        # (`input_value={'title': 'db password=hu...`), which is raw model output
        # and carries the title, unscrubbed, into the log (#1981).
        logger.error(f"Failed to parse analysis response ({type(e).__name__})")
        raise ConversionRejectedError(
            # Static. ``ConversionRejectedError`` is serialized to the
            # caller by ``conversion_routes`` as ``detail=str(e)``, on the
            # strength of every other construction being a hand-written
            # caller-facing sentence. This arm is a broad ``except`` over a
            # JSON decode and a Pydantic construction, so the text is a
            # decoder message or a ValidationError echoing the model's raw
            # output — the one place that promise was not kept (#1400).
            # Already logged immediately above.
            "LLM analysis response could not be parsed",
            error_code=ConversionErrorCode.LLM_PARSE_ERROR,
        ) from e
