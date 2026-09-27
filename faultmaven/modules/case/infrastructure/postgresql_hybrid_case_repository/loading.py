"""SELECT-side helpers for the PostgreSQL case repository.

Each function loads one child collection (evidence, evidence needs,
hypothesis-evidence links, the causal graph, case actions) for a single
case, taking the async session (``db``) as its first argument and
returning domain objects built via ``rows.py``'s mappers. ``_row_to_case``
lives here rather than in ``rows.py``: it is the one row mapper that also
issues a query (loading the case-action audit trail via
``_load_case_actions``), so homing it beside the other loaders keeps
``rows.py`` free of any dependency on this module.
"""

import builtins
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.causal import (
    CausalEdge,
    CausalNode,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ValidationMethod,
)
from faultmaven.modules.case.domain.models.conclusion import (
    RootCauseConclusion,
    WorkingConclusion,
)
from faultmaven.modules.case.domain.models.documentation import (
    DocumentationData,
    EscalationState,
)
from faultmaven.modules.case.domain.models.evidence import (
    EvidenceStance,
    UploadedFile,
)
from faultmaven.modules.case.domain.models.evidence_needs import (
    EvidenceNeed,
)
from faultmaven.modules.case.domain.models.hypothesis import (
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
)
from faultmaven.modules.case.domain.models.lifecycle import (
    CaseAction,
    CaseState,
    InvestigationStrategy,
)
from faultmaven.modules.case.domain.models.problem import (
    InquiryData,
    ProblemVerification,
)
from faultmaven.modules.case.domain.models.progress import InvestigationProgress
from faultmaven.modules.case.domain.models.solution import (
    ActionAttempt,
    ProposedAction,
    Solution,
)
from faultmaven.modules.case.domain.models.turn import TurnProgress
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.rows import (
    _RELATIONSHIP_TO_STANCE,
    _as_datetime,
    _bind_ids,
    _row_to_evidence,
    _row_to_evidence_need,
)

logger = logging.getLogger(__name__)


async def _load_evidence_for_case(db, case: Case) -> None:
    """Load investigation evidence from the evidence table.

    Post-010 columns (in this fixed order, consumed positionally by
    ``_row_to_evidence``): ``evidence_id``, ``category``,
    ``source_type``, ``summary``, ``extract``, ``is_primary``,
    ``reliability_score``, ``tags``, ``collected_at_turn``,
    ``source_file_id``, ``vectorized``, ``coverage_start_ts``,
    ``coverage_end_ts``, ``metadata``, ``created_at``,
    ``primary_purpose``, ``analysis``, ``processing_mode``,
    ``advances_milestones``, ``collected_by``, ``coverage_source``.

    File-level metadata (filename, content_hash, content_type, size,
    storage_ref) lives on ``uploaded_files``, reachable via
    ``source_file_id``.
    """
    try:
        query = text("""
                SELECT
                    evidence_id, category, source_type,
                    summary, extract,
                    is_primary, reliability_score, tags,
                    collected_at_turn, source_file_id, vectorized,
                    coverage_start_ts, coverage_end_ts,
                    metadata, created_at,
                    primary_purpose, analysis, processing_mode,
                    advances_milestones, collected_by,
                    coverage_source
                FROM evidence
                WHERE case_id = :case_id
                ORDER BY created_at DESC
                LIMIT 1000
                """)
        result = await db.execute(query, {"case_id": case.case_id})
        rows = result.fetchall()

        evidence_list = [_row_to_evidence(row) for row in rows if row]
        case.evidence = [ev for ev in evidence_list if ev is not None]
    except Exception as e:
        logger.warning(
            "Failed to load evidence for case %s: %s",
            case.case_id,
            e,
        )


