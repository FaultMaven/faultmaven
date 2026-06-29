"""Pins the frozen collect→validate seam (``differential_intake``).

The matcher bodies are not implemented yet, so the evaluator stubs are inert and
the contract shape is what's under test here. Both sides build against this.
"""

import pytest

from faultmaven.core.investigation.differential_intake import (
    StanceVerdict,
    evaluate_datum_against_differential,
    recheck_proposed_predicate,
)
from faultmaven.modules.case.contracts import EvidenceStance

pytestmark = pytest.mark.unit


def test_stance_verdict_shape_is_frozen_with_provenance_and_structured_predicate():
    v = StanceVerdict(
        cause_id="rb1:A",  # differential candidate id, not a node_id
        stance=EvidenceStance.SUPPORTS,
        provenance="runbook",
        predicate={"predicate": "contains", "target": "NotFound"},
    )
    assert v.cause_id == "rb1:A"
    assert v.provenance == "runbook"
    assert v.predicate["predicate"] == "contains"  # round-trippable spec, not prose
    with pytest.raises(Exception):  # frozen — verdict is an immutable judgment
        v.cause_id = "x"  # type: ignore[misc]


def test_evaluator_stubs_are_inert_until_matcher_body_lands():
    # Runbook tier: returns [] (no verdicts) so the intake loop is a no-op.
    assert (
        evaluate_datum_against_differential(evidence=None, active_causes=[], case=None)
        == []
    )
    # Fallback tier: returns None when nothing fires.
    assert (
        recheck_proposed_predicate(
            evidence=None, cause_id="rb1:A", proposed_predicate={}, case=None
        )
        is None
    )
