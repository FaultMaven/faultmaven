import logging
from bisect import bisect_right
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .causal import CausalEdge, CausalNode
from .conclusion import RootCauseConclusion, WorkingConclusion
from .documentation import DocumentationData, EscalationState, JournalEntry
from .evidence import Evidence, UploadedFile
from .evidence_needs import EvidenceNeed
from .hypothesis import Hypothesis, HypothesisState
from .lifecycle import (
    VALID_CLOSURE_REASONS,
    CaseAction,
    CaseState,
    InvestigationStrategy,
)
from .problem import InquiryData, InvestigationStage, ProblemVerification
from .progress import InvestigationProgress
from .solution import ActionAttempt, ProposedAction, Solution
from .turn import InvestigationMomentum, TurnOutcome, TurnProgress

logger = logging.getLogger(__name__)

# Cap synthetic SKIPPED inserts per turn_history gap; a larger gap signals
# corruption, not a normal one-turn interruption, so we renumber instead.
_MAX_TURN_BACKFILL = 100


# ============================================================
# Derivation rules, stated once
# ============================================================
#
# Pure functions over primitives. The ``Case`` properties below delegate to
# them, and so does the cross-enterprise operator list (``CaseMetadata``), which
# reads a case's primitive inputs from the database without loading the case.
# Keeping one copy of each rule is what lets that list and a loaded case agree
# on every derived field.


def stage_while_investigating(
    state: CaseState, stage: InvestigationStage
) -> Optional[InvestigationStage]:
    """The stage a case in ``state`` displays: ``stage`` while INVESTIGATING,
    ``None`` in every other state."""
    return stage if state == CaseState.INVESTIGATING else None


def distinct_turns(turn_numbers: Iterable[int]) -> List[int]:
    """``turn_numbers`` de-duplicated and ascending — the shape
    :func:`investigation_turn_at` bisects (see :attr:`Case.out_of_band_turns`
    for why both halves matter)."""
    return sorted(set(turn_numbers))


def investigation_turn_at(
    turn_number: int, *, current_turn: int, asides: Sequence[int]
) -> int:
    """The investigation ordinal of message turn ``turn_number`` (#1387).

    The message clock at that row, bounded by ``current_turn``, minus the
    asides (sorted, distinct out-of-band turn numbers) at or before it. See
    :meth:`Case.investigation_turn_at` for why the clock bounds it.
    """
    effective = min(turn_number, current_turn)
    return max(0, effective - bisect_right(asides, effective))


def reconcile_turn_numbers(
    turn_numbers: Sequence[int], current_turn: int
) -> Tuple[List[Tuple[Optional[int], int]], int]:
    """The repair :meth:`Case.reconcile_turn_sequence` applies, on numbers alone.

    ``turn_numbers`` is a history's turn numbers in stored order. Returns
    ``(slots, current_turn)``: ``slots`` is the repaired history as
    ``(source, turn_number)`` pairs, where ``source`` is the index into
    ``turn_numbers`` of the entry that fills the slot, or ``None`` for a
    backfilled ``SKIPPED`` placeholder; ``current_turn`` is the clock after the
    repair. A healthy (consecutive) history comes back slot for slot, with the
    clock only ever raised to its last number.
    """
    if not turn_numbers:
        return [], current_turn

    slots: List[Tuple[Optional[int], int]] = [(0, turn_numbers[0])]
    for source in range(1, len(turn_numbers)):
        number = turn_numbers[source]
        prev = slots[-1][1]
        gap = number - prev - 1
        if number <= prev or gap > _MAX_TURN_BACKFILL:
            # duplicate / out-of-order, or a gap too large to be a normal
            # one-turn interruption → renumber.
            slots.append((source, prev + 1))
            continue
        slots.extend((None, missing) for missing in range(prev + 1, number))
        slots.append((source, number))

    last = slots[-1][1]
    # Keep the monotonic guarantee: never lower an in-flight current_turn.
    # The one exception is a cap-renumber that TRUNCATED the trailing number
    # downward (corruption recovery) — there the lowered last is authoritative
    # so the next turn doesn't re-open the huge gap.
    if last < turn_numbers[-1] or current_turn < last:
        current_turn = last
    return slots, current_turn


# ============================================================
# Core Case Model (Section 1)
# ============================================================


