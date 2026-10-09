"""Loading the redaction context and generating the LLM (or single-shot) response for a turn."""

import logging
from typing import Optional

from faultmaven.core.investigation.milestone_engine.generation import (
    TOOLLESS_INFERENCE_OUTPUT_FLOOR,
)
from faultmaven.core.investigation.prompts.templates.assembly import get_prompt_for_case
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    TerminalResponse,
    get_schema_for_stage,
)
from faultmaven.infrastructure.llm.providers import ReasoningIntent
from faultmaven.modules.case.contracts import (
    CaseState,
    InvestigationStage,
)

from .stage_gates import (
    _route_toolless_turn_single_shot,
    _should_force_tools,
)

logger = logging.getLogger(__name__)


async def _generate_turn_response(
    investigation_tools,
    llm_provider,
    redis_client,
    repository,
    sanitizer,
    generator,
    *,
    case,
    intent_data,
    user_id,
    user_message,
    kb_rendered=None,
):
    """Load the redaction context and generate the LLM (or single-shot) response for this turn.

    ``kb_rendered`` is refilled by every prompt build with the KB entries that
    prompt carries; the last build is the prompt the model answered from.
    """
    from faultmaven.config.settings import get_settings
    from faultmaven.infrastructure.security.case_redaction import (
        CaseRedactionContext,
        should_redact,
    )

    redaction_settings = get_settings()
    redaction_ctx = CaseRedactionContext(
        case_id=case.case_id,
        sanitizer=sanitizer,
        redis_client=redis_client,
        enabled=should_redact(sanitizer),
        ttl_hours=redaction_settings.protection.redaction_registry_ttl_hours,
    )
    await redaction_ctx.load()

    # Build prompt using the adaptive template system
    # Gap #6: Pass provider info for dynamic token budget calculation
    provider_name = getattr(llm_provider, "provider_name", None)
    model_name = (
        getattr(llm_provider.config, "default_model", None)
        if hasattr(llm_provider, "config")
        else None
    )

    # Processing mode for prompt framing (structural-index role
    # tagging). Prefer the authoritative query_mode the service already
    # computed — it factors in a fresh evidence-bearing attachment and
    # re-routes a generic cover message to DIRECTED_ANALYSIS (#708).
    # Fall back to a local text classification for engine entry points
    # that don't thread query_mode (tests, direct calls). Keeping the
    # prompt mode and the force_tools mode in sync avoids framing the
    # turn as TRIAGE while tools are forced for DIRECTED_ANALYSIS.
    processing_mode = (intent_data or {}).get("query_mode")
    if not processing_mode:
        from faultmaven.modules.agent.domain.services.query_classifier import (
            classify_query,
        )

        processing_mode = classify_query(
            user_message, has_attachments=bool(case.evidence)
        ).mode.value

    # Phase 4c — prefetch entity highlight ROWS from the Phase 4
    # ``case_entities`` registry when the feature is on. When
    # the flag is off (or the producer wrote no entities),
    # ``fetch_entity_highlights`` returns [] and the template
    # slot renders empty. Rows rather than a formatted block: the
    # values come out of file content, so the block is fenced, and
    # the fence re-renders on a token collision — which it cannot do
    # around an awaited query (#1228). Formatting happens inside the
    # fenced assembly in ``build_investigation_context``.
    entity_highlight_groups: list = []
    try:
        from faultmaven.config.settings import get_settings
        from faultmaven.core.investigation.prompts.context_builder.entity_highlights import (
            fetch_entity_highlights,
        )

        if get_settings().preprocessing.entity_registry_enabled:
            entity_highlight_groups = await fetch_entity_highlights(
                repository, case.case_id
            )
    except Exception as exc:
        logger.warning(
            "Entity highlights prefetch failed for case %s (non-fatal): %s",
            case.case_id,
            exc,
        )

    # Build the prompt. In directed-analysis turns with tools available,
    # historical evidence is rendered as index+stub (the agent will
    # search_file). If tools then fail at RUNTIME and we fall through to
    # the non-tool path, that elided prompt would strand the agent (no
    # tool to recover the evidence), so keep a builder that reconstructs
    # the full-evidence prompt for that fallback.
    _tools_avail = generator.tools_effectively_available()

    def _build_prompt(
        tools_available: bool,
        *,
        target_tokens: Optional[int] = None,
        sizing_provider: Optional[str] = provider_name,
        sizing_model: Optional[str] = model_name,
    ) -> str:
        return get_prompt_for_case(
            case,
            user_message,
            kb_results=None,
            provider_name=sizing_provider,
            model_name=sizing_model,
            processing_mode=processing_mode,
            entity_highlight_groups=entity_highlight_groups,
            tools_available=tools_available,
            target_tokens=target_tokens,
            kb_rendered=kb_rendered,
        )

    def _build_tool_loop_base(
        *,
        target_tokens: int,
        provider_name: Optional[str],
        model_name: Optional[str],
    ) -> str:
        # #614: the same prompt, re-assembled for the model the tool
        # loop sends to, when the chat-sized one does not fit there.
        return _build_prompt(
            _tools_avail,
            target_tokens=target_tokens,
            sizing_provider=provider_name,
            sizing_model=model_name,
        )

    # fm#1116: decide the generation route BEFORE the single prompt
    # build. A tool-less turn with nothing to search takes the
    # single-shot structured path (reasoning declared) and must get the
    # un-elided prompt — no search_file will run to recover an extract
    # replaced by an index stub. Pure over case state; computed once.
    has_pending = hasattr(case, "pending_transition") and case.pending_transition
    force_tools = (
        _should_force_tools(processing_mode, case, bool(has_pending))
        if investigation_tools
        else False
    )
    route_single_shot = bool(investigation_tools) and _route_toolless_turn_single_shot(
        processing_mode, case, force_tools
    )

    prompt = _build_prompt(_tools_avail and not route_single_shot)

    # Determine schema based on status/stage
    if case.state == CaseState.INQUIRY:
        schema_model = InquiryResponse
        logger.info(
            f"Turn {case.current_turn} schema selection: "
            f"state={case.state.value}, schema=InquiryResponse"
        )
    elif case.state in [CaseState.RESOLVED, CaseState.CLOSED]:
        schema_model = TerminalResponse
        logger.info(
            f"Turn {case.current_turn} schema selection: "
            f"state={case.state.value}, schema=TerminalResponse"
        )
    else:
        schema_model = get_schema_for_stage(
            case.current_stage or InvestigationStage.DIAGNOSIS
        )
        logger.info(
            f"Turn {case.current_turn} schema selection: "
            f"state={case.state.value}, stage={case.current_stage}, "
            f"schema={schema_model.__name__}"
        )

    # 2. Invoke LLM with structured output
    # Tool availability: all turns get tools when tools are registered.
    # The LLM decides which tool to invoke based on the user's question.
    #
    # tool_choice varies by query mode:
    # - directed_analysis + searchable material: "required" — LLM must
    #   search evidence
    # - all other turns: "auto" — LLM decides whether to use tools
    #
    # Searchable material is Evidence rows OR fresh uploaded files
    # (post-010, a delivering turn has only an UploadedFile, not yet an
    # Evidence row — ``bool(case.evidence)`` alone would leave the
    # evidence-delivering turn on tool_choice=auto and let the agent
    # skip analysis, #708). ``_has_searchable_material`` guarantees a
    # real search target so forcing tools cannot crash the loop.
    #
    # Safety net: when a pending_transition exists, the user is in a
    # confirmation flow. Don't force tool_choice=required — the user's
    # message is a confirmation/decline that may have fallen through
    # pattern matching (typed instead of clicked). Forcing tools crashes
    # the tool loop when the LLM has nothing to search for.
    if route_single_shot:
        # fm#1116: nothing to search on this turn — take the single-shot
        # structured path with reasoning declared, instead of a tool
        # loop that would pin reasoning to "none" for tools it cannot
        # use. ``prompt`` was built un-elided above for this route.
        logger.info(
            f"Turn {case.current_turn}: no searchable material and tools "
            f"not forced (mode={processing_mode}) — single-shot "
            f"structured path with reasoning_intent=INFERENCE"
        )
        response_obj = await generator.generate_structured_output(
            prompt,
            schema_model,
            redaction_ctx=redaction_ctx,
            case=case,
            user_message=user_message,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=TOOLLESS_INFERENCE_OUTPUT_FLOOR,
        )
    elif investigation_tools:
        da_tools = generator.build_da_tool_schemas()
        da_context = await generator.build_tool_context(case, user_id=user_id)
        response_obj = await generator.generate_structured_output(
            prompt,
            schema_model,
            investigation_tools=da_tools,
            tool_context=da_context,
            force_tool_use=force_tools,
            redaction_ctx=redaction_ctx,
            case=case,
            user_message=user_message,
            # Only meaningful when elision happened (tools available);
            # the non-tool fallback rebuilds with full evidence.
            fallback_prompt_builder=(
                (lambda: _build_prompt(False)) if _tools_avail else None
            ),
            base_prompt_builder=_build_tool_loop_base,
        )
    else:
        response_obj = await generator.generate_structured_output(
            prompt,
            schema_model,
            redaction_ctx=redaction_ctx,
            case=case,
            user_message=user_message,
        )

    # Debug: Log what type was actually returned
    logger.info(
        f"Turn {case.current_turn} response type: {type(response_obj).__name__}"
    )
    return redaction_ctx, response_obj
