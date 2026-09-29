import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

# ============================================================
# Turn Tracking Models (Section 8)
# ============================================================


class TurnOutcome(str, Enum):
    """
    Turn outcome classification.

    NOTE: Outcomes are LLM-observable only (what happened this turn).
    Workflow control uses direct metrics (turns_without_progress).
    Outcomes are for analytics and prompt context, not control flow.

    Each member carries an LLM-facing ``description`` accessible at
    runtime via ``TurnOutcome.MEMBER.description``. The prompt block in
    ``SCHEMA_INSTRUCTIONS`` is auto-generated from these descriptions so
    there is no second source of truth to drift against — adding a value
    here automatically extends the prompt.

    Maintainer-only notes (implementation details that should NOT reach
    the LLM) live as ``#`` comments next to the value, not in the
    description string.
    """

    description: str  # Type hint for the runtime attribute set in __new__.

    def __new__(cls, value: str, description: str) -> "TurnOutcome":
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.description = description
        return obj

    MILESTONE_COMPLETED = (
        "milestone_completed",
        "one or more milestones flipped True this turn.",
    )
    DATA_PROVIDED = (
        "data_provided",
        "user shared data/evidence (uploaded, pasted).",
    )
    DATA_REQUESTED = (
        "data_requested",
        "you asked the user for data; awaiting response.",
    )
    # Maintainer note: system tracks the data_not_provided pattern — 3+
    # consecutive turns triggers degraded mode (see progress_monitor.py).
    # Not exposed to the LLM in the description below.
    DATA_NOT_PROVIDED = (
        "data_not_provided",
        "you previously requested data and the user did not address the request this turn.",
    )
    HYPOTHESIS_TESTED = (
        "hypothesis_tested",
        "a hypothesis was validated or refuted this turn.",
    )
    CASE_RESOLVED = (
        "case_resolved",
        "solution verified; case is resolvable.",
    )
    CONVERSATION = (
        "conversation",
        "normal Q&A, no data requests or milestone changes.",
    )
    OTHER = (
        "other",
        "does not fit any of the above.",
    )
    # Maintainer note: synthesized by Case.reconcile_turn_sequence to backfill a
    # turn whose record was lost (e.g. an interrupted save). Not LLM-emitted.
    OUT_OF_BAND = (
        "out_of_band",
        "the message was an aside — small talk, trivia, or about FaultMaven "
        "itself — answered briefly with no investigation work.",
    )
    SKIPPED = (
        "skipped",
        "turn not recorded — recovered after an interrupted turn.",
    )


class InvestigationMomentum(str, Enum):
    """
    Investigation momentum indicator for progress tracking.

    Used to signal overall investigation health and guide agent behavior.
    Calculated from recent progress patterns (evidence collection, hypothesis updates).
    """

    HIGH = "high"
    """
    Evidence flowing, hypotheses being tested, confidence increasing.
    Investigation progressing well.
    """

    MODERATE = "moderate"
    """
    Some progress being made, investigation moving forward.
    Default state when enough data to assess.
    """

    LOW = "low"
    """
    Little progress recently, confidence plateaued.
    May need different approach or more data.
    """

    BLOCKED = "blocked"
    """
    Critical evidence unavailable, investigation stalled.
    Likely to enter degraded mode if continues.
    """


#: Outcomes that are NOT investigative work. Shared by every "investigative
#: turns since the last milestone" counter (``progress_monitor``,
#: ``case_ui_adapter``) and by ``Case.investigation_turn_count`` so the three
#: cannot disagree about which turns count. ``out_of_band`` joined in #1329:
#: an aside answered outside the investigation is not diagnostic effort.
NON_INVESTIGATIVE_OUTCOMES = frozenset({"conversation", "other", "out_of_band"})

#: How a user confirmed the terminal transition a turn executed (#1748): a
#: clicked intent (a DECIDE card, or the dropdown pick repeated); a typed
#: consent opening with an explicit token, bare ("yes", "go ahead!") or carrying
#: more ("yes please close it"); a typed consent opening with a weak token, bare
#: ("ok", "lgtm 👍") or carrying more ("ok go ahead"); or typed text the intent
#: resolver accepted that is not a typed consent ("that works"). A typed
#: refusal confirms nothing (#1783), so it has no label. The ONE copy of the
#: label set: ``TurnProgress`` stores it and the terminal-confirmation counters
#: are labelled by it.
TerminalConfirmedVia = Literal[
    "intent",
    "explicit_token",
    "explicit_prefixed",
    "weak_token",
    "weak_prefixed",
    "typed_other",
]

#: Unknown channel values already warned about in this process (#1748): a stale
#: record is re-read on every load of its case, and one WARNING per distinct
#: value says everything a repeat would.
_WARNED_UNKNOWN_CHANNELS: set[str] = set()


