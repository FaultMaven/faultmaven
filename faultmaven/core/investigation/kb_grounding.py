"""Whether a retrieved KB hit may back a seeded candidate cause.

A two-field predicate over a retrieval result. It lives in its own module
because its callers are not all in the investigation engine: the KB
cause-seeder eval applies it to raw retrieval output, and making that import
``milestone_engine`` would drag twelve thousand lines of turn orchestration in
to answer a question about a search result.

The rule, its measurement, and why there is only one ground are documented at
``milestone_engine.KB_SEED_MIN_CORROBORATING_CHUNKS``' neighbouring block —
see "WHY THERE IS NO SECOND, 'COVERS' GROUND (#1285)".
"""

from enum import Enum
from typing import Any


class KBSeedGrounding(str, Enum):
    """Why a retrieved hit may or may not back a seeded cause.

    Three values, not a boolean, because "the query did not name this runbook"
    and "nothing measured whether it did" are different facts that the gate
    acts on differently — and because ``identity_terms_in_query`` is an empty
    list in BOTH cases, so emptiness cannot tell them apart.
    """

    NAMED = "named"
    UNGROUNDED = "ungrounded"
    UNMEASURED = "unmeasured"


def kb_hit_grounding(hit: Any) -> KBSeedGrounding:
    """Grounding verdict for one retrieved hit. The single source for the gate.

    Shared with ``tests/eval/kb_cause_seeder/run_corroboration_eval.py``, which
    re-measures this gate against the shipped corpus: an eval that re-implements
    the predicate cannot observe a defect in it, which is how #1285 stayed
    invisible while the eval reported on it.

    ``term_coverage`` is read for ONE purpose here — as the witness that the
    reranker ran at all. Only ``KnowledgeVectorStore._rerank`` writes it, and it
    writes a float on every path it takes, INCLUDING when no corpus term index
    was available (there ``_compute_term_overlap`` degrades to an unweighted
    binary fraction rather than returning None). So ``None`` means "no lexical
    grounding evidence was computed for this hit" — the pure-vector path — and
    never "the term index was missing". The distinction matters: it is what
    keeps a term-index outage from silently switching this gate off.

    UNMEASURED is deliberately permissive downstream: an absent measurement must
    not authorise what the gate withholds, but neither may it disable seeding
    wholesale. It is counted and logged rather than passed over in silence,
    because a gate that has quietly stopped applying looks exactly like a gate
    that is applying and finding nothing.
    """
    named = getattr(hit, "identity_terms_in_query", None) or []
    if named:
        return KBSeedGrounding.NAMED
    if getattr(hit, "term_coverage", None) is None:
        return KBSeedGrounding.UNMEASURED
    return KBSeedGrounding.UNGROUNDED