class Case(BaseModel):
    """
    Root case entity.
    Represents one complete troubleshooting investigation.
    """

    # ============================================================
    # Core Identity
    # ============================================================
    case_id: str = Field(
        default_factory=lambda: f"case_{uuid4().hex[:12]}",
        description="Unique case identifier",
        min_length=17,
        max_length=17,
        pattern=r"^case_[a-f0-9]{12}$",
    )

    user_id: Optional[str] = Field(
        default=None,
        description=(
            "User who created the case. NULL after the originating user is "
            "deleted (FK SET NULL). Required to be non-empty at creation time; "
            "creation logic enforces this separately from Pydantic validation."
        ),
        max_length=36,
    )

    enterprise_id: str = Field(
        description=(
            "Enterprise this case is isolated to (ADR-017 D1). The RLS key: "
            "nothing outside this enterprise can ever read the case."
        ),
        min_length=1,
        max_length=36,
    )

    organization_id: Optional[str] = Field(
        default=None,
        description=(
            "Organization the case is billed to (ADR-017 D2) — nullable "
            "attribution stamped from the actor's own organization at write "
            "time. It decides nothing about visibility, and ``None`` is the "
            "ordinary answer for an account nobody pays for."
        ),
        max_length=36,
    )

    source: Literal["copilot", "slack", "api"] = Field(
        default="copilot",
        description="Case origin (ADR-012), stamped at creation from the creator's account_kind",
    )

    title: str = Field(
        description="Short case title for list views and headers (e.g., 'API Performance Issue')",
        min_length=1,
        max_length=200,
    )

    description: str = Field(
        default="",
        description="""
        Confirmed problem description - canonical, user-facing, displayed prominently in UI.

        Lifecycle:
        1. Empty initially during INQUIRY (while agent formalizes problem)
        2. Set when user confirms proposed_problem_statement and decides to investigate
        3. Immutable after status becomes INVESTIGATING (provides stable reference)
        4. Used for UI display, search, and documentation

        Example: "API experiencing slowness with 30% of requests taking >5s response time
                  across all US regions, started 2 hours ago coinciding with v2.1.3 deployment"
        """,
        max_length=2000,
    )

    # ============================================================
    # Status (PRIMARY - User-Facing Lifecycle)
    # Phase (INQUIRY, INVESTIGATING) or Disposition (RESOLVED, CLOSED)
    # ============================================================
    state: CaseState = Field(
        default=CaseState.INQUIRY,
        description="Current lifecycle state (phase or disposition)",
    )

    action_history: List[CaseAction] = Field(
        default_factory=list,
        description="Complete history of case actions (phase transitions and dispositions)",
    )

    closure_reason: Optional[str] = Field(
        default=None,
        description=(
            "Sub-categorization of a CLOSED case, derived by the engine; None for "
            "non-terminal and RESOLVED cases. One of: "
            + " | ".join(sorted(VALID_CLOSURE_REASONS))
        ),
        max_length=100,
    )

    disposition_eligibility: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Per-disposition eligibility view for the case-action dropdown. "
            "Denormalized read view maintained at the single chokepoint "
            "``CaseRepository.save()`` via ``derive_disposition_eligibility``. "
            "Shape: ``{'resolved': str, 'closed': str}`` where each value is "
            "one of ``ready`` / ``needs_info`` / ``not_eligible``. "
            "Frontend uses this to gate Resolve/Close affordances on the "
            "current case content, not just the structural action graph. "
            "Always populated for persisted cases; may be None on in-memory "
            "Case objects before the first save."
        ),
    )

    pending_transition: Optional[Dict[str, Any]] = Field(
        default=None,
        description="""
        Pending status transition awaiting user confirmation (User-Agent Handshake pattern).

        Used for terminal transitions that require explicit user confirmation:
        - to_state: Target status (str)
        - reason: Why transition is being proposed (str)
        - summary: Agent's explanation to user (str)
        - evidence_ids: Supporting evidence (List[str])
        - proposed_at: When transition was proposed (str ISO datetime)
        - proposed_by: Who proposed it ("agent" | "user" | user_id)
        - closure_reason: Derived closure categorization for CLOSED
          proposals (str, set by propose_transition)
        - needs_info: RESOLVED proposal parked while the readiness ask is
          outstanding (bool; resolution NEEDS_INFO flow)

        Cleared after transition executes or is cancelled. propose_transition
        rebuilds the dict from scratch, so the per-proposal flag
        (needs_info) resets on every new proposal.
        """,
    )

    last_suggestions: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "DECIDE suggestions with intent metadata from the last agent turn. "
            "Used by the intent resolver to match typed responses against offered choices. "
            "Updated after each turn; only suggestions carrying intent metadata are stored."
        ),
    )

    kb_context: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "Deterministic KB pre-fetch results injected at key transitions. "
            "Populated at INQUIRY→INVESTIGATING (symptom search) and when "
            "root_cause_identified completes (remediation search). Included "
            "in the LLM context as historical suggestions, not absolute truths."
        ),
    )

    # ============================================================
    # Investigation Progress (SECONDARY - Internal Detail)
    # ============================================================
    progress: InvestigationProgress = Field(
        default_factory=InvestigationProgress,
        description="Milestone-based progress tracking",
    )

    # ============================================================
    # Turn Tracking
    # ============================================================
    current_turn: int = Field(
        default=0,
        ge=0,
        description="Current turn number (increments with each user-agent exchange)",
    )

    turns_without_progress: int = Field(
        default=0,
        ge=0,
        description="Consecutive turns with no milestone advancement (for stuck detection)",
    )

    turn_history: List[TurnProgress] = Field(
        default_factory=list, description="Complete history of all turns"
    )

    # ============================================================
    # Conversation Messages (RESTORED)
    # ============================================================
    messages: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="""
        Complete conversation history (user queries + agent responses).

        Per case-storage-design.md Section 4.7, each message contains:
        - message_id: str - Unique identifier
        - turn_number: int - Which turn this message belongs to
        - role: str - "user" | "assistant" | "system"
        - content: str - The actual message text
        - created_at: datetime - When message was created (ISO format)
        - token_count: Optional[int] - Number of tokens in content
        - metadata: dict - Additional data (sources, tools used, etc.)
        - author_id: Optional[str] - The user who wrote it; None on server rows

        A row is appended ONLY by ``append_message_row`` (case contracts), which
        decides per row kind what blank content means — a blank row aborts the
        whole aggregate save (#1452). The case id is not carried on the row:
        the repository binds it from the case that holds it.

        NOTE: Does NOT contain session_id (per case-and-session-concepts.md)
        Sessions provide authentication only, not message ownership.

        Relationship to turn_history:
        - messages[i].turn_number references turn_history[j].turn_number
        - Provides the "what was said" to complement turn_history's "what happened"
        """,
    )

    message_count: int = Field(
        default=0, ge=0, description="Total number of messages (user + agent combined)"
    )

    # ============================================================
    # Investigation Strategy
    # ============================================================
    investigation_strategy: InvestigationStrategy = Field(
        default=InvestigationStrategy.POST_MORTEM,
        description="Investigation approach: ACTIVE_INCIDENT (speed) vs POST_MORTEM (thoroughness)",
    )

    # ============================================================
    # Problem Context
    # ============================================================
    inquiry: InquiryData = Field(
        default_factory=InquiryData,
        description="Pre-investigation INQUIRY state data",
    )

    problem_verification: Optional[ProblemVerification] = Field(
        default=None,
        description="Consolidated verification data (symptom, scope, timeline, changes)",
    )

    # ============================================================
    # Investigation Data
    # ============================================================
    uploaded_files: List["UploadedFile"] = Field(
        default_factory=list,
        description="""
        All files uploaded to this case (raw file metadata).

        Files can be uploaded at ANY phase (INQUIRY or INVESTIGATING).
        Evidence is DERIVED from uploaded files after analysis during INVESTIGATING phase.

        Difference from evidence:
        - uploaded_files: Raw file metadata (file_id, filename, size, upload time)
        - evidence: Investigation data linked to hypotheses (only in INVESTIGATING phase)
        """,
    )

    evidence: List[Evidence] = Field(
        default_factory=list, description="All evidence collected during investigation"
    )

    evidence_needs: List[EvidenceNeed] = Field(
        default_factory=list,
        description=(
            "Demand-side pool: verification requirements the investigation "
            "has identified. Created by the LLM at problem-statement "
            "confirmation (symptom needs) and at hypothesis creation "
            "(causal needs). See evidence-needs-design.md."
        ),
    )

    hypotheses: Dict[str, Hypothesis] = Field(
        default_factory=dict, description="Generated hypotheses (key = hypothesis_id)"
    )

    solutions: List[Solution] = Field(
        default_factory=list, description="Proposed and applied solutions"
    )

    # ============================================================
    # Causal Graph (Two-Dimensional Hypothesis Methodology §3, §9.1)
    # ------------------------------------------------------------
    # The case owns ONE causal DAG rooted at the active problem D. Nodes are the
    # problem / intermediate states / candidate roots; edges are cause->effect
    # (with and_group for AND-sets). A Hypothesis is a named root->D path over
    # this graph (Hypothesis.path / root_node_id). Phase 1 adds the collections
    # additively; the engine seeds the PROBLEM node and grows the graph by lazy
    # backward expansion in Phase 2/3.
    # ============================================================
    causal_nodes: Dict[str, CausalNode] = Field(
        default_factory=dict,
        description="Causal-graph nodes, keyed by node_id (D + intermediate + roots)",
    )

    causal_edges: List[CausalEdge] = Field(
        default_factory=list,
        description="Directed cause->effect edges (and_group marks AND-sets)",
    )

    proposed_actions: List[ProposedAction] = Field(
        default_factory=list,
        description="Actions proposed by agent for user to execute (evidence-driven framework)",
    )

    action_attempts: List[ActionAttempt] = Field(
        default_factory=list,
        description="User attempts to execute proposed actions (compliance tracking)",
    )

    # ============================================================
    # Cross-Cutting State
    # ============================================================
    working_conclusion: Optional[WorkingConclusion] = Field(
        default=None,
        description="Agent current best understanding (updated iteratively)",
    )

    root_cause_conclusion: Optional[RootCauseConclusion] = Field(
        default=None, description="Final root cause determination"
    )

    investigation_journal: List[JournalEntry] = Field(
        default_factory=list,
        description="Structured log of key findings, decisions, and context. "
        "Append-only. Always included in full in LLM context.",
    )

    # ============================================================
    # Special States
    # ============================================================
    escalation_state: Optional[EscalationState] = Field(
        default=None, description="Escalated to human expert"
    )

    # ============================================================
    # Documentation
    # ============================================================
    documentation: DocumentationData = Field(
        default_factory=DocumentationData,
        description="Generated documentation and lessons learned",
    )

    # ============================================================
    # Timestamps
    # ============================================================
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When case was created",
    )

    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last modification timestamp",
    )

    last_activity_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Most recent user/agent interaction (for 'updated Xm ago' display)",
    )

    version: int = Field(
        default=1,
        ge=1,
        description=(
            "Optimistic concurrency control token. Incremented on every "
            "successful aggregate save. Callers that read-modify-write a "
            "case must pass the loaded version back through save(case); "
            "`save` raises StaleCaseException on mismatch. Scoped "
            "single-row UPDATEs (update_evidence_vectorized, etc.) do "
            "NOT bump this field — they operate on child tables."
        ),
    )

    resolved_at: Optional[datetime] = Field(
        default=None, description="When case reached RESOLVED state"
    )

    closed_at: Optional[datetime] = Field(
        default=None,
        description="When case reached terminal state (RESOLVED or CLOSED)",
    )

    # ============================================================
    # Computed Properties
    # ============================================================
    def find_uploaded_file(self, file_id: Optional[str]) -> Optional["UploadedFile"]:
        """Resolve an UploadedFile by file_id from this case's aggregate.

        The canonical aggregate-traversal point for code that holds an
        Evidence row (which carries source_file_id) and needs the file's
        metadata (filename, content_type, content_hash, storage_ref,
        upload_source, etc.). Replaces the old denormalized
        evidence.original_filename / evidence.content_ref / evidence.data_type
        fields — those were copies of upload data; this method walks the
        FK instead.

        None-safe at the source_file_id end: chat-extracted evidence
        (source_type=USER_DESCRIPTION) has source_file_id IS NULL, so
        callers can pass it through without a guard:

            file_meta = case.find_uploaded_file(ev.source_file_id)
            filename = file_meta.filename if file_meta else None
            is_page_capture = file_meta is not None and file_meta.upload_source == "page_capture"

        Assumes the case aggregate is fully loaded (uploaded_files
        populated by the repository's get_case path). Partial-load
        paths must NOT call this — they would silently return None for
        every lookup.
        """
        if not file_id:
            return None
        return next(
            (f for f in self.uploaded_files if f.file_id == file_id),
            None,
        )

    @property
    def current_stage(self) -> Optional[InvestigationStage]:
        """
        Computed investigation stage (only when INVESTIGATING).
        Returns: DIAGNOSIS | MITIGATION | TREATMENT | None
        """
        return stage_while_investigating(self.state, self.progress.current_stage)

    @property
    def current_momentum(self) -> Optional[InvestigationMomentum]:
        """
        Get momentum from the most recent turn for real-time dashboard display.

        Returns the momentum value from the latest turn in turn_history,
        or None if no turns recorded yet.
        """
        if not self.turn_history:
            return None
        return self.turn_history[-1].momentum

    @property
    def is_terminal(self) -> bool:
        """
        Check if case is in terminal state.
        Terminal states: RESOLVED, CLOSED (no further transitions).
        """
        return self.state.is_terminal

    @property
    def time_to_resolution(self) -> Optional[timedelta]:
        """
        Time from case creation to terminal state.
        Returns None if case not yet closed.
        """
        if self.closed_at:
            return self.closed_at - self.created_at
        return None

    @property
    def evidence_count_by_category(self) -> Dict[str, int]:
        """Count evidence by category for analytics"""
        counts: Dict[str, int] = {}
        for ev in self.evidence:
            cat = ev.category.value
            counts[cat] = counts.get(cat, 0) + 1
        return counts

    @property
    def active_hypotheses(self) -> List[Hypothesis]:
        """Get hypotheses currently being tested"""
        return [
            h for h in self.hypotheses.values() if h.state == HypothesisState.ACTIVE
        ]

    @property
    def warnings(self) -> List[Dict[str, Any]]:
        """
        Get active warnings for UI display.

        Returns list of warning dictionaries with type, severity, message.
        Used by frontend to display alert banners.
        """
        warnings: List[Dict[str, Any]] = []

        # Info: Escalation active
        if self.escalation_state and self.escalation_state.is_active:
            warnings.append(
                {
                    "type": "escalation",
                    "severity": "info",
                    "message": f"Escalated to {self.escalation_state.escalated_to or 'expert'}",
                    "escalated_at": self.escalation_state.escalated_at.isoformat(),
                }
            )

        # Warning: Terminal state but no documentation
        if self.is_terminal and len(self.documentation.documents_generated) == 0:
            warnings.append(
                {
                    "type": "no_documentation",
                    "severity": "info",
                    "message": "Case closed but no documentation generated",
                    "action": "Generate post-mortem or runbook",
                }
            )

        return warnings

    # ============================================================
    # Validation
    # ============================================================
    @field_validator("title")
    @classmethod
    def title_not_empty(cls, v):
        """Ensure title is not just whitespace"""
        if not v or not v.strip():
            raise ValueError("Title cannot be empty")
        return v.strip()

    @field_validator("description")
    @classmethod
    def description_valid(cls, v):
        """Ensure description is meaningful if not empty"""
        if v and not v.strip():
            raise ValueError("Description cannot be only whitespace")
        return v.strip() if v else ""

    @model_validator(mode="after")
    def description_required_when_investigating(self):
        """Ensure description is set before transitioning to INVESTIGATING"""
        state = self.state
        description = self.description.strip()

        # INVESTIGATING requires confirmed problem description
        if state == CaseState.INVESTIGATING and not description:
            raise ValueError(
                "description must be set (from confirmed proposed_problem_statement) "
                "before transitioning to INVESTIGATING status"
            )

        return self

    @field_validator("closure_reason")
    @classmethod
    def valid_closure_reason(cls, v):
        """closure_reason is a sub-categorization of CLOSED state, all
        engine-derived. None for non-terminal and RESOLVED cases.
        See: VALID_CLOSURE_REASONS in lifecycle.py."""
        if v is not None and v not in VALID_CLOSURE_REASONS:
            raise ValueError(
                f"closure_reason must be one of: {sorted(VALID_CLOSURE_REASONS)}"
            )
        return v

    @field_validator("action_history")
    @classmethod
    def action_history_ordered(cls, v):
        """Ensure action history is chronologically ordered."""
        if len(v) > 1:
            for i in range(len(v) - 1):
                if v[i].triggered_at > v[i + 1].triggered_at:
                    raise ValueError("Action history must be chronologically ordered")
        return v

    @field_validator("turn_history")
    @classmethod
    def turn_history_sequential(cls, v):
        """Detect non-sequential turn numbers WITHOUT failing.

        A sequencing anomaly is *repairable derived state*, so it must never
        brick persistence (raising here is what permanently wedged a case after
        a single interrupted turn). We log it for visibility; the actual repair —
        backfilling ``SKIPPED`` placeholders so numbers stay consecutive and
        existing turn_numbers (referenced by hypotheses/messages) stay valid — is
        done by :meth:`Case.reconcile_turn_sequence` on load and before save.
        """
        if len(v) > 1:
            for i in range(len(v) - 1):
                if v[i].turn_number + 1 != v[i + 1].turn_number:
                    logger.warning(
                        "Non-sequential turn_history at index %d (%s -> %s); "
                        "will be reconciled.",
                        i,
                        v[i].turn_number,
                        v[i + 1].turn_number,
                    )
                    break
        return v

    @property
    def out_of_band_turns(self) -> List[int]:
        """The DISTINCT turn numbers recorded as asides (#1329), ascending.

        Sorted because :meth:`investigation_turn_at` bisects this, and the
        order is maintained by ``reconcile_turn_sequence`` on load and before
        save rather than by the validator (which only logs) — so an in-flight
        history can be out of order.

        De-duplicated for the same reason the sort exists. ``reconcile_turn_sequence``
        treats a duplicate turn number as the SAME anomaly as an out-of-order
        one, and the corpus behind #1264 is exactly that shape: seven dev cases
        carry two user messages on one ``(case_id, turn_number)``, one carries
        three. A duplicate means the clock did NOT advance for the second
        record, so counting it twice subtracts a turn the clock never counted
        and shifts every later row's ordinal down by one.
        """
        return distinct_turns(
            t.turn_number for t in self.turn_history if t.is_out_of_band
        )

    def investigation_turn_at(
        self, turn_number: int, *, asides: Optional[Sequence[int]] = None
    ) -> int:
        """Which turn OF THE INVESTIGATION the given message turn is (#1387).

        The ordinal a client prints beside one conversation row: the message
        clock at that row, minus the asides at or before it. An aside does not
        advance it, so an out-of-band turn carries the same ordinal as the
        investigation turn that preceded it — which is the whole point, and is
        what "``Turn 7`` stays ``Turn 7``" means.

        **Bounded by the clock**, so the ordinal can never exceed
        :attr:`investigation_turn_count` — the invariant the whole design rests
        on, held here by construction rather than by the two agreeing. It is
        reachable: ``create_case(initial_message=...)`` stamps that row
        ``turn_number: 1`` and leaves ``current_turn`` at 0, because no turn has
        been processed yet. Clamping reports 0 there, which is the honest answer
        — the investigation has had no turns — and the row becomes turn 1 when
        the first one runs, since ``process_turn`` numbers it 1 as well.

        ``asides`` lets a caller labelling MANY rows pass
        :attr:`out_of_band_turns` once instead of rebuilding it per row; the
        formula is the module-level :func:`investigation_turn_at`, so there is
        only one of it.
        """
        if asides is None:
            asides = self.out_of_band_turns
        return investigation_turn_at(
            turn_number, current_turn=self.current_turn, asides=asides
        )

    @property
    def investigation_turn_count(self) -> int:
        """How many consumed turns were part of the investigation (#1329).

        ``current_turn`` is the MESSAGE clock — every persisted exchange
        advances it, because ``case_messages``, ``turn_history``, telemetry
        and suggestion liveness are keyed on it. This is the count a user
        means by "turn 7": the clock minus the asides (out-of-band exchanges).
        Subtracted from ``current_turn`` rather than counted from
        ``turn_history`` on purpose: older cases carry turns that were consumed
        without a record (the #500/#1264 corpus — 8 of 283 dev cases), and
        ``reconcile_turn_sequence`` fills those with ``skipped`` placeholders.
        Every such turn ran the engine, so it IS investigation work; counting
        only recorded, non-skipped entries would silently undercount exactly
        those cases. Derived, not stored, so it cannot drift from the clock.

        The case-level COUNT and the per-row ORDINAL are the same quantity read
        at two points, so this is :meth:`investigation_turn_at` evaluated at the
        clock rather than a second implementation of it. That is what makes the
        number a client sees while typing (``TurnResponse.investigation_turn``,
        which is this) and the number it sees after a reload
        (``Message.investigation_turn`` on the newest row) provably the same —
        the disagreement #1387 exists to prevent.
        """
        return self.investigation_turn_at(self.current_turn)

    @property
    def effective_current_turn(self) -> int:
        """The committed turn number: the last recorded ``turn_history`` number,
        or ``current_turn`` when no turn has been recorded yet.

        Repositories persist THIS (not the raw ``current_turn``) so the stored
        counter can never run ahead of ``turn_history`` — the drift that wedged
        cases (#500). The in-memory ``current_turn`` (the in-flight turn number
        business logic reads) is left untouched.

        The two are equal on a successful turn **only because every consuming
        route records a turn**. That is not automatic: it is maintained by the
        milestone engine's two writers plus the
        ``investigation_service._backfill_consumed_turn`` backstop, which covers
        the routes that never reach them (greeting, file reclassification, the
        terminal short-circuit). Before that backstop existed this docstring
        asserted the equality as an invariant and it did not hold — the counter
        froze on those turns, and because ``process_turn`` re-derives
        ``next_turn`` from the persisted column on every request, the NEXT turn
        reused the number. Corpus evidence at the time: 7 cases with a
        ``(case_id, turn_number)`` pair carrying two user messages, and one
        resolved case with three user turns all stamped turn 9 (#1264).

        So: if you add a route that consumes a turn number, it must end up with
        a ``turn_history`` entry, or this property silently stops holding again.
        """
        return (
            self.turn_history[-1].turn_number
            if self.turn_history
            else self.current_turn
        )

    def reconcile_turn_sequence(self) -> int:
        """Make ``turn_history`` strictly consecutive and ``current_turn`` consistent.

        Returns the number of repairs applied (backfilled + renumbered turns);
        ``0`` means the history was already healthy. **Never raises** — a
        sequencing anomaly is repairable derived state, so it must never brick
        persistence.

        Repairs:

        * **gap** (``next > prev + 1``) → backfill ``SKIPPED`` placeholder turns
          for the missing numbers (timestamped from the preceding turn so the
          history stays time-ordered), preserving every existing ``turn_number``
          so references from hypotheses/messages stay valid.
        * **duplicate / out-of-order** (``next <= prev``) or a **gap larger than
          ``_MAX_TURN_BACKFILL``** (corruption, not a one-turn interruption) →
          renumber the later entry to ``prev + 1``.

        ``current_turn``: on the healthy fast path it is only ever *raised* to the
        last number (never lowered, so an in-flight turn number is preserved);
        when a repair rewrites the history, the now-authoritative last number
        wins (so a destructive renumber can't leave the counter stranded ahead).

        A no-op on healthy cases. Called on load and before save so a transient
        anomaly self-heals into a visible, contained ``SKIPPED`` turn instead of
        permanently wedging the case.

        Which slot each entry lands in, and the clock afterwards, is decided by
        :func:`reconcile_turn_numbers`; this method only builds the entries.
        """
        history = self.turn_history
        if not history:
            return 0

        # Fast path: already consecutive → no allocation. Only keep current_turn
        # from falling behind the last recorded turn (never lower it).
        if all(
            history[i].turn_number + 1 == history[i + 1].turn_number
            for i in range(len(history) - 1)
        ):
            last = history[-1].turn_number
            if self.current_turn < last:
                self.current_turn = last
            return 0

        slots, current_turn = reconcile_turn_numbers(
            [entry.turn_number for entry in history], self.current_turn
        )

        repairs = 0
        rebuilt: List[TurnProgress] = []
        for source, number in slots:
            if source is None:
                rebuilt.append(
                    TurnProgress(
                        turn_number=number,
                        timestamp=rebuilt[-1].timestamp,
                        outcome=TurnOutcome.SKIPPED,
                        progress_made=False,
                        user_message_summary="(turn not recorded)",
                        agent_response_summary=(
                            "(turn not recorded — recovered after an "
                            "interrupted turn)"
                        ),
                    )
                )
                repairs += 1
                continue
            entry = history[source]
            if entry.turn_number != number:
                # A renumber only ever LOWERS a number when the gap exceeded the
                # backfill cap (a duplicate / out-of-order entry is raised).
                if number < entry.turn_number:
                    logger.error(
                        "Turn-sequence gap of %d on case %s exceeds cap (%d); "
                        "renumbering instead of backfilling.",
                        entry.turn_number - number,
                        getattr(self, "case_id", "?"),
                        _MAX_TURN_BACKFILL,
                    )
                # TurnProgress is frozen.
                entry = entry.model_copy(update={"turn_number": number})
                repairs += 1
            rebuilt.append(entry)

        # A history that is not consecutive always needs at least one repair.
        self.turn_history = rebuilt
        if current_turn != self.current_turn:
            self.current_turn = current_turn
        logger.warning(
            "Reconciled turn_history for case %s: %d repair(s), %d turns.",
            getattr(self, "case_id", "?"),
            repairs,
            len(rebuilt),
        )
        return repairs

    @model_validator(mode="after")
    def validate_timestamp_ordering(self) -> "Case":
        """
        Enforce timestamp chronological ordering per DB spec.

        Spec Reference: DB Design Specification lines 183-188
        Constraint: cases_timestamp_order_check
        """
        # created_at <= updated_at
        if self.created_at > self.updated_at:
            raise ValueError(
                f"created_at ({self.created_at}) cannot be after updated_at ({self.updated_at})"
            )

        # created_at <= last_activity_at
        if self.created_at > self.last_activity_at:
            raise ValueError(
                f"created_at ({self.created_at}) cannot be after last_activity_at ({self.last_activity_at})"
            )

        # resolved_at must be after created_at (if set)
        if self.resolved_at and self.created_at > self.resolved_at:
            raise ValueError(
                f"created_at ({self.created_at}) cannot be after resolved_at ({self.resolved_at})"
            )

        # closed_at must be after created_at (if set)
        if self.closed_at and self.created_at > self.closed_at:
            raise ValueError(
                f"created_at ({self.created_at}) cannot be after closed_at ({self.closed_at})"
            )

        # resolved_at <= closed_at (if both set)
        if self.resolved_at and self.closed_at and self.resolved_at > self.closed_at:
            raise ValueError(
                f"resolved_at ({self.resolved_at}) cannot be after closed_at ({self.closed_at})"
            )

        return self

    @model_validator(mode="after")
    def validate_state_timestamp_consistency(self) -> "Case":
        """
        Enforce state-timestamp consistency per DB spec.

        Spec Reference: DB Design Specification lines 157-176
        """
        # Skip validation during atomic_update() to avoid Catch-22
        if getattr(self, "_in_atomic_update", False):
            return self

        # Allow atomic transitions by checking if multiple terminal fields are being set
        # This is a private flag used to verify transient states during atomic updates
        if hasattr(self, "_in_terminal_transition"):
            return self

        # RESOLVED requires resolved_at and closed_at
        if self.state == CaseState.RESOLVED:
            if not self.resolved_at:
                raise ValueError("RESOLVED state requires resolved_at timestamp")
            if not self.closed_at:
                raise ValueError("RESOLVED state requires closed_at timestamp")

        # Non-RESOLVED must not have resolved_at
        if self.state != CaseState.RESOLVED and self.resolved_at:
            raise ValueError(
                f"resolved_at can only be set when state is RESOLVED (current: {self.state})"
            )

        # RESOLVED or CLOSED requires closed_at
        if self.state in [CaseState.RESOLVED, CaseState.CLOSED] and not self.closed_at:
            raise ValueError(
                f"Terminal state {self.state} requires closed_at timestamp"
            )

        # Non-terminal must not have closed_at
        if self.state not in [CaseState.RESOLVED, CaseState.CLOSED] and self.closed_at:
            raise ValueError(
                f"closed_at can only be set when state is RESOLVED or CLOSED (current: {self.state})"
            )

        # closure_reason is a sub-categorization of CLOSED only.
        #   - state == CLOSED:    closure_reason MUST be set (engine-derived)
        #   - state == RESOLVED:  closure_reason MUST be None (resolution
        #                          itself is the reason; sub-categorization
        #                          would be redundant with the state)
        #   - non-terminal:        closure_reason MUST be None
        if self.state == CaseState.CLOSED and not self.closure_reason:
            raise ValueError("CLOSED state requires closure_reason")
        if self.state != CaseState.CLOSED and self.closure_reason:
            raise ValueError(
                f"closure_reason can only be set when state is CLOSED "
                f"(current: {self.state})"
            )

        return self

    def atomic_transition(self):
        """
        Context manager to allow atomic updates of interdependent fields.
        Useful for transitioning to terminal states where state/timestamps depend on each other.
        """

        class AtomicContext:
            def __init__(self, case):
                self.case = case

            def __enter__(self):
                self.case._in_terminal_transition = True
                return self.case

            def __exit__(self, exc_type, exc_val, exc_tb):
                if hasattr(self.case, "_in_terminal_transition"):
                    del self.case._in_terminal_transition
                # Only validate if no exception occurred during the block
                if exc_type is None:
                    self.case.validate_state_timestamp_consistency()

        return AtomicContext(self)

    @model_validator(mode="after")
    def validate_description_for_investigation_and_resolution(self) -> "Case":
        """
        Description = problem statement. Required for the two states that
        carry "we know what the problem is" semantics:

        * INVESTIGATING: entry gate — investigation without a stated problem
          is wandering. Also requires problem_statement_confirmed, the single
          Gate 1 condition. (It used to require ``decided_to_investigate``
          as well — a field that was always written alongside this one and
          never read to decide anything. #1611 made this the single condition;
          the field itself is gone.)
        * RESOLVED: the case must have a known problem to be meaningfully
          resolved (the resolution would otherwise have nothing to attach
          to). Per the legitimate transitions spec (RESOLVED only comes
          from INVESTIGATING), this should be naturally satisfied; the
          check is defense-in-depth against bypassed validators.

        INQUIRY: empty allowed (still being formulated).
        CLOSED: empty allowed when path is inquiry → closed early-abandon;
                non-empty when path went through INVESTIGATING. The state
                itself imposes no requirement — closure_reason captures
                the why-closed fact instead.

        Mirror of the DB CHECK cases_description_required_for_investigation.
        """
        if self.state in (CaseState.INVESTIGATING, CaseState.RESOLVED):
            if not self.description or self.description == "":
                raise ValueError(
                    f"{self.state.value} state requires non-empty description "
                    "(description carries the case's problem statement)"
                )

        if self.state == CaseState.INVESTIGATING:
            # Inquiry-phase readiness — separate from the description check
            # but enforced at the same gate.
            if not self.inquiry.problem_statement_confirmed:
                raise ValueError(
                    "INVESTIGATING state requires confirmed problem statement"
                )

        return self

    # ============================================================
    # Atomic State Update Helper
    # ============================================================
    def atomic_update(self, **updates: Any) -> None:
        """
        Perform atomic updates to multiple fields, bypassing incremental validation.

        This method is necessary for state transitions that require multiple fields
        to be updated simultaneously (e.g., setting state=RESOLVED requires
        resolved_at to be set, but resolved_at can only be set when state=RESOLVED).

        The validation Catch-22:
        - Cannot set state=RESOLVED if resolved_at is None (validator line 3361)
        - Cannot set resolved_at if status != RESOLVED (validator line 3367)

        Usage:
            case.atomic_update(
                state=CaseState.RESOLVED,
                resolved_at=datetime.now(UTC),
                closed_at=datetime.now(UTC),
            )

        Args:
            **updates: Field names and values to update atomically

        Raises:
            ValueError: If the post-update state violates a cross-field invariant
                (e.g., state=RESOLVED with no resolved_at). Pre-update state is
                restored before the exception propagates.

        Note:
            Sets _in_atomic_update flag to bypass per-field validators that would
            otherwise reject transient inconsistent states during multi-field
            updates. After all updates apply, this method re-runs the cross-field
            validators on the final state to catch callers that forgot a required
            field — without that revalidation step, atomic_update would silently
            accept malformed transitions (e.g., state=RESOLVED without timestamps).
        """
        snapshot = {f: getattr(self, f) for f in updates}

        object.__setattr__(self, "_in_atomic_update", True)
        try:
            for field_name, value in updates.items():
                object.__setattr__(self, field_name, value)
        finally:
            object.__setattr__(self, "_in_atomic_update", False)

        try:
            self.validate_state_timestamp_consistency()
            self.validate_timestamp_ordering()
            self.validate_description_for_investigation_and_resolution()
        except ValueError:
            object.__setattr__(self, "_in_atomic_update", True)
            try:
                for field_name, value in snapshot.items():
                    object.__setattr__(self, field_name, value)
            finally:
                object.__setattr__(self, "_in_atomic_update", False)
            raise

    # ============================================================
    # Configuration
    # ============================================================
    # `json_encoders` removed — deprecated in Pydantic V2, and both entries were
    # doing harm or nothing:
    #
    #   datetime  — for TZ-AWARE values it appended "Z" to a string
    #               `isoformat()` had already suffixed with "+00:00", emitting
    #               `2026-08-08T09:14:05+00:00Z`. That is not a valid timestamp;
    #               utils/datetime.py documents it as "CORRUPTED - legacy data
    #               only" and repairs it on read — this model was the producer.
    #               V2's default emits the correct `...T09:14:05Z` unaided, so
    #               for aware values this is a fix. The repair path stays, for
    #               rows written before it.
    #
    #               For NAIVE values the output does change: old `...05Z`, new
    #               `...05` with no designator. Naive timestamps are reachable —
    #               the SQLite mapper returns them unnormalised — but only when
    #               no COMPARED PAIR mixes awareness: `validate_timestamp_ordering`
    #               always compares created_at against updated_at and
    #               last_activity_at, and against resolved_at/closed_at when
    #               those are set, and a mixed pair raises TypeError before any
    #               serialisation happens (a pre-existing hazard, not one this
    #               introduced). In practice that means all-naive or all-aware,
    #               since they share a source. Either way it round-trips safely:
    #               `parse_utc_timestamp` reads a bare timestamp back as UTC,
    #               identically to the `Z` form — verified for all three shapes.
    #               No live API surface serialises the domain `Case`; the only
    #               consumer is the checkpoint hash, which is never compared
    #               against a stored value.
    #   timedelta — dead. Config does not propagate to nested models in V2, and
    #               no model reachable from `Case` carries a timedelta field.
    model_config = ConfigDict(
        validate_assignment=True,  # Validate on field assignment
        use_enum_values=False,  # Keep enum instances
    )
