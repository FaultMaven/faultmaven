"""Pure row and parameter mappers for the SQLite case repository. Each function converts a raw SELECT row into a domain object, or a domain object into bound SQL parameters; none touches the database. The tag serializers and evidence-stance maps these mappers read are homed here too, since both `loading.py` and `saving.py` read them and this is the lower module in the package's import order."""

import builtins
import json
import logging
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from faultmaven.modules.case.contracts import (
    ActionAttempt,
    Case,
    CaseAction,
    CaseReport,
    CaseState,
    DocumentationData,
    EscalationState,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    InquiryData,
    InvestigationProgress,
    InvestigationStrategy,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
    ProblemVerification,
    ProposedAction,
    ReportStatus,
    ReportType,
    RootCauseConclusion,
    RunbookMetadata,
    Solution,
    TurnProgress,
    UploadedFile,
    WorkingConclusion,
    normalize_stored_report_content,
)
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)


def _serialize_tags(tags: Optional[List[str]]) -> Optional[str]:
    """Serialize an Evidence.tags list to the SQLite TEXT column.

    Empty list and None both round-trip as NULL. Comma-separated for
    SQLite (Pydantic's ``_no_commas_in_tags`` validator forbids commas
    inside individual tag values, which keeps the round-trip lossless).
    """
    if not tags:
        return None
    return ",".join(tags)


def _deserialize_tags(value: Optional[str]) -> List[str]:
    """Inverse of ``_serialize_tags``. Empty/None → empty list."""
    if not value:
        return []
    return [t for t in value.split(",") if t]


_STANCE_TO_RELATIONSHIP: Dict[EvidenceStance, str] = {
    EvidenceStance.SUPPORTS: "supports",
    EvidenceStance.REFUTES: "refutes",
    # The junction CHECK constraint allows ('supports', 'refutes', 'related').
    # Map domain NEUTRAL → 'related' (the closest neutral-not-irrelevant slot).
    EvidenceStance.NEUTRAL: "related",
}


_RELATIONSHIP_TO_STANCE: Dict[str, EvidenceStance] = {
    "supports": EvidenceStance.SUPPORTS,
    "refutes": EvidenceStance.REFUTES,
    "related": EvidenceStance.NEUTRAL,
}


def _row_to_evidence(row: Any) -> Optional[Evidence]:
    """Reconstruct a domain ``Evidence`` from a SELECT row.

    Column order: ``evidence_id, category, source_type, summary,
    extract, is_primary, reliability_score, tags, collected_at_turn,
    source_file_id, vectorized, coverage_start_ts, coverage_end_ts,
    metadata, created_at, primary_purpose, analysis, processing_mode,
    advances_milestones, collected_by, coverage_source``.

    Returns ``None`` and logs a warning when reconstruction fails so
    one bad row doesn't blank an entire result set.
    """
    try:
        # Strict category validation — bad rows fail loudly. Every row
        # is born with a valid 4-category classification.
        category = EvidenceCategory(row[1])

        source_type = EvidenceSourceType(row[2]) if row[2] else None

        metadata_raw = row[13]
        parsed_metadata: Optional[Dict[str, Any]] = None
        if metadata_raw:
            try:
                parsed = json.loads(metadata_raw)
                if isinstance(parsed, dict) and parsed:
                    parsed_metadata = parsed
            except (json.JSONDecodeError, TypeError):
                parsed_metadata = None

        collected_at = row[14]
        if isinstance(collected_at, str):
            try:
                collected_at = datetime.fromisoformat(collected_at.replace(" ", "T"))
            except ValueError:
                collected_at = datetime.now(UTC)
        elif collected_at is None:
            collected_at = datetime.now(UTC)

        return Evidence(
            evidence_id=str(row[0]),
            category=category,
            primary_purpose=row[15],
            summary=row[3] if row[3] else "Evidence",
            extract=row[4],
            analysis=row[16],
            processing_mode=row[17],
            source_type=source_type,
            source_file_id=row[9],
            is_primary=bool(row[5]),
            reliability_score=(float(row[6]) if row[6] is not None else None),
            tags=_deserialize_tags(row[7]),
            advances_milestones=_deserialize_tags(row[18]),
            collected_by=row[19] or "system",
            collected_at=collected_at,
            collected_at_turn=row[8] if row[8] else 0,
            vectorized=bool(row[10]),
            metadata=parsed_metadata,
            coverage_start_ts=row[11],
            coverage_end_ts=row[12],
            # Length-guarded: callers that remap a wider row (the bulk
            # loader) and older fixtures both pass shorter tuples, and an
            # absent provenance is exactly the NULL this column means.
            coverage_source=row[20] if len(row) > 20 else None,
        )
    except Exception as ev_err:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "Failed to load evidence %s: %s", row[0], ev_err
        )
        return None


