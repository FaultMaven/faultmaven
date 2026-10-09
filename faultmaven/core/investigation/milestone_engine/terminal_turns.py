"""Handling a terminal (RESOLVED/CLOSED) turn: report regeneration and the terminal Q&A path. Runbook creation is a sibling collaborator (runbook_creation.py)."""

import logging
from enum import Enum
from typing import Any, Optional

from faultmaven.core.investigation.lifecycle_metrics import terminal_summary_total
from faultmaven.core.investigation.milestone_engine.regeneration import (
    _remaining_regens_for,
)
from faultmaven.core.investigation.milestone_engine.turn_commit import TurnCommitPlan
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


class TerminalCardAction(str, Enum):
    """Which terminal-case DECIDE card a message is the payload of."""

    REGENERATE_REPORT = "regenerate_report"
    CREATE_RUNBOOK = "create_runbook"
    CONFIRM_RUNBOOK = "confirm_runbook"


def terminal_card_action(
    user_message: str, case_state: CaseState
) -> Optional[TerminalCardAction]:
    """The terminal card ``user_message`` acts as on a case in ``case_state``, or None.

    The ONE recogniser for these cards. They carry no ``intent``, so a click
    arrives as its payload text, and exact match is the only thing that tells
    a click from typing (INV-12). ``TerminalTurnHandler`` dispatches on it, and
    the terminal-confirmation follow-up counter excludes what it recognises
    (#1748) — so the two can never disagree about what a card click is.

    The runbook cards act only on a RESOLVED case: runbooks codify a confirmed
    root-cause-to-solution chain. On any other state their text is typed text
    and goes to Q&A, so it is recognised as nothing here.
    """
    msg_lower = user_message.lower().strip().rstrip(".!? ")
    if msg_lower in _REPORT_REGEN_PATTERNS:
        return TerminalCardAction.REGENERATE_REPORT
    if case_state != CaseState.RESOLVED:
        return None
    if msg_lower in _RUNBOOK_CREATION_PATTERNS:
        return TerminalCardAction.CREATE_RUNBOOK
    if msg_lower in _RUNBOOK_CONFIRM_PATTERNS:
        return TerminalCardAction.CONFIRM_RUNBOOK
    return None


