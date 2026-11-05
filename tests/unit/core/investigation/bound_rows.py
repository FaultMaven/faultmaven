"""One builder for a post-fix observation that binds (#1906 D1), shared by every
test that means a confirming (or a persisting) row.

A post-fix row confirms or disconfirms only the subject it names: its
``rechecks`` must name one of the rows that showed the problem
(``ProblemVerification.verifying_evidence_ids``) or a row supporting the cause
alone (``cause_assurance.SubjectIndex.cause_targets``), and it counts as after a
fix only when its data was uploaded after it (#2145). So a test that means "the
user showed the problem gone" needs all three: a verifying row, a cause the row
can be about, and a file uploaded on the row's turn. ``ensure_subjects`` gives a
case the first two; ``bound_row`` (a domain row) and ``bound_to_add`` (the
model's ``EvidenceToAdd``, for ``_apply_investigation_updates``) the third.

Both kinds of case the suite builds are served: a real ``Case`` and a
``SimpleNamespace`` stand-in (whose rows are namespaces too).
"""

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

from faultmaven.core.investigation.causal_graph.ingestion import seed_problem_node
from faultmaven.core.investigation.cause_assurance import subject_index
from faultmaven.core.investigation.schemas import EvidenceToAdd
from faultmaven.modules.case.contracts import (
    Case,
    CausalEdge,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    UploadedFile,
    ValidationMethod,
)
from tests.utils import terminal_verdict

#: The cause a case is given when it has none: an identified root.
BOUND_ROOT_ID = "cn_00000000b0dd"
BOUND_HYPOTHESIS_ID = "hyp_00000000b0dd"
#: The rows ``ensure_subjects`` adds when a case has none: the row that showed
#: the problem, and the identified root's support.
VERIFYING_ID = "ev_00000000fe01"
SUPPORT_ID = "ev_000000005a01"

_SYMPTOMS = (
    EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
    EvidenceCategory.SYMPTOM_EVIDENCE,
)


def _id(prefix: str, label: str) -> str:
    return prefix + hashlib.md5(label.encode()).hexdigest()[:12]


def upload(case, turn: int, label: str | None = None) -> str:
    """A file uploaded at ``turn`` on ``case``; its id."""
    file_id = _id("file_", label or f"upload-{turn}-{len(case.uploaded_files)}")
    if all(f.file_id != file_id for f in case.uploaded_files):
        case.uploaded_files.append(
            UploadedFile(
                file_id=file_id,
                filename=f"{file_id}.log",
                size_bytes=1,
                uploaded_at_turn=turn,
            )
        )
    return file_id