class TurnProgress(BaseModel):
    """
    Record of what happened in one turn.
    Turn = one user message + one agent response.
    """

    turn_number: int = Field(ge=0, description="Sequential turn number")

    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When turn occurred",
    )

    # ============================================================
    # What Advanced This Turn
    # ============================================================
    milestones_completed: List[str] = Field(
        default_factory=list,
        description="Milestone names completed this turn (e.g., 'symptom_verified')",
    )

    evidence_added: List[str] = Field(
        default_factory=list, description="Evidence IDs added this turn"
    )

    hypotheses_generated: List[str] = Field(
        default_factory=list, description="Hypothesis IDs generated this turn"
    )

    hypotheses_validated: List[str] = Field(
        default_factory=list, description="Hypothesis IDs validated this turn"
    )

    solutions_proposed: List[str] = Field(
        default_factory=list, description="Solution IDs proposed this turn"
    )

    # ============================================================
    # Progress Assessment
    # ============================================================
    progress_made: bool = Field(description="Did investigation advance this turn?")

    # ============================================================
    # Outcome
    # ============================================================
    outcome: TurnOutcome = Field(description="Turn outcome classification")

    # ============================================================
    # User Interaction
    # ============================================================
    terminal_confirmed_via: Optional[TerminalConfirmedVia] = Field(
        default=None,
        description=(
            "How the user confirmed the terminal transition this turn executed "
            "(clicked intent; typed text opening with an explicit or a weak "
            "token, bare or with more text; or other typed text the resolver "
            "accepted). None on every turn that executed no terminal transition."
        ),
    )

    user_message_summary: Optional[str] = Field(
        default=None, description="Summary of user message", max_length=500
    )

    agent_response_summary: Optional[str] = Field(
        default=None, description="Summary of agent response", max_length=500
    )

    # The turn-history counterpart of the ``agent_response_synthesized`` row
    # flag (#1451). It has to live here too: the prompt's EARLIER TURNS and
    # <previous_turn> blocks render from this record, never from the row, and
    # the summary above holds whatever text the turn returned — so on a turn
    # the model did not answer it holds the server's placeholder. Storing None
    # as the summary instead is not an alternative: the renderer then falls
    # back to the outcome name, which says the turn was a conversation.
    agent_response_synthesized: bool = Field(
        default=False,
        description=(
            "True when agent_response_summary summarizes a placeholder the "
            "server wrote in place of an answer, not something the agent said"
        ),
    )

    # ============================================================
    # System Feedback (for iterative correction)
    # ============================================================
    system_feedback: Optional[str] = Field(
        default=None,
        description="Instruction or error from system to agent (e.g., 'Invalid evidence ID')",
        max_length=1000,
    )
    system_feedback_forwarded: bool = Field(
        default=False,
        description=(
            "True when this turn built no prompt and carried the previous "
            "record's system_feedback forward unread, so the notice belongs to "
            "an earlier turn (#1688)"
        ),
    )

    # ============================================================
    # Progress Metrics (populated by WorkingConclusionGenerator)
    # ============================================================
    momentum: Optional[InvestigationMomentum] = Field(
        default=None,
        description="Investigation momentum indicator for this turn",
    )

    blocked_reasons: List[str] = Field(
        default_factory=list,
        description="Reasons why investigation is blocked or progressing slowly",
    )

    next_steps: List[str] = Field(
        default_factory=list,
        description="Suggested next steps for the investigation",
    )

    # ============================================================
    # Observability Fields (for progress monitoring and validation tracking)
    # ============================================================
    repair_pattern: Optional[str] = Field(
        default=None,
        description="Agent state repair pattern detected this turn: "
        "hypothesis_anchoring, hypothesis_deadlock, exhausted, "
        "fix_failure_cycle, action_loop",
    )

    validation_repairs: List[str] = Field(
        default_factory=list,
        description=(
            "What the engine corrected this turn: the StateValidator's repairs "
            "(e.g., 'Fixed milestone ordering'), the apply step's rejections, "
            "and out-of-range confidence values rescaled, coerced, dropped or "
            "pruned (fm#1502)"
        ),
    )

    @field_validator("terminal_confirmed_via", mode="before")
    @classmethod
    def _unknown_channel_is_none(cls, v: Any) -> Any:
        """Read a channel outside the label set as None — this runs on LOAD.

        Both repositories rebuild every record with ``TurnProgress(**t)`` inside
        the case loader, so a ``Literal`` rejection here would not drop one
        label: it would make the whole CASE unloadable, over a telemetry field.
        A value the label set no longer names (a renamed or retired channel)
        is a record of a confirmation nobody can count any more; None says
        exactly that.

        Never silently: it runs on construction too, so a producer writing a
        value the set does not name would otherwise mute that channel with
        nothing to show for it. The WARNING carries the value, once per
        distinct value per process — a stale record is re-read on every load.
        """
        if v is None or v in get_args(TerminalConfirmedVia):
            return v
        if repr(v) not in _WARNED_UNKNOWN_CHANNELS:
            _WARNED_UNKNOWN_CHANNELS.add(repr(v))
            logger.warning(
                "terminal_confirmed_via %r is not a known channel; recorded as None",
                v,
            )
        return None

    # ============================================================
    # Computed Properties
    # ============================================================
    @property
    def advancement_count(self) -> int:
        """Total items advanced this turn"""
        return (
            len(self.milestones_completed)
            + len(self.evidence_added)
            + len(self.hypotheses_validated)
            + len(self.solutions_proposed)
        )

    @property
    def is_skipped(self) -> bool:
        """True for a synthetic recovery placeholder (a turn that was *not
        recorded*, backfilled by ``Case.reconcile_turn_sequence``).

        The single source of truth for SKIPPED screening — analyses that count or
        window ``turn_history`` must exclude these so a placeholder isn't mistaken
        for real diagnostic work.
        """
        return self.outcome is TurnOutcome.SKIPPED

    @property
    def is_out_of_band(self) -> bool:
        """True for an aside — small talk, trivia, a question about FaultMaven
        itself — answered outside the investigation (#1329).

        The counterpart to :attr:`is_skipped`, and for the same reason: the
        screen was written out by hand at four call sites (two in
        ``prompts/context_builder``, the investigation-turn count and the
        per-row ordinal), which is four places a refinement would have to land
        in step.
        """
        return self.outcome is TurnOutcome.OUT_OF_BAND

    # ============================================================
    # Configuration
    # ============================================================
    model_config = ConfigDict(
        frozen=True,  # Immutable once created
    )