class TerminalTurnHandler:
    """Drives a terminal-state turn: regenerating the case report on request, and answering terminal Q&A without reopening the investigation."""

    def __init__(self, *, deps, generator, runbooks) -> None:
        self.deps = deps
        self.generator = generator
        self.runbooks = runbooks

    async def auto_generate_report(
        self, case: "Case", *, plan: TurnCommitPlan
    ) -> tuple[str | None, bool]:
        """Render the terminal summary and carry its row to the turn's commit.

        RESOLVED cases always generate (a confirmed solution is meaningful
        content by definition). CLOSED cases generate only when the
        substance gate passes — gated by
        ``should_generate_terminal_summary``.

        The row is rendered here (``render_reports``, deterministic: no LLM
        call and no write) and added to ``plan``, so it commits in the same
        transaction as the CLOSED/RESOLVED state it summarises, or not at all
        (#1882): a summary row can never exist for a case whose terminal state
        did not commit, and a terminal state never commits half-summarised.

        Returns:
            A tuple ``(payload, generation_failed)``:

            - ``(rendered_markdown, False)`` on success — embed inline.
            - ``(failure_note, True)`` when the render raised — embed inline
              AND offer the regen affordance on the ack-turn (G2).
            - ``(skip_note, False)`` when the substance gate skipped
              generation (CLOSED-only path).
            - ``(None, False)`` when no report service is configured.

        Callers embed ``payload`` in the closure-turn agent reply and use
        ``generation_failed`` to decide whether to offer the regen
        affordance on the ack-turn. A render failure is reported as a return
        value rather than propagated: the case still closes, with the failure
        note and the regenerate card (owner ruling on #1882, INV-13). A
        failure of the turn's COMMIT is different — nothing of the turn
        commits, the terminal state included.
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
            terminal_summary_total.labels(
                summary_type="closure_summary", outcome="skipped"
            ).inc()
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
            # Rendered, not written: the rows ride the turn's commit.
            # ``pending`` counts a same-type row this turn already holds
            # toward the cap and the version, as a committed row would.
            reports = await self.deps.report_service.render_reports(
                case, [report_type], pending=plan.pending_reports()
            )
            plan.add_reports(reports)
            logger.info(
                f"Auto-generated {report_type.value} for case {case.case_id}",
                extra={"case_id": case.case_id, "report_type": report_type.value},
            )
            # Pull the rendered markdown content from the freshly-rendered
            # report so it can be embedded in the closure-turn reply.
            if reports:
                content = reports[0].content
                if content:
                    terminal_summary_total.labels(
                        summary_type=report_type.value, outcome="generated"
                    ).inc()
                    return content, False
            terminal_summary_total.labels(
                summary_type=report_type.value, outcome="empty"
            ).inc()
            return None, False
        except Exception as e:
            logger.warning(
                f"Auto-summary generation failed for case {case.case_id}: {e}",
                extra={"case_id": case.case_id},
            )
            terminal_summary_total.labels(
                summary_type=report_type.value, outcome="failed"
            ).inc()
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
        *,
        plan: TurnCommitPlan,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Handle turns on terminal cases: Q&A, report regeneration, runbook creation.

        Writes nothing: a regenerated report row and the runbook conversion's
        gate go into ``plan``, which commits (or releases) with the turn.

        Terminal cases are immutable — no evidence, milestones, or state changes.
        Three scenarios:
          1. User requests report regeneration → regenerate summary.
          2. User accepts runbook suggestion → evaluate, create draft.
             Eligible: RESOLVED cases only — runbooks codify complete
             troubleshooting scenarios (root cause + verified solution).
          3. User asks questions about the case → answer via TERMINAL_TEMPLATE.
        """
        card = terminal_card_action(user_message, case.state)

        # Scenario 1: Report regeneration. Strict exact-match against the
        # DECIDE suggestion payloads — free-typed paraphrases fall
        # through to Q&A so typing can never produce a persisted Report
        # side effect.
        if card is TerminalCardAction.REGENERATE_REPORT:
            return await self._handle_report_regeneration(case, metadata, plan=plan)

        # Scenario 2: Runbook creation. Strict exact-match (same policy
        # as regen): only the DECIDE suggestion's precomposed
        # payload triggers persisted runbook generation; paraphrases
        # fall through to Q&A. RESOLVED-only, decided inside
        # ``terminal_card_action`` — runbooks codify a confirmed
        # root-cause-to-solution chain.
        if card is TerminalCardAction.CREATE_RUNBOOK:
            return await self.runbooks.handle_runbook_creation(
                case, metadata, plan=plan
            )
        if card is TerminalCardAction.CONFIRM_RUNBOOK:
            return await self.runbooks.handle_runbook_creation(
                case, metadata, plan=plan, dedup_confirmed=True
            )

        # Scenario 3: Q&A
        return await self._process_terminal_qa(
            case, user_message, metadata, plan=plan, user_id=user_id
        )

    async def _handle_report_regeneration(
        self,
        case: "Case",
        metadata: dict[str, Any],
        *,
        plan: TurnCommitPlan,
    ) -> dict[str, Any]:
        """Regenerate the terminal summary report for a terminal case.

        The new version is rendered and added to ``plan``, so it commits with
        the turn: a turn that fails consumes no regeneration slot (#1882).

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
            # Rendered, not written: the row rides the turn's commit.
            reports = await self.deps.report_service.render_reports(
                case, [report_type], pending=plan.pending_reports()
            )
            plan.add_reports(reports)
            content = reports[0].content if reports else None
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
        # The "remaining" count is the committed rows plus the one this turn
        # just rendered (``pending``), which commits with this reply, so it
        # correctly decrements turn-over-turn.
        remaining = await _remaining_regens_for(
            self.deps.report_service,
            self.deps.repository,
            case,
            pending=plan.pending_reports(),
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
        *,
        plan: TurnCommitPlan,
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
            should_redact,
        )

        redaction_settings = get_settings()
        redaction_ctx = CaseRedactionContext(
            case_id=case.case_id,
            sanitizer=self.deps.sanitizer,
            redis_client=self.deps.redis_client,
            enabled=should_redact(self.deps.sanitizer),
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
                self.deps.report_service,
                self.deps.repository,
                case,
                pending=plan.pending_reports(),
            )
            follow_ups = follow_ups + _closed_suggestions(case, remaining)
        elif case.state == CaseState.RESOLVED:
            remaining = await _remaining_regens_for(
                self.deps.report_service,
                self.deps.repository,
                case,
                pending=plan.pending_reports(),
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
