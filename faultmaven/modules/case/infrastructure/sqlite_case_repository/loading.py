"""SQLite loaders that hydrate a `Case` aggregate's sub-collections (hypotheses, hypothesis-evidence links, solutions, causal-graph nodes and edges, uploaded files, messages, evidence, evidence needs, case actions) from the database, including the bulk variants used when several cases are loaded at once."""

import builtins
import json
import logging
from datetime import UTC, datetime
from typing import Any, Dict, List

from sqlalchemy import text

from faultmaven.modules.case.contracts import (
    Case,
    CaseAction,
    CaseState,
    CausalEdge,
    CausalNode,
    Evidence,
    EvidenceNeed,
    EvidenceStance,
    HypothesisCategory,
    HypothesisEvidenceLink,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ValidationMethod,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository.rows import (
    _RELATIONSHIP_TO_STANCE,
    _bind_ids,
    _row_to_evidence,
    _row_to_evidence_need,
)


async def _load_hypotheses(db, case_id: str) -> list[dict]:
    """Load hypotheses for a case.

    Returns dicts keyed by hypothesis_id with ``evidence_links`` set to
    a List[HypothesisEvidenceLink] hydrated from the
    ``hypothesis_evidence`` junction table (not the dropped
    ``hypotheses.evidence_links`` JSON blob).
    """
    query = text("""
            SELECT hypothesis_id, statement, state, likelihood, initial_likelihood,
                   generated_at_turn, last_updated_turn, last_progress_at_turn,
                   iterations_without_progress,
                   category, generation_mode, rationale, retirement_reason,
                   refutation_reason,
                   tested_at, concluded_at, proposed_at, updated_at, metadata,
                   root_node_id, path
            FROM hypotheses
            WHERE case_id = :case_id
        """)
    result = await db.execute(query, {"case_id": case_id})
    rows = result.fetchall()

    hypothesis_ids = [row[0] for row in rows]
    links_by_hyp = await _load_hypothesis_evidence_links(db, hypothesis_ids)

    hypotheses = []
    for row in rows:
        hypotheses.append(
            {
                "hypothesis_id": row[0],
                "statement": row[1],
                "state": row[2],
                "likelihood": row[3],
                "initial_likelihood": row[4],
                "generated_at_turn": row[5] or 0,
                "last_updated_turn": row[6],
                "last_progress_at_turn": row[7],
                "iterations_without_progress": row[8],
                "category": row[9],
                "generation_mode": row[10],
                "rationale": row[11],
                "retirement_reason": row[12],
                "refutation_reason": row[13],
                "evidence_links": links_by_hyp.get(row[0], []),
                "tested_at": row[14],
                "concluded_at": row[15],
                "proposed_at": row[16],
                "updated_at": row[17],
                "metadata": json.loads(row[18]) if row[18] else {},
                "root_node_id": row[19],
                "path": json.loads(row[20]) if row[20] else [],
            }
        )
    return hypotheses


async def _load_hypothesis_evidence_links(
    db, hypothesis_ids: builtins.list[str]
) -> dict[str, builtins.list[HypothesisEvidenceLink]]:
    """Load junction-table rows and return them as
    ``{hypothesis_id: [HypothesisEvidenceLink, ...]}``.

    Empty input returns ``{}``. Hypotheses with no links are absent
    from the result (callers default to an empty list per hypothesis).
    """
    if not hypothesis_ids:
        return {}
    params: dict[str, Any] = {}
    placeholders = _bind_ids(params, hypothesis_ids)
    # Reuse _bind_ids for hypothesis_ids by re-keying — _bind_ids
    # produces ``cid_<i>`` keys but we just need any unique placeholders.
    query = text(f"""
            SELECT hypothesis_id, evidence_id, relationship_type, confidence,
                   linked_at_turn, created_at
            FROM hypothesis_evidence
            WHERE hypothesis_id IN ({placeholders})
        """)
    result = await db.execute(query, params)
    rows = result.fetchall()

    by_hyp: dict[str, builtins.list[HypothesisEvidenceLink]] = {}
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
                analyzed_at = datetime.now(UTC)
        elif analyzed_at is None:
            analyzed_at = datetime.now(UTC)
        link = HypothesisEvidenceLink(
            hypothesis_id=str(hyp_id),
            evidence_id=str(row[1]),
            stance=stance,
            # ``reasoning`` is required on the Pydantic model but the
            # junction table doesn't carry the LLM's free-text rationale —
            # it lives on case_messages / agent reasoning logs. Persist
            # an empty marker; the Pydantic validator allows any string.
            reasoning="",
            stance_confidence=max(0.0, min(1.0, confidence)),
            analyzed_at=analyzed_at,
        )
        by_hyp.setdefault(str(hyp_id), []).append(link)
    return by_hyp


async def _load_solutions(db, case_id: str) -> list[dict]:
    """Load solutions for a case.

    Selects the full audit trail (proposed_by, applied_at/by,
    verified_at, verification_method, verification_evidence_id,
    effectiveness) so ``Solution(**s)`` reconstruction is faithful
    to what was persisted. Pre-009 this loader returned only a
    subset, leaving every audit field at its Pydantic default.
    """
    query = text("""
            SELECT solution_id, solution_type, title, immediate_action, longterm_fix,
                   implementation_steps, commands, risks,
                   proposed_at, proposed_by,
                   applied_at, applied_by,
                   verified_at, verification_method,
                   verification_evidence_id, effectiveness,
                   node_id, quadrant
            FROM solutions
            WHERE case_id = :case_id
        """)
    result = await db.execute(query, {"case_id": case_id})
    rows = result.fetchall()

    solutions = []
    for row in rows:
        solutions.append(
            {
                "solution_id": row[0],
                "solution_type": row[1] or "other",
                "title": row[2] or "Untitled solution",
                "immediate_action": row[3],
                "longterm_fix": row[4],
                "implementation_steps": json.loads(row[5]) if row[5] else [],
                "commands": json.loads(row[6]) if row[6] else [],
                "risks": json.loads(row[7]) if row[7] else [],
                "proposed_at": row[8],
                "proposed_by": row[9],
                "applied_at": row[10],
                "applied_by": row[11],
                "verified_at": row[12],
                "verification_method": row[13],
                "verification_evidence_id": row[14],
                "effectiveness": row[15],
                "node_id": row[16],
                "quadrant": row[17],
            }
        )
    return solutions


async def _load_node_evidence_links(
    db, node_ids: builtins.list[str]
) -> dict[str, builtins.list[NodeEvidenceLink]]:
    """Load causal_node_evidence rows as ``{node_id: [NodeEvidenceLink]}``.
    Stance is stored verbatim (supports/refutes/neutral)."""
    if not node_ids:
        return {}
    params: dict[str, Any] = {}
    placeholders = _bind_ids(params, node_ids)
    query = text(f"""
            SELECT node_id, evidence_id, stance, stance_confidence,
                   reasoning, linked_at_turn, created_at
            FROM causal_node_evidence
            WHERE node_id IN ({placeholders})
        """)
    result = await db.execute(query, params)
    by_node: dict[str, builtins.list[NodeEvidenceLink]] = {}
    for row in result.fetchall():
        nid = str(row[0])
        analyzed_at = row[6]
        if isinstance(analyzed_at, str):
            try:
                analyzed_at = datetime.fromisoformat(analyzed_at.replace(" ", "T"))
            except ValueError:
                analyzed_at = datetime.now(UTC)
        elif analyzed_at is None:
            analyzed_at = datetime.now(UTC)
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
    """Load the case's causal graph (nodes + edges) + node-scoped evidence."""
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

    nodes: dict[str, CausalNode] = {}
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
            proposed_at=r[16] or datetime.now(UTC),
            updated_at=r[17] or datetime.now(UTC),
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
            created_at=r[6] or datetime.now(UTC),
        )
        for r in edge_rows
    ]


