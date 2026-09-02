"""Readers of the provenance the KB cause seeder wrote — kept for legacy rows.

The seeder (fm#1295) instantiated a retrieved runbook's cause chains as
CANDIDATE causal nodes and stamped them ``seeded_from_runbook`` (and, on the
root, ``seeded_interventions``). It ran on by default from 2026-07-16 (#727)
to 2026-09-02 (#1302) and was removed afterwards, so **no code writes these
keys any more** — but cases opened in that window persist them, and three
behaviours keyed on them are still correct for those cases and wrong without:

- the seeded-candidate directive in the diagnosis prompt, so the model
  validates/refutes a candidate that is already in its graph instead of
  re-creating it beside the seed (``templates._select_diagnosis_block``);
- the R9 ``<candidate_solutions>`` handoff of the seeded runbook's captured
  interventions once its root is confirmed (``context_builder``);
- the sync tier of runbook-generation dedup — "this case was resolved by
  applying runbook X" — above the async similarity dedup (``milestone_engine``
  offer gate and ``_handle_runbook_creation`` step 0).

**Sunset.** Delete this module, its three call sites and their tests when no
non-terminal case row carries ``seeded_from_runbook`` in ``causal_nodes``
metadata and no RESOLVED case that could still be offered runbook generation
does either. Until then this is live code for a closed population, not a
feature: it has no writer, and nothing here may grow.
"""

from typing import TYPE_CHECKING, Optional

from faultmaven.modules.case.contracts import NodeType

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

# Provenance key on a seeded node's metadata (read surface only).
SEEDED_FROM_RUNBOOK_KEY = "seeded_from_runbook"

# Distinctive prefix of a seeded hypothesis's rationale — the *second* provenance
# surface (alongside SEEDED_FROM_RUNBOOK_KEY). A read surface only: the
# provenance-blindness invariant test greps safety modules for this literal too,
# so a mechanism can never sniff seed origin out of the rationale text either.
SEEDED_RATIONALE_PREFIX = "Seeded from runbook"

# Provenance key on a seeded ROOT node's metadata carrying the cause's runbook
# ``interventions`` (captured at seed time so the SOLUTION-stage context render
# surfaces them as candidate-solution priors without re-fetching the runbook).
# A THIRD origin-derived surface — a node carries it only if it was seeded — so
# the provenance-blindness invariant test bans it from every safety module too.
SEEDED_INTERVENTIONS_KEY = "seeded_interventions"


def case_has_seeded_candidates(case: "Case") -> bool:
    """True if any causal node in the case was seeded from a runbook.

    Used to gate the seeded-candidate AUTHORITY prompt variant: the prompt must
    only tell the LLM "candidates are already in your graph" when that is
    actually true this case (the flag can be on while no runbook matched).
    """
    return any(
        SEEDED_FROM_RUNBOOK_KEY in (node.metadata or {})
        for node in case.causal_nodes.values()
    )


def confirmed_root_seed_origin(case: "Case") -> Optional[str]:
    """The runbook a RESOLVED case's CONFIRMED root cause was SEEDED from, or
    ``None`` if that cause was reached by exploration (or the case has no single
    counterfactually-confirmed root).

    This is the direct provenance signal behind runbook-generation UNIQUENESS
    (Phase 5.2b): if a case was resolved by validating a cause the seeder planted
    from an existing runbook, generating a runbook from it would only duplicate
    that runbook. The offer gate reads this to skip the redundant offer and point
    the user at the covering runbook instead.

    Keyed on the CONFIRMED root's distinct-cause CLUSTER
    (``distinct_cause_clusters``), NOT "does any seed exist on the case"
    (``case_has_seeded_candidates``): a case routinely carries seeded candidates
    the investigation later refuted, and those must never suppress a runbook for
    a DIFFERENT, self-discovered cause. A seed marker anywhere in the confirmed
    cause's cluster counts — the confirmed root itself, a re-emitted duplicate of
    it, or a deepened rung on the same causal line all collapse into one cluster,
    so a seeded candidate the LLM validated-then-restated still resolves to its
    origin. Clustering ranges over ALL roots (not just the confirmed ones) so a
    seeded *candidate* duplicate that never itself validated still collapses onto
    the confirmed root.

    NOT a safety mechanism, and deliberately outside the provenance-blindness
    invariant that keeps a seed indistinguishable from a self-generated
    hypothesis for every VALIDATION / decay / anchoring / gating path. The only
    reader is the runbook-generation OFFER gate — a knowledge-lifecycle decision,
    not a conclusion. The worst outcome of a wrong answer here is a missing or
    redundant "generate runbook" affordance, never an incorrect conclusion or a
    collapse under pressure: the manual ``POST /knowledge/runbooks/create``
    escape hatch and the async similarity dedup (which surfaces a ≥70% match
    by title and score for the user to judge) both
    remain. Known false-negatives (a reused node the seeder never restamped, a
    ``BENIGN_DEDUP`` overlap, a retrieval miss) are exactly why this is a cheap
    SYNC tier ABOVE the similarity backstop, not a replacement for it.
    """
    nodes = getattr(case, "causal_nodes", None) or {}
    for member_id in _confirmed_cause_cluster_members(case):
        origin = (nodes[member_id].metadata or {}).get(SEEDED_FROM_RUNBOOK_KEY)
        if origin:
            return origin
    return None