def _row_to_evidence_need(
    row: Any,
    *,
    case_id: str,
    fulfilling_evidence_ids: List[str],
) -> Optional[EvidenceNeed]:
    """Reconstruct an ``EvidenceNeed`` from a SELECT row.

    Column order: ``need_id, purpose, request_text, rationale,
    priority, state, motivating_hypothesis_ids (JSON),
    superseded_reason, created_at_turn, created_at, updated_at,
    obtainability, surfaced_turns (JSON), engine_inferred``.
    ``case_id`` and ``fulfilling_evidence_ids`` are passed in by
    the caller (the row doesn't carry case_id explicitly because
    the WHERE clause already filtered by case).

    Returns ``None`` and logs a warning when reconstruction fails
    so one bad row doesn't blank an entire result set.
    """
    try:
        motivating_raw = row[6]
        try:
            motivating = json.loads(motivating_raw) if motivating_raw else []
        except (json.JSONDecodeError, TypeError):
            motivating = []

        # Ask history (#1079). Absent/corrupt reads as "never surfaced" —
        # the same fail-safe direction the pre-043 rows carry, so a bad
        # blob understates the count rather than silencing a live ask.
        surfaced_raw = row[12] if len(row) > 12 else None
        try:
            surfaced = json.loads(surfaced_raw) if surfaced_raw else []
        except (json.JSONDecodeError, TypeError):
            surfaced = []

        def _parse_dt(value: Any) -> datetime:
            if isinstance(value, datetime):
                return value
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace(" ", "T"))
                except ValueError:
                    return datetime.now(UTC)
            return datetime.now(UTC)

        return EvidenceNeed(
            need_id=str(row[0]),
            case_id=case_id,
            purpose=NeedPurpose(row[1]),
            request_text=row[2],
            rationale=row[3],
            priority=NeedPriority(row[4]),
            state=NeedState(row[5]),
            motivating_hypothesis_ids=motivating,
            fulfilling_evidence_ids=fulfilling_evidence_ids,
            superseded_reason=row[7],
            created_at_turn=row[8],
            created_at=_parse_dt(row[9]),
            updated_at=_parse_dt(row[10]),
            obtainability=(
                NeedObtainability(row[11])
                if len(row) > 11 and row[11]
                else NeedObtainability.UNKNOWN
            ),
            surfaced_turns=surfaced,
            engine_inferred=bool(row[13]) if len(row) > 13 else False,
        )
    except Exception as need_err:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "Failed to load evidence_need %s: %s", row[0], need_err
        )
        return None


def _bind_ids(params: dict[str, Any], ids: builtins.list[str]) -> str:
    """Expand a list of case_ids into named parameters. Returns the
    SQL placeholder clause (``:cid_0, :cid_1, ...``) to splice into
    an ``IN (...)`` and mutates ``params`` with the values."""
    names = []
    for i, cid in enumerate(ids):
        key = f"cid_{i}"
        params[key] = cid
        names.append(f":{key}")
    return ", ".join(names)


