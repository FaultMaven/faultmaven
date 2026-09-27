"""Handling a terminal (RESOLVED/CLOSED) turn: report regeneration and the terminal Q&A path. Runbook creation is a sibling collaborator (runbook_creation.py)."""

import logging
from typing import Any, Optional

from faultmaven.core.investigation.milestone_engine.redaction import _should_redact
from faultmaven.core.investigation.milestone_engine.regeneration import (
    _remaining_regens_for,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _flatten_follow_ups,
)
from faultmaven.core.investigation.prompts.templates.assembly import get_prompt_for_case
from faultmaven.core.investigation.schemas import (
    TerminalResponse,
)
from faultmaven.modules.case.contracts import (
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    Case,
    CaseState,
)

from .response_synthesis import (
    is_agent_response_synthesized,
)
from .terminal_replies import (
    GENERATE_RUNBOOK_ANYWAY_PAYLOAD,
    GENERATE_RUNBOOK_PAYLOAD,
    _closed_suggestions,
    _resolved_suggestions,
)

logger = logging.getLogger(__name__)


class TerminalTurnHandler:
    """Drives a terminal-state turn: regenerating the case report on request, and answering terminal Q&A without reopening the investigation."""

    # Only the precomposed payloads submitted by the DECIDE regen
    # suggestions reach this set. Free-typed summary-shaped requests
    # (e.g. "give me a recap", "summarize what we discussed") fall through
    # to terminal Q&A on purpose: typing should never produce a persisted
    # Report side effect. The Q&A prompt is instructed to redirect those
    # asks to the existing summary + regen affordance.
    _REPORT_REGEN_PATTERNS = (
        "regenerate the closure summary report for this case",
        "regenerate the resolution summary report for this case",
    )
    # Same exact-match policy as _REPORT_REGEN_PATTERNS: only the
    # precomposed DECIDE-suggestion payload reaches the runbook
    # creation path. Free-typed paraphrases ("create a runbook please")
    # fall through to Q&A. This keeps the principle consistent across
    # terminal-state actions: clicking triggers persisted side effects;
    # typing never does.
    _RUNBOOK_CREATION_PATTERNS = (GENERATE_RUNBOOK_PAYLOAD.lower(),)
    # The explicit "generate anyway" confirmation offered on the
    # SIMILAR_FOUND stop turn. Dispatched separately so the handler knows
    # the user has already seen the similar-runbook candidate and chosen —
    # the similar-match stop is waived, nothing else is.
    _RUNBOOK_CONFIRM_PATTERNS = (GENERATE_RUNBOOK_ANYWAY_PAYLOAD.lower(),)

    def __init__(self, *, deps, generator, runbooks) -> None:
        self.deps = deps
        self.generator = generator
        self.runbooks = runbooks

    async def auto_generate_report(self, case: "Case") -> tuple[str | None, bool]:
        """Synchronous auto-generation of terminal summary.

        RESOLVED cases always generate (a confirmed solution is meaningful
        content by definition). CLOSED cases generate only when the
        substance gate passes — gated by
        ``should_generate_terminal_summary``.

        Returns:
            A tuple ``(payload, generation_failed)``:

            - ``(rendered_markdown, False)`` on success — embed inline.
            - ``(failure_note, True)`` on LLM exception — embed inline AND
              offer the regen affordance on the ack-turn (G2).
            - ``(skip_note, False)`` when the substance gate skipped
              generation (CLOSED-only path).
            - ``(None, False)`` when no report service is configured.

        Callers embed ``payload`` in the closure-turn agent reply and use
        ``generation_failed`` to decide whether to offer the regen
        affordance on the ack-turn. Exceptions are caught and reported as
        a return value rather than propagated — the closure state
        transition has already committed and must not be undone by a
        synthesis-LLM hiccup.
        """
        from faultmaven.core.investigation.terminal_transitions import (
            should_generate_terminal_summary,
            terminal_summary_skip_reason,
        )

        if case.state == CaseState.CLOSED and not should_generate_terminal_summary(
            case
        ):
            skip = terminal_summary_skip_reason(case)
            logger.info(f"Auto-summary skipped for case {case.case_id}: {skip}")
            return skip, False

        if not self.deps.report_service:
            logger.debug("No report service available — skipping auto-summary")
            return None, False

        from faultmaven.modules.case.domain.owned_models.report import ReportType

        if case.state == CaseState.RESOLVED:
            report_type = ReportType.RESOLUTION_SUMMARY
            report_label = "Resolution summary"
        elif case.state == CaseState.CLOSED:
            report_type = ReportType.CLOSURE_SUMMARY
            report_label = "Closure summary"
        else:
            logger.warning(
                f"Unexpected state {case.state} for auto-summary on case {case.case_id}"
            )
            return None, False

        try:
            # generate_reports returns ReportGenerationResponse; its
            # .reports field is the list of newly-persisted CaseReports.
            response = await self.deps.report_service.generate_reports(
                case, [report_type]
            )
            logger.info(
                f"Auto-generated {report_type.value} for case {case.case_id}",
                extra={"case_id": case.case_id, "report_type": report_type.value},
            )
            # Pull the rendered markdown content from the freshly-generated
            # report so it can be embedded in the closure-turn reply.
            if response.reports:
                content = response.reports[0].content
                if content:
                    return content, False
            return None, False
        except Exception as e:
            logger.warning(
                f"Auto-summary generation failed for case {case.case_id}: {e}",
                extra={"case_id": case.case_id},
            )
            return (
                f"{report_label} generation did not complete. "
                f"You can retry from the **Regenerate** option.",
                True,
            )

    async def process_terminal_turn(
        self,
        case: "Case",
        user_message: str,
        metadata: dict[str, Any],
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Handle turns on terminal cases: Q&A, report regeneration, runbook creation.

        Terminal cases are immutable — no evidence, milestones, or state changes.
        Three scenarios:
          1. User requests report regeneration → regenerate summary.
          2. User accepts runbook suggestion → evaluate, create draft.
             Eligible: RESOLVED cases only — runbooks codify complete
             troubleshooting scenarios (root cause + verified solution).
          3. User asks questions about the case → answer via TERMINAL_TEMPLATE.
        """
        msg_lower = user_message.lower().strip().rstrip(".!? ")

        # Scenario 1: Report regeneration. Strict exact-match against the
        # DECIDE suggestion payloads — free-typed paraphrases fall
        # through to Q&A so typing can never produce a persisted Report
        # side effect.
        if msg_lower in self._REPORT_REGEN_PATTERNS:
            return await self._handle_report_regeneration(case, metadata)

        # Scenario 2: Runbook creation. Strict exact-match (same policy
        # as regen): only the DECIDE suggestion's precomposed
        # payload triggers persisted runbook generation; paraphrases
        # fall through to Q&A. RESOLVED-only — runbooks codify a
        # confirmed root-cause-to-solution chain.
        is_runbook_eligible = case.state == CaseState.RESOLVED
        if is_runbook_eligible and msg_lower in self._RUNBOOK_CREATION_PATTERNS:
            return await self.runbooks.handle_runbook_creation(case, metadata)
        if is_runbook_eligible and msg_lower in self._RUNBOOK_CONFIRM_PATTERNS:
            return await self.runbooks.handle_runbook_creation(
                case, metadata, dedup_confirmed=True
            )

        # Scenario 3: Q&A
        return await self._process_terminal_qa(
            case, user_message, metadata, user_id=user_id
        )

    async def _handle_report_regeneration(
        self,
        case: "Case",
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Regenerate the terminal summary report for a terminal case.

        For CLOSED cases, the same substance gate applied at closure time
        applies here — strict gating, no end-run around
        ``should_generate_terminal_summary``. RESOLVED cases regenerate
        unconditionally (a confirmed solution is always summarizable).

        The freshly-generated content is rendered inline in chat (same
        principle as the closure-ack turn), since summary writing is an
        interactive operation in this codebase.
        """
        from faultmaven.core.investigation.terminal_transitions import (
            should_generate_terminal_summary,
            terminal_summary_skip_reason,
        )
        from faultmaven.modules.case.domain.owned_models.report import ReportType

        if case.state == CaseState.RESOLVED:
            report_type = ReportType.RESOLUTION_SUMMARY
            report_label = "Resolution Summary"
        else:
            report_type = ReportType.CLOSURE_SUMMARY
            report_label = "Closure Summary"

        # Strict gating for CLOSED: the verdict at regen time must agree
        # with the verdict at closure time. Substance signals are frozen
        # in CLOSED state, so this is a stable check.
        if case.state == CaseState.CLOSED and not should_generate_terminal_summary(
            case
        ):
            skip = terminal_summary_skip_reason(case) or (
                "No closure summary can be generated for this case."
            )
            return {
                "agent_response": skip,
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        if not self.deps.report_service:
            return {
                "agent_response": (
                    "Report generation is not available at the moment. "
                    "Please try again later."
                ),
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": metadata,
            }

        try:
            # generate_reports returns ReportGenerationResponse; its
            # .reports field is the list of newly-persisted CaseReports.
            response = await self.deps.report_service.generate_reports(
                case, [report_type]
            )
            content = response.reports[0].content if response.reports else None
            agent_response = (
                content
                if content
                else f"The {report_label} has been regenerated. "
                f"You can view it in the Dashboard."
            )
            logger.info(
                f"Regenerated {report_type.value} for terminal case {case.case_id}",
                extra={"case_id": case.case_id, "report_type": report_type.value},
            )
        except Exception as e:
            logger.warning(
                f"Report regeneration failed for case {case.case_id}: {e}",
                extra={"case_id": case.case_id},
            )
            agent_response = (
                f"Failed to regenerate the {report_label}. Please try again."
            )

        # Re-offer the regen affordance — the user may want to iterate.
        # The "remaining" count comes from the DB and reflects the row
        # just written, so it correctly decrements turn-over-turn.
        remaining = await _remaining_regens_for(
            self.deps.report_service, self.deps.repository, case
        )
        if case.state == CaseState.RESOLVED:
            runbook_exists = await self.runbooks.case_has_runbook_draft(case)
            follow_ups = _resolved_suggestions(case, remaining, runbook_exists)
        else:
            follow_ups = _closed_suggestions(case, remaining)

        return {
            "agent_response": agent_response,
            "suggested_follow_ups": follow_ups,
            "case_updated": case,
            "metadata": metadata,
        }

    async def _process_terminal_qa(
        self,
        case: "Case",
        user_message: str,
        metadata: dict[str, Any],
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Process a Q&A turn on a terminal case via the LLM.

        Uses TERMINAL_TEMPLATE and TerminalResponse schema. No state mutations.

        ``user_id`` is the authenticated principal for the turn; it keys the KB
        read allowlist the ``kb_qa`` tool builds (owner + team arms). A terminal
        case still answers questions, so its Q&A turn must read the same KB the
        user's non-terminal turns do.
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.infrastructure.security.case_redaction import (
            CaseRedactionContext,
        )

        redaction_settings = get_settings()
        redaction_ctx = CaseRedactionContext(
            case_id=case.case_id,
            sanitizer=self.deps.sanitizer,
            redis_client=self.deps.redis_client,
            enabled=_should_redact(self.deps.sanitizer),
            ttl_hours=redaction_settings.protection.redaction_registry_ttl_hours,
        )
        await redaction_ctx.load()

        # Pass provider/model so the whole-prompt accountant (GAP-1/2/3) can
        # size the budget and engage the overflow backstop on the terminal-QA
        # path too (previously this call supplied neither, so it fell back to
        # the static char cap and was never measured against the model window).
        provider_name = getattr(self.deps.llm_provider, "provider_name", None)
        model_name = (
            getattr(self.deps.llm_provider.config, "default_model", None)
            if hasattr(self.deps.llm_provider, "config")
            else None
        )
        prompt = get_prompt_for_case(
            case,
            user_message,
            provider_name=provider_name,
            model_name=model_name,
        )

        def _build_tool_loop_base(
            *,
            target_tokens: int,
            provider_name: Optional[str],
            model_name: Optional[str],
        ) -> str:
            # #614: re-assembled for the model the tool loop sends to, when the
            # chat-sized prompt does not fit there.
            return get_prompt_for_case(
                case,
                user_message,
                provider_name=provider_name,
                model_name=model_name,
                target_tokens=target_tokens,
            )

        # Pass tools with auto tool_choice — LLM decides whether to invoke
        # kb_qa, web_search, etc. based on the user's question.
        tools_kwargs: dict[str, Any] = {}
        if self.deps.investigation_tools:
            tools_kwargs["investigation_tools"] = self.generator.build_da_tool_schemas()
            tools_kwargs["tool_context"] = await self.generator.build_tool_context(
                case, user_id=user_id
            )
            tools_kwargs["force_tool_use"] = False
            tools_kwargs["base_prompt_builder"] = _build_tool_loop_base

        response_obj = await self.generator.generate_structured_output(
            prompt,
            TerminalResponse,
            **tools_kwargs,
            redaction_ctx=redaction_ctx,
            case=case,
            user_message=user_message,
        )

        await redaction_ctx.save()

        # Extract follow-up suggestions
        follow_ups: list[dict[str, Any]] = []
        if (
            hasattr(response_obj, "suggested_follow_ups")
            and response_obj.suggested_follow_ups
        ):
            follow_ups = _flatten_follow_ups(
                response_obj.suggested_follow_ups, metadata
            )

        # Attach terminal-Q&A suggestions deterministically. The
        # TERMINAL_TEMPLATE instructs the LLM to leave its own
        # suggested_follow_ups empty; the engine owns these so the rules
        # don't drift turn-to-turn:
        #   - CLOSED: regen-closure-summary card iff the substance gate
        #     PASSes (also the chat-side retry path when initial
        #     generation failed).
        #   - RESOLVED: regen-resolution-summary + runbook cards. Regen
        #     mirrors CLOSED's offering; runbook is the forward action
        #     RESOLVED enables.
        if case.state == CaseState.CLOSED:
            remaining = await _remaining_regens_for(
                self.deps.report_service, self.deps.repository, case
            )
            follow_ups = follow_ups + _closed_suggestions(case, remaining)
        elif case.state == CaseState.RESOLVED:
            remaining = await _remaining_regens_for(
                self.deps.report_service, self.deps.repository, case
            )
            runbook_exists = await self.runbooks.case_has_runbook_draft(case)
            follow_ups = follow_ups + _resolved_suggestions(
                case, remaining, runbook_already_exists=runbook_exists
            )

        # #1451: this path returns before Step 6, so the service's turn-record
        # backfill reads the flag off this metadata to mark the TurnProgress,
        # and persists it onto the assistant row.
        if is_agent_response_synthesized(response_obj):
            metadata[MESSAGE_METADATA_AGENT_SYNTHESIZED] = True

        return {
            "agent_response": response_obj.agent_response,
            "suggested_follow_ups": follow_ups,
            "case_updated": case,
            "metadata": metadata,
        }
