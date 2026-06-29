"""GAP 4 (process realignment): a freshly-created hypothesis is a PRIOR, never a
conclusion. ``create_hypothesis`` caps ``initial_likelihood`` strictly below the
SSOT ``CAUSE_IDENTIFIED_LIKELIHOOD`` gate, so no single emission — an over-confident
LLM ``likelihood: 1.0`` especially — can arrive near-conclusion on creation.
"""

import pytest

from faultmaven.core.investigation.hypothesis_manager import (
    NEW_HYPOTHESIS_MAX_PRIOR,
    HypothesisManager,
)
from faultmaven.core.investigation.terminal_transitions import (
    CAUSE_IDENTIFIED_LIKELIHOOD,
)
from faultmaven.modules.case.contracts import HypothesisCategory

pytestmark = pytest.mark.unit


def _mk(initial_likelihood: float):
    return HypothesisManager().create_hypothesis(
        statement="connection pool exhausted",
        category=HypothesisCategory.DATABASE.value,
        initial_likelihood=initial_likelihood,
        current_turn=1,
    )


def test_cap_is_below_the_identified_gate_ssot():
    """The cap is pinned below the single SSOT gate — the invariant that keeps a
    prior from ever tripping IDENTIFIED. (The import-time assert in
    hypothesis_manager enforces this at boot; this test documents it.)"""
    assert NEW_HYPOTHESIS_MAX_PRIOR < CAUSE_IDENTIFIED_LIKELIHOOD


def test_overconfident_llm_prior_is_capped():
    """The headline GAP 4 case: an LLM emits likelihood=1.0; the stored hypothesis
    is capped at the prior bound, not stored near-conclusion."""
    h = _mk(1.0)
    assert h.likelihood == NEW_HYPOTHESIS_MAX_PRIOR
    # initial_likelihood is also capped — it is the baseline for the evidence
    # climb (update_likelihood_from_evidence starts from it), so the climb must
    # start from the capped prior, not the raw 1.0.
    assert h.initial_likelihood == NEW_HYPOTHESIS_MAX_PRIOR


def test_value_below_cap_passes_through_unchanged():
    """The cap is a ceiling, not a rewrite — a modest prior is preserved (the
    runbook matcher's pre-capped <=0.5 values pass through untouched)."""
    h = _mk(0.3)
    assert h.likelihood == 0.3
    assert h.initial_likelihood == 0.3


def test_value_at_cap_is_preserved():
    h = _mk(NEW_HYPOTHESIS_MAX_PRIOR)
    assert h.likelihood == NEW_HYPOTHESIS_MAX_PRIOR


def test_negative_prior_floored_to_zero():
    """Defensive: a nonsensical negative prior floors to 0.0 (never negative)."""
    h = _mk(-0.5)
    assert h.likelihood == 0.0
