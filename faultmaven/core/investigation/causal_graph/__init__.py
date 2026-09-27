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

from faultmaven.core.investigation.cause_assurance import register_graph_hooks

from .clusters import sole_cluster_origin
from .derivation import derive_node_states
from .projection import project_hypothesis_states_from_roots
from .queries import conjuncts_for_chain, mechanism_for_chain
from .support import summarize_restatement_hold, support_count_held_root_ids

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