async def _load_evidence_needs_for_case(db, case: Case) -> None:
    """Load evidence-need rows + their fulfillment junctions.

    Mirrors the SQLite repo's ``_load_evidence_needs_for_case`` —
    same column shape, JSON parsing for ``motivating_hypothesis_ids``,
    and one-shot junction query for fulfillment.
    """
    try:
        need_query = text("""
                SELECT
                    need_id, purpose, request_text, rationale,
                    priority, state,
                    motivating_hypothesis_ids,
                    superseded_reason,
                    created_at_turn, created_at, updated_at,
                    obtainability, surfaced_turns, engine_inferred
                FROM evidence_needs
                WHERE case_id = :case_id
                ORDER BY created_at ASC, need_id ASC
            """)
        need_rows = (await db.execute(need_query, {"case_id": case.case_id})).fetchall()

        if not need_rows:
            case.evidence_needs = []
            return

        need_ids = [row[0] for row in need_rows]
        params: Dict[str, Any] = {}
        placeholders = _bind_ids(params, need_ids)
        junction_query = text(f"""
                SELECT need_id, evidence_id
                FROM evidence_need_fulfillment
                WHERE need_id IN ({placeholders})
            """)
        junction_rows = (await db.execute(junction_query, params)).fetchall()

        fulfillments_by_need: Dict[str, builtins.list[str]] = {}
        for nid, eid in junction_rows:
            fulfillments_by_need.setdefault(nid, []).append(eid)

        needs: builtins.list[EvidenceNeed] = []
        for row in need_rows:
            need = _row_to_evidence_need(
                row,
                case_id=case.case_id,
                fulfilling_evidence_ids=fulfillments_by_need.get(row[0], []),
            )
            if need is not None:
                needs.append(need)
        case.evidence_needs = needs
    except Exception as e:
        logger.warning("Failed to load evidence_needs for case %s: %s", case.case_id, e)


async def _load_hypothesis_evidence_links(
    db, hypothesis_ids: builtins.list[str]
) -> Dict[str, builtins.list[HypothesisEvidenceLink]]:
    """Load junction-table rows and return them as
    ``{hypothesis_id: [HypothesisEvidenceLink, ...]}``.

    Empty input returns ``{}``. Hypotheses with no links are absent
    from the result (callers default to an empty list per hypothesis).
    The junction table doesn't carry the LLM's free-text rationale —
    that lives on ``case_messages`` / agent reasoning logs — so we
    persist an empty marker on the reconstructed link.
    """
    if not hypothesis_ids:
        return {}
    params: Dict[str, Any] = {}
    placeholders = _bind_ids(params, hypothesis_ids)
    query = text(f"""
            SELECT hypothesis_id, evidence_id, relationship_type, confidence,
                   linked_at_turn, created_at
            FROM hypothesis_evidence
            WHERE hypothesis_id IN ({placeholders})
        """)
    result = await db.execute(query, params)
    rows = result.fetchall()

    by_hyp: Dict[str, builtins.list[HypothesisEvidenceLink]] = {}
    for row in rows:
        hyp_id = row[0]
        relationship = row[2] or "related"
        stance = _RELATIONSHIP_TO_STANCE.get(relationship, EvidenceStance.NEUTRAL)
        confidence = float(row[3]) if row[3] is not None else 0.0
        analyzed_at = row[5]
        if isinstance(analyzed_at, str):
            try:
                analyzed_at = datetime.fromisoformat(analyzed_at.replace(" ", "T"))
            except ValueError:
                analyzed_at = datetime.now(timezone.utc)
        elif analyzed_at is None:
            analyzed_at = datetime.now(timezone.utc)
        link = HypothesisEvidenceLink(
            hypothesis_id=str(hyp_id),
            evidence_id=str(row[1]),
            stance=stance,
            # Junction has no reasoning column; required-by-Pydantic
            # field is satisfied with empty marker.
            reasoning="",
            stance_confidence=max(0.0, min(1.0, confidence)),
            analyzed_at=analyzed_at,
        )
        by_hyp.setdefault(str(hyp_id), []).append(link)
    return by_hyp


async def _load_node_evidence_links(
    db, node_ids: List[str]
) -> Dict[str, List[NodeEvidenceLink]]:
    """Load causal_node_evidence rows as ``{node_id: [NodeEvidenceLink]}``.
    Stance is stored verbatim (supports/refutes/neutral)."""
    if not node_ids:
        return {}
    params: Dict[str, Any] = {}
    placeholders = _bind_ids(params, node_ids)
    query = text(f"""
            SELECT node_id, evidence_id, stance, stance_confidence,
                   reasoning, linked_at_turn, created_at
            FROM causal_node_evidence
            WHERE node_id IN ({placeholders})
        """)
    result = await db.execute(query, params)
    by_node: Dict[str, List[NodeEvidenceLink]] = {}
    for row in result.fetchall():
        nid = str(row[0])
        # Reuse the canonical timestamptz coercion (handles str/None/datetime)
        # rather than a weaker inline parser — same helper the message path uses.
        analyzed_at = _as_datetime(row[6], datetime.now(timezone.utc))
        conf = float(row[3]) if row[3] is not None else 1.0
        by_node.setdefault(nid, []).append(
            NodeEvidenceLink(
                evidence_id=str(row[1]),
                stance=EvidenceStance(row[2]),
                reasoning=row[4] or "",
                stance_confidence=max(0.0, min(1.0, conf)),
                linked_at_turn=row[5] or 0,
                analyzed_at=analyzed_at,
            )
        )
    return by_node