def _case_record_params(case: Case, last_activity_at: datetime) -> dict[str, Any]:
    """Build the parameter dict for the cases-row INSERT/UPDATE.

    Shared between the UPDATE and fallback INSERT paths in
    _upsert_case_record — keeps column serialization in one place.

    Post-redesign: ``description``, ``investigation_strategy``,
    ``current_turn``, ``turns_without_progress`` are first-class
    columns. The ``metadata`` JSON blob still holds the transient
    runtime state (proposed_actions / action_attempts / turn_history /
    pending_transition / message_count / last_suggestions) — those have
    no first-class column yet.
    """
    return {
        "case_id": case.case_id,
        "user_id": case.user_id,
        # ADR-020 D1: the stored driver, NULL = the creator drives. Carried on
        # every full-row save so a save never writes back a stale driver; a
        # concurrent reassignment bumps ``version`` and refuses the save.
        "driver_id": case.driver_id,
        "enterprise_id": case.enterprise_id,
        "organization_id": case.organization_id,
        "title": case.title,
        "description": case.description or "",
        "state": case.state.value,
        "source": case.source,
        "investigation_strategy": case.investigation_strategy.value,
        # Prevention: persist the DERIVED counter so it can never be saved
        # ahead of turn_history (the drift that wedged cases). See
        # Case.effective_current_turn — single source so the two repos can't
        # drift. The in-memory case.current_turn is untouched.
        "current_turn": case.effective_current_turn,
        "turns_without_progress": case.turns_without_progress,
        "created_at": case.created_at,
        "updated_at": case.updated_at,
        "closure_reason": getattr(case, "closure_reason", None),
        "last_activity_at": last_activity_at,
        "resolved_at": getattr(case, "resolved_at", None),
        "closed_at": getattr(case, "closed_at", None),
        "disposition_eligibility": (
            json.dumps(case.disposition_eligibility)
            if case.disposition_eligibility
            else None
        ),
        "inquiry": json.dumps(to_json_compatible(case.inquiry.model_dump())),
        "problem_verification": (
            json.dumps(to_json_compatible(case.problem_verification.model_dump()))
            if case.problem_verification
            else None
        ),
        "working_conclusion": (
            json.dumps(to_json_compatible(case.working_conclusion.model_dump()))
            if case.working_conclusion
            else None
        ),
        "root_cause_conclusion": (
            json.dumps(to_json_compatible(case.root_cause_conclusion.model_dump()))
            if case.root_cause_conclusion
            else None
        ),
        "escalation_state": (
            json.dumps(to_json_compatible(case.escalation_state.model_dump()))
            if case.escalation_state
            else None
        ),
        "documentation": json.dumps(
            to_json_compatible(case.documentation.model_dump())
        ),
        "progress": json.dumps(to_json_compatible(case.progress.model_dump())),
        "metadata": json.dumps(
            {
                "message_count": case.message_count,
                "pending_transition": case.pending_transition,
                "proposed_actions": (
                    [to_json_compatible(a.model_dump()) for a in case.proposed_actions]
                    if case.proposed_actions
                    else []
                ),
                "action_attempts": (
                    [to_json_compatible(a.model_dump()) for a in case.action_attempts]
                    if case.action_attempts
                    else []
                ),
                "turn_history": (
                    [to_json_compatible(t.model_dump()) for t in case.turn_history]
                    if case.turn_history
                    else []
                ),
                # Intent-bearing DECIDE suggestions from the last agent
                # turn — the resolver matches typed replies against them
                # on the NEXT request, so they must survive the reload
                # (#914). Normalized like every sibling entry: the dicts
                # are engine-built and currently plain, but this save
                # atomically commits the whole turn — an advisory field
                # must never be the json.dumps TypeError that loses it.
                "last_suggestions": (
                    to_json_compatible(case.last_suggestions)
                    if case.last_suggestions
                    else None
                ),
                # The KB PUSH channel's payload (fm#1360). It MUST round
                # trip, or the push is inert: both triggers fire during
                # response application — after this turn's prompt was
                # built — so the only prompt the pre-fetched runbooks can
                # ever reach is a LATER turn's, and a field dropped at save
                # never gets there. Measured before this line existed:
                # save() → get() returned ``kb_context is None`` for a case
                # saved with three admitted hits, and 7 of 7 pre-fetch
                # firings across three recorded runs had no LLM call after
                # them in the same request.
                "kb_context": (
                    to_json_compatible(case.kb_context) if case.kb_context else None
                ),
            }
        ),
    }