async def _load_uploaded_files(db, case_id: str) -> list[dict]:
    """Load uploaded files for a case.

    Schema columns: ``file_id``, ``filename``, ``size_bytes``,
    ``content_type`` (MIME), ``content_hash``, ``storage_ref``,
    ``upload_source``, ``uploaded_at_turn``, ``uploaded_at``,
    ``uploaded_by``, plus the preprocessing artifacts (``summary``,
    ``structural_index``, ``data_type``, ``coverage_start_ts``,
    ``coverage_end_ts``).
    """
    query = text("""
            SELECT file_id, filename, size_bytes, content_type, content_hash,
                   storage_ref, upload_source, uploaded_at_turn, uploaded_at,
                   uploaded_by,
                   summary, structural_index, data_type,
                   coverage_start_ts, coverage_end_ts, coverage_source
            FROM uploaded_files
            WHERE case_id = :case_id
        """)
    result = await db.execute(query, {"case_id": case_id})
    rows = result.fetchall()

    if not rows:
        return []

    columns = list(result.keys())
    files = []
    for row in rows:
        row_dict = dict(zip(columns, row))
        files.append(
            {
                "file_id": row_dict.get("file_id"),
                "filename": row_dict.get("filename"),
                "size_bytes": row_dict.get("size_bytes", 0),
                "content_type": row_dict.get("content_type"),
                "content_hash": row_dict.get("content_hash"),
                "storage_ref": row_dict.get("storage_ref"),
                "upload_source": row_dict.get("upload_source", "file_upload"),
                "uploaded_at_turn": row_dict.get("uploaded_at_turn", 0),
                "uploaded_at": row_dict.get("uploaded_at"),
                "uploaded_by": row_dict.get("uploaded_by"),
                "summary": row_dict.get("summary"),
                "structural_index": row_dict.get("structural_index"),
                "data_type": row_dict.get("data_type"),
                "coverage_start_ts": row_dict.get("coverage_start_ts"),
                "coverage_end_ts": row_dict.get("coverage_end_ts"),
                "coverage_source": row_dict.get("coverage_source"),
            }
        )
    return files


