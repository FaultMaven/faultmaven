from datetime import UTC, datetime
from enum import Enum
from typing import Any, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

# =============================================================================
# Evidence Needs (Demand-Side Counterpart to Evidence Rows)
# =============================================================================
#
# Evidence needs are a flat pool on the case — a registry of *what data
# the investigation requires*. They are the demand-side counterpart to
# Evidence rows. Needs are NOT anchored to specific hypotheses; the
# hypothesis-evidence relationship is recorded through the existing
# ``hypothesis_evidence`` junction at evidence-collection time.
#
# Design reference:
# docs/architecture/investigation-engine/evidence-needs-design.md


class NeedPurpose(str, Enum):
    """Why this need was created — maps to evidence categories.

    A symptom_verification need produces SYMPTOM_EVIDENCE (presence)
    initially and SYMPTOM_ABSENCE_EVIDENCE (absence) on re-check after
    mitigation/solution. A causal_verification need produces
    CAUSAL_EVIDENCE (presence) initially and CAUSAL_ABSENCE_EVIDENCE
    (absence) on re-check after solution.

    The same need produces multiple evidence rows of different
    categories across the case's lifetime; the need's state stays
    FULFILLED once fulfilled — re-check evidence is appended via
    ``fulfilling_evidence_ids``, it does not reset the state.
    """

    SYMPTOM_VERIFICATION = "symptom_verification"
    CAUSAL_VERIFICATION = "causal_verification"


class NeedState(str, Enum):
    """Lifecycle states of an evidence need.

    PENDING        — Need identified, no evidence yet.
    PARTIALLY_MET  — Some evidence collected but insufficient.
    FULFILLED      — Sufficient evidence collected (terminal-positive).
    SUPERSEDED     — No longer relevant (terminal-negative). Either all
                     motivating hypotheses went terminal (engine rule) or
                     LLM judged irrelevant (LLM update emission).

    FULFILLED and SUPERSEDED are terminal — a need cannot resurrect from
    SUPERSEDED. If the LLM later concludes the underlying data is
    relevant again, it must create a new need.
    """

    PENDING = "pending"
    PARTIALLY_MET = "partially_met"
    FULFILLED = "fulfilled"
    SUPERSEDED = "superseded"


