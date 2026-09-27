"""Pure causal-graph mechanics for the Two-Dimensional Hypothesis Methodology.

Operates on a case's causal graph — ``causal_nodes`` (dict ``node_id -> CausalNode``)
and ``causal_edges`` (list of ``CausalEdge``) — with NO I/O and NO LLM. These are
the engine-side validation primitives the ``cause_state`` derivation, the
failed-treatment demotion, and (later) the prompt-driven chain emission all build
on, keeping the methodology's load-bearing invariants unit-testable against
hand-built graphs. Mostly pure/deterministic; the exceptions are the two
authoring paths — ``_attach_engine_refutation`` (M6, mints an Evidence row) and
``synthesize_rcc_from_validated_root`` (§9.3, mints a RootCauseConclusion) — which
use a ``uuid4`` id / wall-clock timestamp, so assert on their shape, not exact
equality.

Spec: docs/architecture/investigation-engine/two-dimensional-hypothesis-methodology.md
  - §0 invariants (M4 empirical/deductive validation, M7 AND-proof)
  - §7.1 / §7.1.1 (empirical vs deductive validation, strict exclusion)

Contents: structural primitives (AND-proof, chain-root validation, deductive
strict-exclusion); LLM-emitted-chain ingestion + orphan-chain resolution;
grounded-root promotion; and M6 counterfactual-disconfirmation demotion. (The
transitional flat->graph bridge was removed in PR B2c — the graph is now
emission-only.) Belief propagation (§6.1 / §9.4) is a follow-on.
"""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, NamedTuple
from uuid import uuid4