async def _load_causal_graph_for_case(db, case: Case) -> None:
    """Load the case's causal graph (nodes + edges) + node-scoped evidence.
    Loaded separately from the parent aggregate to avoid a cartesian
    blow-up in the multi-LEFT-JOIN fetch (same rationale as evidence)."""
    node_rows = (
        await db.execute(
            text("""
                    SELECT node_id, statement, node_type, node_state,
                           validation_method, belief, signature_consistent,
                           actionable, category, state_epoch, generated_at_turn,
                           last_updated_turn, last_progress_at_turn,
                           iterations_without_progress, refutation_reason,
                           rationale, proposed_at, updated_at, metadata
                    FROM causal_nodes
                    WHERE case_id = :case_id
                """),
            {"case_id": case.case_id},
        )
    ).fetchall()
    node_ids = [str(r[0]) for r in node_rows]
    links_by_node = await _load_node_evidence_links(db, node_ids)

    nodes: Dict[str, CausalNode] = {}
    for r in node_rows:
        nid = str(r[0])
        nodes[nid] = CausalNode(
            node_id=nid,
            statement=r[1],
            node_type=NodeType(r[2]),
            node_state=NodeState(r[3]),
            validation_method=ValidationMethod(r[4]),
            belief=float(r[5]) if r[5] is not None else 0.5,
            signature_consistent=bool(r[6]),
            actionable=bool(r[7]),
            category=HypothesisCategory(r[8]) if r[8] else None,
            state_epoch=r[9] or 0,
            generated_at_turn=r[10] or 0,
            last_updated_turn=r[11] or 0,
            last_progress_at_turn=r[12] or 0,
            iterations_without_progress=r[13] or 0,
            refutation_reason=r[14],
            rationale=r[15],
            evidence_links=links_by_node.get(nid, []),
            proposed_at=r[16] or datetime.now(timezone.utc),
            updated_at=r[17] or datetime.now(timezone.utc),
            metadata=(json.loads(r[18]) if isinstance(r[18], str) else (r[18] or {})),
        )
    case.causal_nodes = nodes

    edge_rows = (
        await db.execute(
            text("""
                    SELECT edge_id, cause_node_id, effect_node_id, and_group,
                           reasoning, created_at_turn, created_at
                    FROM causal_edges
                    WHERE case_id = :case_id
                """),
            {"case_id": case.case_id},
        )
    ).fetchall()
    case.causal_edges = [
        CausalEdge(
            edge_id=str(r[0]),
            cause_node_id=str(r[1]),
            effect_node_id=str(r[2]),
            and_group=r[3],
            reasoning=r[4],
            created_at_turn=r[5] or 0,
            created_at=r[6] or datetime.now(timezone.utc),
        )
        for r in edge_rows
    ]


async def _load_case_actions(db, case_id: str) -> List[CaseAction]:
    """Hydrate the audit trail for a case from ``case_actions``.

    Replaces the prior write-only pattern (``action_history=[]`` hardcoded
    in ``_to_domain``). Rows are returned ordered oldest-first.
    """
    query = text("""
            SELECT from_state, to_state, reason, triggered_by, transitioned_at
            FROM case_actions
            WHERE case_id = :case_id
            ORDER BY transitioned_at ASC, transition_id ASC
        """)
    result = await db.execute(query, {"case_id": case_id})
    rows = result.fetchall()
    actions: List[CaseAction] = []
    for row in rows:
        actions.append(
            CaseAction(
                from_state=(CaseState(row.from_state) if row.from_state else None),
                to_state=CaseState(row.to_state),
                triggered_at=row.transitioned_at,
                triggered_by=row.triggered_by,
                reason=row.reason or "",
            )
        )
    return actions


