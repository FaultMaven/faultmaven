"""Pins the frozen collect→validate seam (``differential_intake``).

The matcher bodies are not implemented yet, so the evaluator stubs are inert and
the contract shape is what's under test here. Both sides build against this.
"""

import pytest

from faultmaven.core.investigation.cause_schemas import CauseRecord
from faultmaven.core.investigation.differential_intake import (
    ActiveCause,
    StanceVerdict,
    evaluate_datum_against_differential,
    recheck_proposed_predicate,
)
from faultmaven.core.investigation.runbook_cause_matcher import resolve_root
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


def test_active_cause_pairs_candidate_id_with_record():
    # A bare CauseRecord isn't cross-runbook-unique; ActiveCause supplies the
    # cross-runbook-unique candidate_id that becomes StanceVerdict.cause_id.
    record = CauseRecord(cause_letter="A", cause_statement="the SC is missing")
    ac = ActiveCause(candidate_id="kb_pvc:A", record=record)
    assert ac.candidate_id == "kb_pvc:A"
    assert ac.record is record
    with pytest.raises(Exception):  # frozen
        ac.candidate_id = "x"  # type: ignore[misc]


def test_resolve_root_may_instantiate_is_keyword_only():
    # The seam pins may_instantiate keyword-only so a positional bool can't
    # silently flip promote/lookup mode at a call site. Binding fails before the
    # body runs, so the case arg is never dereferenced here. (Behavior of the
    # body itself is covered in test_runbook_cause_matcher.py::TestResolveRoot.)
    record = CauseRecord(cause_letter="A")
    with pytest.raises(TypeError):
        resolve_root(None, record, True)  # type: ignore[misc]


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
