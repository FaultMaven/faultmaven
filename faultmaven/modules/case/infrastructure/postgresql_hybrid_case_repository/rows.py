"""Pure row <-> domain-model mapping for the PostgreSQL case repository.

Functions that read no instance state: turning a SELECT row into a domain
object (``_row_to_evidence``, ``_row_to_evidence_need``, ``_row_to_report``),
or a domain object into the bound-parameter
values a write needs (``_case_record_params``, ``_bind_ids``,
``_derive_solution_state``), plus the tag and evidence-stance codecs the
mappers share. ``_cast`` and ``_as_datetime`` live here too, taking
``is_pg`` (or nothing) as an argument rather than reading it off an
instance, since they format values rather than own a connection.
``_row_to_case`` — the one row mapper that also loads a child collection
(case actions) — lives in ``loading.py`` instead, alongside the other
async loaders it must call.
"""

import builtins
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.conclusion import (
    normalize_stored_report_content,
)
from faultmaven.modules.case.domain.models.evidence import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
)
from faultmaven.modules.case.domain.models.evidence_needs import (
    EvidenceNeed,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
)
from faultmaven.modules.case.domain.models.solution import (
    Solution,
)
from faultmaven.modules.case.domain.owned_models.report import CaseReport
from faultmaven.utils.datetime import parse_utc_timestamp

logger = logging.getLogger(__name__)


def _serialize_tags(tags: Optional[List[str]]) -> Optional[List[str]]:
    """Serialize Evidence.tags for the PG ``tags`` column.

    PostgreSQL stores tags as ``TEXT[]`` (see ``TagsArray`` in
    infrastructure/persistence/models.py). asyncpg / psycopg bind a
    Python list directly to that array, so the serializer is just an
    ``[] → None`` normalization. Pydantic's ``_no_commas_in_tags``
    validator already rejects values containing commas — same rule
    SQLite needs for its TEXT round-trip.
    """
    if not tags:
        return None
    return list(tags)


def _deserialize_tags(value: Any) -> List[str]:
    """Inverse of ``_serialize_tags``.

    On PG, asyncpg returns the column as ``list[str]``. Older rows
    written before the schema rewrite may surface as a comma-separated
    TEXT (the SQLite shape) — accept both for robustness.
    """
    if not value:
        return []
    if isinstance(value, list):
        return [str(t) for t in value if t]
    if isinstance(value, str):
        return [t for t in value.split(",") if t]
    return []


_STANCE_TO_RELATIONSHIP: Dict[EvidenceStance, str] = {
    EvidenceStance.SUPPORTS: "supports",
    EvidenceStance.REFUTES: "refutes",
    # The hypothesis_evidence CHECK allows ('supports', 'refutes', 'related').
    # Domain NEUTRAL maps to 'related' (closest neutral-not-irrelevant slot).
    EvidenceStance.NEUTRAL: "related",
}


_RELATIONSHIP_TO_STANCE: Dict[str, EvidenceStance] = {
    "supports": EvidenceStance.SUPPORTS,
    "refutes": EvidenceStance.REFUTES,
    "related": EvidenceStance.NEUTRAL,
}


def _cast(is_pg, name: str, pg_type: str = "JSONB") -> str:
    """Render a bound-parameter type cast safe on both backends.

    Returns ``CAST(:name AS <pg_type>)`` on PostgreSQL and a bare
    ``:name`` on SQLite (which stores these columns as TEXT and needs no
    cast). Reads the dialect resolved once at construction
    (``self._is_pg``) — it cannot change for the session's lifetime.

    Why never ``:name::pg_type``: SQLAlchemy 2.0's ``text()`` bind parser
    reads a ``::`` immediately following a placeholder as the start of a
    PostgreSQL cast and silently DROPS the preceding ``:name`` bind. The
    value then reaches asyncpg as the literal string ``:name::jsonb``
    while sibling columns compile to ``$N`` params — the
    ``syntax error at or near ":"`` that broke every JSONB/timestamptz
    write on the first real-PostgreSQL deployment. ``CAST(:name AS ...)``
    keeps the placeholder bound. See
    ``test_postgresql_cast_binds_survive.py`` for the regression guard.
    """
    if is_pg:
        return f"CAST(:{name} AS {pg_type})"
    return f":{name}"