def bound_evidence(
    category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
    *,
    turn: int,
    evidence_id: str,
    rechecks: str | None,
    summary: str = "the re-check after the fix",
    **overrides,
) -> Evidence:
    """THE keyword-override builder for a domain post-fix row: recorded at
    ``turn``, naming ``rechecks`` (None: names nothing, so it binds to
    nothing). ``overrides`` set any other ``Evidence`` field
    (``source_file_id`` defaults to a fixed file, ``collected_by`` to the
    model). Not appended anywhere: ``bound_row`` does that for a case."""
    fields = dict(
        evidence_id=evidence_id,
        summary=summary,
        primary_purpose="test",
        category=category,
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_0123456789ab",
        collected_by="llm",
        collected_at_turn=turn,
        collected_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    row = Evidence(**fields)
    if rechecks is not None:
        row.record_rechecks(rechecks, named=rechecks)
    return row


def _row(case, category, *, turn, evidence_id, summary, rechecks=None, **overrides):
    """A row on ``case`` (a domain ``Evidence``, or a namespace on a stand-in),
    its data uploaded at ``turn``; appended and returned."""
    if isinstance(case, Case):
        fields = dict(source_file_id=upload(case, turn))
        fields.update(overrides)
        row = bound_evidence(
            category,
            turn=turn,
            evidence_id=evidence_id,
            rechecks=rechecks,
            summary=summary,
            **fields,
        )
    else:
        fields = dict(
            evidence_id=evidence_id,
            summary=summary,
            extract=None,
            category=category,
            collected_by="llm",
            collected_at_turn=turn,
            source_file_id=None,
            rechecks_evidence_id=rechecks,
        )
        fields.update(overrides)
        row = SimpleNamespace(**fields)
    case.evidence.append(row)
    return row


def ensure_subjects(case, *, turn: int = 1) -> tuple[str, str | None]:
    """Give ``case`` what a post-fix row binds to, keeping what it has: a
    verifying symptom row recorded as such, and, when no row binds to a cause
    yet and the case has no ROOT node, a cause with a support of its own
    (``_cause_on_record``). Returns ``(verifying id, cause support id or
    None)``."""
    if case.problem_verification is None and isinstance(case, Case):
        # Nothing can have shown a problem the case never recorded.
        case.problem_verification = ProblemVerification(
            symptom_statement=case.description or "the reported problem"
        )
    pv = case.problem_verification
    if not getattr(pv, "verifying_evidence_ids", None):
        row = _row(
            case,
            EvidenceCategory.SYMPTOM_EVIDENCE,
            turn=turn,
            evidence_id=VERIFYING_ID,
            summary="the reported problem, as first shown",
        )
        pv.verifying_evidence_ids = [row.evidence_id]
    subjects = subject_index(case)
    problem, cause = subjects.bindable_rows()
    roots = [
        n
        for n in (getattr(case, "causal_nodes", None) or {}).values()
        if n.node_type == NodeType.ROOT
    ]
    if not cause and not roots:
        _cause_on_record(case, turn=turn)
        problem, cause = subject_index(case).bindable_rows()
    return problem[0].evidence_id, (cause[0].evidence_id if cause else None)


def _cause_on_record(case, *, turn: int) -> None:
    """A cause a cause observation can be about, with a support of its own
    (``cause_assurance._cause_pool``). A stand-in gets an identified root
    (VALIDATED, marked terminal): nothing recomputes it. A real case gets a
    COUNT-HELD root — one causal support, a hypothesis rooted on it, an edge
    to the PROBLEM node, marked terminal — which the engine's recompute keeps
    in the pool without identifying the cause, so a fixture that had no cause
    on record keeps ``cause_state`` short of IDENTIFIED (no revision bar, no
    solution license it did not have)."""
    real = isinstance(case, Case)
    support = _row(
        case,
        EvidenceCategory.CAUSAL_EVIDENCE,
        turn=turn,
        evidence_id=SUPPORT_ID,
        summary="postgres log: FATAL too many connections for the worker pool",
    )
    root = CausalNode(
        metadata=terminal_verdict(),
        node_id=BOUND_ROOT_ID,
        statement="max_connections is set to 20, below the worker pool size",
        node_type=NodeType.ROOT,
        node_state=NodeState.INCONCLUSIVE if real else NodeState.VALIDATED,
        validation_method=(
            ValidationMethod.NONE if real else ValidationMethod.EMPIRICAL
        ),
        belief=0.6 if real else 0.9,
        actionable=True,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=support.evidence_id,
                stance=EvidenceStance.SUPPORTS,
                reasoning="supports the root alone",
                linked_at_turn=turn,
            )
        ],
        generated_at_turn=turn,
    )
    if getattr(case, "causal_nodes", None) is None:
        case.causal_nodes = {}
    case.causal_nodes[BOUND_ROOT_ID] = root
    if not real:
        return
    case.hypotheses[BOUND_HYPOTHESIS_ID] = Hypothesis(
        hypothesis_id=BOUND_HYPOTHESIS_ID,
        statement="the postgres connection limit starves the worker pool",
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="supported once",
        root_node_id=BOUND_ROOT_ID,
        generated_at_turn=turn,
    )
    problem = seed_problem_node(case)
    if problem is not None:
        case.causal_edges.append(
            CausalEdge(cause_node_id=BOUND_ROOT_ID, effect_node_id=problem.node_id)
        )


def _target(case, category, rechecks):
    if rechecks is not None:
        return rechecks
    verifying, support = ensure_subjects(case)
    return verifying if category in _SYMPTOMS else support


def bound_row(
    case,
    category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
    *,
    turn: int,
    evidence_id: str | None = None,
    summary: str = "the re-check after the fix",
    rechecks: str | None = None,
    **overrides,
):
    """A post-fix observation recorded at ``turn`` from data uploaded that
    turn, naming the row it re-checks (default: the case's verifying row for a
    symptom category, its first root-only cause support for causal_absence).
    Appended to ``case.evidence`` and returned. ``overrides`` set any other
    field of the row."""
    label = f"bound-{category.value}-{turn}-{id(case)}-{len(case.evidence)}"
    return _row(
        case,
        category,
        turn=turn,
        evidence_id=evidence_id or _id("ev_", label),
        summary=summary,
        rechecks=_target(case, category, rechecks),
        **overrides,
    )


def bound_to_add(
    case,
    category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
    summary: str = "the re-check after the fix",
    *,
    turn: int,
    rechecks: str | None = None,
    **overrides,
) -> EvidenceToAdd:
    """The model's ``EvidenceToAdd`` for a post-fix observation on the turn it
    is applied: its data uploaded at ``turn``, naming the row it re-checks
    (as ``bound_row``). ``rechecks`` may be a ``new_index_N`` ref."""
    fields = dict(
        summary=summary,
        extract=summary,
        category=category,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=upload(case, turn),
        rechecks=_target(case, category, rechecks),
    )
    fields.update(overrides)
    return EvidenceToAdd(**fields)