async def _load_messages(db, case_id: str) -> list[dict]:
    """Load messages for a case from case_messages table.

    Schema per design spec (case-schema.md §4.7):
    - message_id, turn_number, role, content, created_at, token_count,
      metadata, author_id
    """
    query = text("""
            SELECT message_id, turn_number, role, content, created_at, token_count, metadata,
                   author_id
            FROM case_messages
            WHERE case_id = :case_id
            ORDER BY created_at ASC, turn_number ASC
        """)
    result = await db.execute(query, {"case_id": case_id})
    rows = result.fetchall()

    if not rows:
        return []

    columns = list(result.keys())
    messages = []
    for row in rows:
        row_dict = dict(zip(columns, row))

        msg_timestamp = row_dict.get("created_at")
        if msg_timestamp:
            if isinstance(msg_timestamp, str):
                msg_timestamp = msg_timestamp.replace(" ", "T")
            elif hasattr(msg_timestamp, "isoformat"):
                msg_timestamp = msg_timestamp.isoformat()

        metadata = row_dict.get("metadata")
        if isinstance(metadata, str):
            metadata = json.loads(metadata) if metadata else {}
        elif metadata is None:
            metadata = {}

        messages.append(
            {
                "message_id": row_dict.get("message_id"),
                "turn_number": row_dict.get("turn_number", 0),
                "role": row_dict.get("role"),
                "content": row_dict.get("content"),
                "created_at": msg_timestamp,
                "token_count": row_dict.get("token_count"),
                "metadata": metadata,
                "author_id": row_dict.get("author_id"),
            }
        )
    return messages


async def _load_evidence_for_case(db, case: Case) -> None:
    """Load investigation evidence from the evidence table.

    Columns selected (in this fixed order, consumed positionally by
    ``_row_to_evidence``): ``evidence_id``, ``category``,
    ``source_type``, ``summary``, ``extract``, ``is_primary``,
    ``reliability_score``, ``tags``, ``collected_at_turn``,
    ``source_file_id``, ``vectorized``, ``coverage_start_ts``,
    ``coverage_end_ts``, ``metadata``, ``created_at``,
    ``primary_purpose``, ``analysis``, ``processing_mode``,
    ``advances_milestones``, ``collected_by``.
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
        logging.getLogger(__name__).warning(
            "Failed to load evidence for case %s: %s",
            case.case_id,
            e,
        )


async def _load_evidence_needs_for_case(db, case: Case) -> None:
    """Load evidence-need rows + their fulfillment junctions.

    Hydrates ``case.evidence_needs`` with the persisted pool. The
    fulfilling-evidence list comes from the junction; the need's
    own row doesn't store it. The pair is loaded together so each
    ``EvidenceNeed`` is fully reconstructed before being attached
    to the case.
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

        # Load all junctions for this case's needs in one query.
        need_ids = [row[0] for row in need_rows]
        params: Dict[str, Any] = {}
        placeholder_names: List[str] = []
        for i, nid in enumerate(need_ids):
            key = f"nid_{i}"
            params[key] = nid
            placeholder_names.append(f":{key}")
        placeholders = ", ".join(placeholder_names)
        junction_query = text(f"""
                SELECT need_id, evidence_id
                FROM evidence_need_fulfillment
                WHERE need_id IN ({placeholders})
            """)
        junction_rows = (await db.execute(junction_query, params)).fetchall()

        fulfillments_by_need: Dict[str, List[str]] = {}
        for nid, eid in junction_rows:
            fulfillments_by_need.setdefault(nid, []).append(eid)

        needs: List[EvidenceNeed] = []
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
        logging.getLogger(__name__).warning(
            "Failed to load evidence_needs for case %s: %s",
            case.case_id,
            e,
        )


