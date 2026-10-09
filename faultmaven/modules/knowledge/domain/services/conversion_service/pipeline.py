"""Document analysis (the LLM pass that identifies failure modes) and
the scope/route helpers it and the conversion pass share."""

import logging
from pathlib import Path

from faultmaven.infrastructure.llm.json_response import loads_llm_json
from faultmaven.infrastructure.llm.truncation import generate_with_truncation_retry
from faultmaven.infrastructure.security.case_redaction import CaseRedactionContext
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
from faultmaven.utils.runbook_id import (
    safe_path_component,
)

logger = logging.getLogger(__name__)


def _scope_dir(data_dir, scope: str, team_id: str = None, user_id: str = None) -> Path:
    """Scope directory for a draft, with both id components sanitised.

    #1213: these are interpolated into a directory NAME. They come from the
    auth context rather than a request body, so they are a lower-risk source
    than a title — but the same shape bit ``KnowledgeService.upload_document``,
    where a ``user_id`` of ``../../../../escaped`` sent the write outside
    ``data/knowledge`` entirely. ``safe_path_component`` reduces each to one
    segment, so the layout (``global/``, ``team_*/``, ``user_*/``) that the
    scan pass infers scope from is preserved while an escape is
    unconstructible.
    """
    if scope == "global":
        return data_dir / "global"
    elif scope == "team" and team_id:
        return data_dir / f"team_{safe_path_component(team_id)}"
    elif scope == "personal" and user_id:
        return data_dir / f"user_{safe_path_component(user_id)}"
    return data_dir / "global"


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
    redaction: CaseRedactionContext,
) -> AnalysisResult:
    """Analyze document for failure modes using KNOWLEDGE_PROVIDER.

    ``redaction`` is applied to everything sent, once, before the truncation
    retry's closure, so the retry resends the redacted text (#1901). It is a
    required argument so a new caller cannot send without deciding. A
    ``RedactionUnavailableError`` propagates: nothing is sent.
    """
    knowledge_model = settings.llm.get_knowledge_model()

    messages = await redaction.asanitize_messages(
        [
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": f"Analyze this document:\n\n{text}"},
        ]
    )

    async def _analyze(cap: int):
        return await llm_router.route(
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
    except Exception as e:
        logger.error(f"Failed to parse analysis response: {e}")
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