def _derive_solution_state(solution: Solution) -> str:
    """Map Pydantic Solution lifecycle fields to the schema's
    state CHECK vocabulary ('proposed', 'accepted', 'rejected',
    'implemented', 'verified'). Single source of truth for the
    write path, used by both ``_upsert_solutions`` and the PG
    hybrid repo.

    verified_at present  -> 'verified' (the model_validator
                            guarantees effectiveness is also set)
    applied_at present   -> 'implemented'
    otherwise            -> 'proposed'
    """
    if solution.verified_at is not None:
        return "verified"
    if solution.applied_at is not None:
        return "implemented"
    return "proposed"


def _row_to_case(
    row,
    hypotheses_data: builtins.list[dict],
    solutions_data: builtins.list[dict],
    uploaded_files_data: builtins.list[dict],
    messages_data: builtins.list[dict] | None = None,
    actions_data: builtins.list[CaseAction] | None = None,
) -> Case:
    """Reconstruct Case domain object from database row."""
    inquiry_data = json.loads(row.inquiry) if row.inquiry else {}
    inquiry = InquiryData(**inquiry_data) if inquiry_data else InquiryData()
    problem_verification = (
        ProblemVerification(**json.loads(row.problem_verification))
        if row.problem_verification
        else None
    )
    working_conclusion = (
        WorkingConclusion(**json.loads(row.working_conclusion))
        if row.working_conclusion
        else None
    )
    root_cause_conclusion = (
        RootCauseConclusion(**json.loads(row.root_cause_conclusion))
        if row.root_cause_conclusion
        else None
    )
    escalation_state = (
        EscalationState(**json.loads(row.escalation_state))
        if row.escalation_state
        else None
    )
    documentation = (
        DocumentationData(**json.loads(row.documentation))
        if row.documentation
        else DocumentationData()
    )
    progress = (
        InvestigationProgress(**json.loads(row.progress))
        if row.progress
        else InvestigationProgress()
    )

    # Convert loaded data to domain objects
    hypotheses_dict = (
        {h["hypothesis_id"]: Hypothesis(**h) for h in hypotheses_data}
        if hypotheses_data
        else {}
    )

    solutions_list = [Solution(**s) for s in solutions_data] if solutions_data else []

    uploaded_files = (
        [UploadedFile(**f) for f in uploaded_files_data] if uploaded_files_data else []
    )

    # Parse metadata for the transient runtime state that has no
    # first-class column yet (proposed_actions / action_attempts /
    # turn_history / pending_transition / message_count /
    # last_suggestions).
    metadata = json.loads(row.metadata) if row.metadata else {}

    # Promoted columns: read directly from the row.
    case_data = {
        "case_id": row.case_id,
        "user_id": row.user_id,
        "driver_id": row.driver_id,
        "enterprise_id": row.enterprise_id,  # NOT NULL in DB
        "organization_id": row.organization_id,  # nullable billing
        "source": getattr(row, "source", "copilot"),
        "title": row.title,
        "state": CaseState(row.state),
        "action_history": actions_data or [],
        "closure_reason": row.closure_reason,
        "disposition_eligibility": (
            json.loads(row.disposition_eligibility)
            if getattr(row, "disposition_eligibility", None)
            else None
        ),
        "pending_transition": metadata.get("pending_transition"),
        "last_suggestions": metadata.get("last_suggestions"),
        # Pre-fetched runbooks (the KB push channel, fm#1360). See the
        # writer for why dropping this made the channel inert.
        "kb_context": metadata.get("kb_context"),
        "progress": progress,
        "current_turn": int(row.current_turn or 0),
        "turns_without_progress": int(row.turns_without_progress or 0),
        "message_count": metadata.get("message_count", 0),
        "turn_history": (
            [TurnProgress(**t) for t in metadata.get("turn_history", [])]
            if metadata.get("turn_history")
            else []
        ),
        "proposed_actions": (
            [ProposedAction(**a) for a in metadata.get("proposed_actions", [])]
            if metadata.get("proposed_actions")
            else []
        ),
        "action_attempts": (
            [ActionAttempt(**a) for a in metadata.get("action_attempts", [])]
            if metadata.get("action_attempts")
            else []
        ),
        "inquiry": inquiry,
        "problem_verification": problem_verification,
        "uploaded_files": uploaded_files,
        "evidence": [],  # Loaded separately
        "hypotheses": hypotheses_dict,
        "solutions": solutions_list,
        "messages": messages_data if messages_data else [],
        "working_conclusion": working_conclusion,
        "root_cause_conclusion": root_cause_conclusion,
        "escalation_state": escalation_state,
        "documentation": documentation,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        # Optimistic concurrency token. Must round-trip so the next
        # save() can assert it still matches the DB.
        "version": (
            int(row.version)
            if hasattr(row, "version") and row.version is not None
            else 1
        ),
    }

    # ``description`` is a first-class column. The CHECK constraint
    # forbids empty description for non-INQUIRY rows; the ``or ""``
    # is paranoid coalesce against any row that somehow has NULL.
    description = row.description or ""
    if (
        CaseState(row.state) == CaseState.INVESTIGATING
        and (not description or not description.strip())
        and inquiry.proposed_problem_statement
    ):
        description = inquiry.proposed_problem_statement
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"Auto-healed missing description for case {row.case_id} "
                f"from proposed_problem_statement"
            )

    if description:
        case_data["description"] = description

    # ``investigation_strategy`` is now a first-class column.
    if row.investigation_strategy:
        case_data["investigation_strategy"] = InvestigationStrategy(
            row.investigation_strategy
        )

    if row.last_activity_at:
        case_data["last_activity_at"] = row.last_activity_at

    if row.resolved_at:
        case_data["resolved_at"] = row.resolved_at

    if row.closed_at:
        case_data["closed_at"] = row.closed_at

    return Case(**case_data)