class NeedPriority(str, Enum):
    """Priority hint for surfacing needs as EVIDENCE-type suggestions.

    High-priority unfulfilled needs are surfaced first; medium and low
    are deferred until higher priorities are addressed. Priority is an
    LLM hint, not a hard ordering.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class NeedObtainability(str, Enum):
    """Whether the discriminating data a causal_verification need requests can
    be gathered at all — the one judgment the engine cannot compute, so it is
    model-declared (verification-status handling, insufficient-evidence §5.3).

    UNKNOWN      — default; the model has not declared. Fail-safe: treated as
                   still-obtainable (keep-engaging), never contributes to a wall.
    OBTAINABLE   — the data can be gathered; the need stays a live ask.
    UNOBTAINABLE — the data cannot be gathered (never collected, rotated away,
                   too costly, no access). A causal_verification need declared
                   UNOBTAINABLE is a *declared discriminator wall* for its
                   candidate; it yields its surface slot and stops rotating, and
                   is retained in the insufficient-evidence record as the
                   specific unmet need.

    Monotonic in effect: only UNOBTAINABLE moves the reading toward
    INSUFFICIENT_EVIDENCE, never back toward safety.
    """

    UNKNOWN = "unknown"
    OBTAINABLE = "obtainable"
    UNOBTAINABLE = "unobtainable"


class EvidenceNeed(BaseModel):
    """A verification requirement on a case.

    Each need represents "data that would advance the investigation."
    Needs live in a flat pool on the case — not anchored to specific
    hypotheses. ``motivating_hypothesis_ids`` records *why* the need
    exists (which hypotheses motivated creating it) for context and
    for the engine's retirement-supersession rule, but is not a hard
    ownership association.

    Lifecycle (see evidence-needs-design.md §7):

    - Created by the LLM via ``EvidenceNeedUpdate`` emissions at
      problem-statement confirmation (symptom needs) and at hypothesis
      creation (causal needs).
    - Updated by the LLM as evidence arrives (state, fulfilling
      evidence linkage, motivating hypothesis IDs).
    - Auto-superseded by the engine when a motivating hypothesis goes
      TERMINAL (REFUTED or RETIRED) and the motivating list becomes
      empty AND purpose is CAUSAL_VERIFICATION AND state is not
      FULFILLED. Symptom needs (empty motivating list by design) are
      exempt — they're motivated by the problem statement, not by a
      hypothesis.
    """

    need_id: str = Field(
        default_factory=lambda: f"eneed_{uuid4().hex[:12]}",
        description="Unique evidence-need identifier",
        pattern=r"^eneed_[a-f0-9]{12}$",
    )

    case_id: str = Field(
        description="Case this need belongs to",
        min_length=17,
        max_length=36,
    )

    purpose: NeedPurpose = Field(
        description=(
            "Why this need exists: symptom_verification (motivated by "
            "the problem statement) or causal_verification (motivated "
            "by one or more hypotheses)."
        ),
    )

    request_text: str = Field(
        description=(
            "What data would fulfill this need, in a form suitable for "
            "surfacing to the user as an EVIDENCE-type suggestion. "
            "Example: 'kubectl get pods -n production showing current "
            "restart counts'."
        ),
        min_length=1,
        max_length=500,
    )

    rationale: str = Field(
        description=(
            "Why this data would advance the investigation. Used in "
            "the LLM's <evidence_needs> context block to remind the "
            "LLM why the need was created. Example: 'confirms whether "
            "the pod-level OOMKill pattern is still active after the "
            "memory-limit increase'."
        ),
        min_length=1,
        max_length=500,
    )

    priority: NeedPriority = Field(
        default=NeedPriority.MEDIUM,
        description="LLM hint for surfacing-order on the suggestion side.",
    )

    state: NeedState = Field(
        default=NeedState.PENDING,
        description="Lifecycle state — see NeedState.",
    )

    obtainability: NeedObtainability = Field(
        default=NeedObtainability.UNKNOWN,
        description=(
            "Whether the discriminating data can be gathered at all — the one "
            "judgment the engine cannot compute (insufficient-evidence §5.3). "
            "Model-declared, opt-in; UNKNOWN default is fail-safe (keep-"
            "engaging). Scoped to causal_verification needs; auto-revoked to "
            "UNKNOWN once the need is FULFILLED or SUPERSEDED (the question is "
            "moot). Only UNOBTAINABLE moves the case toward INSUFFICIENT_"
            "EVIDENCE — never back toward safety."
        ),
    )

    motivating_hypothesis_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Hypothesis IDs that motivated this need's existence. Empty "
            "list means the need is motivated by the problem statement "
            "(symptom needs). Engine appends/removes IDs as hypotheses "
            "share needs (cross-hypothesis evaluation per "
            "evidence-needs-design.md §5.2) and as hypotheses are "
            "retired (engine auto-supersession rule)."
        ),
    )

    fulfilling_evidence_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Evidence rows that fulfill this need. Multiple entries may "
            "accumulate across stages: presence evidence collected during "
            "DIAGNOSIS plus absence evidence collected during "
            "MITIGATION/TREATMENT. The list is append-only in practice — "
            "the need's state stays FULFILLED once fulfilled even when "
            "post-fix absence evidence is added."
        ),
    )

    superseded_reason: Optional[str] = Field(
        default=None,
        description=(
            "Human-readable explanation when state=SUPERSEDED. Set by "
            "engine auto-supersession ('all motivating hypotheses are "
            "terminal') or by LLM emission ('superseded by refined "
            "problem statement'). Required when state=SUPERSEDED, "
            "must be None otherwise."
        ),
        max_length=500,
    )

    surfaced_turns: List[int] = Field(
        default_factory=list,
        description=(
            "Turns on which this need was surfaced to the user as an "
            "EVIDENCE-type suggestion. The durable ask history (#1079): the "
            "anti-nagging rule used to tell the model to count its own prior "
            "mentions by reading conversation history, which is unreachable "
            "past the verbatim window (older turns collapse to a summary that "
            "records no asks at all). Recorded by the engine at the suggestion "
            "seam so the count is a fact the prompt can state rather than "
            "something the model must reconstruct."
        ),
    )

    engine_inferred: bool = Field(
        default=False,
        description=(
            "True when the engine created this need from an EVIDENCE suggestion "
            "the model raised without declaring one (#1079), rather than the "
            "model authoring it through ``evidence_need_updates``.\n\n"
            "Provenance matters because an inferred need is thinner than an "
            "authored one: it has no real rationale and no "
            "``motivating_hypothesis_ids``, because the engine knows an ask was "
            "made but not which candidate it discriminates. Readers that reason "
            "about the model's *deliberate* demand must exclude these — see "
            "``MilestoneEngine._awaiting_recent_evidence``, where counting them "
            "would stand anti-anchoring down on every turn the agent happens to "
            "ask for something."
        ),
    )

    created_at_turn: int = Field(
        description="Turn number when the need was created.",
        ge=0,
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Wall-clock creation time.",
    )

    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Wall-clock last-update time.",
    )

    @property
    def is_outstanding(self) -> bool:
        """The need still awaits (sufficient) data — PENDING or PARTIALLY_MET.

        FULFILLED and SUPERSEDED are the terminal states; an outstanding need is
        one the investigation is still waiting on.
        """
        return self.state in (NeedState.PENDING, NeedState.PARTIALLY_MET)

    @property
    def times_surfaced(self) -> int:
        """How many distinct turns this need has been asked for."""
        return len(self.surfaced_turns)

    @property
    def last_surfaced_turn(self) -> Optional[int]:
        """The most recent turn this need was asked for, or None if never."""
        return max(self.surfaced_turns) if self.surfaced_turns else None

    def record_surfaced(self, turn: int) -> None:
        """Record that this need was surfaced as an ask on ``turn``.

        Idempotent per turn — a turn that surfaces the same need through more
        than one suggestion counts once, so the rendered count reads as "turns
        on which I asked", which is what the anti-nagging judgment needs.
        """
        if turn not in self.surfaced_turns:
            self.surfaced_turns.append(turn)
            self.surfaced_turns.sort()

    @staticmethod
    def admissible_state(
        state: Optional["NeedState"], fulfilling_evidence_ids: List[str]
    ) -> Optional["NeedState"]:
        """The state a need may legally hold given the evidence backing it:
        FULFILLED with an empty ``fulfilling_evidence_ids`` is demoted to
        PARTIALLY_MET. Any other state passes through unchanged (``None`` means
        "no state change requested" on the apply-layer's update path).

        Single owner of the FULFILLED admission rule. The invariant itself lives
        in ``_validate_state_consistency``, but that is a ``model_validator`` and
        ``EvidenceNeed`` runs with ``validate_assignment`` off — so an in-place
        ``need.state = FULFILLED`` bypasses it and only trips on the next
        reconstruction, i.e. at Case persistence: a late, hard-to-trace 500 far
        from the code that caused it. Every apply-layer site that admits a
        caller-supplied state routes through here so the demotion is decided in
        one place, and a new site cannot reintroduce the save-time failure by
        forgetting its own copy of the check.

        Callers compare the returned state with the one they passed in to detect
        the demotion and record their own ``validation_repairs`` note — the
        wording is site-specific, the rule is not.
        """
        if state == NeedState.FULFILLED and not fulfilling_evidence_ids:
            return NeedState.PARTIALLY_MET
        return state

    def revoke_obtainability_if_terminal(self) -> None:
        """Reset ``obtainability`` to UNKNOWN once the need is terminal
        (FULFILLED/SUPERSEDED) — the §5.3 auto-revoke ("the question is moot").

        The construction validator enforces this, but ``EvidenceNeed`` runs with
        ``validate_assignment`` off (the apply layer mutates fields in place and
        relies on the validator NOT re-firing mid-update), so every site that
        flips ``state`` to a terminal value in place must call this to keep a
        stale UNOBTAINABLE from lingering on a resolved/moot need. One method so
        the invariant lives in a single place rather than per call-site.
        """
        if self.state in (NeedState.FULFILLED, NeedState.SUPERSEDED):
            self.obtainability = NeedObtainability.UNKNOWN

    @field_validator("request_text", "rationale", mode="after")
    @classmethod
    def _text_not_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be whitespace-only")
        return v

    @field_validator("motivating_hypothesis_ids", mode="after")
    @classmethod
    def _hypothesis_ids_well_formed(cls, v: List[str]) -> List[str]:
        # Hypothesis IDs follow the same pattern as elsewhere in the
        # codebase (uuid4-based). Cross-row existence is checked at the
        # engine apply-layer, not here — Pydantic only validates shape.
        for hyp_id in v:
            if not hyp_id or not isinstance(hyp_id, str):
                raise ValueError(
                    "motivating_hypothesis_ids entries must be non-empty strings"
                )
        # Deduplicate while preserving order (LLM may re-emit the same ID).
        seen: set[str] = set()
        deduped: List[str] = []
        for hyp_id in v:
            if hyp_id not in seen:
                seen.add(hyp_id)
                deduped.append(hyp_id)
        return deduped

    @field_validator("fulfilling_evidence_ids", mode="after")
    @classmethod
    def _evidence_ids_well_formed(cls, v: List[str]) -> List[str]:
        for ev_id in v:
            if not ev_id or not isinstance(ev_id, str):
                raise ValueError(
                    "fulfilling_evidence_ids entries must be non-empty strings"
                )
        seen: set[str] = set()
        deduped: List[str] = []
        for ev_id in v:
            if ev_id not in seen:
                seen.add(ev_id)
                deduped.append(ev_id)
        return deduped

    @model_validator(mode="after")
    def _validate_state_consistency(self) -> "EvidenceNeed":
        # SUPERSEDED needs must carry a reason; non-SUPERSEDED needs
        # must not. The asymmetric requirement mirrors how
        # ``closure_reason`` is enforced on Case for terminal states.
        if self.state == NeedState.SUPERSEDED and not (
            self.superseded_reason and self.superseded_reason.strip()
        ):
            raise ValueError(
                "EvidenceNeed.state=SUPERSEDED requires a non-empty "
                "superseded_reason"
            )
        if self.state != NeedState.SUPERSEDED and self.superseded_reason is not None:
            raise ValueError(
                "EvidenceNeed.superseded_reason must be None unless " "state=SUPERSEDED"
            )

        # FULFILLED needs must carry at least one fulfilling evidence
        # ID. The engine's apply-layer is responsible for adding the
        # ID; this validator catches a bad LLM emission that claims
        # FULFILLED with no supporting evidence.
        if self.state == NeedState.FULFILLED and not self.fulfilling_evidence_ids:
            raise ValueError(
                "EvidenceNeed.state=FULFILLED requires at least one "
                "fulfilling_evidence_id"
            )

        # Obtainability (§5.3) is a still-outstanding, causal_verification-only
        # judgment. Auto-revoke it to UNKNOWN when the need is terminal (data
        # arrived or need is moot) or is a symptom need (out of scope), so a
        # stale UNOBTAINABLE can never linger on a resolved/irrelevant need or
        # be read for a symptom ask. Coerce rather than reject — a mis-scoped
        # declaration is fail-safe, not a turn-breaking error.
        if (
            self.purpose != NeedPurpose.CAUSAL_VERIFICATION
            or self.state in (NeedState.FULFILLED, NeedState.SUPERSEDED)
        ) and self.obtainability != NeedObtainability.UNKNOWN:
            object.__setattr__(self, "obtainability", NeedObtainability.UNKNOWN)

        return self