async def _row_to_case(
    db,
    row,
    links_by_hyp: Optional[Dict[str, builtins.list[HypothesisEvidenceLink]]] = None,
) -> Case:
    """Reconstruct Case domain object from a SELECT row.

    Post-redesign: ``description``, ``investigation_strategy``,
    ``current_turn``, ``turns_without_progress``, ``closure_reason``,
    ``last_activity_at``, ``resolved_at``, ``closed_at`` all come
    directly from first-class columns. ``is_archived`` /
    ``archived_at`` are gone. Hypothesis evidence_links come from the
    ``hypothesis_evidence`` junction table (the JSON blob is gone),
    loaded by the caller and passed in via ``links_by_hyp``.

    Evidence is NOT populated here — the caller (``get`` /
    ``list``) loads it separately via ``_load_evidence_for_case`` /
    bulk equivalent.
    """
    if links_by_hyp is None:
        links_by_hyp = {}

    def _maybe_load(value: Any) -> Any:
        """JSONB columns arrive as Python objects on PG; legacy
        TEXT rows arrive as JSON-serialized strings. Accept both."""
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)

    inquiry_data = _maybe_load(row.inquiry) or {}
    inquiry = InquiryData(**inquiry_data) if inquiry_data else InquiryData()
    problem_verification = (
        ProblemVerification(**_maybe_load(row.problem_verification))
        if row.problem_verification
        else None
    )
    working_conclusion = (
        WorkingConclusion(**_maybe_load(row.working_conclusion))
        if row.working_conclusion
        else None
    )
    root_cause_conclusion = (
        RootCauseConclusion(**_maybe_load(row.root_cause_conclusion))
        if row.root_cause_conclusion
        else None
    )
    escalation_state = (
        EscalationState(**_maybe_load(row.escalation_state))
        if row.escalation_state
        else None
    )
    documentation = (
        DocumentationData(**_maybe_load(row.documentation))
        if row.documentation
        else DocumentationData()
    )
    progress = (
        InvestigationProgress(**_maybe_load(row.progress))
        if row.progress
        else InvestigationProgress()
    )

    # Parse aggregated JSON sub-collections.
    hypotheses_payload = _maybe_load(row.hypotheses_data) or []
    solutions_payload = _maybe_load(row.solutions_data) or []
    uploaded_files_payload = _maybe_load(row.uploaded_files_data) or []
    messages_payload = _maybe_load(row.messages_data) or []

    # Hydrate hypothesis_evidence links onto each hypothesis.
    hypotheses_dict: Dict[str, Hypothesis] = {}
    for h in hypotheses_payload:
        hyp_id = h["hypothesis_id"]
        h["evidence_links"] = links_by_hyp.get(hyp_id, [])
        hypotheses_dict[hyp_id] = Hypothesis(**h)

    solutions_list = [Solution(**s) for s in solutions_payload]
    uploaded_files = [UploadedFile(**f) for f in uploaded_files_payload]

    # Promoted columns: read directly from the row.
    metadata = _maybe_load(getattr(row, "metadata", None)) or {}

    # ``description`` is now a first-class column. Auto-heal the
    # legacy case where an INVESTIGATING row lost its description
    # (rare; pre-redesign rows that fell through the migration).
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

    # Hydrate the audit trail. PG's _row_to_case is async and is used
    # by both get() and list() (list calls get() per case_id), so the
    # extra round-trip is acceptable at list granularity.
    actions_data = await _load_case_actions(db, row.case_id)

    case_data: Dict[str, Any] = {
        "case_id": row.case_id,
        "user_id": row.user_id,
        "enterprise_id": row.enterprise_id,
        "organization_id": row.organization_id,
        "source": getattr(row, "source", "copilot"),
        "title": row.title,
        "state": CaseState(row.state),
        "action_history": actions_data,
        "closure_reason": row.closure_reason,
        "disposition_eligibility": (
            (
                json.loads(row.disposition_eligibility)
                if isinstance(row.disposition_eligibility, str)
                else row.disposition_eligibility
            )
            if getattr(row, "disposition_eligibility", None)
            else None
        ),
        "pending_transition": metadata.get("pending_transition"),
        "last_suggestions": metadata.get("last_suggestions"),
        # Pre-fetched runbooks (the KB push channel, fm#1360).
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
        "messages": messages_payload,
        "working_conclusion": working_conclusion,
        "root_cause_conclusion": root_cause_conclusion,
        "escalation_state": escalation_state,
        "documentation": documentation,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "version": (
            int(row.version)
            if hasattr(row, "version") and row.version is not None
            else 1
        ),
    }

    if description:
        case_data["description"] = description

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