def confirmed_cause_interventions(case: "Case") -> list[dict[str, str]]:
    """The runbook ``interventions`` captured on a RESOLVED case's CONFIRMED root
    cause, or ``[]`` if that cause was self-discovered (or the case has no single
    counterfactually-confirmed seeded root).

    The R9 read half of seed-time interventions capture: at seed time
    ``_seed_one_cause`` stashes a cause's ``interventions`` list onto its ROOT
    node's metadata (``SEEDED_INTERVENTIONS_KEY``); once that root is
    counterfactually confirmed, the SOLUTION-stage context render surfaces those
    interventions as CANDIDATE-solution priors (quadrant + text) so the LLM emits
    a quadrant-carrying ``SolutionToAdd`` instead of re-deriving the fix from
    prose. Keyed on the confirmed root's distinct-cause CLUSTER exactly like
    ``confirmed_root_seed_origin`` (a validated-then-restated seed still resolves
    to its captured interventions).

    NOT a safety mechanism, and deliberately outside the provenance-blindness
    invariant: the only reader is the prompt-render path (a prior offered to the
    LLM, still gated by M5 and the user's accept/verify), never a VALIDATION /
    decay / anchoring / gating path. Returns the seed-time-sanitized list verbatim
    (``[{"quadrant","ref","text"}]``); an empty list when nothing was captured.
    """
    nodes = getattr(case, "causal_nodes", None) or {}
    for member_id in _confirmed_cause_cluster_members(case):
        interventions = (nodes[member_id].metadata or {}).get(SEEDED_INTERVENTIONS_KEY)
        if interventions:
            return interventions
    return []


def _confirmed_cause_cluster_members(case: "Case") -> list[str]:
    """Node ids of every member of the CONFIRMED root's distinct-cause cluster(s).

    Shared core of the two case-level origin readers
    (``confirmed_root_seed_origin`` + ``confirmed_cause_interventions``): find the
    cluster(s) containing a counterfactually-confirmed VALIDATED root and return
    their members (in cluster iteration order) for a provenance scan. Empty when
    the case has no single counterfactually-confirmed root.

    Clusters over the LIVE roots only. A REFUTED seeded root must never be the
    basis for "resolved by applying runbook X" — a disproven seed was, by
    definition, NOT what resolved the case. Excluding them before clustering also
    honors ``_live_descendant_ids``'s precondition (endpoints are never REFUTED):
    it prunes refuted intermediates but not a refuted START node, so a refuted
    seeded root could otherwise cluster with a deeper confirmed root on its line
    and falsely claim its origin. Seeded-candidate duplicates are CANDIDATE (not
    REFUTED) and are kept, so the duplicate-collapse holds. Clustering ranges over
    ALL live roots (not just the confirmed ones) so a seeded *candidate* duplicate
    that never itself validated still collapses onto the confirmed root.
    """
    from faultmaven.core.investigation.causal_graph import distinct_cause_clusters
    from faultmaven.core.investigation.cause_assurance import (
        evidence_category_map,
        root_counterfactually_confirmed,
    )
    from faultmaven.modules.case.contracts import NodeState

    nodes = getattr(case, "causal_nodes", None) or {}
    live_root_ids = {
        nid
        for nid, node in nodes.items()
        if node.node_type == NodeType.ROOT and node.node_state != NodeState.REFUTED
    }
    if not live_root_ids:
        return []

    cat_by_id = evidence_category_map(case)
    confirmed_root_ids = {
        rid
        for rid in live_root_ids
        if nodes[rid].node_state == NodeState.VALIDATED
        and root_counterfactually_confirmed(nodes[rid], cat_by_id)
    }
    if not confirmed_root_ids:
        return []

    members: list[str] = []
    for cluster in distinct_cause_clusters(case, live_root_ids):
        if not cluster & confirmed_root_ids:
            continue
        members.extend(cluster)
    return members