async def _load_hypotheses_bulk(
    db, case_ids: builtins.list[str]
) -> dict[str, builtins.list[dict]]:
    if not case_ids:
        return {}
    params: dict[str, Any] = {}
    placeholders = _bind_ids(params, case_ids)
    query = text(f"""
            SELECT case_id, hypothesis_id, statement, state, likelihood,
                   initial_likelihood, generated_at_turn, last_updated_turn,
                   last_progress_at_turn, iterations_without_progress,
                   category, generation_mode, rationale, retirement_reason,
                   refutation_reason, tested_at, concluded_at,
                   proposed_at, updated_at, metadata
            FROM hypotheses
            WHERE case_id IN ({placeholders})
        """)
    rows = (await db.execute(query, params)).fetchall()

    # Hydrate evidence_links from the junction table for every hypothesis
    # we just loaded, in one round-trip.
    all_hyp_ids = [row[1] for row in rows]
    links_by_hyp = await _load_hypothesis_evidence_links(db, all_hyp_ids)

    by_case: dict[str, builtins.list[dict]] = {cid: [] for cid in case_ids}
    for row in rows:
        by_case.setdefault(row[0], []).append(
            {
                "hypothesis_id": row[1],
                "statement": row[2],
                "state": row[3],
                "likelihood": row[4],
                "initial_likelihood": row[5],
                "generated_at_turn": row[6] or 0,
                "last_updated_turn": row[7],
                "last_progress_at_turn": row[8],
                "iterations_without_progress": row[9],
                "category": row[10],
                "generation_mode": row[11],
                "rationale": row[12],
                "retirement_reason": row[13],
                "refutation_reason": row[14],
                "evidence_links": links_by_hyp.get(row[1], []),
                "tested_at": row[15],
                "concluded_at": row[16],
                "proposed_at": row[17],
                "updated_at": row[18],
                "metadata": json.loads(row[19]) if row[19] else {},
            }
        )
    return by_case


async def _load_solutions_bulk(
    db, case_ids: builtins.list[str]
) -> dict[str, builtins.list[dict]]:
    if not case_ids:
        return {}
    params: dict[str, Any] = {}
    placeholders = _bind_ids(params, case_ids)
    query = text(f"""
            SELECT case_id, solution_id, solution_type, title, immediate_action,
                   longterm_fix, implementation_steps, commands, risks,
                   proposed_at, proposed_by,
                   applied_at, applied_by,
                   verified_at, verification_method,
                   verification_evidence_id, effectiveness
            FROM solutions
            WHERE case_id IN ({placeholders})
        """)
    rows = (await db.execute(query, params)).fetchall()

    by_case: dict[str, builtins.list[dict]] = {cid: [] for cid in case_ids}
    for row in rows:
        by_case.setdefault(row[0], []).append(
            {
                "solution_id": row[1],
                "solution_type": row[2] or "other",
                "title": row[3] or "Untitled solution",
                "immediate_action": row[4],
                "longterm_fix": row[5],
                "implementation_steps": json.loads(row[6]) if row[6] else [],
                "commands": json.loads(row[7]) if row[7] else [],
                "risks": json.loads(row[8]) if row[8] else [],
                "proposed_at": row[9],
                "proposed_by": row[10],
                "applied_at": row[11],
                "applied_by": row[12],
                "verified_at": row[13],
                "verification_method": row[14],
                "verification_evidence_id": row[15],
                "effectiveness": row[16],
            }
        )
    return by_case


async def _load_uploaded_files_bulk(
    db, case_ids: builtins.list[str]
) -> dict[str, builtins.list[dict]]:
    if not case_ids:
        return {}
    params: dict[str, Any] = {}
    placeholders = _bind_ids(params, case_ids)
    query = text(f"""
            SELECT case_id, file_id, filename, size_bytes, content_type,
                   content_hash, storage_ref, upload_source, uploaded_at_turn,
                   uploaded_at, uploaded_by,
                   summary, structural_index, data_type,
                   coverage_start_ts, coverage_end_ts, coverage_source
            FROM uploaded_files
            WHERE case_id IN ({placeholders})
        """)
    result = await db.execute(query, params)
    rows = result.fetchall()

    by_case: dict[str, builtins.list[dict]] = {cid: [] for cid in case_ids}
    for row in rows:
        by_case.setdefault(row[0], []).append(
            {
                "file_id": row[1],
                "filename": row[2],
                "size_bytes": row[3] or 0,
                "content_type": row[4],
                "content_hash": row[5],
                "storage_ref": row[6],
                "upload_source": row[7] or "file_upload",
                "uploaded_at_turn": row[8] or 0,
                "uploaded_at": row[9],
                "uploaded_by": row[10],
                "summary": row[11],
                "structural_index": row[12],
                "data_type": row[13],
                "coverage_start_ts": row[14],
                "coverage_end_ts": row[15],
                "coverage_source": row[16],
            }
        )
    return by_case