def _row_to_report(row) -> "CaseReport":
    """Convert database row to CaseReport domain object."""
    from faultmaven.utils.serialization import to_json_compatible

    metadata = None
    if row.metadata and row.metadata != "{}":
        try:
            metadata_dict = (
                json.loads(row.metadata)
                if isinstance(row.metadata, str)
                else row.metadata
            )
            if metadata_dict:
                metadata = RunbookMetadata(**metadata_dict)
        except Exception:
            pass

    # Handle timestamps (SQLite stores as strings)
    if row.generated_at:
        if isinstance(row.generated_at, str):
            generated_at = row.generated_at
        else:
            gen_dt = row.generated_at
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=UTC)
            generated_at = to_json_compatible(gen_dt)
    else:
        generated_at = to_json_compatible(datetime.now(UTC))

    if row.updated_at:
        if isinstance(row.updated_at, str):
            updated_at = row.updated_at
        else:
            upd_dt = row.updated_at
            if upd_dt.tzinfo is None:
                upd_dt = upd_dt.replace(tzinfo=UTC)
            updated_at = to_json_compatible(upd_dt)
    else:
        updated_at = generated_at

    return CaseReport(
        report_id=row.report_id,
        case_id=row.case_id,
        report_type=ReportType(row.report_type),
        version=row.version,
        is_current=bool(row.is_current),
        linked_to_closure=bool(row.linked_to_closure),
        title=row.title,
        # Normalized where a stored report BECOMES a CaseReport
        # (#1097): a summary is generated once at the terminal
        # transition and never re-rendered, so rows written before
        # the audit/prose split still carry the engine notation.
        # Here rather than at each presentation site — applied
        # per-reader it is a discipline every future consumer must
        # opt into, and the download endpoint had already been
        # missed that way; here it is a property of any report
        # loaded from storage.
        content=normalize_stored_report_content(row.content),
        format=row.format,
        generation_status=ReportStatus(row.generation_status),
        generation_time_ms=row.generation_time_ms,
        generated_at=generated_at,
        updated_at=updated_at,
        metadata=metadata,
    )
