"""Runbook Cause matcher — per-turn instantiation (increments 4a, 4b-1, 4b-2).

Bridges the structured matcher (``kb_qa.aget_cause_matches`` → ``CauseMatchResult``)
to the case's causal graph: when a retrieved runbook's Cause matches with a single
confident verdict, instantiate that Cause's causal chain as CANDIDATE nodes by
REUSING ``causal_graph.ingest_emitted_chain`` (seed-D, exact-match dedup, ``cn_``
id render-back, edges, never-``VALIDATED``), then attach a hypothesis to the
chain's root (4b-1) so the chain is *load-bearing* — an unattached chain is
invisible to ``cause_state`` / ``any_chain_root_validated`` / RCC synthesis. The
matcher seeds a structural *prior*; everything downstream (``derive_node_states``,
RCC synthesis, the M5 solution gate) then treats these nodes exactly like
LLM-emitted ones — so they inherit the same soundness treatment with no
matcher-specific bypass. That treatment is the engine's standing contract, NOT a
static guarantee from the matcher: a seeded node stays CANDIDATE and only
VALIDATES (driving ``cause_state`` to IDENTIFIED) on real case evidence; the
capped prior (≤ 0.5) is the matcher's initial belief, not a durable ceiling (the
engine clamps belief to ``[0, 1]`` only), and a failed fix demotes the cause
(counterfactual backstop). So a false match degrades to a capped, evidence-gated
candidate — noise/cost, not a wrong conclusion — never a soundness break.

Matching fires on the **T2 semantic tier** (``case_evidence_qa`` over the case's
vectorized evidence — wired in 4b-2). The T1 deterministic tier stays inert:
FaultMaven investigates uploaded evidence rather than executing a runbook's
numbered diagnostic steps, so there is no per-step output to resolve (the spec
frames T1 as an opportunistic fast-path, T2 as the canonical robustness floor).

The matched Cause's documented fixes (``interventions``) are stashed on the chain
ROOT node's metadata (4b-3); the prompt context surfaces them as a
``<documented_fixes>`` block ONLY once that cause is established
(``cause_state == IDENTIFIED``), so the LLM proposes them and they flow through
the M5 gate normally. The matcher never creates ``Solution`` objects directly —
that would bypass M5. Still flag-gated OFF until increment 5.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from faultmaven.core.investigation.causal_graph import (
    _normalize_statement,
    chain_path_to_problem,
    ingest_emitted_chain,
)
from faultmaven.core.investigation.cause_schemas import (
    CauseMatchResult,
    CauseRecord,
    is_problem_node,
)
from faultmaven.core.investigation.schemas import CausalEdgeToAdd, CausalNodeToAdd
from faultmaven.core.investigation.terminal_transitions import (
    CAUSE_IDENTIFIED_LIKELIHOOD,
)
from faultmaven.modules.case.domain.models import (
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    NodeType,
)
from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult

if TYPE_CHECKING:
    from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
    from faultmaven.core.investigation.indicator_evaluator import IndicatorEvaluator
    from faultmaven.modules.case.domain.models import Case, Evidence, Hypothesis

logger = logging.getLogger(__name__)

# Distinct runbooks the matcher evaluates per turn.
_DEFAULT_MAX_RUNBOOKS = 3

# A matched runbook seeds a PRIOR, not a conclusion. Cap its hypothesis
# likelihood strictly below the ``CAUSE_IDENTIFIED_LIKELIHOOD`` "cause identified"
# threshold (``working_conclusion.likelihood >= CAUSE_IDENTIFIED_LIKELIHOOD`` →
# ``terminal_transitions._cause_identified``, which gates the M5 solution gate and
# resolution readiness) so a runbook match ALONE can never make FM conclude the
# cause. Real evidence lifts it past the gate.
_MATCHER_MAX_PRIOR = 0.5

# Soundness invariant (the matcher's core "no incorrect conclusion" guarantee):
# the instantiated prior MUST sit strictly below the cause-identified gate, else a
# runbook match alone could trip resolution. Enforced at import so a careless edit
# to either constant fails fast rather than silently breaking soundness.
if _MATCHER_MAX_PRIOR >= CAUSE_IDENTIFIED_LIKELIHOOD:  # pragma: no cover
    raise AssertionError(
        f"_MATCHER_MAX_PRIOR ({_MATCHER_MAX_PRIOR}) must be < "
        f"CAUSE_IDENTIFIED_LIKELIHOOD ({CAUSE_IDENTIFIED_LIKELIHOOD})"
    )

# Prefix on a matcher hypothesis's ``rationale``. It doubles as the per-case
# skip-guard signal (``is_runbook_match_hypothesis``): the matcher runs every
# turn, so it must recognize its own prior cheaply — and ``rationale`` PERSISTS
# (a ``hypotheses`` column) where a fresh model field would not, so the guard
# survives the case being reloaded between turns.
RUNBOOK_MATCH_RATIONALE_PREFIX = "Instantiated from runbook"


def is_runbook_match_hypothesis(hyp: "Hypothesis") -> bool:
    """True if ``hyp`` was seeded by the runbook Cause matcher (its rationale
    carries the matcher's marker prefix). The per-case skip-guard signal."""
    return str(getattr(hyp, "rationale", "") or "").startswith(
        RUNBOOK_MATCH_RATIONALE_PREFIX
    )


# Budget for the raw-evidence fallback text (chars). Case evidence is curated and
# bounded (summary ≤500 + optional extract per row), so this comfortably holds
# a typical case's evidence while capping the classifier prompt on large cases.
_FALLBACK_EVIDENCE_MAX_CHARS = 8000


def build_case_evidence_fallback_text(
    case: "Case", max_chars: int = _FALLBACK_EVIDENCE_MAX_CHARS
) -> Optional[str]:
    """Assemble the T2 raw-evidence fallback from ``case.evidence``.

    The matcher's T2 tier judges each rung indicator against the case's
    *vectorized* evidence. But case evidence is vectorized only in DA mode and
    above a size gate, so small/conversational evidence is frequently absent from
    the index — leaving T2 nothing to retrieve and the matcher unable to fire
    (issue #543). This renders the already-recorded ``Evidence`` rows (the LLM's
    claim-anchored ``summary`` + optional verbatim ``extract``) into a compact
    text block the tool can judge against when the vector collection is empty.

    Returns ``None`` when the case has no evidence (the tool then abstains, i.e.
    keeps the conservative pre-fallback behavior). The output is hard-capped at
    ``max_chars``: whole rows are kept on a boundary, and a single oversized row
    (``Evidence.extract`` has no length limit, unlike ``summary``) is itself
    truncated so the classifier prompt stays bounded even on the first row.
    """
    evidence = getattr(case, "evidence", None) or []
    if not evidence:
        return None

    parts: List[str] = []
    used = 0  # running length of the joined output ("\n\n" between rows)
    joiner = 2  # len("\n\n")
    for ev in evidence:
        summary = (getattr(ev, "summary", "") or "").strip()
        if not summary:
            continue
        category = getattr(getattr(ev, "category", None), "value", "") or ""
        extract = (getattr(ev, "extract", "") or "").strip()
        header = f"[{category}] {summary}" if category else summary
        block = f"{header}\n{extract}" if extract else header
        sep = joiner if parts else 0  # no joiner before the first row
        remaining = max_chars - used - sep
        if remaining <= 0:
            break
        if len(block) > remaining:
            # Oversized row (typically a large extract): truncate it to the
            # budget rather than dropping it, so even an unbounded first-row
            # extract can't blow the classifier prompt. Then stop.
            parts.append(block[:remaining])
            break
        parts.append(block)
        used += sep + len(block)
    if not parts:
        return None
    return "\n\n".join(parts)


def chain_to_specs(
    cause: CauseRecord,
) -> Tuple[List[CausalNodeToAdd], List[CausalEdgeToAdd]]:
    """Convert a Cause's chain into ``ingest_emitted_chain`` spec shapes.

    - The PROBLEM node (D) is engine-seeded, so it is dropped from the node
      specs; any ref to it maps to the literal ``'D'`` token ingest understands.
    - Every other chain node becomes a ``CausalNodeToAdd``; its ref maps to
      ``'new_index_N'`` (its position in the node-spec list).
    - Edge endpoints map through that table; an edge with an unresolvable
      endpoint is dropped (ingest would skip it anyway).
    - A node whose ``statement`` is not a string (a malformed pack might nest a
      dict/list there) is skipped, not ``str()``-coerced into a garbage node.
    - A linear chain has exactly one ROOT; multiple ROOTs signal a malformed pack
      and are logged (the chain is still instantiated as authored — the engine's
      node-state derivation, not this converter, owns root semantics).
    """
    nodes: List[CausalNodeToAdd] = []
    ref_token: Dict[str, str] = {}
    root_refs: List[str] = []
    for node in cause.chain_nodes:
        ref = str(node.get("ref", "")).strip()
        ntype_raw = str(node.get("node_type", "")).strip().lower()
        # D is engine-seeded; never emit it, just record its ref → 'D'.
        if is_problem_node(node):
            if ref:
                ref_token[ref] = "D"
            continue
        raw_statement = node.get("statement", "")
        if not isinstance(raw_statement, str):
            # A non-string statement (e.g. a nested dict) is malformed; coercing
            # it would mint a node whose text is a Python repr. Skip it.
            logger.warning(
                "Non-string chain-node statement (%s) in cause %s; skipping node",
                type(raw_statement).__name__,
                cause.cause_letter,
            )
            continue
        statement = raw_statement.strip()
        if not statement:
            continue
        # A duplicate ref would overwrite the earlier node's token and misdirect
        # any edge pointing at it — skip the duplicate rather than mis-wire.
        if ref and ref in ref_token:
            logger.warning(
                "Duplicate chain ref %r in cause %s; skipping duplicate node",
                ref,
                cause.cause_letter,
            )
            continue
        try:
            node_type = NodeType(ntype_raw)
        except ValueError:
            node_type = NodeType.INTERMEDIATE
        if node_type == NodeType.ROOT:
            root_refs.append(ref or f"<unref:{len(nodes)}>")
        # An unreferenced (empty-ref) node is still a valid node; just keep it out
        # of the ref table (no edge can target it).
        if ref:
            ref_token[ref] = f"new_index_{len(nodes)}"
        nodes.append(CausalNodeToAdd(statement=statement, node_type=node_type))

    if len(root_refs) > 1:
        logger.warning(
            "Cause %s chain has %d ROOT nodes (%s); expected one — instantiating "
            "as authored",
            cause.cause_letter,
            len(root_refs),
            ", ".join(root_refs),
        )

    edges: List[CausalEdgeToAdd] = []
    for edge in cause.chain_edges:
        cause_ref = str(edge.get("cause_ref", "")).strip()
        effect_ref = str(edge.get("effect_ref", "")).strip()
        cause_tok = ref_token.get(cause_ref)
        effect_tok = ref_token.get(effect_ref)
        if cause_tok and effect_tok:
            edges.append(CausalEdgeToAdd(cause=cause_tok, effect=effect_tok))
        else:
            # An edge with an endpoint outside this cause's node table is dropped.
            # The common case is a cross-cause ``converges:`` edge — convergence
            # across causes is documented UNSUPPORTED (runbook-content-architecture
            # .md § "Convergence"), so this is expected, but it must not vanish
            # silently: log it so a malformed/cross-cause chain is diagnosable.
            logger.warning(
                "Dropping chain edge %s→%s in cause %s (endpoint not in this "
                "cause's nodes; cross-cause convergence is unsupported)",
                cause_ref or "?",
                effect_ref or "?",
                cause.cause_letter,
            )
    return nodes, edges


def instantiate_cause_chain(
    case: "Case", cause: CauseRecord, current_turn: int
) -> List[Optional[str]]:
    """Instantiate ``cause``'s chain into ``case`` via ``ingest_emitted_chain``.

    Returns the created node ids (empty when the chain has no instantiable node,
    e.g. a degenerate Cause carrying only the problem node)."""
    nodes, edges = chain_to_specs(cause)
    if not nodes:
        return []
    return ingest_emitted_chain(case, nodes, edges, [], current_turn)


# Node-metadata key under which the matched runbook's documented fixes are
# stashed on the chain ROOT node, for the prompt context to surface once the
# cause is established (see context_builder._build_documented_fixes_block).
RUNBOOK_INTERVENTIONS_META_KEY = "runbook_interventions"


def _stash_interventions(
    case: "Case", root_id: str, record: Optional[CauseRecord]
) -> None:
    """Stash the matched Cause's interventions (documented fixes) on its ROOT
    node's metadata. No-op when there are none. ``node.metadata`` persists (a
    JSON column), so the fixes survive reload until the cause is established."""
    interventions = list(record.interventions) if record else []
    if not interventions:
        return
    node = case.causal_nodes.get(root_id)
    if node is None:
        return
    # Symmetric with the reader's ``node.metadata or {}`` — never assume the dict.
    if not node.metadata:
        node.metadata = {}
    node.metadata[RUNBOOK_INTERVENTIONS_META_KEY] = interventions


# Structured key recording which runbook seeded a ROOT node. The differential's
# source of truth — read by ``differential_runbook_ids``, NOT parsed from the
# hypothesis rationale string. ``node.metadata`` persists (JSON column).
RUNBOOK_ID_META_KEY = "runbook_id"


def _stamp_runbook_id(case: "Case", root_id: str, runbook_id: str) -> None:
    """Record (structured) which runbook seeded this ROOT, for differential
    re-resolution. Unconditional — unlike interventions, the differential needs
    the id even when the matched cause documents no fixes."""
    node = case.causal_nodes.get(root_id)
    if node is None or not runbook_id:
        return
    if not node.metadata:
        node.metadata = {}
    node.metadata[RUNBOOK_ID_META_KEY] = runbook_id


def differential_runbook_ids(case: "Case") -> List[str]:
    """The matched candidate runbook id(s) backing the case's differential.

    Read from the structured ``runbook_id`` key the matcher stamps on each seeded
    ROOT node's metadata — NOT parsed from any rationale string. The per-turn
    intake hook re-resolves these into the candidate differential
    (``resolve_causes(id)`` → ``KbQAService._build_cause_records`` →
    ``assemble_active_causes``). One id today (``max_runbooks`` defaults low), but
    a list by contract. De-duplicated, insertion-ordered.
    """
    ids: List[str] = []
    seen: set = set()
    for node in case.causal_nodes.values():
        rid = (getattr(node, "metadata", None) or {}).get(RUNBOOK_ID_META_KEY)
        if isinstance(rid, str) and rid and rid not in seen:
            seen.add(rid)
            ids.append(rid)
    return ids


def _root_node_id(case: "Case", created: List[Optional[str]]) -> Optional[str]:
    """The instantiated chain's ROOT node id. Found by node_type (not position),
    so a skipped/deduped node can't misidentify the root. The v4 chain has
    exactly one ROOT."""
    for node_id in created:
        node = case.causal_nodes.get(node_id) if node_id else None
        if node is not None and node.node_type == NodeType.ROOT:
            return node_id
    return None


def _existing_root_id(case: "Case", record: CauseRecord) -> Optional[str]:
    """Id of an already-instantiated ROOT for ``record``'s chain, or ``None``.

    Pure lookup — no mutation. Keys on the SAME identity ``ingest_emitted_chain``
    mints and merges with: ``(NodeType.ROOT, _normalize_statement(statement))``.
    Reusing the shared ``_normalize_statement`` (rather than re-deriving the key
    here) is what makes the lookup and the mint ONE identity — so a matcher-seeded
    root and a verbatim LLM re-emit of the same cause can never split into two
    roots (§2.2). Returns ``None`` for a degenerate / ``[Default]`` cause whose
    chain has no instantiable ROOT spec.
    """
    nodes, _edges = chain_to_specs(record)
    root_spec = next((n for n in nodes if n.node_type == NodeType.ROOT), None)
    if root_spec is None:
        return None
    key = _normalize_statement(root_spec.statement)
    for node_id, node in case.causal_nodes.items():
        if (
            node.node_type == NodeType.ROOT
            and _normalize_statement(node.statement) == key
        ):
            return node_id
    return None


def resolve_root(
    case: "Case", record: CauseRecord, *, may_instantiate: bool
) -> Optional[str]:
    """Map a differential candidate (its ``CauseRecord``) to its ROOT node id.

    The intake-evaluation loop (process layer, ``differential_intake``) calls this
    to turn a ``StanceVerdict`` into a link on the cause's root — instantiating the
    chain lazily on the first SUPPORTS. Matcher-owned: it packages
    ``instantiate_cause_chain`` + ``_root_node_id`` + the existing exact-match dedup
    *lookup*, idempotently, behind ``may_instantiate``.

    Args:
        case: the live case (the turn is read from ``case.current_turn`` — no turn
            param, so the seam stays clean).
        record: the candidate's full cause record.
        may_instantiate:
            - ``True``  (SUPPORTS): return the existing root if the cause already
              stands in the graph; else instantiate the chain and return its new
              root id (lazy promotion).
            - ``False`` (REFUTES): return the existing root id, or ``None`` — never
              a side effect.

    Returns the ROOT node id, or ``None``. **``None`` is possible even when
    ``may_instantiate=True``** — a degenerate / ``[Default]`` cause has no
    instantiable root (``instantiate_cause_chain`` returns ``[]``); the caller must
    skip the verdict on ``None``, SUPPORTS or not.

    SOUNDNESS — single identity for lookup + mint. Both the ``may_instantiate=
    False`` lookup and the ``True`` instantiation route through the SAME exact-match
    dedup identity ``ingest_emitted_chain`` uses to mint/merge, so a root seeded by
    the matcher and one emitted by the LLM for the same cause resolve to the **one**
    canonical node — never duplicate roots (the spec's hardest prior bug, §2.2).

    """
    existing = _existing_root_id(case, record)
    if existing is not None:
        # Idempotent: the cause already stands in the graph — same root for a
        # SUPPORTS or a REFUTES, no mutation.
        return existing
    if not may_instantiate:
        # REFUTES (or any non-promoting check) never instantiates — a refutation
        # of an unseen cause has nothing to attach to and must not seed a node.
        return None
    # First SUPPORTS for this cause: lazily promote it. instantiate_cause_chain
    # routes through ingest_emitted_chain, whose exact-match dedup reuses an
    # identical standing root rather than minting a second one — so even this
    # mint path cannot duplicate a root (the single-identity guarantee holds on
    # both branches). Returns None for a degenerate / [Default] cause (no
    # instantiable node), which the caller skips.
    created = instantiate_cause_chain(case, record, case.current_turn)
    return _root_node_id(case, created)


def resolve_datum_text(evidence: "Evidence", case: "Case") -> Optional[str]:
    """The trusted, code-normalized text a datum's predicates evaluate against.

    Resolves the datum's backing ``UploadedFile`` (via ``source_file_id``) and
    returns its Tier-1 preprocessing digest (``structural_index.file_extract``) —
    a verbatim SUBSET of the raw file produced deterministically by the extractor,
    on the trusted side of the boundary. NEVER ``Evidence.summary`` / ``extract``
    (in-loop-LLM interpretation). Because it is a subset, callers must evaluate it
    under subset-trust (``complete=False``): trust what is present, never infer
    from absence.

    Returns ``None`` when the datum has no backing file or the file has no
    extracted index — every predicate is then ``untested`` and the intake loop
    abstains (it never refutes on missing content).
    """
    file_id = getattr(evidence, "source_file_id", None)
    if not file_id:
        return None
    uf = case.find_uploaded_file(file_id)
    raw = getattr(uf, "structural_index", None) if uf is not None else None
    if not raw:
        return None
    try:
        text = ExtractResult.from_json(raw).file_extract or ""
    except (json.JSONDecodeError, TypeError, AttributeError):
        # Pre-schema / non-JSON index: treat the whole blob as the extract text
        # (tolerant fallback, mirroring the prompt path's parser).
        text = raw
    return text or None


def attach_matched_hypothesis(
    case: "Case",
    match: CauseMatchResult,
    root_id: str,
    hypothesis_manager: "HypothesisManager",
) -> Optional["Hypothesis"]:
    """Create a hypothesis rooted at the matched chain's root, so the chain is
    *load-bearing* — an unattached chain is invisible to ``cause_state`` /
    ``any_chain_root_validated`` / RCC synthesis (those read standing
    hypotheses, not bare nodes).

    Idempotent: the matcher runs every turn, but ``ingest_emitted_chain`` dedups
    the nodes, so a re-match resolves to the SAME root id; if a hypothesis
    already roots there, do nothing rather than spawn a duplicate.

    The hypothesis is a *prior*: its likelihood is capped below the
    "cause identified" gate (see ``_MATCHER_MAX_PRIOR``) and its root is a
    CANDIDATE node — ``cause_state`` reaches IDENTIFIED only when that root
    VALIDATES from real evidence (M4/M5). The runbook never concludes on its own.
    """
    if any(h.root_node_id == root_id for h in case.hypotheses.values()):
        return None

    # Only attach to a chain that actually materialized a root → D path. A
    # disconnected chain (malformed runbook edges, or a lone root never linked
    # to D) would otherwise yield a hypothesis with root_node_id set and an
    # empty path — an inconsistent record (latent path[0] failures). Skip it;
    # the bare nodes remain, inert.
    path = chain_path_to_problem(root_id, case)
    if not path:
        logger.warning(
            "Matched chain root %s has no path to D; skipping hypothesis", root_id
        )
        return None

    record = match.selected_record
    cause = match.selected_cause
    statement = (
        (record.cause_statement or record.cause_name or "").strip()
        or (cause.cause_name if cause else "")
        or "Runbook-matched cause"
    )
    # A runbook match is a PRIOR, never a conclusion. Cap the likelihood below
    # the 0.6 "cause identified" threshold (working_conclusion → _cause_identified,
    # which gates M5 + resolution) so a runbook ALONE can never make FM conclude
    # the cause. Only real evidence — the LLM raising the likelihood, or the root
    # VALIDATING — lifts it past the gate.
    belief = float(cause.belief if cause else 0.0)
    likelihood = min(max(0.0, belief), _MATCHER_MAX_PRIOR)
    letter = cause.cause_letter if cause else "?"
    hyp = hypothesis_manager.create_hypothesis(
        statement=statement[:500],
        category=HypothesisCategory.OTHER.value,
        initial_likelihood=likelihood,
        current_turn=case.current_turn,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        state=HypothesisState.ACTIVE,
        # The prefix is the persisted skip-guard signal (see
        # is_runbook_match_hypothesis) — keep it leading.
        rationale=(
            f"{RUNBOOK_MATCH_RATIONALE_PREFIX} {match.runbook_id} "
            f"(cause {letter}) matching the case."
        ),
    )
    # path before root_node_id so the lenient root==path[0] invariant holds at
    # every intermediate state.
    hyp.path = path
    hyp.root_node_id = root_id
    case.hypotheses[hyp.hypothesis_id] = hyp
    return hyp


async def apply_runbook_cause_matcher(
    case: "Case",
    *,
    kb_tool,
    resolve_causes,
    evaluator: "IndicatorEvaluator",
    question: str,
    user_id: str,
    team_ids: Optional[List[str]] = None,
    max_runbooks: int = _DEFAULT_MAX_RUNBOOKS,
    hypothesis_manager: Optional["HypothesisManager"] = None,
) -> Optional[CauseMatchResult]:
    """Match retrieved runbooks against the case and instantiate the winner.

    Runs the structured matcher, picks the first runbook with a confident
    single-Cause verdict, instantiates that Cause's chain as CANDIDATE priors,
    and (when ``hypothesis_manager`` is supplied) attaches a hypothesis to the
    chain's root so it becomes load-bearing. Returns the chosen
    ``CauseMatchResult`` (or None if nothing matched confidently). The matcher is
    conservative by construction: 'none'/'multiple' verdicts instantiate nothing,
    leaving attribution to the LLM.

    A *prior, not a gate*: the engine caller wraps this so it can never break a
    turn.
    """
    matches = await kb_tool.aget_cause_matches(
        question,
        user_id,
        resolve_causes=resolve_causes,
        evaluator=evaluator,
        team_ids=team_ids,
        max_runbooks=max_runbooks,
    )
    chosen = next(
        (m for m in matches if m.verdict == "single" and m.selected_record is not None),
        None,
    )
    if chosen is None:
        return None

    created = instantiate_cause_chain(case, chosen.selected_record, case.current_turn)
    root_id = _root_node_id(case, created)
    attached = None
    if root_id is not None:
        # Stash the runbook's documented fixes on the root node so the prompt
        # context can surface them ONCE this cause is established (4b-3). Stored
        # here (not re-resolved later) because context building is sync; the
        # node.metadata JSON column round-trips. The matcher never creates
        # Solutions — the LLM proposes the fix and the M5 gate governs it.
        _stash_interventions(case, root_id, chosen.selected_record)
        # Record the matched runbook (structured) so the per-turn intake hook can
        # re-resolve the differential from differential_runbook_ids(case).
        _stamp_runbook_id(case, root_id, chosen.runbook_id)
        if hypothesis_manager is not None:
            attached = attach_matched_hypothesis(
                case, chosen, root_id, hypothesis_manager
            )
    logger.info(
        "Runbook cause matcher: instantiated %d node(s) from runbook %s (cause %s); "
        "hypothesis %s",
        len([c for c in created if c]),
        chosen.runbook_id,
        chosen.selected_cause.cause_letter if chosen.selected_cause else "?",
        attached.hypothesis_id if attached else "none/existing",
    )
    return chosen