async def _load_messages_bulk(
    db, case_ids: builtins.list[str]
) -> dict[str, builtins.list[dict]]:
    if not case_ids:
        return {}
    params: dict[str, Any] = {}
    placeholders = _bind_ids(params, case_ids)
    query = text(f"""
            SELECT case_id, message_id, turn_number, role, content,
                   created_at, token_count, metadata, author_id
            FROM case_messages
            WHERE case_id IN ({placeholders})
            ORDER BY created_at ASC, turn_number ASC
        """)
    rows = (await db.execute(query, params)).fetchall()

    by_case: dict[str, builtins.list[dict]] = {cid: [] for cid in case_ids}
    for row in rows:
        msg_timestamp = row[5]
        if msg_timestamp:
            if isinstance(msg_timestamp, str):
                msg_timestamp = msg_timestamp.replace(" ", "T")
            elif hasattr(msg_timestamp, "isoformat"):
                msg_timestamp = msg_timestamp.isoformat()

        metadata_raw = row[7]
        if isinstance(metadata_raw, str):
            parsed_metadata = json.loads(metadata_raw) if metadata_raw else {}
        elif metadata_raw is None:
            parsed_metadata = {}
        else:
            parsed_metadata = metadata_raw

        by_case.setdefault(row[0], []).append(
            {
                "message_id": row[1],
                "turn_number": row[2] or 0,
                "role": row[3],
                "content": row[4],
                "created_at": msg_timestamp,
                "token_count": row[6],
                "metadata": parsed_metadata,
                "author_id": row[8],
            }
        )
    return by_case


async def _load_evidence_for_cases_bulk(db, cases: builtins.list[Case]) -> None:
    """Hydrate ``Case.evidence`` on every case in ``cases`` with one
    SELECT. Failures on individual rows are logged and skipped so one
    bad evidence row doesn't blank the whole list.
    """
    if not cases:
        return
    case_ids = [c.case_id for c in cases]
    params: dict[str, Any] = {}
    placeholders = _bind_ids(params, case_ids)
    try:
        query = text(f"""
                SELECT
                    evidence_id, case_id, category, source_type,
                    summary, extract,
                    is_primary, reliability_score, tags,
                    collected_at_turn, source_file_id, vectorized,
                    coverage_start_ts, coverage_end_ts,
                    metadata, created_at,
                    primary_purpose, analysis, processing_mode,
                    advances_milestones, collected_by,
                    coverage_source
                FROM evidence
                WHERE case_id IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT 1000
            """)
        rows = (await db.execute(query, params)).fetchall()

        by_case: dict[str, builtins.list[Evidence]] = {cid: [] for cid in case_ids}
        for row in rows:
            # Bulk row order is offset by one column (case_id at index 1).
            # _row_to_evidence expects the per-case shape; remap by skipping
            # case_id when calling.
            ev_row = (
                row[0],  # evidence_id
                row[2],  # category
                row[3],  # source_type
                row[4],  # summary
                row[5],  # extract
                row[6],  # is_primary
                row[7],  # reliability_score
                row[8],  # tags
                row[9],  # collected_at_turn
                row[10],  # source_file_id
                row[11],  # vectorized
                row[12],  # coverage_start_ts
                row[13],  # coverage_end_ts
                row[14],  # metadata
                row[15],  # created_at
                row[16],  # primary_purpose
                row[17],  # analysis
                row[18],  # processing_mode
                row[19],  # advances_milestones
                row[20],  # collected_by
                row[21],  # coverage_source
            )
            ev = _row_to_evidence(ev_row)
            if ev is not None:
                by_case.setdefault(row[1], []).append(ev)
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "Failed to bulk-load evidence for %d cases: %s", len(cases), e
        )
        return

    for case in cases:
        case.evidence = by_case.get(case.case_id, [])


async def _load_case_actions(db, case_id: str) -> builtins.list[CaseAction]:
    """Hydrate the audit trail for a case from ``case_actions``.

    Replaces the prior write-only pattern (``action_history=[]`` hardcoded
    in ``_to_domain``). Rows are returned ordered oldest-first to match
    the in-memory append order.
    """
    query = text("""
            SELECT from_state, to_state, reason, triggered_by, transitioned_at
            FROM case_actions
            WHERE case_id = :case_id
            ORDER BY transitioned_at ASC, transition_id ASC
        """)
    result = await db.execute(query, {"case_id": case_id})
    rows = result.fetchall()
    actions: builtins.list[CaseAction] = []
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