def _as_datetime(value: Any, default: datetime) -> datetime:
    """Coerce a timestamp value to a tz-aware ``datetime`` for asyncpg.

    Message rows reach the repository as plain dicts (``message_dict`` /
    ``case.messages`` entries), NOT Pydantic models, so a ``created_at``
    can arrive as an ISO STRING. asyncpg binds a ``timestamptz`` parameter
    only from a Python ``datetime`` — a ``str`` raises ``DataError``
    ("invalid input for query argument"), and (unlike a JSONB cast) it
    fails even inside ``CAST(:ts AS TIMESTAMPTZ)`` because asyncpg encodes
    the bind as timestamptz BEFORE the cast applies. Pydantic-backed rows
    (cases / evidence / hypotheses / solutions / reports)
    are already datetimes via field validation, so only the dict-sourced
    message timestamps need this coercion. SQLite's repository already
    does the same via its own ``_parse_dt`` — this restores parity.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return parse_utc_timestamp(value)
        except ValueError:
            return default
    return default


def _row_to_evidence(row: Any) -> Optional[Evidence]:
    """Reconstruct a domain ``Evidence`` from a SELECT row.

    Column order: ``evidence_id, category, source_type, summary,
    extract, is_primary, reliability_score, tags, collected_at_turn,
    source_file_id, vectorized, coverage_start_ts, coverage_end_ts,
    metadata, created_at, primary_purpose, analysis, processing_mode,
    advances_milestones, collected_by``.

    Returns ``None`` and logs a warning when reconstruction fails so
    one bad row doesn't blank an entire result set.
    """
    try:
        # Strict category validation — every row is born with a valid
        # 4-category classification.
        category = EvidenceCategory(row[1])

        source_type = EvidenceSourceType(row[2]) if row[2] else None

        metadata_raw = row[13]
        parsed_metadata: Optional[Dict[str, Any]] = None
        if metadata_raw:
            # Postgres JSONB returns a dict directly; legacy TEXT
            # rows arrive as JSON-serialized strings.
            if isinstance(metadata_raw, dict):
                parsed_metadata = metadata_raw or None
            else:
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
                collected_at = datetime.now(timezone.utc)
        elif collected_at is None:
            collected_at = datetime.now(timezone.utc)

        # Postgres ARRAY(String) returns a list; the SQLite repo uses
        # comma-encoded TEXT and _deserialize_tags. Handle both shapes.
        advances_raw = row[18] if len(row) > 18 else None
        if isinstance(advances_raw, list):
            advances_milestones = list(advances_raw)
        else:
            advances_milestones = _deserialize_tags(advances_raw)

        tags_raw = row[7]
        if isinstance(tags_raw, list):
            tags = list(tags_raw)
        else:
            tags = _deserialize_tags(tags_raw)

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
            tags=tags,
            advances_milestones=advances_milestones,
            collected_by=row[19] or "system",
            collected_at=collected_at,
            collected_at_turn=row[8] if row[8] else 0,
            vectorized=bool(row[10]),
            metadata=parsed_metadata,
            coverage_start_ts=row[11],
            coverage_end_ts=row[12],
            # Length-guarded for the same reason as the SQLite path: an
            # absent provenance IS the NULL this column means.
            coverage_source=row[20] if len(row) > 20 else None,
        )
    except Exception as ev_err:  # noqa: BLE001
        logger.warning("Failed to load evidence %s: %s", row[0], ev_err)
        return None


def _row_to_evidence_need(
    row: Any,
    *,
    case_id: str,
    fulfilling_evidence_ids: builtins.list[str],
) -> Optional[EvidenceNeed]:
    """Reconstruct an ``EvidenceNeed`` from a SELECT row.

    Column order: ``need_id, purpose, request_text, rationale,
    priority, state, motivating_hypothesis_ids (JSONB),
    superseded_reason, created_at_turn, created_at, updated_at,
    obtainability, surfaced_turns (JSONB), engine_inferred``.
    On PG, JSONB is returned as a Python list directly (asyncpg);
    on dialect-compatibility paths a JSON string is also tolerated.
    """
    try:
        motivating_raw = row[6]
        if isinstance(motivating_raw, list):
            motivating = list(motivating_raw)
        elif motivating_raw is None:
            motivating = []
        else:
            try:
                motivating = json.loads(motivating_raw)
            except (json.JSONDecodeError, TypeError):
                motivating = []

        # Ask history (#1079). Absent/corrupt reads as "never surfaced" —
        # understating the count is the fail-safe direction (it keeps a live
        # ask visible rather than silencing it on a bad blob).
        surfaced_raw = row[12] if len(row) > 12 else None
        if isinstance(surfaced_raw, list):
            surfaced = list(surfaced_raw)
        elif surfaced_raw is None:
            surfaced = []
        else:
            try:
                surfaced = json.loads(surfaced_raw)
            except (json.JSONDecodeError, TypeError):
                surfaced = []

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
            created_at=row[9] if row[9] else datetime.now(timezone.utc),
            updated_at=row[10] if row[10] else datetime.now(timezone.utc),
            obtainability=(
                NeedObtainability(row[11])
                if len(row) > 11 and row[11]
                else NeedObtainability.UNKNOWN
            ),
            surfaced_turns=surfaced,
            engine_inferred=bool(row[13]) if len(row) > 13 else False,
        )
    except Exception as need_err:  # noqa: BLE001
        logger.warning("Failed to load evidence_need %s: %s", row[0], need_err)
        return None


def _bind_ids(params: Dict[str, Any], ids: builtins.list[str]) -> str:
    """Expand a list of identifiers into named bind parameters.

    Returns the SQL placeholder clause (``:cid_0, :cid_1, ...``) and
    mutates ``params`` with the values. Used to splice into an
    ``IN (...)`` filter without resorting to f-string interpolation
    of values.
    """
    names = []
    for i, cid in enumerate(ids):
        key = f"cid_{i}"
        params[key] = cid
        names.append(f":{key}")
    return ", ".join(names)


def _case_record_params(case: Case, last_activity_at: datetime) -> Dict[str, Any]:
    """Parameter dict for the cases-row INSERT/UPDATE.

    Shared between the UPDATE and fallback INSERT paths in
    _upsert_case_record — keeps column serialization in one place.

    Post-redesign: ``description``, ``investigation_strategy``,
    ``current_turn``, ``turns_without_progress`` are first-class
    columns (the PG hybrid had been writing them as phantom columns
    before the schema baseline; now they're real). The ``metadata``
    JSON blob still holds the transient runtime state (proposed_actions /
    action_attempts / turn_history / pending_transition /
    message_count / last_suggestions) — those have no first-class
    column yet.
    """
    from faultmaven.utils.serialization import to_json_compatible

    return {
        "case_id": case.case_id,
        "user_id": case.user_id,
        # ADR-020 D1: the stored driver, NULL = the creator drives. Bound for
        # the INSERT of a new case only: the full-row UPDATE does not write it,
        # because its one writer is the versioned reassign/release
        # (``case_driver_sql``), so no save can write a driver back.
        "driver_id": case.driver_id,
        "enterprise_id": case.enterprise_id,
        "organization_id": case.organization_id,
        "title": case.title,
        "description": case.description or "",
        "investigation_strategy": case.investigation_strategy.value,
        "state": case.state.value,
        "source": case.source,
        "closure_reason": case.closure_reason,
        # Prevention: persist the DERIVED counter so it can never be saved
        # ahead of turn_history (the drift that wedged cases). See
        # Case.effective_current_turn — single source so the two repos can't
        # drift. The in-memory case.current_turn is untouched.
        "current_turn": case.effective_current_turn,
        "turns_without_progress": case.turns_without_progress,
        "created_at": case.created_at,
        "updated_at": case.updated_at,
        "last_activity_at": last_activity_at,
        "resolved_at": case.resolved_at,
        "closed_at": case.closed_at,
        "disposition_eligibility": (
            json.dumps(case.disposition_eligibility)
            if case.disposition_eligibility
            else None
        ),
        "inquiry": json.dumps(case.inquiry.model_dump(mode="json")),
        "problem_verification": (
            json.dumps(case.problem_verification.model_dump(mode="json"))
            if case.problem_verification
            else None
        ),
        "working_conclusion": (
            json.dumps(case.working_conclusion.model_dump(mode="json"))
            if case.working_conclusion
            else None
        ),
        "root_cause_conclusion": (
            json.dumps(case.root_cause_conclusion.model_dump(mode="json"))
            if case.root_cause_conclusion
            else None
        ),
        "escalation_state": (
            json.dumps(case.escalation_state.model_dump(mode="json"))
            if case.escalation_state
            else None
        ),
        "documentation": json.dumps(case.documentation.model_dump(mode="json")),
        "progress": json.dumps(case.progress.model_dump(mode="json")),
        "metadata": json.dumps(
            {
                k: v
                for k, v in {
                    # _row_to_case reads message_count from this bag
                    # (same as SQLite) but PG never wrote it, so every
                    # reload reset the counter to 0 — the same
                    # write/read asymmetry class as #914. A count of 0
                    # is dropped by the falsy filter below; the read
                    # side's default already covers that.
                    "message_count": case.message_count,
                    "pending_transition": case.pending_transition,
                    "proposed_actions": (
                        [a.model_dump(mode="json") for a in case.proposed_actions]
                        if case.proposed_actions
                        else []
                    ),
                    "action_attempts": (
                        [a.model_dump(mode="json") for a in case.action_attempts]
                        if case.action_attempts
                        else []
                    ),
                    "turn_history": (
                        [t.model_dump(mode="json") for t in case.turn_history]
                        if case.turn_history
                        else []
                    ),
                    # Intent-bearing DECIDE suggestions from the last
                    # agent turn — the resolver matches typed replies
                    # against them on the NEXT request, so they must
                    # survive the reload (#914). Normalized like every
                    # sibling so a non-primitive value can never turn
                    # into the json.dumps TypeError that loses the
                    # atomically-committed turn; the falsy filter
                    # below drops None/empty.
                    "last_suggestions": (
                        to_json_compatible(case.last_suggestions)
                        if case.last_suggestions
                        else None
                    ),
                    # The KB PUSH channel's payload (fm#1360) — see the
                    # SQLite repository's writer for why it must round
                    # trip. The falsy filter below drops it when empty,
                    # which is the same "no context" the reader's
                    # ``.get`` produces.
                    "kb_context": (
                        to_json_compatible(case.kb_context) if case.kb_context else None
                    ),
                }.items()
                if v
            }
        ),
    }


def _derive_solution_state(solution: Solution) -> str:
    """Map Pydantic Solution lifecycle fields to the schema's
    state CHECK vocabulary. Mirrors the SQLite repo logic.
    """
    if solution.verified_at is not None:
        return "verified"
    if solution.applied_at is not None:
        return "implemented"
    return "proposed"


def _row_to_report(row) -> "CaseReport":
    """Convert database row to CaseReport domain object."""
    from faultmaven.modules.case.domain.owned_models.report import (
        CaseReport,
        ReportStatus,
        ReportType,
        RunbookMetadata,
    )
    from faultmaven.utils.serialization import to_json_compatible

    # Parse metadata if present
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

    # Convert timestamps to ISO 8601 strings (ensuring UTC consistency)
    # PostgreSQL TIMESTAMP WITH TIME ZONE is stored in UTC but may return in session timezone
    # Normalize to UTC explicitly to avoid timezone jitter between implementations
    if row.generated_at:
        # Ensure UTC: if timezone-aware, convert to UTC; if naive, assume UTC
        gen_dt = row.generated_at
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=timezone.utc)
        elif gen_dt.tzinfo != timezone.utc:
            gen_dt = gen_dt.astimezone(timezone.utc)
        generated_at = to_json_compatible(gen_dt)
    else:
        generated_at = to_json_compatible(datetime.now(timezone.utc))

    if row.updated_at:
        # Ensure UTC: if timezone-aware, convert to UTC; if naive, assume UTC
        upd_dt = row.updated_at
        if upd_dt.tzinfo is None:
            upd_dt = upd_dt.replace(tzinfo=timezone.utc)
        elif upd_dt.tzinfo != timezone.utc:
            upd_dt = upd_dt.astimezone(timezone.utc)
        updated_at = to_json_compatible(upd_dt)
    else:
        updated_at = generated_at  # Fallback to generated_at if NULL

    return CaseReport(
        report_id=row.report_id,
        case_id=row.case_id,
        report_type=ReportType(row.report_type),
        version=row.version,
        is_current=row.is_current,
        linked_to_closure=row.linked_to_closure,
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