# The statement/content tokenizer moved to ``cause_assurance`` (the shared
# leaf — the absence-bearing check there needs it and cannot import this
# module); the private aliases keep this module's call sites and the
# calibration tests' import paths stable. ``_stem`` is re-exported solely for
# its stemming-contract unit pins.
from faultmaven.core.investigation.cause_assurance import (
    CAUSAL_STANCE_CONFIDENCE_MIN,
    CONFIRMED_RCC_LIKELIHOOD_FLOOR,
    ENGINE_EVIDENCE_AUTHOR,
    MECHANISTIC_RCC_LIKELIHOOD,
    _stem,  # noqa: F401
    absence_row_link_refused,
    counterfactual_link_decisive,
    register_graph_hooks,
    resolution_confirmation_rows,
    root_counterfactually_confirmed,
)
from faultmaven.core.investigation.cause_assurance import (
    ENGINE_RCC_AUTHOR as _ENGINE_RCC_AUTHOR,
)
from faultmaven.core.investigation.cause_assurance import (
    cached_content_tokens as _cached_content_tokens,
)
from faultmaven.core.investigation.cause_assurance import (
    content_tokens as _content_tokens,
)
from faultmaven.core.investigation.cause_assurance import (
    evidence_category_map as _evidence_category_map,
)
from faultmaven.core.investigation.cause_assurance import (
    problem_anchor_statements as _problem_anchor_statements,
)
from faultmaven.core.investigation.confidence_repair import (
    ConfidenceAction,
    settle_set_aside_link,
)
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.lifecycle_metrics import (
    causal_and_group_regroup_refused_total,
    causal_and_set_late_grouping_total,
    hypothesis_support_mirrored_to_root_total,
    llm_rcc_cause_linked_total,
    llm_rcc_cause_named_total,
    llm_rcc_retracted_disconfirmed_total,
    m6_demotion_refused_total,
    rcc_precedence_inversion_total,
    root_validation_blocked_restatement_total,
    root_validation_blocked_support_count_total,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    CauseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisState,
    InvestigationActionType,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    RootCauseConclusion,
    ValidationMethod,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, Hypothesis

# Observability only. This module derives graph truth and is otherwise free of
# I/O, but it already owns the §7.1/§7.1.2 calibration counters, and the #1096
# grouping events below need a per-case witness a counter cannot carry (which
# group, which members, which turn).
logger = logging.getLogger(__name__)

# Facade re-exports (fm#1707): every name the old flat module defined stays
# importable from this package's top level. Submodules never import back from
# here (see each submodule's own imports).
from .clusters import (
    _AND_GROUP_MAX_LEN,
    _ROOT_DISTINCT_JACCARD,
    _cluster_relations,
    _co_necessary_sets,
    _distinct_cause_partition,
    _edge_adjacency,
    _live_descendant_ids,
    _normalize_and_group,
    _observe_late_grouping,
    _origin_of,
    distinct_cause_clusters,
    mece_contested_root_ids,
    sole_cluster_origin,
)
from .derivation import (
    _survivor_or_sets,
    derive_node_states,
    validate_by_exclusion,
)
from .disconfirmation import (
    _DISCONFIRMATION_REASON,
    _attach_engine_refutation,
    _disconfirmed_cause_trigger,
    _evidence_disconfirmation_provenance,
    _fix_application_turn,
    _has_undatable_solution_acceptance,
    _node_has_counterfactual_refute,
    _node_has_engine_counterfactual_refute,
    _problem_persistence_observed_after,
    any_chain_root_inconclusive,
    demote_disconfirmed_cause_via_evidence,
    m6_disconfirmation_basis,
)
from .ingestion import (
    _normalize_statement,
    chain_path_to_problem,
    ingest_emitted_chain,
    mirror_hypothesis_support_to_root_nodes,
    seed_problem_node,
)
from .projection import (
    _STANDING_HYP_STATES,
    HypothesisProjection,
    _standing_hypotheses,
    any_chain_root_validated,
    project_hypothesis_states_from_roots,
)
from .pruning import (
    _hypothesis_lacks_real_chain,
    _referenced_node_ids,
    prune_abandoned_nodes,
    resolve_orphan_chains,
)
from .queries import (
    DEDUCTIVE_EXCLUSION_MAX_BELIEF,
    _state,
    and_constraints_refuted,
    and_constraints_satisfied,
    conjuncts_for_chain,
    deductively_validated,
    incoming_and_groups,
    is_chain_root_validated,
    mechanism_for_chain,
    validated_and_conjuncts,
)
from .rcc import (
    _chain_outranks_llm_conclusion,
    _conclusion_provider_label,
    _hypothesis_disconfirmed,
    _net_refuted,
    _representative_cause_hypothesis,
    link_llm_rcc_to_cause,
    retract_disconfirmed_rcc,
    retract_stale_engine_rcc,
    synthesize_rcc_from_validated_root,
)
from .similarity import (
    _FRAME_OWNER_JACCARD,
    _HYPOTHESIS_DUPLICATE_JACCARD,
    _MIN_SHARED_TOKENS_FOR_REATTACH,
    _NEGATION_CUES,
    RESTATEMENT_AMBIGUOUS,
    RESTATEMENT_STRONG,
    ROOT_NOVELTY_MIN_FRACTION,
    _has_negation,
    _mutual_mirror,
    _numeric_discriminators,
    _substantive_overlap,
    find_duplicate_hypothesis,
    hypothesis_statements_duplicate,
    restatement_score,
)
from .support import (
    _COUNT_HELD_REASONS,
    _EVIDENCE_MIRROR_JACCARD,
    _MIS_EXACT_MAX,
    BLOCK_REASON_COUNT,
    BLOCK_REASON_HEDGED,
    BLOCK_REASON_MIRROR,
    BLOCK_REASON_RESTATEMENT,
    ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN,
    RestatementHold,
    _causal_evidence_tokens,
    _frame_components,
    _independent_causal_support_count,
    _node_evidence_tally,
    _node_restates,
    _restating_root_ids,
    _support_block_reason,
    restatement_held_root_ids,
    root_restates_case_frame,
    root_support_block_reasons,
    summarize_restatement_hold,
    support_count_held_root_ids,
)

# ---------------------------------------------------------------------------
# Hook registration (inversion seam — see cause_assurance.register_graph_hooks)
# ---------------------------------------------------------------------------

register_graph_hooks(
    support_count_held_root_ids=support_count_held_root_ids,
    derive_node_states=derive_node_states,
    sole_cluster_origin=sole_cluster_origin,
    mechanism_for_chain=mechanism_for_chain,
    project_hypothesis_states_from_roots=project_hypothesis_states_from_roots,
    conjuncts_for_chain=conjuncts_for_chain,
    summarize_restatement_hold=summarize_restatement_hold,
)
