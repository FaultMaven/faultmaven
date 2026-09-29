"""Generating the engine's structured LLM output: the tool-augmented investigation loop, the tool-loop token budget, and the single-shot fallback, all behind one collaborator so every caller shares the same tool-loop constants."""

import asyncio
import json
import logging
from typing import Any, Callable, Optional

from faultmaven.core.investigation.lifecycle_metrics import (
    prompt_context_recovery_total,
)
from faultmaven.core.investigation.llm_error_handler import (
    OutputTruncationError,
    classify_token_limit_reason,
    is_output_truncation_error,
    is_truncated_json_error,
)
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.milestone_engine.structured_output import (
    _fix_enum_violations,
    _normalize_state_updates,
    _parse_nested_json,
    _parse_schema_tool_call,
    _parse_text_as_schema,
    _synthesize_agent_response,
    _validate_with_degradation,
)
from faultmaven.core.investigation.milestone_engine.tool_messages import (
    _build_assistant_message,
    _build_da_system_instruction,
    _build_schema_tool,
)
from faultmaven.core.investigation.reliability_metrics import (
    tool_call_attempts_total,
)
from faultmaven.core.investigation.schemas import (
    BaseInteractionResponse,
)
from faultmaven.core.investigation.tool_loop_metrics import (
    tool_result_chars,
    tool_result_relayed_total,
    tool_result_truncated_total,
)
from faultmaven.infrastructure.llm.json_response import (
    json_payload_text,
)
from faultmaven.infrastructure.llm.metering import (
    active_token_tracker,
    record_provider_call,
)
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputMode,
)
from faultmaven.infrastructure.llm.truncation import generate_with_truncation_retry

from .affordances import (
    _schema_prompt_instruction,
)
from .response_synthesis import (
    schema_answer_stop_reason,
)
from .text_budget import (
    _TOOL_LOOP_ELISION_MARKER,
    KB_QA_ANSWER_TRUNCATED_MARKER,
    KB_QA_RELAY_PREFIX,
    KB_QA_RELAY_SUFFIX,
    _elide_answer_middle,
    _is_context_length_error,
    _tool_loop_message_tokens,
    _tool_payload_tokens,
    _ToolLoopBudget,
)

logger = logging.getLogger(__name__)


# Generation cap for schema-bound calls, and the ceiling the truncation ladder
# may raise it to. Investigation schemas (``_Verification`` especially) are
# large and turn 2+ carries substantial context, so the starting cap is
# generous; the ceiling bounds how much a single turn may spend chasing an
# answer that keeps overrunning. Reaching the ceiling is the signal to switch
# levers — from "give the answer more room" to "give the answer more room by
# shrinking the question" (the #662 minimal-prompt degrade).
STRUCTURED_OUTPUT_MAX_TOKENS = 8000


STRUCTURED_OUTPUT_MAX_TOKENS_CEILING = 16000


# Visible-output floor declared with ``ReasoningIntent.INFERENCE`` on the
# tool-less single-shot diagnostic call (fm#1116). INFERENCE lifts the
# provider's starvation guards, so the router requires a floor (#1117).
# Anchor: the full Diagnosis body on the replayed case_bf484a484a77 turn 9
# measured 1,495-1,756 completion tokens at effort none and 1,641-1,8xx at
# low/medium (probe results_reasoning.jsonl); 2048 sits above every observed
# body and well under STRUCTURED_OUTPUT_MAX_TOKENS, so the floor never raises
# the cap and only forbids a starvable partition.
TOOLLESS_INFERENCE_OUTPUT_FLOOR = 2048


