"""SQLite upsert helpers that persist a `Case` aggregate's sub-collections (the case record itself, evidence, evidence needs, hypotheses and their evidence links, causal-graph nodes/edges, solutions, uploaded files, messages) back to the database, plus causal-graph reconciliation and case-action append."""

import builtins
import json
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import bindparam, text

from faultmaven.modules.case.contracts import (
    Case,
    CaseAction,
    CausalEdge,
    CausalNode,
    Evidence,
    EvidenceNeed,
    Hypothesis,
    HypothesisEvidenceLink,
    NodeEvidenceLink,
    Solution,
    UploadedFile,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure.sqlite_case_repository.rows import (
    _STANCE_TO_RELATIONSHIP,
    _case_record_params,
    _derive_solution_state,
    _serialize_tags,
)


async def _upsert_case_record(db, case: Case) -> None:
    """Upsert main cases table (SQLite-compatible - no type casts).

    Optimistic concurrency control: first attempts an UPDATE with a
    version predicate; raises StaleCaseException on version mismatch;
    falls back to INSERT when no row exists for ``case_id``. The
    in-memory ``case.version`` is bumped on successful update so
    subsequent saves within the same flow work without reloading.

    ``closure_reason``, ``last_activity_at``, ``resolved_at`` and
    ``closed_at`` live in first-class columns; ``last_activity_at``
    is bumped to the current UTC time on every save so staleness
    queries can run without scanning JSON. The ``metadata`` JSON
    column carries transient runtime state that has no first-class
    column yet (``message_count``, ``pending_transition``,
    ``proposed_actions``, ``action_attempts``, ``turn_history``).
    """
    last_activity_at = datetime.now(UTC)
    params = _case_record_params(case, last_activity_at)

    # Step 1: attempt UPDATE with version check.
    update_query = text("""
            UPDATE cases SET
                user_id = :user_id,
                enterprise_id = :enterprise_id,
                organization_id = :organization_id,
                title = :title,
                description = :description,
                state = :state,
                source = :source,
                investigation_strategy = :investigation_strategy,
                current_turn = :current_turn,
                turns_without_progress = :turns_without_progress,
                updated_at = :updated_at,
                closure_reason = :closure_reason,
                last_activity_at = :last_activity_at,
                resolved_at = :resolved_at,
                closed_at = :closed_at,
                disposition_eligibility = :disposition_eligibility,
                inquiry = :inquiry,
                problem_verification = :problem_verification,
                working_conclusion = :working_conclusion,
                root_cause_conclusion = :root_cause_conclusion,
                escalation_state = :escalation_state,
                documentation = :documentation,
                progress = :progress,
                metadata = :metadata,
                version = :new_version
            WHERE case_id = :case_id AND version = :expected_version
        """)
    expected_version = case.version
    new_version = expected_version + 1
    update_params = {
        **params,
        "expected_version": expected_version,
        "new_version": new_version,
    }
    result = await db.execute(update_query, update_params)

    if result.rowcount > 0:
        case.version = new_version
        return

    # Step 2: UPDATE matched no rows — either the case is new, or the
    # version predicate failed. One SELECT to disambiguate.
    probe = await db.execute(
        text("SELECT version FROM cases WHERE case_id = :case_id"),
        {"case_id": case.case_id},
    )
    row = probe.fetchone()
    if row is None:
        # New case — INSERT with version = 1.
        insert_query = text("""
                INSERT INTO cases (
                    case_id, user_id, enterprise_id, organization_id, title, description,
                    state, source, investigation_strategy, current_turn,
                    turns_without_progress, created_at, updated_at,
                    closure_reason, last_activity_at, resolved_at, closed_at,
                    disposition_eligibility,
                    inquiry, problem_verification, working_conclusion,
                    root_cause_conclusion,
                    escalation_state, documentation, progress, metadata,
                    version
                ) VALUES (
                    :case_id, :user_id, :enterprise_id, :organization_id, :title, :description,
                    :state, :source, :investigation_strategy, :current_turn,
                    :turns_without_progress, :created_at, :updated_at,
                    :closure_reason, :last_activity_at, :resolved_at, :closed_at,
                    :disposition_eligibility,
                    :inquiry, :problem_verification, :working_conclusion,
                    :root_cause_conclusion,
                    :escalation_state, :documentation, :progress, :metadata,
                    1
                )
            """)
        await db.execute(insert_query, params)
        case.version = 1
        return

    # Row exists but version mismatched — caller holds stale state.
    raise StaleCaseException(
        case_id=case.case_id,
        expected_version=expected_version,
        actual_version=row[0],
    )


async def _upsert_evidence(
    db,
    case_id: str,
    evidence_list: builtins.list[Evidence],
    enterprise_id: str,
    organization_id: Optional[str],
) -> None:
    """Upsert evidence records.

    Purely additive: inserts new rows and updates existing ones keyed by
    evidence_id. Does NOT remove rows absent from `evidence_list`. The
    in-memory case is a working snapshot, not the canonical truth for
    which rows should exist — callers holding a stale snapshot (e.g.
    background tasks) must not be able to silently delete rows that
    other concurrent writers have added. For intentional removal, use
    `delete_evidence(case_id, evidence_id)` explicitly.

    File-level metadata (filename, content_type, content_hash, size,
    storage_ref) lives on ``uploaded_files`` and is reached via
    ``source_file_id``. Chat-extracted evidence
    (``source_type=USER_DESCRIPTION``) has ``source_file_id IS NULL``
    and persists no file metadata.
    """
    for evidence in evidence_list:
        query = text("""
                INSERT INTO evidence (
                    evidence_id, case_id, enterprise_id, organization_id, source_file_id,
                    category, source_type,
                    summary, extract,
                    primary_purpose, analysis, processing_mode, advances_milestones,
                    is_primary, reliability_score, tags,
                    collected_at_turn, collected_by, vectorized,
                    coverage_start_ts, coverage_end_ts, coverage_source,
                    metadata, created_at, updated_at
                ) VALUES (
                    :evidence_id, :case_id, :enterprise_id, :organization_id, :source_file_id,
                    :category, :source_type,
                    :summary, :extract,
                    :primary_purpose, :analysis, :processing_mode, :advances_milestones,
                    :is_primary, :reliability_score, :tags,
                    :collected_at_turn, :collected_by, :vectorized,
                    :coverage_start_ts, :coverage_end_ts, :coverage_source,
                    :metadata, :created_at, :updated_at
                )
                ON CONFLICT (evidence_id) DO UPDATE SET
                    source_file_id = EXCLUDED.source_file_id,
                    category = EXCLUDED.category,
                    source_type = EXCLUDED.source_type,
                    summary = EXCLUDED.summary,
                    extract = EXCLUDED.extract,
                    primary_purpose = EXCLUDED.primary_purpose,
                    analysis = EXCLUDED.analysis,
                    processing_mode = EXCLUDED.processing_mode,
                    advances_milestones = EXCLUDED.advances_milestones,
                    is_primary = EXCLUDED.is_primary,
                    reliability_score = EXCLUDED.reliability_score,
                    tags = EXCLUDED.tags,
                    collected_at_turn = EXCLUDED.collected_at_turn,
                    collected_by = EXCLUDED.collected_by,
                    vectorized = EXCLUDED.vectorized,
                    coverage_start_ts = EXCLUDED.coverage_start_ts,
                    coverage_end_ts = EXCLUDED.coverage_end_ts,
                    coverage_source = EXCLUDED.coverage_source,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
            """)

        now = datetime.now(UTC)
        await db.execute(
            query,
            {
                "evidence_id": evidence.evidence_id,
                "case_id": case_id,
                "enterprise_id": enterprise_id,
                "organization_id": organization_id,
                "source_file_id": evidence.source_file_id,
                "category": evidence.category.value,
                "source_type": evidence.source_type.value,
                "summary": evidence.summary,
                "extract": evidence.extract,
                "primary_purpose": evidence.primary_purpose,
                "analysis": evidence.analysis,
                "processing_mode": evidence.processing_mode,
                # advances_milestones uses the same TagsArray storage
                # shape as tags — comma-encoded TEXT on SQLite,
                # TEXT[] on PG. The same _serialize_tags helper applies.
                "advances_milestones": _serialize_tags(
                    list(evidence.advances_milestones)
                ),
                "is_primary": 1 if evidence.is_primary else 0,
                "reliability_score": evidence.reliability_score,
                "tags": _serialize_tags(evidence.tags),
                "collected_at_turn": evidence.collected_at_turn,
                "collected_by": evidence.collected_by,
                "vectorized": 1 if evidence.vectorized else 0,
                "coverage_start_ts": (
                    evidence.coverage_start_ts.isoformat()
                    if evidence.coverage_start_ts
                    else None
                ),
                "coverage_source": evidence.coverage_source,
                "coverage_end_ts": (
                    evidence.coverage_end_ts.isoformat()
                    if evidence.coverage_end_ts
                    else None
                ),
                "metadata": json.dumps(evidence.metadata or {}),
                "created_at": evidence.collected_at or now,
                "updated_at": now,
            },
        )


async def _upsert_evidence_needs(
    db,
    case_id: str,
    needs_list: builtins.list[EvidenceNeed],
    enterprise_id: str,
    organization_id: Optional[str],
    current_turn: int,
) -> None:
    """Upsert evidence-need records + their fulfillment junction rows.

    Purely additive — see ``_upsert_evidence`` for rationale. A stale
    Case snapshot must not be able to silently delete needs another
    concurrent writer added; intentional removal of a need would
    require an explicit ``delete_evidence_need`` method (not yet
    defined, no caller).

    The fulfillment junction is rebuilt per-need from
    ``need.fulfilling_evidence_ids``: existing junction rows whose
    ``evidence_id`` is no longer in the list are kept (additive
    only), and new pairs are inserted with ``INSERT OR IGNORE`` so
    the original ``linked_at_turn`` is preserved on re-save.

    Must run AFTER ``_upsert_evidence`` so the junction's FK to
    ``evidence.evidence_id`` is satisfied.
    """
    for need in needs_list:
        query = text("""
                INSERT INTO evidence_needs (
                    need_id, case_id, enterprise_id, organization_id,
                    purpose, request_text, rationale,
                    priority, state,
                    motivating_hypothesis_ids,
                    superseded_reason,
                    created_at_turn, created_at, updated_at,
                    obtainability, surfaced_turns, engine_inferred
                ) VALUES (
                    :need_id, :case_id, :enterprise_id, :organization_id,
                    :purpose, :request_text, :rationale,
                    :priority, :state,
                    :motivating_hypothesis_ids,
                    :superseded_reason,
                    :created_at_turn, :created_at, :updated_at,
                    :obtainability, :surfaced_turns, :engine_inferred
                )
                ON CONFLICT (need_id) DO UPDATE SET
                    purpose = EXCLUDED.purpose,
                    request_text = EXCLUDED.request_text,
                    rationale = EXCLUDED.rationale,
                    priority = EXCLUDED.priority,
                    state = EXCLUDED.state,
                    motivating_hypothesis_ids = EXCLUDED.motivating_hypothesis_ids,
                    superseded_reason = EXCLUDED.superseded_reason,
                    updated_at = EXCLUDED.updated_at,
                    obtainability = EXCLUDED.obtainability,
                    surfaced_turns = EXCLUDED.surfaced_turns,
                    engine_inferred = EXCLUDED.engine_inferred
            """)

        now = datetime.now(UTC)
        await db.execute(
            query,
            {
                "need_id": need.need_id,
                "case_id": case_id,
                "enterprise_id": enterprise_id,
                "organization_id": organization_id,
                "purpose": need.purpose.value,
                "request_text": need.request_text,
                "rationale": need.rationale,
                "priority": need.priority.value,
                "state": need.state.value,
                "motivating_hypothesis_ids": json.dumps(need.motivating_hypothesis_ids),
                "superseded_reason": need.superseded_reason,
                "created_at_turn": need.created_at_turn,
                "created_at": need.created_at or now,
                "updated_at": now,
                "obtainability": need.obtainability.value,
                "surfaced_turns": json.dumps(need.surfaced_turns),
                "engine_inferred": need.engine_inferred,
            },
        )

        # Junction rows — additive, preserve original linked_at_turn.
        if need.fulfilling_evidence_ids:
            junction_query = text("""
                    INSERT OR IGNORE INTO evidence_need_fulfillment (
                        need_id, evidence_id, enterprise_id, organization_id, linked_at_turn
                    ) VALUES (
                        :need_id, :evidence_id, :enterprise_id, :organization_id, :linked_at_turn
                    )
                """)
            for evidence_id in need.fulfilling_evidence_ids:
                await db.execute(
                    junction_query,
                    {
                        "need_id": need.need_id,
                        "evidence_id": evidence_id,
                        "enterprise_id": enterprise_id,
                        "organization_id": organization_id,
                        "linked_at_turn": current_turn,
                    },
                )


async def _upsert_hypotheses(
    db,
    case_id: str,
    hypotheses_dict: dict[str, Hypothesis],
    enterprise_id: str,
    organization_id: Optional[str],
) -> None:
    """Upsert hypotheses records.

    Purely additive — see `_upsert_evidence` for rationale. There is
    no concrete delete-hypothesis API on the case repo today; if a
    single-hypothesis remove path is needed, add it explicitly.

    The dropped ``hypotheses.evidence_links`` JSON blob has been
    replaced by the ``hypothesis_evidence`` junction table; that
    upsert runs after the parent row is in place so FK constraints
    are satisfied.
    """
    for hypothesis_id, hypothesis in hypotheses_dict.items():
        query = text("""
                INSERT INTO hypotheses (
                    hypothesis_id, case_id, enterprise_id, organization_id, statement, state,
                    likelihood, initial_likelihood,
                    root_node_id, path,
                    generated_at_turn, last_updated_turn, last_progress_at_turn,
                    iterations_without_progress,
                    category, generation_mode, rationale, retirement_reason,
                    refutation_reason,
                    tested_at, concluded_at, proposed_at, updated_at, metadata,
                    created_by, updated_by
                ) VALUES (
                    :hypothesis_id, :case_id, :enterprise_id, :organization_id, :statement, :state,
                    :likelihood, :initial_likelihood,
                    :root_node_id, :path,
                    :generated_at_turn, :last_updated_turn, :last_progress_at_turn,
                    :iterations_without_progress,
                    :category, :generation_mode, :rationale, :retirement_reason,
                    :refutation_reason,
                    :tested_at, :concluded_at, :proposed_at, :updated_at, :metadata,
                    :created_by, :updated_by
                )
                ON CONFLICT (hypothesis_id) DO UPDATE SET
                    statement = EXCLUDED.statement,
                    state = EXCLUDED.state,
                    likelihood = EXCLUDED.likelihood,
                    root_node_id = EXCLUDED.root_node_id,
                    path = EXCLUDED.path,
                    generated_at_turn = EXCLUDED.generated_at_turn,
                    last_updated_turn = EXCLUDED.last_updated_turn,
                    last_progress_at_turn = EXCLUDED.last_progress_at_turn,
                    iterations_without_progress = EXCLUDED.iterations_without_progress,
                    retirement_reason = EXCLUDED.retirement_reason,
                    refutation_reason = EXCLUDED.refutation_reason,
                    concluded_at = EXCLUDED.concluded_at,
                    updated_at = EXCLUDED.updated_at,
                    metadata = EXCLUDED.metadata
            """)

        await db.execute(
            query,
            {
                "hypothesis_id": hypothesis_id,
                "case_id": case_id,
                "enterprise_id": enterprise_id,
                "organization_id": organization_id,
                "statement": hypothesis.statement,
                "state": hypothesis.state.value,
                "likelihood": hypothesis.likelihood,
                "initial_likelihood": hypothesis.initial_likelihood,
                "root_node_id": hypothesis.root_node_id,
                "path": json.dumps(list(hypothesis.path)),
                "generated_at_turn": hypothesis.generated_at_turn,
                "last_updated_turn": hypothesis.last_updated_turn,
                "last_progress_at_turn": hypothesis.last_progress_at_turn,
                "iterations_without_progress": hypothesis.iterations_without_progress,
                "category": hypothesis.category.value,
                "generation_mode": hypothesis.generation_mode.value,
                "rationale": hypothesis.rationale,
                "retirement_reason": hypothesis.retirement_reason,
                "refutation_reason": hypothesis.refutation_reason,
                "tested_at": hypothesis.tested_at,
                "concluded_at": hypothesis.concluded_at,
                "proposed_at": getattr(hypothesis, "proposed_at", None)
                or datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "metadata": json.dumps({}),
                "created_by": None,
                "updated_by": None,
            },
        )

        await _upsert_hypothesis_evidence(
            db,
            hypothesis_id,
            hypothesis.evidence_links,
            enterprise_id,
            organization_id,
        )


async def _upsert_hypothesis_evidence(
    db,
    hypothesis_id: str,
    links: builtins.list[HypothesisEvidenceLink],
    enterprise_id: str,
    organization_id: Optional[str],
) -> None:
    """Upsert rows on the ``hypothesis_evidence`` junction table.

    Purely additive — never deletes rows. Composite PK
    ``(hypothesis_id, evidence_id)`` makes the upsert idempotent.
    Stance → relationship_type mapping is in
    ``_STANCE_TO_RELATIONSHIP``; NEUTRAL maps to ``related`` because
    the junction CHECK constraint only allows
    ``('supports', 'refutes', 'related')``.
    """
    if not links:
        return
    query = text("""
            INSERT INTO hypothesis_evidence (
                hypothesis_id, evidence_id, enterprise_id, organization_id,
                relationship_type, confidence, linked_at_turn,
                linked_by, created_at
            ) VALUES (
                :hypothesis_id, :evidence_id, :enterprise_id, :organization_id,
                :relationship_type, :confidence, :linked_at_turn,
                :linked_by, :created_at
            )
            ON CONFLICT (hypothesis_id, evidence_id) DO UPDATE SET
                relationship_type = EXCLUDED.relationship_type,
                confidence = EXCLUDED.confidence,
                linked_at_turn = EXCLUDED.linked_at_turn,
                linked_by = EXCLUDED.linked_by
        """)

    for link in links:
        relationship = _STANCE_TO_RELATIONSHIP.get(link.stance, "related")
        await db.execute(
            query,
            {
                "hypothesis_id": hypothesis_id,
                "evidence_id": link.evidence_id,
                "enterprise_id": enterprise_id,
                "organization_id": organization_id,
                "relationship_type": relationship,
                "confidence": link.stance_confidence,
                # Domain ``HypothesisEvidenceLink`` doesn't carry a
                # turn number; the junction column is nullable.
                "linked_at_turn": None,
                # Linker user_id isn't tracked on the link object —
                # nullable column, FK SET NULL on user delete.
                "linked_by": None,
                "created_at": link.analyzed_at,
            },
        )


async def _upsert_causal_nodes(
    db,
    case_id: str,
    nodes_dict: dict[str, CausalNode],
    enterprise_id: str,
    organization_id: Optional[str],
) -> None:
    """Upsert causal-graph nodes; node-scoped evidence follows into the
    causal_node_evidence junction. Additive + idempotent on node_id."""
    for node_id, node in nodes_dict.items():
        query = text("""
                INSERT INTO causal_nodes (
                    node_id, case_id, enterprise_id, organization_id, statement,
                    node_type, node_state, validation_method, belief,
                    signature_consistent, actionable, category, state_epoch,
                    generated_at_turn, last_updated_turn, last_progress_at_turn,
                    iterations_without_progress, refutation_reason, rationale,
                    metadata, proposed_at, updated_at
                ) VALUES (
                    :node_id, :case_id, :enterprise_id, :organization_id, :statement,
                    :node_type, :node_state, :validation_method, :belief,
                    :signature_consistent, :actionable, :category, :state_epoch,
                    :generated_at_turn, :last_updated_turn, :last_progress_at_turn,
                    :iterations_without_progress, :refutation_reason, :rationale,
                    :metadata, :proposed_at, :updated_at
                )
                ON CONFLICT (node_id) DO UPDATE SET
                    statement = EXCLUDED.statement,
                    node_type = EXCLUDED.node_type,
                    node_state = EXCLUDED.node_state,
                    validation_method = EXCLUDED.validation_method,
                    belief = EXCLUDED.belief,
                    signature_consistent = EXCLUDED.signature_consistent,
                    actionable = EXCLUDED.actionable,
                    category = EXCLUDED.category,
                    state_epoch = EXCLUDED.state_epoch,
                    last_updated_turn = EXCLUDED.last_updated_turn,
                    last_progress_at_turn = EXCLUDED.last_progress_at_turn,
                    iterations_without_progress = EXCLUDED.iterations_without_progress,
                    refutation_reason = EXCLUDED.refutation_reason,
                    rationale = EXCLUDED.rationale,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
            """)
        await db.execute(
            query,
            {
                "node_id": node_id,
                "case_id": case_id,
                "enterprise_id": enterprise_id,
                "organization_id": organization_id,
                "statement": node.statement,
                "node_type": node.node_type.value,
                "node_state": node.node_state.value,
                "validation_method": node.validation_method.value,
                "belief": node.belief,
                "signature_consistent": node.signature_consistent,
                "actionable": node.actionable,
                "category": node.category.value if node.category else None,
                "state_epoch": node.state_epoch,
                "generated_at_turn": node.generated_at_turn,
                "last_updated_turn": node.last_updated_turn,
                "last_progress_at_turn": node.last_progress_at_turn,
                "iterations_without_progress": node.iterations_without_progress,
                "refutation_reason": node.refutation_reason,
                "rationale": node.rationale,
                "metadata": json.dumps(node.metadata or {}),
                "proposed_at": node.proposed_at,
                "updated_at": datetime.now(UTC),
            },
        )
        await _upsert_node_evidence(
            db, node_id, node.evidence_links, enterprise_id, organization_id
        )


async def _upsert_node_evidence(
    db,
    node_id: str,
    links: builtins.list[NodeEvidenceLink],
    enterprise_id: str,
    organization_id: Optional[str],
) -> None:
    """Upsert rows on the causal_node_evidence junction. Composite PK
    (node_id, evidence_id) makes it idempotent; stance is stored verbatim
    (supports/refutes/neutral)."""
    if not links:
        return
    query = text("""
            INSERT INTO causal_node_evidence (
                node_id, evidence_id, enterprise_id, organization_id, stance,
                stance_confidence, reasoning, linked_at_turn,
                created_at
            ) VALUES (
                :node_id, :evidence_id, :enterprise_id, :organization_id, :stance,
                :stance_confidence, :reasoning, :linked_at_turn,
                :created_at
            )
            ON CONFLICT (node_id, evidence_id) DO UPDATE SET
                stance = EXCLUDED.stance,
                stance_confidence = EXCLUDED.stance_confidence,
                reasoning = EXCLUDED.reasoning,
                linked_at_turn = EXCLUDED.linked_at_turn
        """)
    for link in links:
        await db.execute(
            query,
            {
                "node_id": node_id,
                "evidence_id": link.evidence_id,
                "enterprise_id": enterprise_id,
                "organization_id": organization_id,
                "stance": link.stance.value,
                "stance_confidence": link.stance_confidence,
                "reasoning": link.reasoning,
                "linked_at_turn": link.linked_at_turn,
                "created_at": link.analyzed_at,
            },
        )


async def _reconcile_causal_graph(
    db, case_id: str, node_ids: set[str], edge_ids: set[str]
) -> None:
    """Delete persisted causal nodes/edges no longer in the in-memory graph.

    The upserts are additive, so a node/edge removed in memory (e.g. an
    abandoned bridge stub GC'd by a hypothesis re-root, or any orphan-chain
    resolution) would otherwise survive in the DB and RESURRECT on the next
    load. This reconciles the persisted rows to the authoritative in-memory
    set: stale edges are deleted explicitly (a pruned edge whose endpoints
    both survive is not caught by the node-delete FK cascade), then stale
    nodes (their causal_node_evidence + endpoint edges cascade away, and
    solutions.node_id is SET NULL).

    Guarded on a non-empty node set: ``save`` persists the FULL graph and
    ``get`` loads it whole, so an empty set means a brand-new case with no
    graph yet — never a partial load — and must not wipe a populated graph.
    """
    if not node_ids:
        return
    await db.execute(
        text(
            "DELETE FROM causal_edges WHERE case_id = :cid AND edge_id NOT IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"cid": case_id, "ids": list(edge_ids) or [""]},
    )
    await db.execute(
        text(
            "DELETE FROM causal_nodes WHERE case_id = :cid AND node_id NOT IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"cid": case_id, "ids": list(node_ids)},
    )


async def _upsert_causal_edges(
    db,
    case_id: str,
    edges: builtins.list[CausalEdge],
    enterprise_id: str,
    organization_id: Optional[str],
) -> None:
    """Upsert causal-graph edges. Additive + idempotent on edge_id."""
    if not edges:
        return
    query = text("""
            INSERT INTO causal_edges (
                edge_id, case_id, enterprise_id, organization_id,
                cause_node_id, effect_node_id, and_group, reasoning,
                created_at_turn, created_at
            ) VALUES (
                :edge_id, :case_id, :enterprise_id, :organization_id,
                :cause_node_id, :effect_node_id, :and_group, :reasoning,
                :created_at_turn, :created_at
            )
            ON CONFLICT (edge_id) DO UPDATE SET
                and_group = EXCLUDED.and_group,
                reasoning = EXCLUDED.reasoning
        """)
    for edge in edges:
        await db.execute(
            query,
            {
                "edge_id": edge.edge_id,
                "case_id": case_id,
                "enterprise_id": enterprise_id,
                "organization_id": organization_id,
                "cause_node_id": edge.cause_node_id,
                "effect_node_id": edge.effect_node_id,
                "and_group": edge.and_group,
                "reasoning": edge.reasoning,
                "created_at_turn": edge.created_at_turn,
                "created_at": edge.created_at,
            },
        )


async def _upsert_solutions(
    db,
    case_id: str,
    solutions_list: builtins.list[Solution],
    enterprise_id: str,
    organization_id: Optional[str],
) -> None:
    """Upsert solutions records (SQLite-compatible).

    Purely additive — see `_upsert_evidence` for rationale. There is
    no concrete delete-solution API on the case repo today; if a
    single-solution remove path is needed, add it explicitly.

    Post-009 schema: writes the full Solution audit trail
    (proposed_by, applied_at/by, verified_at, verification_method,
    verification_evidence_id, effectiveness). Status is included
    in ON CONFLICT UPDATE so the lifecycle can advance.
    """
    for solution in solutions_list:
        applied_at = solution.applied_at
        verified_at = solution.verified_at
        state = _derive_solution_state(solution)

        query = text("""
                INSERT INTO solutions (
                    solution_id, case_id, enterprise_id, organization_id, solution_type, title,
                    node_id, quadrant,
                    immediate_action, longterm_fix, implementation_steps, commands, risks,
                    description, state,
                    proposed_by, applied_by,
                    verification_method, verification_evidence_id, effectiveness,
                    verification_result, verified_at,
                    proposed_at, applied_at, updated_at, metadata
                ) VALUES (
                    :solution_id, :case_id, :enterprise_id, :organization_id, :solution_type, :title,
                    :node_id, :quadrant,
                    :immediate_action, :longterm_fix, :implementation_steps, :commands, :risks,
                    :description, :state,
                    :proposed_by, :applied_by,
                    :verification_method, :verification_evidence_id, :effectiveness,
                    :verification_result, :verified_at,
                    :proposed_at, :applied_at, :updated_at, :metadata
                )
                ON CONFLICT (solution_id) DO UPDATE SET
                    solution_type = EXCLUDED.solution_type,
                    title = EXCLUDED.title,
                    node_id = EXCLUDED.node_id,
                    quadrant = EXCLUDED.quadrant,
                    immediate_action = EXCLUDED.immediate_action,
                    longterm_fix = EXCLUDED.longterm_fix,
                    implementation_steps = EXCLUDED.implementation_steps,
                    commands = EXCLUDED.commands,
                    risks = EXCLUDED.risks,
                    description = EXCLUDED.description,
                    state = EXCLUDED.state,
                    proposed_by = EXCLUDED.proposed_by,
                    applied_by = EXCLUDED.applied_by,
                    verification_method = EXCLUDED.verification_method,
                    verification_evidence_id = EXCLUDED.verification_evidence_id,
                    effectiveness = EXCLUDED.effectiveness,
                    verification_result = EXCLUDED.verification_result,
                    verified_at = EXCLUDED.verified_at,
                    applied_at = EXCLUDED.applied_at,
                    updated_at = EXCLUDED.updated_at,
                    metadata = EXCLUDED.metadata
            """)

        await db.execute(
            query,
            {
                "solution_id": solution.solution_id,
                "case_id": case_id,
                "enterprise_id": enterprise_id,
                "organization_id": organization_id,
                "solution_type": solution.solution_type.value,
                "title": solution.title,
                "node_id": solution.node_id,
                "quadrant": solution.quadrant.value if solution.quadrant else None,
                "immediate_action": solution.immediate_action,
                "longterm_fix": solution.longterm_fix,
                "implementation_steps": json.dumps(list(solution.implementation_steps)),
                "commands": json.dumps(list(solution.commands)),
                "risks": json.dumps(list(solution.risks)),
                "description": (
                    solution.immediate_action or solution.longterm_fix or solution.title
                ),
                "state": state,
                "proposed_by": solution.proposed_by,
                "applied_by": solution.applied_by,
                "verification_method": solution.verification_method,
                "verification_evidence_id": solution.verification_evidence_id,
                "effectiveness": solution.effectiveness,
                "verification_result": None,
                "verified_at": verified_at,
                "proposed_at": solution.proposed_at,
                "applied_at": applied_at,
                "updated_at": datetime.now(UTC),
                "metadata": json.dumps({}),
            },
        )


async def _upsert_uploaded_files(
    db,
    case_id: str,
    files_list: builtins.list[UploadedFile],
    enterprise_id: str,
    organization_id: Optional[str],
) -> None:
    """Upsert ``uploaded_files`` records.

    Purely additive — see ``_upsert_evidence`` for rationale. For
    intentional removal use ``delete_uploaded_file(case_id, file_id)``.
    Preprocessing artifacts (``summary``, ``structural_index``,
    ``data_type``, ``coverage_start_ts``, ``coverage_end_ts``) ride on
    this row; ``COALESCE`` on UPDATE prevents a failed re-extraction
    from clobbering a prior good extraction.
    """
    for file in files_list:
        query = text("""
                INSERT INTO uploaded_files (
                    file_id, case_id, enterprise_id, organization_id, uploaded_by,
                    filename, size_bytes, content_type, content_hash,
                    storage_ref, upload_source,
                    uploaded_at_turn, uploaded_at,
                    metadata,
                    summary, structural_index, data_type,
                    coverage_start_ts, coverage_end_ts, coverage_source
                ) VALUES (
                    :file_id, :case_id, :enterprise_id, :organization_id, :uploaded_by,
                    :filename, :size_bytes, :content_type, :content_hash,
                    :storage_ref, :upload_source,
                    :uploaded_at_turn, :uploaded_at,
                    :metadata,
                    :summary, :structural_index, :data_type,
                    :coverage_start_ts, :coverage_end_ts, :coverage_source
                )
                ON CONFLICT (file_id) DO UPDATE SET
                    uploaded_by = EXCLUDED.uploaded_by,
                    filename = EXCLUDED.filename,
                    size_bytes = EXCLUDED.size_bytes,
                    content_type = EXCLUDED.content_type,
                    content_hash = EXCLUDED.content_hash,
                    storage_ref = EXCLUDED.storage_ref,
                    upload_source = EXCLUDED.upload_source,
                    uploaded_at_turn = EXCLUDED.uploaded_at_turn,
                    metadata = EXCLUDED.metadata,
                    -- Preprocessing artifacts use COALESCE so a failed re-run
                    -- (NULL incoming) cannot clobber a prior good extraction.
                    -- Intentional clearing must go through a dedicated path.
                    summary = COALESCE(EXCLUDED.summary, uploaded_files.summary),
                    structural_index = COALESCE(EXCLUDED.structural_index, uploaded_files.structural_index),
                    data_type = COALESCE(EXCLUDED.data_type, uploaded_files.data_type),
                    coverage_start_ts = COALESCE(EXCLUDED.coverage_start_ts, uploaded_files.coverage_start_ts),
                    coverage_end_ts = COALESCE(EXCLUDED.coverage_end_ts, uploaded_files.coverage_end_ts),
                    coverage_source = COALESCE(EXCLUDED.coverage_source, uploaded_files.coverage_source)
            """)

        await db.execute(
            query,
            {
                "file_id": file.file_id,
                "case_id": case_id,
                "enterprise_id": enterprise_id,
                "organization_id": organization_id,
                "uploaded_by": file.uploaded_by,
                "filename": file.filename,
                "size_bytes": file.size_bytes,
                "content_type": file.content_type,
                "content_hash": file.content_hash,
                "storage_ref": file.storage_ref,
                "upload_source": file.upload_source,
                "uploaded_at_turn": file.uploaded_at_turn,
                "uploaded_at": file.uploaded_at,
                "metadata": json.dumps({}),
                "summary": file.summary,
                "structural_index": file.structural_index,
                "data_type": file.data_type,
                "coverage_source": file.coverage_source,
                "coverage_start_ts": file.coverage_start_ts,
                "coverage_end_ts": file.coverage_end_ts,
            },
        )


async def _upsert_messages(
    db,
    normalise_message_row,
    case_id: str,
    messages_list: builtins.list[dict],
    enterprise_id: str,
    organization_id: Optional[str],
) -> None:
    """Upsert case messages (SQLite-compatible).

    Purely additive — messages are an append-only log at the domain
    level; nothing intentionally deletes them. A stale in-memory
    ``case.messages`` MUST NOT silently truncate rows other concurrent
    writers have persisted.

    Schema per design spec (case-schema.md §4.7):
    - message_id, turn_number, role, content, created_at, token_count,
      metadata, author_id

    Authorship is never overwritten with a blank — see the COALESCE on
    ``author_id`` in the conflict clause below.
    """
    # Validate and complete the WHOLE list before any SQL runs. A row with
    # no ``message_id`` used to be SKIPPED here, silently: the save
    # reported success and the transcript line was gone (#1418). It is
    # completed in place now — the id is the conflict target, so the
    # caller's list must carry what the row carries — and a row the
    # repository cannot complete honestly is REFUSED by name here rather
    # than part-written and then aborted by a constraint.
    for idx, msg in enumerate(messages_list):
        normalise_message_row(msg, index=idx)

    for idx, msg in enumerate(messages_list):

        query = text("""
                INSERT INTO case_messages (
                    message_id, case_id, enterprise_id, organization_id, turn_number, role, content, author_id, created_at, token_count, metadata
                ) VALUES (
                    :message_id, :case_id, :enterprise_id, :organization_id, :turn_number, :role, :content, :author_id, :created_at, :token_count, :metadata
                )
                ON CONFLICT (message_id) DO UPDATE SET
                    turn_number = EXCLUDED.turn_number,
                    role = EXCLUDED.role,
                    content = EXCLUDED.content,
                    created_at = EXCLUDED.created_at,
                    token_count = EXCLUDED.token_count,
                    metadata = EXCLUDED.metadata,
                    author_id = COALESCE(case_messages.author_id, EXCLUDED.author_id)
            """)
        # Authorship is write-once but still fillable. COALESCE keeps an
        # author already on the row (a re-save whose in-memory dict lacked
        # the field cannot NULL it out — the unrecoverable loss this column
        # exists to prevent) while still letting a later save supply one for
        # a row that has none. A bare `EXCLUDED.author_id` would erase;
        # omitting the column entirely would make a NULL permanent.

        await db.execute(
            query,
            {
                "message_id": msg["message_id"],
                "case_id": case_id,
                "enterprise_id": enterprise_id,
                "organization_id": organization_id,
                "turn_number": msg["turn_number"],
                "role": msg.get("role", "user"),
                "content": msg.get("content", ""),
                "author_id": msg.get("author_id"),
                "created_at": msg["created_at"],
                "token_count": msg.get("token_count"),
                "metadata": json.dumps(msg.get("metadata", {})),
            },
        )


async def _append_case_actions(
    db,
    case_id: str,
    transitions: builtins.list[CaseAction],
    enterprise_id: str,
    organization_id: Optional[str],
) -> None:
    """Append only newly-added case actions (append-only audit trail).

    ``action_history`` is hydrated oldest-first by ``_load_case_actions``
    and new actions are appended to the tail, so the in-memory list is
    always ``[persisted_prefix..., new_tail...]``. Only the unpersisted
    tail is inserted.

    Re-inserting the full list every ``save()`` previously caused
    *geometric* row growth: ``transition_id`` is an autoincrement PK with
    no natural-key conflict target, so the ``ON CONFLICT DO NOTHING``
    clause could never fire and every save duplicated the entire history
    (R rows → 2R + new). Counting already-persisted rows and inserting
    only ``transitions[already_persisted:]`` makes each save O(new), not
    O(history).
    """
    count_result = await db.execute(
        text("SELECT COUNT(*) FROM case_actions WHERE case_id = :case_id"),
        {"case_id": case_id},
    )
    already_persisted = count_result.scalar() or 0
    new_transitions = transitions[already_persisted:]
    for transition in new_transitions:
        query = text("""
                INSERT INTO case_actions (
                    case_id, enterprise_id, organization_id, from_state, to_state, reason,
                    triggered_by, transitioned_at, metadata
                ) VALUES (
                    :case_id, :enterprise_id, :organization_id, :from_state, :to_state, :reason,
                    :triggered_by, :transitioned_at, :metadata
                )
            """)

        await db.execute(
            query,
            {
                "case_id": case_id,
                "enterprise_id": enterprise_id,
                "organization_id": organization_id,
                "from_state": (
                    transition.from_state.value if transition.from_state else None
                ),
                "to_state": transition.to_state.value,
                "reason": (
                    transition.reason if hasattr(transition, "reason") else None
                ),
                "triggered_by": transition.triggered_by,
                "transitioned_at": transition.triggered_at,
                "metadata": json.dumps({}),
            },
        )