class StructuredOutputGenerator:
    """Drives the tool-augmented and single-shot structured-output generation paths, holding the class constants (MAX_TOOL_ITERATIONS, TOOL_RESULT_MAX_CHARS, MAX_DEEP_ANALYSIS) that bound them."""

    # Constants for tool-augmented generation
    #
    # What bounds a turn's tool loop (#611, #614). The loop makes
    # MAX_TOOL_ITERATIONS + 1 calls: iterations 0..MAX_TOOL_ITERATIONS-1 may
    # call tools, the last is schema-only.
    #
    # The MESSAGE bound is structural. _bound_tool_loop_messages trims the
    # ``messages`` of every call to the SOFT cap from _resolve_tool_loop_budget:
    #
    #     soft = PROMPT_TARGET_TOKENS + PROMPT_TOOL_OBSERVATION_MAX_TOKENS
    #          = 32,000 + 16,000 = 48,000 with shipped defaults.
    #
    # That is a bound on ``messages`` only, in ESTIMATED tokens: providers with
    # no local tokenizer (gemini, and the router, whose name is not a provider)
    # are estimated at len // 4, which read 1.5-1.7x under cl100k on two log
    # samples. It does not count the ``tools=`` payload — the schema tool alone
    # is ~950 (TerminalResponse) to ~11,300 (InvestigationResponse_Diagnosis)
    # cl100k tokens, plus ~1,100 for the six investigation tools the DA
    # registry can hold. That payload is bounded only by the WINDOW (#614):
    # when the resolver knows the receiving model's context window, the HARD
    # cap holds messages plus that call's tools= payload (the schema tool alone
    # on the final iteration) to what the window leaves beside the completion
    # the call asks for — its max_tokens (8,000; 16,000 on a truncation retry,
    # which is bounded again), or the registry's reserve if larger.
    # _fit_tool_loop_base fits the base to that before the first call,
    # re-assembling it for the receiving model, or refusing the loop, when it
    # does not fit; with the window unknown it never touches the base. With no
    # window known (the router) or a large one, the loop sends and elides
    # exactly what it did before.
    #
    # PROMPT_TURN_TOKEN_CEILING (150,000) is a separate, METERED net: after each
    # non-final call it compares the turn's spend_weighted_tokens — real
    # provider tokens of every metered call in the turn (messages, tools
    # payload, output, truncation retries, fallback attempts, LLM calls made by
    # tools), cache reads weighted 0.25 — and once over, every remaining
    # iteration is schema-only. It can change the loop only when crossed within
    # the first MAX_TOOL_ITERATIONS - 1 calls (after that, the next iteration is
    # final anyway), i.e. when those calls average more than 50,000 each with
    # defaults. That is NOT excluded by the message bound. An uncached turn
    # with a full-size base and the Diagnosis schema meters ~3 x (32,000 +
    # 1,350 + 11,300 + 1,100) = ~137,000 for the base, system instruction and
    # tools alone; the observation allowance, outputs, or a len // 4 undercount
    # carries it past 150,000, and the ceiling removes the last tool round.
    # Where the provider serves the repeated prefix from its prompt cache on
    # calls after the first, that prefix counts at 0.25 and the ceiling stays
    # out of normal turns.
    #
    # Raising PROMPT_TARGET_TOKENS or PROMPT_TOOL_OBSERVATION_MAX_TOKENS moves
    # the metered spend toward the ceiling; raise the ceiling with them. Pinned
    # by TestToolLoopSpendBound in test_milestone_engine_tool_loop.py, and what
    # each cap counts by TestToolLoopBoundSplitsSoftAndHardCaps /
    # TestToolLoopBaseFitsTheReceivingModel.
    MAX_TOOL_ITERATIONS = 4
    TOOL_RESULT_MAX_CHARS = 8000
    MAX_DEEP_ANALYSIS = 1

    def __init__(self, *, deps, vectorizer) -> None:
        self.deps = deps
        self.vectorizer = vectorizer

    def _resolve_tool_loop_budget(self, provider_name: str) -> _ToolLoopBudget:
        """The two caps on every tool-loop call (#614). The SOFT cap is
        ``prompt_target + a bounded observation scratchpad`` on ``messages``
        alone — the base task fits the jar, the accumulated tool observations get
        a bounded allowance. The HARD cap comes from the model's window when
        known: no call's prompt — messages plus the tools payload it carries —
        plus the completion it asks for may exceed it (``_ToolLoopBudget.hard``).
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.utils.model_context import resolve_model_budget

        try:
            obs = get_settings().prompt_budget.tool_observation_max_tokens
        except Exception:
            obs = 16_000
        try:
            pn = provider_name if isinstance(provider_name, str) else None
            resolved = resolve_model_budget(pn, self.deps.da_model)
            return _ToolLoopBudget(
                soft=resolved.prompt_target + obs,
                window=resolved.context_window,  # None: unknown, trust the target
                response_reserve=resolved.response_reserve or 0,
            )
        except Exception:
            # Best-effort — never let budget resolution break a turn.
            return _ToolLoopBudget(soft=32_000 + obs, window=None)

    def _bound_tool_loop_messages(
        self,
        messages: list[dict],
        budget_tokens: int,
        provider_name: str,
        token_cache: Optional[dict] = None,
        *,
        tools: Optional[list[dict]],
        window_tokens: Optional[int],
    ) -> list[dict]:
        """Keep one tool-loop call within its two caps by eliding the OLDEST
        tool-exchange groups (an assistant tool-call message plus its tool
        results), preserving the system + base task messages and the
        most-recent exchanges. This bounds the ACCUMULATED tool observations so
        the request cannot grow unbounded across iterations.

        - ``budget_tokens`` is the SOFT cap: ``messages`` alone, a size/cost
          target. The ``tools=`` payload is not counted against it, so where no
          window is known the bound is exactly what it was before #614.
        - ``window_tokens`` is the HARD cap: the prompt tokens the receiving
          model's window leaves beside THIS call's completion
          (``_ToolLoopBudget.hard(max_tokens)``), or ``None`` when the window is
          unknown. ``messages`` PLUS ``tools`` — the list sent as ``tools=`` on
          this call, the schema tool alone on the final iteration — must fit it
          (#614). A request that would still exceed it is refused with
          ``ToolCallingUnsupportedError`` (the caller takes the non-tool path),
          never returned; ``_fit_tool_loop_base`` makes that unreachable for the
          head it has fitted, at the first attempt's completion cap.

        Both are REQUIRED keywords, so no call site can bound a request while
        silently skipping the window or leaving its tools uncounted: ``None``
        states "no tools" / "window unknown".

        Invariants: the elided span is replaced by a single marker (INV-4 — never
        a silent drop; the agent can re-run a search), and whole assistant/tool
        groups are elided together so tool_call ↔ tool_result pairing stays valid
        (providers reject an orphan tool result).
        """

        def _tok(m: dict) -> int:
            return _tool_loop_message_tokens(
                m, provider_name, self.deps.da_model, token_cache
            )

        msg_cap = budget_tokens
        tools_tokens = 0
        if window_tokens is not None:
            tools_tokens = _tool_payload_tokens(
                tools, provider_name, self.deps.da_model, token_cache
            )
            msg_cap = min(msg_cap, window_tokens - tools_tokens)
        total = sum(_tok(m) for m in messages)
        if total <= msg_cap:
            return messages

        head = messages[:2]  # system + base task (always kept)
        rest = messages[2:]
        groups: list[list[dict]] = []
        for m in rest:
            if m.get("role") == "assistant" or not groups:
                groups.append([m])
            else:
                groups[-1].append(m)

        marker = {"role": "user", "content": _TOOL_LOOP_ELISION_MARKER}
        # Counted WITHOUT the by-id cache: the marker is not held in `messages`,
        # so once the returned list is dropped its address can be reused by a
        # later message dict — which would then read the marker's count from the
        # cache and be under-counted (#614).
        marker_tokens = _tool_loop_message_tokens(
            marker, provider_name, self.deps.da_model
        )
        head_tokens = sum(_tok(m) for m in head)
        avail = msg_cap - head_tokens - marker_tokens
        kept: list[list[dict]] = []
        for g in reversed(groups):
            gt = sum(_tok(m) for m in g)
            if gt <= avail:
                kept.insert(0, g)
                avail -= gt
            else:
                break
        if len(kept) == len(groups):
            out, sent = messages, total  # nothing to elide (head near the cap)
        else:
            logger.warning(
                "tool_loop_context_bounded: elided %d of %d tool-exchange group(s) "
                "to fit the %d-token budget",
                len(groups) - len(kept),
                len(groups),
                msg_cap,
            )
            out = list(head) + [marker]
            for g in kept:
                out.extend(g)
            sent = msg_cap - avail
        if window_tokens is not None and sent + tools_tokens > window_tokens:
            from faultmaven.exceptions import ToolCallingUnsupportedError

            raise ToolCallingUnsupportedError(
                message=(
                    f"Tool-loop head plus tools payload exceeds the "
                    f"{window_tokens}-token window budget; not sending."
                ),
                provider=provider_name if isinstance(provider_name, str) else None,
                model=self.deps.da_model,
            )
        return out

    async def _fit_tool_loop_base(
        self,
        messages: list[dict],
        tools: list[dict],
        budget: _ToolLoopBudget,
        provider_name: str,
        max_tokens: int,
        base_prompt_builder: Optional[Callable[..., str]] = None,
        redaction_ctx: Any | None = None,
        token_cache: Optional[dict] = None,
    ) -> list[dict]:
        """Return the loop's opening ``[system, base task]`` messages with the
        base sized to the WINDOW of the model that receives the tool loop
        (#614), or refuse the loop.

        The base arrives assembled for the chat path (through the router, which
        names no provider or model: ``PROMPT_TARGET_TOKENS``, no window clamp,
        ``len // 4``), but the loop sends it to ``provider_name`` /
        ``self.da_model`` — a dedicated DA model whose window can be too small
        for it — beside a system instruction and a ``tools=`` payload the
        assembly never saw. When that window is known, the head must fit
        ``budget.hard(max_tokens)`` — the window less the completion the first
        attempt asks for — beside the largest ``tools`` list the loop offers and
        the elision marker, or no iteration's request can.

        Only the window. When it is unknown, or the head fits it, the base is
        sent as assembled, exactly as before #614: the SOFT cap governs
        observation elision, not the base, and shrinking the base to it would
        send less case context to buy observation room nobody was short of.

        When it does not fit, it is RE-ASSEMBLED through the same allocator at
        the room that is left (``base_prompt_builder(target_tokens=...,
        provider_name=..., model_name=...)`` → ``get_prompt_for_case``), which
        drops content by the assembly's own priority rules and, if even that
        cannot fit, takes the minimal fallback prompt. The rebuilt text is raw
        case content, so it is redacted like the original, and it goes in a NEW
        message dict; the dropped one's count leaves the by-id ``token_cache``
        with it, so a later dict reusing its address cannot read it. A base that
        still does not fit — or no builder — raises
        ``ToolCallingUnsupportedError``: nothing is sent, and the caller takes
        the non-tool path through the router the original base was sized for.

        Counted through ``token_cache`` on the loop's own message dicts, so the
        head is tokenized once per turn here, and the bound reuses the counts.
        """
        from faultmaven.exceptions import ToolCallingUnsupportedError

        limit = budget.hard(max_tokens)
        if limit is None:
            return messages  # window unknown: nothing to overflow, send as main

        def _tok(m: dict) -> int:
            return _tool_loop_message_tokens(
                m, provider_name, self.deps.da_model, token_cache
            )

        system_msg, base_msg = messages[0], messages[1]
        tools_tokens = _tool_payload_tokens(
            tools, provider_name, self.deps.da_model, token_cache
        )
        fixed = (
            _tok(system_msg)
            + _tool_loop_message_tokens(
                {"content": _TOOL_LOOP_ELISION_MARKER},
                provider_name,
                self.deps.da_model,
            )
            + tools_tokens
        )
        room = limit - fixed
        base_tokens = _tok(base_msg)
        if base_tokens <= room:
            return messages

        # What the base is re-assembled FOR: the name and model the tool loop
        # sends to, so the allocator counts with this estimator and clamps to
        # this model's window.
        sizing_provider = provider_name if isinstance(provider_name, str) else None
        model = self.deps.da_model if isinstance(self.deps.da_model, str) else None
        logger.warning(
            "tool_loop_base_resized: base task prompt (%d tokens) does not fit the "
            "%d-token window of provider %s (model %s) beside a %d-token "
            "completion and %d tokens of system instruction, tools payload and "
            "elision marker; re-assembling it at %d tokens",
            base_tokens,
            budget.window,
            provider_name,
            model,
            budget.window - limit,
            fixed,
            room,
        )
        resized: Optional[str] = None
        if base_prompt_builder is not None and room > 0:
            try:
                resized = base_prompt_builder(
                    target_tokens=room,
                    provider_name=sizing_provider,
                    model_name=model,
                )
                if redaction_ctx and resized:
                    resized = await redaction_ctx.asanitize(resized)
            except Exception as exc:  # never break the turn on a rebuild
                logger.warning("tool-loop base re-assembly failed: %s", exc)
                resized = None
        if resized:
            resized_msg = {**base_msg, "content": resized}
            if _tok(resized_msg) <= room:
                if token_cache is not None:
                    token_cache.pop(id(base_msg), None)
                return [system_msg, resized_msg, *messages[2:]]
        raise ToolCallingUnsupportedError(
            message=(
                f"The base task prompt cannot fit the {budget.window}-token window "
                f"of provider {provider_name} (model {model}) beside a "
                f"{budget.window - limit}-token completion and {fixed} tokens of "
                f"system instruction, tools payload and elision marker; not "
                f"sending the tool loop."
            ),
            provider=provider_name if isinstance(provider_name, str) else None,
            model=self.deps.da_model,
        )

    async def _tool_augmented_generate(
        self,
        prompt: str,
        schema_model: Any,
        investigation_tools: list[dict],
        tool_context: Any,
        max_tokens: int = 8000,
        redaction_ctx: Any | None = None,
        case: Any | None = None,
        force_tool_use: bool = False,
        base_prompt_builder: Optional[Callable[..., str]] = None,
    ) -> BaseInteractionResponse:
        """Run a bounded tool-calling loop with investigation tools.

        The LLM gets real investigation tools (search_file, deep_analysis,
        kb_qa, web_search) alongside the response schema tool.

        Algorithm:
        1. Build schema tool from Pydantic model (reuses existing converter)
        2. Combine: all_tools = investigation_tools + schema_tools
        3. Loop with tool_choice per force_tool_use:
           - force_tool_use=True (DA turns): "required" — LLM must call a tool
           - force_tool_use=False (other turns): "auto" — LLM may respond directly
        4. When LLM calls schema tool → parse and return structured output
        5. After max iterations → force schema with only schema tools available

        Vectorization (v5.2):
        - Proactive: starts background vectorization for large evidence files
          at loop entry. Runs concurrently with tool calls.
        - Reactive: tracks per-evidence DA failure signals (empty searches,
          timeouts, low confidence). Triggers vectorization as fallback.

        Args:
            prompt: Full investigation prompt
            schema_model: Pydantic model class for structured output
            investigation_tools: OpenAI-format tool defs for search/analysis
            tool_context: ToolContext for tool execution
            max_tokens: Max tokens for LLM calls
            case: Case object for evidence access and DA count persistence
            base_prompt_builder: Re-assembles the base task prompt for the model
                that receives this loop, called as ``(target_tokens=...,
                provider_name=..., model_name=...)`` only when ``prompt`` does
                not fit a KNOWN window beside the completion, the system
                instruction and the tools (see ``_fit_tool_loop_base``).
                ``None``: a base that does not fit is refused instead.

        Returns:
            Instantiated Pydantic model (BaseInteractionResponse)
        """
        # Use dedicated DA provider (DA_PROVIDER from .env) if available,
        # otherwise fall back to the default router
        provider = self.deps.da_provider or self.deps.llm_provider
        provider_name = getattr(provider, "provider_name", type(provider).__name__)
        model_info = f", model: {self.deps.da_model}" if self.deps.da_model else ""
        logger.info(
            f"Tool-augmented generate using provider: {provider_name}{model_info}"
        )
        # Per-call caps: messages within the soft cap (prompt_target +
        # observations), and — when the model's window is known — messages plus
        # that call's tools= payload within what the window leaves beside that
        # call's completion, so no request is sent whose ESTIMATED prompt plus
        # requested completion exceeds the window (the base is fitted before the
        # loop, accumulated observations compact to fit — see
        # _fit_tool_loop_base / _bound_tool_loop_messages /
        # _resolve_tool_loop_budget).
        tool_loop_budget = self._resolve_tool_loop_budget(provider_name)
        # Label vocabulary for the tool-result budget metrics below. The
        # tool name on a tool call is MODEL-SUPPLIED, so it is unbounded:
        # a hallucinated name reaches `execute_tool`, comes back as a short
        # "Tool 'x' not found" error, and would still be relayed -- and a
        # model that invents names freely would mint a Prometheus label per
        # invention. Bound it to what this call actually OFFERED; anything
        # else is by definition not a tool and folds into `unknown`.
        offered_tool_names = frozenset(
            (t.get("function") or {}).get("name") or ""
            for t in investigation_tools
            if isinstance(t, dict)
        ) - {""}
        # Per-message token-count cache (by id) reused across iterations so the
        # large stable head isn't re-tokenized every loop — see
        # _bound_tool_loop_messages.
        _msg_token_cache: dict = {}

        # Build the schema tool, asking for NATIVE ENFORCEMENT when the provider
        # can give it (fm#1051).
        #
        # This path used to deliver the response schema as a plain function with
        # no `strict` key, and never consulted the provider's capability at all —
        # only the single-shot branch below did. So on a provider documented as
        # STRICT, every turn that had tools available (which is every turn with a
        # tool registry, not just Directed Analysis) got unenforced function
        # calling: BEST_EFFORT semantics. The model could omit a required field,
        # the engine dropped the whole `state_updates` payload, and the turn
        # advanced nothing — observed on the first live cloud turn after #819 as
        # a missing `state_updates.knowledge_match.match_type`.
        #
        # Scoped to the SCHEMA tool. The investigation tools keep their existing
        # non-strict definitions: several take optional parameters, and strict
        # mode has no optional keys, so enforcing them would force the model to
        # emit explicit nulls and change directed-analysis behaviour — a
        # regression risk with no bearing on the bug being fixed.
        #
        # Marked from the primary provider's capability. On a mid-chain fallback
        # the request can still land elsewhere carrying `strict: true`; that is
        # valid OpenAI-spec (Anthropic and Gemini rebuild tool definitions and
        # drop it), and the alternative — marking nothing — is the bug itself.
        schema_tools = _build_schema_tool(schema_model, provider)
        schema_tool_name = schema_tools[0]["function"]["name"]

        # Combine investigation tools + schema tool
        all_tools = investigation_tools + schema_tools

        # Build tool name list for the DA system instruction
        tool_names = [t["function"]["name"] for t in investigation_tools]

        # Initialize conversation with DA system instruction + user prompt
        da_system_instruction = _build_da_system_instruction(
            tool_names,
            schema_tool_name,
        )
        # Size the base to the WINDOW of the model that receives it (#614): when
        # the window is known, the head must fit it beside the first attempt's
        # completion, the largest tools= payload (all_tools) and the elision
        # marker — or no call can. With the window unknown it is sent as
        # assembled. Raises ToolCallingUnsupportedError (→ the non-tool path)
        # when it cannot fit.
        messages = await self._fit_tool_loop_base(
            [
                {"role": "system", "content": da_system_instruction},
                {"role": "user", "content": prompt},
            ],
            all_tools,
            tool_loop_budget,
            provider_name,
            max_tokens,
            base_prompt_builder=base_prompt_builder,
            redaction_ctx=redaction_ctx,
            token_cache=_msg_token_cache,
        )
        deep_analysis_count = 0

        # Per-evidence DA failure tracking for auto-vectorization (v5.2)
        # Same pattern as deep_analysis_count above — mechanical counters
        # that trigger system actions when thresholds are met.
        # "Already vectorized" is sourced from the persistent
        # Evidence.vectorized flag (set + saved by _vectorize_evidence on
        # success) so dedup holds both within a turn and across turns.
        da_empty_search_counts: dict[str, int] = {}  # evidence_id → consecutive empties

        # Proactive vectorization: start background tasks for large evidence
        # files before the tool loop begins. Runs concurrently so semantic
        # search is available by the time the agent needs it.
        # Gated on force_tool_use=True (Directed Analysis). Triage and
        # Knowledge Query turns don't consult case evidence via semantic
        # search, so preemptive embedding would be wasted work — and on a
        # cold-cached model it can dominate the turn budget. See
        # data-preprocessing-design-specification.md §5 (vectorization is
        # scoped to DA-mode turns).
        proactive_tasks: dict[str, asyncio.Task] = {}
        if case and force_tool_use:
            proactive_tasks = await self.vectorizer.start_proactive_vectorization(
                case, tool_context
            )

        force_schema_next = False
        # Sticky: once the per-turn token ceiling is crossed we must wrap up on
        # every subsequent iteration. Unlike force_schema_next (reset to False on
        # each successful tool response, ~line "Reset the flag on successful tool
        # usage"), this is NEVER cleared — otherwise the ceiling would be a no-op
        # exactly when it matters (the crossing response usually contains tool
        # calls, which would immediately reset force_schema_next).
        ceiling_reached = False

        for iteration in range(self.MAX_TOOL_ITERATIONS + 1):
            is_final = iteration == self.MAX_TOOL_ITERATIONS

            # Tool availability per iteration:
            # - Iteration 0..N-1: all tools (investigation + schema)
            # - Final iteration / force_schema / ceiling reached: schema tools ONLY
            if is_final or force_schema_next or ceiling_reached:
                tools_for_call = schema_tools
            else:
                tools_for_call = all_tools

            # DA turns: "required" — LLM must search evidence before answering
            # Other turns: "auto" — LLM decides whether to use tools
            # Final/force-schema/ceiling iterations always use "required" (schema only)
            if is_final or force_schema_next or ceiling_reached:
                choice = "required"
            elif force_tool_use:
                choice = "required"
            else:
                choice = "auto"

            logger.info(
                f"Tool loop iteration {iteration}/{self.MAX_TOOL_ITERATIONS} "
                f"(is_final={is_final}, force_schema={force_schema_next}, tool_choice={choice})"
            )

            # Pass da_model when using dedicated provider
            # Bound EVERY tool-loop call: messages within the soft cap, and
            # messages plus THIS call's tools= payload within what the window
            # leaves beside its completion, when the window is known (#614 — the
            # schema tool alone on the final iteration).
            # The full `messages` history is kept for accumulation; only a
            # bounded, most-recent view is sent.
            bounded_messages = self._bound_tool_loop_messages(
                messages,
                tool_loop_budget.soft,
                provider_name,
                token_cache=_msg_token_cache,
                tools=tools_for_call,
                window_tokens=tool_loop_budget.hard(max_tokens),
            )
            generate_kwargs = dict(
                prompt="",
                messages=bounded_messages,
                tools=tools_for_call,
                tool_choice=choice,
                max_tokens=max_tokens,
                temperature=0.2,
                case_id=case.case_id if case is not None else None,
                # Cache the stable prefix (system + tools) across the tool-loop
                # iterations. Only Anthropic acts on this; other providers pop it.
                cache_prompt=True,
            )
            if self.deps.da_model and self.deps.da_provider:
                generate_kwargs["model"] = self.deps.da_model

            # Tier 2 — apply STRUCTURED_OUTPUT_PROVIDER override on the
            # tool-augmented path too. Tool-call iterations land Pydantic
            # schemas back through schema_model.model_validate_json (see
            # _parse_schema_tool_call), so the same routing rationale
            # applies: force the LLM call onto a known-STRICT provider
            # when the operator has configured one. The override is only
            # applied when no da_model is set (DA gets first dibs).
            if not (self.deps.da_model and self.deps.da_provider):
                try:
                    from faultmaven.config.settings import get_settings

                    _settings = get_settings()
                    _override_provider = _settings.llm.structured_output_provider
                    if _override_provider is not None:
                        generate_kwargs["provider_override"] = _override_provider.value
                        _override_model = _settings.llm.get_structured_output_model()
                        if _override_model:
                            generate_kwargs["model"] = _override_model
                except Exception:
                    pass

            async def _tool_loop_call(cap: int):
                """One tool-loop generation at *cap*, metered.

                Metering lives INSIDE the retry closure, not after it: a
                truncation retry is a second real API call, billed like the
                first. Counting only the winner would make DA-turn spend
                under-report exactly on the turns that cost the most.
                """
                call_kwargs = dict(generate_kwargs, max_tokens=cap)
                if cap != max_tokens and tool_loop_budget.window is not None:
                    # A truncation retry asks for a bigger completion: bound the
                    # prompt again for THIS cap, so prompt plus completion still
                    # fits the window (#614). Refuses rather than sends if even
                    # the head cannot fit beside it.
                    call_kwargs["messages"] = self._bound_tool_loop_messages(
                        messages,
                        tool_loop_budget.soft,
                        provider_name,
                        token_cache=_msg_token_cache,
                        tools=tools_for_call,
                        window_tokens=tool_loop_budget.hard(cap),
                    )
                result = await provider.generate(**call_kwargs)
                if self.deps.da_provider is not None:
                    # A dedicated DA provider is a concrete provider instance,
                    # so this call bypassed the registry metering chokepoint.
                    # Meter it here so DA-turn spend is still counted. (When no
                    # DA provider is set, `provider` is the router and the
                    # registry already metered the underlying call.)
                    record_provider_call(
                        getattr(provider, "provider_name", "unknown"),
                        call_kwargs.get("model")
                        or getattr(result, "model", None)
                        or "unknown",
                        result,
                        getattr(result, "response_time_ms", 0),
                    )
                return result

            try:
                # Same ladder the non-tool structured path has had since #513:
                # a cut body means the response is unusable, and the first
                # remedy is more room. This path never had it — `max_tokens` was
                # a fixed 8000 and the schema-tool arguments were parsed
                # unguarded, so a truncated tool call went into
                # `_parse_schema_tool_call`, where the partial-repair machinery
                # (nested-JSON parsing, the state_updates → {} coercion,
                # validation degradation) could turn it into a structurally
                # valid response whose state updates were then APPLIED to the
                # case. Raising the cap first is what stops that (#1094).
                #
                # Escalation only; the escalate-to-degrade tail stays on the
                # non-tool path, which owns the case context that drives it. If
                # the retry is also cut, behaviour is what it was before: parse
                # what came back, and let the existing failure handling below
                # take it if the parse fails.
                response = await generate_with_truncation_retry(
                    _tool_loop_call,
                    max_tokens=max_tokens,
                    ceiling=STRUCTURED_OUTPUT_MAX_TOKENS_CEILING,
                    label=f"tool loop iteration {iteration}",
                )
                # Per-turn ceiling: the call is now metered into the active turn
                # tracker (record_provider_call above for a dedicated DA provider,
                # or the registry chokepoint for the router), so its running total
                # reflects this call — force the loop to wrap up if it is over.
                # A net, not the primary bound: see MAX_TOOL_ITERATIONS (#611).
                _turn_tracker = active_token_tracker.get()
                if _turn_tracker is not None:
                    try:
                        from faultmaven.config.settings import get_settings

                        _turn_ceiling = get_settings().prompt_budget.turn_token_ceiling
                    except Exception:
                        _turn_ceiling = 150000
                    if (
                        _turn_tracker.spend_weighted_tokens > _turn_ceiling
                        and not is_final
                        and not ceiling_reached
                    ):
                        logger.warning(
                            f"Turn spend ({_turn_tracker.spend_weighted_tokens} "
                            f"cost-weighted tokens) exceeded ceiling ({_turn_ceiling}). "
                            f"Forcing the tool loop to wrap up."
                        )
                        # Sticky (never reset) so the next iteration forces the
                        # schema even though the current response's tool calls will
                        # clear force_schema_next below. We already spent this
                        # generation; the ceiling stops the NEXT round of tools.
                        ceiling_reached = True
            except Exception as e:
                # Any iteration failure (timeout, provider error, transient
                # issue) raises ToolCallingUnsupportedError so the caller
                # (_generate_structured_output) falls back to the non-tool
                # structured-output path. Iteration 0 typically indicates
                # provider/model incompatibility; iteration 1+ typically
                # indicates a provider can't satisfy tool_choice=required
                # under FaultMaven's schema sizes (e.g., MiniMax M2P7 on
                # Fireworks hangs when forced to use tools, timing out at
                # the 180s LLM_PROVIDER_TIMEOUT_OVERRIDES limit). Either
                # way, the caller's non-tool path is the right recovery —
                # without this, iter-1+ failures killed the turn entirely
                # and subsequent turns operated against a hole in
                # conversation history.
                from faultmaven.exceptions import ToolCallingUnsupportedError

                logger.warning(
                    "Tool loop: generate failed at iteration %d "
                    "(provider=%s, model=%s): %s. "
                    "Raising ToolCallingUnsupportedError for fallback.",
                    iteration,
                    provider_name,
                    model_info,
                    e,
                )
                raise ToolCallingUnsupportedError(
                    message=(
                        f"Tool calling failed at iteration {iteration}: {e}. "
                        f"Falling back to non-tool path."
                    ),
                    provider=provider_name,
                    model=self.deps.da_model,
                ) from e

            # Check for tool calls in response
            if not hasattr(response, "tool_calls") or not response.tool_calls:
                # No tool calls. Two scenarios:
                # 1. Recoverable (force_schema_next=False): the LLM emitted text
                #    instead of calling a tool. Append the text plus a user-role
                #    nudge directing the schema-tool call, then retry with only
                #    schema tools. The nudge is what makes the next turn coherent
                #    — without it, the LLM "already answered" and won't act.
                # 2. Unrecoverable (force_schema_next=True or is_final): we already
                #    nudged once and the LLM still won't call the schema tool. Try
                #    parsing the text as schema JSON; if that fails, raise
                #    ToolCallingUnsupportedError so _generate_structured_output's
                #    fallback path retries via the non-tool structured-output route.
                if is_final or force_schema_next:
                    from faultmaven.exceptions import ToolCallingUnsupportedError

                    text = (response.content or "").strip()
                    if text:
                        try:
                            return _parse_text_as_schema(text, schema_model)
                        except Exception as parse_err:
                            logger.warning(
                                "Tool loop: text content after forced-schema "
                                "iteration not parseable as schema (%s)",
                                parse_err,
                            )
                    logger.warning(
                        "Tool loop: provider %s ignored tool_choice=required "
                        "with only the schema tool exposed; escalating to "
                        "non-tool fallback path",
                        provider_name,
                    )
                    raise ToolCallingUnsupportedError(
                        message=(
                            f"Provider {provider_name} returned no tool calls "
                            f"under tool_choice=required with the schema tool "
                            f"as the only option. Falling back to non-tool path."
                        ),
                        provider=provider_name,
                        model=self.deps.da_model,
                    )

                logger.warning(
                    "Tool loop: LLM returned no tool calls at iteration %d, "
                    "will force schema on next iteration",
                    iteration,
                )

                # Append the plain text response so the LLM knows what it said.
                #
                # Reasoning artifacts (response.provider_metadata — Anthropic
                # thinking blocks, Gemini assistant_parts) are DELIBERATELY
                # dropped here, unlike _build_assistant_message which
                # round-trips them. This is a recovery re-prompt, not a
                # continuation: the point is to re-ask with a fresh
                # instruction after the model failed to call a tool, and
                # replaying signed reasoning blocks across a state the
                # provider may not accept them in is rejected outright
                # (Anthropic 400s on thinking blocks echoed when thinking is
                # not enabled for that call). Re-prompting without the prior
                # reasoning is the safe direction and is the intended
                # behaviour, not an oversight (#1116).
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content
                        or "I should use a tool to proceed.",
                    }
                )
                # Append a user nudge that explicitly directs the schema-tool
                # call. Without this the conversation ends on an assistant
                # message with no fresh user instruction — most models read that
                # as "already answered" and either repeat themselves or return
                # empty content, defeating the recovery.
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"You must now produce your structured response by "
                            f"calling the `{schema_tool_name}` tool. Use the text "
                            f"above as the `agent_response` field and fill in the "
                            f"remaining required fields. Do not reply with plain "
                            f"text — only a tool call is acceptable."
                        ),
                    }
                )

                force_schema_next = True
                continue

            # Reset the flag on successful tool usage
            force_schema_next = False

            # Check if LLM called the schema tool (termination signal)
            for tc in response.tool_calls:
                func_name = tc.function.get("name", "")
                if func_name == schema_tool_name:
                    logger.info(
                        "Tool loop: schema tool called at iteration %d, "
                        "parsing structured output",
                        iteration,
                    )
                    return _synthesize_agent_response(
                        _parse_schema_tool_call(tc, schema_model),
                        schema_answer_stop_reason(response),
                    )

            # Build assistant message with tool calls
            assistant_msg = _build_assistant_message(response)
            messages.append(assistant_msg)

            # Execute each investigation tool call
            for tc in response.tool_calls:
                func_name = tc.function.get("name", "")
                args_str = tc.function.get("arguments", "{}")
                logger.info(
                    "Tool loop iter %d: LLM called tool=%s args=%s",
                    iteration,
                    func_name,
                    (
                        args_str[:200]
                        if isinstance(args_str, str)
                        else str(args_str)[:200]
                    ),
                )

                args_well_formed = True
                try:
                    args = (
                        json.loads(args_str) if isinstance(args_str, str) else args_str
                    )
                except (json.JSONDecodeError, TypeError):
                    args = {}
                    args_well_formed = False

                # Reliability metric (read-only): did the MODEL hold up the
                # invocation contract? Same label bounding as the budget
                # metrics below — a hallucinated name folds into "unknown"
                # so an inventive model can't mint a label per invention.
                # execution_error is applied by the dispatch below, which
                # always records the attempt (see the try/finally).
                metric_tool = (
                    func_name if func_name in offered_tool_names else "unknown"
                )
                if func_name not in offered_tool_names:
                    _attempt_outcome = "unknown_tool"
                elif not args_well_formed:
                    _attempt_outcome = "invalid_args"
                else:
                    _attempt_outcome = "ok"

                # Counted no matter how the dispatch below ends. The
                # increment used to sit AFTER it, so an exception from
                # execute_tool or _track_da_result dropped the invocation
                # from both the numerator and the denominator — the
                # well-formed-invocation rate then reads cleaner the more
                # the infrastructure fails, which is exactly backwards. A
                # raise is a tool-side failure, the same class as a tool
                # returning success=False, so it folds into
                # execution_error instead of minting a new label — and
                # only from "ok", because a model that named a tool that
                # does not exist failed the contract first.
                try:
                    # Enforce deep_analysis limit
                    if (
                        func_name == "deep_analysis"
                        and deep_analysis_count >= self.MAX_DEEP_ANALYSIS
                    ):
                        result_text = (
                            "deep_analysis is limited to 1 call per turn. "
                            "Use search_file for additional searches."
                        )
                    else:
                        tool_result = await self.deps.investigation_tools.execute_tool(
                            func_name,
                            args,
                            tool_context,
                        )
                        if func_name == "deep_analysis":
                            deep_analysis_count += 1
                        if _attempt_outcome == "ok" and not getattr(
                            tool_result, "success", True
                        ):
                            # Well-formed call, tool-side failure: infrastructure
                            # noise, not the model failing the contract.
                            _attempt_outcome = "execution_error"

                        result_text = self._format_tool_result(
                            tool_result, tool_name=func_name
                        )

                        # --- Per-evidence DA failure tracking (v5.2) ---
                        # Track search_file empty results and check vectorization
                        # triggers. Same pattern as deep_analysis_count above.
                        evidence_id = args.get("evidence_id", "")
                        if evidence_id and func_name in (
                            "search_file",
                            "deep_analysis",
                        ):
                            result_text = await self.vectorizer.track_da_result(
                                func_name=func_name,
                                evidence_id=evidence_id,
                                tool_result=tool_result,
                                result_text=result_text,
                                case=case,
                                tool_context=tool_context,
                                da_empty_search_counts=da_empty_search_counts,
                                proactive_tasks=proactive_tasks,
                            )

                except Exception:
                    if _attempt_outcome == "ok":
                        _attempt_outcome = "execution_error"
                    raise
                finally:
                    tool_call_attempts_total.labels(
                        tool=metric_tool, outcome=_attempt_outcome
                    ).inc()

                # Redact PII in tool results before sending to LLM.
                # Tool results contain raw file content (search_file,
                # deep_analysis) which bypasses prompt-level redaction.
                # Off the event loop via the async boundary (#654).
                if redaction_ctx:
                    result_text = await redaction_ctx.asanitize(result_text)

                # Truncate long results.
                #
                # This is the point where a tool result stops being what the
                # tool produced and becomes what the model sees, and until
                # #1088 it was silent: no log line, no counter, nothing
                # recorded that it had fired. "We don't know what this costs
                # us" was therefore a property of the implementation, not a
                # gap in the sample -- the only available estimate came from
                # arithmetic across two unrelated log lines, for one tool, on
                # one run.
                #
                # Record it before deciding the ceiling. The cap is a single
                # global constant shared by tools that are not alike, so the
                # measurement is per tool.
                #
                # Measured here rather than at the tool: after redaction and
                # after per-tool formatting is the string that actually enters
                # the context. The ONE exception is a kb_qa answer the
                # formatter already trimmed -- see below.
                original_chars = len(result_text)
                # ``metric_tool`` is the same bounded label computed once for
                # this tool call, above — both counters must agree on which
                # tool an invocation belongs to, and two copies of the folding
                # rule is how they stop agreeing.
                tool_result_relayed_total.labels(tool=metric_tool).inc()

                # #1086 gave kb_qa a SECOND, earlier cut: _format_tool_result
                # trims the answer to fit the wrapper so the relay instructions
                # survive, which means an oversized kb_qa answer usually lands
                # at or under the cap by the time it reaches this line. Measured
                # only here, kb_qa -- the tool this issue was opened about --
                # would report a clip rate near zero while still being clipped,
                # which is worse than not measuring it: the number looks honest
                # and is wrong. The formatter therefore records its own trim
                # into these same counters, against the TRUE pre-trim size, and
                # this site steps aside for that result so the observation is
                # made exactly once, at whichever site last saw the whole
                # string.
                # Anchored to the END rather than a substring search. The
                # formatter emits `... + marker + suffix`, and both are static
                # instruction text carrying no entity the redactor rewrites, so
                # that tail survives sanitisation intact. A plain `in` test
                # would also match an answer that merely QUOTES the marker --
                # costing that result its histogram sample, and, if redaction
                # then expanded it past the cap, its truncation count too. An
                # answer would now have to END on the marker to be misread.
                formatter_trimmed = func_name == "kb_qa" and result_text.endswith(
                    KB_QA_ANSWER_TRUNCATED_MARKER + KB_QA_RELAY_SUFFIX
                )
                if not formatter_trimmed:
                    tool_result_chars.labels(tool=metric_tool).observe(original_chars)

                if original_chars > self.TOOL_RESULT_MAX_CHARS:
                    # A kb_qa result can reach here already trimmed and STILL be
                    # oversized, because redaction runs in between and expands
                    # text (an IPv4 becomes a 29-char placeholder). That is a
                    # second cut on one result, worth a log line but not a
                    # second increment -- the clip rate must stay a rate.
                    if not formatter_trimmed:
                        tool_result_truncated_total.labels(tool=metric_tool).inc()
                    # Cut FIRST, then report, so the count is what the cut
                    # actually destroyed rather than the overflow it started
                    # from. Those diverged once kb_qa began eliding: the elide
                    # spends markers and paragraph realignment on top of the
                    # overflow. Both sites now report the same thing -- source
                    # characters destroyed -- so the two can be summed.
                    result_text, dropped_chars = self._truncate_tool_result(
                        result_text, func_name
                    )
                    # WARNING, not INFO: this discards content the model was
                    # meant to reason over, and the counters are no-ops unless
                    # ENABLE_METRICS -- which a standalone run does not set. The
                    # log line is what makes the clip observable there at all.
                    logger.warning(
                        "tool_result_truncated",
                        extra={
                            "tool": metric_tool,
                            "original_chars": original_chars,
                            "cap_chars": self.TOOL_RESULT_MAX_CHARS,
                            "dropped_chars": dropped_chars,
                            "at": "tool_loop",
                            # True means this result was ALREADY cut and counted
                            # at the formatter, and redaction pushed it back
                            # over the cap. One physical clip, two records: any
                            # aggregation that counts clips must drop these or
                            # it double-counts exactly the tool the ceiling
                            # question is about (#1088).
                            "after_formatter_trim": formatter_trimmed,
                        },
                    )

                # Append tool result message
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": func_name,
                        "content": result_text,
                    }
                )

        # Should not reach here (final iteration forces schema)
        raise MilestoneEngineError(
            "Tool loop exhausted without producing structured output"
        )

    def _da_provider_supports_tools(self) -> bool:
        """Whether the resolved DA/chat provider+model can do tool calling.

        Single source of truth for the tool-calling capability check, shared by
        ``_tools_effectively_available`` (the directed-analysis elision gate) and
        the Layer-1 pre-check in ``_generate_structured_output_inner`` so the two
        cannot drift. Absent capability info → assume capable (the runtime path
        then catches an actual failure and falls back).
        """
        provider = self.deps.da_provider or self.deps.llm_provider
        model = self.deps.da_model if self.deps.da_provider else None
        supports = getattr(provider, "supports_tool_calling", None)
        if supports is None:
            return True
        try:
            return bool(supports(model))
        except Exception:
            return False

    def tools_effectively_available(self) -> bool:
        """True when investigation tools are registered AND the resolved
        provider/model can actually do tool calling.

        This is the real precondition behind the directed-analysis evidence
        index+stub elision: the inline extract may only be dropped (telling the
        agent to ``search_file`` for specifics) when ``search_file`` will actually
        run this turn. A tool-less / tool-incapable turn that dropped the extract
        would be stranded with neither the data nor a working tool — the
        premature-conclusion failure FaultMaven guards against.
        """
        return (
            bool(self.deps.investigation_tools) and self._da_provider_supports_tools()
        )

    def build_da_tool_schemas(self) -> list[dict]:
        """Build OpenAI-format tool definitions for DA investigation tools."""
        if not self.deps.investigation_tools:
            return []

        tools = []
        for agent_tool in self.deps.investigation_tools.get_all_tools():
            schema = agent_tool.get_schema()
            tools.append(
                {
                    "type": "function",
                    "function": schema,
                }
            )
        return tools

    async def _resolve_shared_kb_ids(self, user_id: str, enterprise_id: Any) -> list:
        """KB item ids shared to ``user_id``'s teams — the team arm of the tool
        path's read allowlist (ADR-013 §D4).

        Keyed on the **session** user, matching the owner arm: ``kb_tool_adapter``
        passes ``ToolContext.user_id`` to ``build_kb_scope_filter`` as the owner,
        so both arms must describe the same principal. Keying the team arm on the
        case owner instead would let a collaborator's turn read the owner's
        team-shared items — a wider allowlist than the reader is entitled to.
        (``_prefetch_kb_context`` keys on the case owner precisely because it is
        not acting for a session user; the two are deliberately different.)

        ``team_service``/``share_repository`` are wired post-construction and are
        absent in standalone, so a missing collaborator collapses the team arm to
        empty rather than raising — global ∪ owned still resolves.
        """
        if not user_id or user_id == "system":
            return []

        team_service = self.deps.team_service
        share_repository = self.deps.share_repository
        if not team_service or not share_repository:
            return []

        from faultmaven.modules.knowledge.domain.services.knowledge_service import (
            resolve_shared_kb_ids,
        )

        try:
            team_ids = await team_service.list_all_user_team_ids(user_id)
            return await resolve_shared_kb_ids(
                share_repository, team_ids, enterprise_id
            )
        except Exception:  # noqa: BLE001
            # Degrade to global ∪ owned rather than failing the turn. Narrowing
            # is safe; the alternative would be an unscoped read.
            logger.warning(
                "shared_kb_id_resolution_failed",
                extra={"user_id": user_id},
                exc_info=True,
            )
            return []

    async def build_tool_context(self, case: Any, user_id: str | None = None) -> Any:
        """Build ToolContext for tool execution during DA turns.

        ``user_id`` is the turn's authenticated principal, threaded down from
        ``process_turn``. It is the *only* source: it previously came off
        ``intent_data``, which no caller populates — ``InvestigationService``
        builds that dict from ``QueryIntent.model_dump()`` (a model with no
        ``user_id`` field) plus ``query_mode``, so every live turn resolved to
        ``"system"`` and both arms of the KB read allowlist
        (``build_kb_scope_filter(user_id, shared_kb_ids)``) collapsed to the
        global corpus. Reading it from the intent payload would also make the
        read principal client-settable; the parameter comes from
        ``current_user.user_id``.

        ``None`` (engine-internal turn, no principal) keeps the historical
        ``"system"`` sentinel, which matches no owner and resolves no teams.
        """
        from faultmaven.modules.agent.tools.base import (
            ToolContext,
            derive_kb_context_metadata,
        )

        user_id = user_id or "system"
        enterprise_id = getattr(case, "enterprise_id", "")

        # Extract current investigation stage for tool context enrichment
        metadata: dict[str, Any] = {}
        progress = getattr(case, "progress", None)
        if progress:
            current_stage = getattr(progress, "current_stage", None)
            if current_stage:
                stage_value = (
                    current_stage.value
                    if hasattr(current_stage, "value")
                    else str(current_stage)
                )
                metadata["stage"] = stage_value.upper()

        return ToolContext(
            session_id=case.case_id,
            case_id=case.case_id,
            enterprise_id=enterprise_id,
            user_id=user_id,
            shared_kb_ids=await self._resolve_shared_kb_ids(user_id, enterprise_id),
            case_repository=self.deps.repository,
            metadata=metadata,
            in_memory_case=case,
            kb_context_metadata=derive_kb_context_metadata(case),
        )

    @classmethod
    def _truncate_tool_result(cls, text: str, tool_name: str) -> tuple[str, int]:
        """Cut an oversized tool result to the cap, protecting a tail if it has one.

        Returns the cut text and the number of *text* characters destroyed --
        the same meaning the formatter's cut reports, so the two sites can be
        aggregated into one number (#1088).

        This runs AFTER PII redaction, and that ordering is why the protection
        cannot live in the formatter alone. Redaction *expands* text: every
        entity becomes a ``<TYPE_digest>`` placeholder, so an IPv4 address grows
        from 8 characters to 29. A reservation computed while wrapping is
        therefore no longer true by the time the cap is applied, and a kb_qa
        answer sized exactly to the budget re-crosses it once its entities are
        replaced.

        For kb_qa the tail is instructions rather than prose, so cutting
        head-first would delete how the model is told to answer while keeping
        the answer it is meant to relay. The last ``len(KB_QA_RELAY_SUFFIX)``
        characters are preserved verbatim: that block is static instruction text
        containing no entity the redactor rewrites, so its length survives
        sanitisation and the slice still lands on the suffix.

        Preserving the suffix is necessary and not sufficient. Everything
        between the head and that suffix is the ANSWER, and cutting it
        head-first here would undo the whole point of eliding its middle in the
        formatter: the remediation steps and the ``Sources:`` line would go,
        leaving a suffix that instructs the model to cite "the primary source
        title(s) from the content above" with the source line gone -- the exact
        failure #1088 fixed one step earlier. This path is not hypothetical: the
        formatter sizes the answer to a budget that redaction then invalidates by
        expanding it. So the answer between the wrapper is elided in the middle
        here too, by the same helper, and only genuinely tail-less results (every
        other tool) take the plain head-first cut.
        """
        cap = cls.TOOL_RESULT_MAX_CHARS
        marker = "\n[truncated]"

        protected = len(KB_QA_RELAY_SUFFIX) if tool_name == "kb_qa" else 0
        if protected and len(text) > protected + len(marker):
            body = text[:-protected]
            suffix = text[-protected:]
            elided, dropped = _elide_answer_middle(body, cap - protected)
            return elided + suffix, dropped

        # NOTE: this branch returns cap + len(marker) characters, i.e. 12 over
        # the cap, while the kb_qa branch above fits inside it. That asymmetry
        # is real and pre-dates this change -- ``test_milestone_engine_tool_loop``
        # pins the looser bound explicitly, and #1090 pins this string
        # byte-identical as its behaviour-unchanged guarantee. Tightening it
        # would change what the model sees for every non-kb_qa tool, which is
        # a behaviour change to paths this issue never measured; kb_qa is
        # stricter because its formatter has to reserve the relay wrapper, not
        # because the cap means something different here. Left alone
        # deliberately (#1088).
        return text[:cap] + marker, max(0, len(text) - cap)

    @staticmethod
    def _format_tool_result(result: Any, tool_name: str = "") -> str:
        """Format a ToolResult into a string for the LLM."""
        if not result.success:
            return f"Error: {result.error or 'Unknown error'}"

        if result.data is None:
            return "Success (no data returned)"

        # KB results: wrap with relay instruction and source citation guidance.
        # Note: _arun returns a pre-formatted string (via KBConfig.format_response),
        # not a dict. The string includes "Sources: ..." at the end.
        if tool_name == "kb_qa" and result.data:
            content = (
                result.data if isinstance(result.data, str) else json.dumps(result.data)
            )
            logger.info(f"kb_qa result: {len(content)} chars")
            prefix = KB_QA_RELAY_PREFIX
            suffix = KB_QA_RELAY_SUFFIX
            # Reserve the wrapper before the generic cap can reach it. That
            # cap keeps the HEAD of whatever it is given, so an oversized
            # result loses its TAIL — and the tail here is not prose, it is the
            # citation format plus "return via the schema tool, do not reply
            # with plain text". A long KB answer would therefore silently strip
            # the instructions that tell the model how to answer at all, which
            # is the opposite of the intended failure. Reserving the wrapper
            # and trimming the ANSWER keeps both instructions intact. How the
            # answer itself is trimmed is a separate question, answered by
            # _elide_answer_middle below: not head-first either.
            budget = (
                StructuredOutputGenerator.TOOL_RESULT_MAX_CHARS
                - len(prefix)
                - len(suffix)
            )
            if len(content) > budget:
                # Elide FIRST, then report. ``len(content) - budget`` was the
                # true drop while the cut was a plain slice to the budget; the
                # middle-elide also spends its two markers and its paragraph
                # realignment, so that expression under-reports. So does a
                # before/after length difference, which nets the inserted
                # markers off the loss. The helper returns the count instead:
                # ANSWER characters destroyed, the same meaning the loop's cut
                # site reports, because ``dropped_chars`` is what the ceiling
                # gets sized from (#1090) and it has to mean one thing.
                original_chars_answer = len(content)
                content, dropped_chars = _elide_answer_middle(content, budget)
                logger.info(
                    "kb_qa answer trimmed to fit the tool-result budget",
                    extra={
                        "original_chars": original_chars_answer,
                        "budget_chars": budget,
                        "dropped_chars": dropped_chars,
                    },
                )
                # Feed this cut into the SAME counters the tool loop uses
                # (#1088). This trim is the one that actually clips kb_qa in
                # practice -- the loop's cap rarely sees an oversized kb_qa
                # result because this ran first -- so leaving it out would make
                # kb_qa report the lowest clip rate in the system while being
                # the tool the ceiling question is about. Sized on the WRAPPED
                # string, so the number is comparable with every other tool's,
                # which is also measured wrapped and pre-cut.
                wrapped_chars = len(prefix) + original_chars_answer + len(suffix)
                tool_result_chars.labels(tool="kb_qa").observe(wrapped_chars)
                tool_result_truncated_total.labels(tool="kb_qa").inc()
                logger.warning(
                    "tool_result_truncated",
                    extra={
                        "tool": "kb_qa",
                        "original_chars": wrapped_chars,
                        "cap_chars": StructuredOutputGenerator.TOOL_RESULT_MAX_CHARS,
                        "dropped_chars": dropped_chars,
                        "at": "formatter",
                    },
                )
            return prefix + content + suffix

        # search_file results: append citation guidance so the LLM cites
        # the source and line numbers in its response.
        #
        # #666: this instruction is the mechanism that put
        # "pasted-content-20260709T105531.txt (line 20)" in front of Beta
        # users — it tells the model to cite a name and hands it one. The
        # tool supplies ``UploadedFile.display_name`` under that key, which
        # is the same string the item's ``label`` attribute carries in
        # <evidence_collected>, so the name the model is told to cite here
        # designates something it can also see there. "source", not
        # "filename": a paste has no filename to cite.
        if tool_name == "search_file" and isinstance(result.data, dict):
            source_name = result.data.get("label", "unknown")
            results_count = result.data.get("results_count", 0)
            content = json.dumps(result.data)
            if results_count > 0:
                # HEAD, not tail. ``_truncate_tool_result`` protects a tail
                # only for kb_qa (#1088); every other tool takes a plain
                # ``text[:cap] + marker``, which is head-first. A search_file
                # excerpts result over a large paste routinely exceeds the cap,
                # so a tail-appended instruction is deleted exactly on the
                # results big enough to need it — and this is the one line that
                # hands the model the correct name to cite. Leading it also
                # reads better: the rule arrives before the data it governs.
                content = (
                    f"CITATION: When referencing these results, cite the source "
                    f'and line numbers exactly as named here (e.g., "In '
                    f'{source_name}, line 42: ...").\n\n' + content
                )
            return content

        if isinstance(result.data, str):
            return result.data
        return json.dumps(result.data)

    async def generate_structured_output(
        self,
        prompt: str,
        schema_model: Any,
        investigation_tools: list[dict] | None = None,
        tool_context: Any | None = None,
        force_tool_use: bool = False,
        redaction_ctx: Any | None = None,
        case: Any | None = None,
        user_message: Optional[str] = None,
        fallback_prompt_builder: Optional[Callable[[], str]] = None,
        reasoning_intent: Optional[Any] = None,
        min_output_tokens: Optional[int] = None,
        base_prompt_builder: Optional[Callable[..., str]] = None,
    ) -> BaseInteractionResponse:
        """Structured-output generation with runtime context-length recovery.

        Wraps the provider call so that a context-length rejection from the LLM
        gateway (which can enforce a smaller window than our registry estimate —
        e.g. a corporate proxy or aggregator) does NOT permanently block the
        case. On such an error it recompiles the turn with the minimal
        ``FALLBACK_*`` prompt and retries once. See the context-management design
        doc §7.1. ``user_message`` is required to build the fallback; when it is
        not supplied the recovery is skipped and the error propagates.
        """
        try:
            return await self._generate_structured_output_inner(
                prompt,
                schema_model,
                investigation_tools=investigation_tools,
                tool_context=tool_context,
                force_tool_use=force_tool_use,
                redaction_ctx=redaction_ctx,
                case=case,
                fallback_prompt_builder=fallback_prompt_builder,
                reasoning_intent=reasoning_intent,
                min_output_tokens=min_output_tokens,
                base_prompt_builder=base_prompt_builder,
            )
        except Exception as exc:
            if (
                user_message is not None
                and case is not None
                and _is_context_length_error(exc)
            ):
                from faultmaven.core.investigation.prompts.templates.fallback import (
                    DEGRADED_NO_TOOLS_NOTICE,
                    get_fallback_prompt_for_case,
                )

                reason = classify_token_limit_reason(exc)
                logger.warning(
                    "prompt_context_error_recovered: provider rejected prompt as "
                    "too long (case %s, reason %s); retrying once with the minimal "
                    "fallback prompt. Original error: %s",
                    getattr(case, "case_id", "?"),
                    reason,
                    exc,
                )
                # Observability half of the degrade: the log line carries the case
                # id, this carries the rate. A SUSTAINED rate means turns are
                # routinely over the window — a prompt-sizing problem, not a
                # recovery problem.
                prompt_context_recovery_total.labels(reason=reason).inc()
                # The notice is required, not cosmetic: the fallback body lists
                # addressable files, but this retry drops the tools to reach them,
                # so without it the agent is told to search what it cannot.
                fb_prompt = get_fallback_prompt_for_case(case, user_message)
                fb_prompt += DEGRADED_NO_TOOLS_NOTICE
                # Minimal retry: drop tools to shrink the request further.
                return await self._generate_structured_output_inner(
                    fb_prompt,
                    schema_model,
                    investigation_tools=None,
                    tool_context=None,
                    force_tool_use=False,
                    redaction_ctx=redaction_ctx,
                    case=case,
                    reasoning_intent=reasoning_intent,
                    min_output_tokens=min_output_tokens,
                )
            raise

    async def _generate_structured_output_inner(
        self,
        prompt: str,
        schema_model: Any,
        investigation_tools: list[dict] | None = None,
        tool_context: Any | None = None,
        force_tool_use: bool = False,
        redaction_ctx: Any | None = None,
        case: Any | None = None,
        fallback_prompt_builder: Optional[Callable[[], str]] = None,
        reasoning_intent: Optional[Any] = None,
        min_output_tokens: Optional[int] = None,
        base_prompt_builder: Optional[Callable[..., str]] = None,
    ) -> BaseInteractionResponse:
        """
        Generate structured output from LLM using provider-agnostic capability system.

        This method automatically detects the provider's structured output capabilities
        and adjusts the prompt and response format accordingly:
        - STRICT mode: Uses json_schema with strict:true (OpenAI GPT-4o, Groq gpt-oss)
        - BEST_EFFORT mode: Uses json_object with schema in prompt (most models)
        - FUNCTION_CALLING mode: Uses tool calling pattern (Anthropic Claude)
        - NONE mode: Schema only in prompt, no API support (legacy models)

        When investigation_tools and tool_context are provided, routes through
        _tool_augmented_generate for a bounded tool-calling loop.

        Args:
            prompt: User prompt
            schema_model: Pydantic model class for expected output
            investigation_tools: OpenAI-format tool defs for investigation tools
            tool_context: ToolContext for tool execution
            force_tool_use: If True, tool_choice="required" (DA turns).
                If False, tool_choice="auto" (LLM decides).
            redaction_ctx: Case-scoped redaction context for PII sanitization
            base_prompt_builder: Re-assembles the base for the model the tool
                loop sends to, when ``prompt`` does not fit there (#614); see
                ``_tool_augmented_generate``. Unused on the non-tool path.

        Returns:
            Instantiated Pydantic model
        """
        # Apply case-scoped PII redaction to the prompt before any LLM call.
        # This covers both the tool-augmented (DA) and single-shot paths.
        # Off the event loop via the async boundary (#654).
        if redaction_ctx:
            prompt = await redaction_ctx.asanitize(prompt)
        # Branch to tool-augmented generation for DA turns with tools.
        # Two layers of protection:
        # 1. Pre-check: skip known-incompatible providers (avoids wasted API call)
        # 2. Runtime fallback: if tool calling fails on first attempt, catch
        #    ToolCallingUnsupportedError and fall through to non-tool path
        if investigation_tools and tool_context:
            from faultmaven.exceptions import ToolCallingUnsupportedError

            # Layer 1: Pre-check for known-incompatible providers/models
            # (shared capability check with the elision gate — see
            # _da_provider_supports_tools).
            if not self._da_provider_supports_tools():
                provider = self.deps.da_provider or self.deps.llm_provider
                model = self.deps.da_model if self.deps.da_provider else None
                logger.warning(
                    "Provider %s (model: %s) does not support tool calling. "
                    "Falling back to non-tool structured output path.",
                    getattr(provider, "provider_name", type(provider).__name__),
                    model or "default",
                )
            else:
                # Layer 2: Runtime fallback for unknown incompatibilities
                try:
                    return await self._tool_augmented_generate(
                        prompt,
                        schema_model,
                        investigation_tools,
                        tool_context,
                        force_tool_use=force_tool_use,
                        redaction_ctx=redaction_ctx,
                        case=case,
                        base_prompt_builder=base_prompt_builder,
                    )
                except ToolCallingUnsupportedError as e:
                    logger.warning(
                        "Tool calling failed at runtime: %s. "
                        "Falling back to non-tool structured output path.",
                        e,
                    )
                    # The prompt may have elided historical evidence on the
                    # assumption search_file would run (directed-analysis
                    # index+stub). On the non-tool path there is no search_file,
                    # so rebuild with full evidence — otherwise the agent would
                    # be stranded (elided evidence + no tool to recover it).
                    if fallback_prompt_builder is not None:
                        try:
                            prompt = fallback_prompt_builder()
                        except Exception as rebuild_exc:  # never break the fallback
                            logger.warning(
                                "fallback_prompt_builder failed (non-fatal): %s",
                                rebuild_exc,
                            )

        # Get provider-specific structured output strategy
        schema = schema_model.model_json_schema()
        strategy = self.deps.llm_provider.get_structured_output_strategy(schema)

        # Conditionally include schema in prompt based on provider capability
        if strategy.include_schema_in_prompt:
            # Provider requires schema in prompt text (json_object or prompt_only
            # modes). SCHEMA_INSTRUCTIONS is gated on the schema's shape inside
            # the helper — see _schema_prompt_instruction.
            final_prompt = f"{prompt}{_schema_prompt_instruction(schema)}"
        else:
            # Provider supports strict json_schema - no need for schema in prompt
            final_prompt = prompt

        # Track the generation cap across retries. ``bumped`` is what tells the
        # next attempt it must not be answered from cache: the cache is keyed on
        # (case, prompt, model) and max_tokens is not part of that key, so the
        # truncated body the first attempt stored would otherwise be served back
        # instantly and the raised cap would never reach the provider (#513).
        max_tokens_state = {"value": STRUCTURED_OUTPUT_MAX_TOKENS, "bumped": False}

        def _on_truncation(exc: Exception) -> OutputTruncationError:
            """Raise the cap for the next attempt and return the typed signal.

            Both truncation sites — the provider reporting the cut, and the
            parse of a body that ran out — funnel through here, so the cap moves
            exactly once per failed attempt whichever site saw it, and the
            ladder (raise the cap, then degrade the prompt) has a single owner.

            The message is written to be self-describing rather than passing the
            original through unchanged: the JSON decoder's wording ("Expecting
            ',' delimiter") carries no hint that this was a truncation, and the
            recovery metric downstream reads exactly this text to attribute the
            degrade.
            """
            old_max = max_tokens_state["value"]
            new_max = min(old_max * 2, STRUCTURED_OUTPUT_MAX_TOKENS_CEILING)
            if new_max <= old_max:
                logger.warning(
                    "JSON truncation at the max_tokens ceiling (%s); handing off "
                    "to the minimal-prompt degrade instead of retrying at the "
                    "same size. Underlying error: %s",
                    old_max,
                    exc,
                )
                return OutputTruncationError(
                    f"Response truncated at the max_tokens ceiling ({old_max}): {exc}",
                    cap_reached=True,
                )
            max_tokens_state["value"] = new_max
            max_tokens_state["bumped"] = True
            logger.warning(
                "JSON truncation detected, increasing max_tokens: %s → %s",
                old_max,
                new_max,
            )
            return OutputTruncationError(
                f"Response truncated at max_tokens={old_max}: {exc}",
                cap_reached=False,
            )

        # Define the LLM operation for retry
        async def llm_operation():
            # Build generate parameters based on strategy mode
            current_max_tokens = max_tokens_state["value"]
            generate_params = {
                "prompt": final_prompt,
                "max_tokens": current_max_tokens,
                "temperature": 0.2,  # Lower temperature for structured output
                "case_id": case.case_id if case is not None else None,
                "bypass_cache": max_tokens_state["bumped"],
            }
            # fm#1116: the tool-less diagnostic call declares its reasoning
            # intent and output floor (#1117/#1118). Only ever set on the
            # single-shot path — the tool loop cannot carry effort on gpt-5.x.
            if reasoning_intent is not None:
                generate_params["reasoning_intent"] = reasoning_intent
            if min_output_tokens is not None:
                generate_params["min_output_tokens"] = min_output_tokens

            # Tier 2 — route schema-bound calls to STRUCTURED_OUTPUT_PROVIDER
            # when set, so operators running a weak-structured-output
            # CHAT_PROVIDER can keep that provider for chat/synthesis but
            # force schema-bound calls onto a known-STRICT provider.
            # Companion to the capability-routing fix (Tier 1):
            # capability detection only helps if the call also LANDS on the
            # provider that has the capability.
            try:
                from faultmaven.config.settings import get_settings

                _settings = get_settings()
                _override_provider = _settings.llm.structured_output_provider
                if _override_provider is not None:
                    generate_params["provider_override"] = _override_provider.value
                    # When override is set, also resolve the override
                    # provider's preferred model so we don't accidentally
                    # send CHAT_PROVIDER's model name to a different provider.
                    _override_model = _settings.llm.get_structured_output_model()
                    if _override_model:
                        generate_params["model"] = _override_model
            except Exception:
                # Settings unavailable (rare; test setup) — proceed with
                # the default routing rather than failing the turn.
                pass

            logger.debug(
                f"Structured output generation attempt with max_tokens={current_max_tokens}"
            )

            # Apply strategy-specific parameters
            if strategy.mode == StructuredOutputMode.FUNCTION_CALLING:
                # Use tools/function calling for structured output (Anthropic, etc.)
                from faultmaven.utils.schema_converter import pydantic_to_openai_tools

                generate_params["tools"] = pydantic_to_openai_tools(schema_model)
                generate_params["tool_choice"] = "required"  # Force tool use
                # Don't include response_format for function calling
            else:
                # Use response_format for JSON modes (STRICT, BEST_EFFORT, NONE)
                if strategy.response_format:
                    generate_params["response_format"] = strategy.response_format

            try:
                response = await self.deps.llm_provider.generate(**generate_params)
            except Exception as gen_exc:
                # Some providers can see the cut themselves and raise before
                # there is any body to parse — Gemini does this on
                # finishReason=MAX_TOKENS. That path never reaches the parse
                # block below, so without this the cap was never raised and the
                # retry repeated the identical full-size call until the attempts
                # ran out (#513).
                if is_output_truncation_error(gen_exc):
                    raise _on_truncation(gen_exc) from None
                raise

            # The provider's own truncation signal, kept for the parse block
            # below rather than acted on here.
            #
            # Deliberately NOT a pre-parse gate. A cut is only a problem if it
            # cost us the ANSWER, and on the prompt-only/BEST_EFFORT modes the
            # answer is not the whole body: those models routinely emit a
            # complete ```json block and then keep talking, which is why the
            # extractor below handles "Some text\n```json\n{...}\n```\nMore
            # text". When the cap lands in that trailing prose the JSON is
            # whole and validates, and raising on the stop reason alone would
            # discard a good response, spend a second full-size generation, and
            # on a second trailing-off hand the turn to the minimal-prompt
            # degrade — throwing away the prompt context too.
            #
            # So: try to parse first, and let the stop reason decide only once
            # something has actually failed.
            provider_reported_cut = not isinstance(response, str) and getattr(
                response, "is_truncated", False
            )

            content = response if isinstance(response, str) else response.content

            # For function calling, extract from tool_calls
            if strategy.mode == StructuredOutputMode.FUNCTION_CALLING:
                # Parse response to handle tool_calls format
                if hasattr(response, "tool_calls") and response.tool_calls:
                    # Extract arguments from first tool call
                    args = response.tool_calls[0].function.get("arguments", "{}")

                    # arguments may be a string (most providers) or dict (some providers)
                    if isinstance(args, dict):
                        # Convert dict to JSON string for model_validate_json
                        content = json.dumps(args)
                    else:
                        # Already a string
                        content = args
            try:
                # First, try to load content as JSON if it's a string.
                #
                # ``content`` is REASSIGNED to whatever actually parsed, which is
                # load-bearing rather than tidiness: ``is_truncated_json_error``
                # below measures a ``JSONDecodeError.pos`` against
                # ``len(content)`` to decide whether the body was cut and the
                # ``max_tokens`` ladder should re-run (#513). Parsing a
                # de-fenced copy while leaving ``content`` fenced makes that
                # comparison read an offset from one string against the length
                # of a longer one, so the guard answers False and the ladder
                # silently stops engaging on any fenced truncated response.
                if isinstance(content, str):
                    content = json_payload_text(content)
                    content_obj = json.loads(content, strict=False)
                else:
                    content_obj = content

                # Parse any nested JSON strings (reuse class static method)
                content_obj = _parse_nested_json(content_obj)

                # Recover the XML parameter form, or coerce an unresolvable
                # state_updates to {} so Pydantic defaults apply (counted).
                content_obj = _normalize_state_updates(content_obj, schema_model)

                # Fix any hallucinated enum values (reuse class static method)
                schema_dict = schema_model.model_json_schema()
                content_obj = _fix_enum_violations(
                    content_obj, schema_dict, root_defs=schema_dict.get("$defs")
                )

                # Convert back to JSON string for Pydantic validation
                content = json.dumps(content_obj)

                # Counted here too, not only in the degradation ladder: this
                # path validates DIRECTLY, so leaving it out made
                # ``schema_validation_total`` a partial population — the A/B
                # schema-validity rate would be read over a denominator that
                # excludes every body the non-tool structured path served (a
                # tool-incapable model, the ToolCallingUnsupportedError
                # fallback, a FUNCTION_CALLING single shot), reporting a rate
                # for a path it never observed. One increment per BODY, so a
                # retried generation contributes one per attempt — the same
                # unit the ladder counts in.
                # Same never-500 backstop the tool-loop path has had
                # (``_parse_schema_tool_call``): a parse-time cross-field
                # validator rejecting ONE list entry (a ``suggested_follow_ups``
                # item carrying ``evidence_need_id`` with the wrong action_type,
                # an ``evidence_to_add`` row without its file id) used to fail
                # the WHOLE turn here, while the tool path pruned the entry and
                # kept the turn. fm#1116 routes tool-less turns to this path,
                # so it must degrade the same way. The helper records the
                # validation outcome itself (one increment per body, as before)
                # and re-raises only when nothing survives, which the
                # truncation check below still sees.
                parsed = _validate_with_degradation(content_obj, schema_model)
                return _synthesize_agent_response(
                    parsed, schema_answer_stop_reason(response)
                )
            except Exception as validation_error:
                # A body that ran out is the recoverable case: raise the cap and
                # retry. Decided POSITIONALLY against the content we just tried
                # to parse, not by matching words in the message — CPython's
                # decoder says "Expecting ',' delimiter" or "Unterminated string
                # starting at" for a cut-off body and never "truncated" or "EOF
                # while parsing", so the phrase test that used to guard this
                # matched nothing the decoder actually emits (#513).
                #
                # ``content`` is the raw body while json.loads is what failed; by
                # the time model_validate_json runs it has been re-serialized
                # from a successfully parsed object, so a schema violation there
                # is a ValidationError and correctly falls through untouched.
                if is_truncated_json_error(validation_error, content):
                    raise _on_truncation(validation_error) from None

                # The provider said it hit the cap and the body did not survive
                # parsing. The positional test above misses two shapes of that:
                # a body cut in a way that leaves it malformed in the MIDDLE
                # (correctly not truncation on its own — a bigger cap cannot fix
                # a stray token — but with the provider confirming a cut, more
                # room is the right remedy), and a ValidationError from a body
                # that parsed but lost a required field to the cut, which
                # ``is_truncated_json_error`` deliberately declines to claim
                # because it cannot tell that case from a schema violation.
                # The stop reason can (#1094).
                if provider_reported_cut:
                    raise _on_truncation(
                        RuntimeError(
                            f"provider {getattr(response, 'provider', '?')} "
                            f"reported stop_reason=max_tokens and the body did "
                            f"not parse: {validation_error}"
                        )
                    ) from None
                # Re-raise to trigger retry
                raise

        # Execute with retry and error handling
        result, error_result = await self.deps.llm_error_handler.with_retry(
            operation=llm_operation
        )

        if result is not None:
            return result

        # All retries exhausted or non-retryable error
        if error_result:
            error_msg = error_result.message
            # Fold the triggering provider wording into the message text (e.g.
            # "prompt is too long: 250000 > 200000") so diagnostics keep it — the
            # ErrorResult's own message is the generic classifier string. We do
            # NOT chain via ``raise ... from``: that would put the provider's
            # LLMException (a context overflow is HTTP 400) on the __cause__ chain,
            # and llm_service_error_http_exception reads a provider status BEFORE
            # the engine error_code, silently re-routing the documented
            # TOKEN_LIMIT -> 503 to a 4xx -> 502. The engine error_code stays the
            # authoritative signal for this failure.
            orig = error_result.original_exception
            detail = f"{error_msg} ({orig})" if orig is not None else error_msg
            logger.error(f"Structured generation failed after retries: {detail}")
            raise MilestoneEngineError(
                f"Structured output generation failed: {detail}",
                error_code=error_result.error_code,
                category=error_result.category,
            )
        else:
            raise MilestoneEngineError(
                "Structured output generation failed with unknown error"
            )
