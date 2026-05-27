"""Unit tests for the terminal suggestion caps:

1. Regen labels/bodies no longer carry the "(N left)" / "N remaining"
   suffix — the count drives the show/hide gate but is not surfaced to
   the user (the cap is small enough that exposing it adds a
   ticking-clock feel without aiding the choice).
2. The runbook affordance is dropped when ``runbook_already_exists`` is
   True (one generation per case; iterate via Dashboard editor, not via
   repeated chat clicks).
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.milestone_engine import (
    _closed_suggestions,
    _regenerate_resolution_summary_suggestion,
    _resolved_suggestions,
    _runbook_suggestion,
    GENERATE_RUNBOOK_PAYLOAD,
    REGENERATE_CLOSURE_SUMMARY_PAYLOAD,
    REGENERATE_RESOLUTION_SUMMARY_PAYLOAD,
)


class TestRegenSuggestionLabelsAreCountFree:
    def test_resolution_regen_label_has_no_count(self):
        s = _regenerate_resolution_summary_suggestion(remaining=2)
        assert s is not None
        assert s["label"] == "Regenerate resolution summary"
        assert "(" not in s["label"]
        assert "left" not in s["label"]

    def test_resolution_regen_body_has_no_count(self):
        s = _regenerate_resolution_summary_suggestion(remaining=2)
        assert s is not None
        assert "remaining" not in s["body"]
        # The body is informational only; users navigate to the
        # Dashboard to find current vs prior versions.
        assert s["body"] == "Re-create the resolution report."

    def test_resolution_regen_payload_unchanged(self):
        # Behavioural contract: clicking still submits the canonical
        # payload that _process_terminal_turn matches against.
        s = _regenerate_resolution_summary_suggestion(remaining=2)
        assert s["payload"] == REGENERATE_RESOLUTION_SUMMARY_PAYLOAD

    def test_resolution_regen_hidden_when_exhausted(self):
        assert _regenerate_resolution_summary_suggestion(remaining=0) is None
        assert _regenerate_resolution_summary_suggestion(remaining=-1) is None


class _StubClosedCase:
    """Minimal Case stand-in passing the substance gate."""

    def __init__(self) -> None:
        self.status = "closed"
        # The substance gate in terminal_transitions checks fields like
        # description, closure_reason, etc. Provide enough to PASS so the
        # gate doesn't short-circuit the suggestion list.
        self.description = "Real problem statement of substantive length."
        self.closure_reason = "fixed_externally"
        self.problem_verification = None
        self.working_conclusion = None
        self.root_cause_conclusion = None
        self.solutions = []
        self.evidence = []
        self.hypotheses = {}


class TestClosedRegenLabel:
    def test_closed_regen_label_has_no_count(self, monkeypatch):
        # Bypass the substance gate (test focus is the label/body shape).
        from faultmaven.core.investigation import terminal_transitions as tt

        monkeypatch.setattr(tt, "should_generate_terminal_summary", lambda _c: True)
        suggestions = _closed_suggestions(_StubClosedCase(), remaining=2)
        assert len(suggestions) == 1
        s = suggestions[0]
        assert s["label"] == "Regenerate closure summary"
        assert "(" not in s["label"]
        assert "remaining" not in s["body"]
        assert s["payload"] == REGENERATE_CLOSURE_SUMMARY_PAYLOAD


class TestRunbookCap:
    def test_runbook_offered_when_not_yet_generated(self):
        suggestions = _resolved_suggestions(remaining=2, runbook_already_exists=False)
        labels = [s["label"] for s in suggestions]
        assert "Generate runbook from this case" in labels

    def test_runbook_dropped_when_already_exists(self):
        suggestions = _resolved_suggestions(remaining=2, runbook_already_exists=True)
        labels = [s["label"] for s in suggestions]
        assert "Generate runbook from this case" not in labels
        # Regen affordance still present (independent cap).
        assert "Regenerate resolution summary" in labels

    def test_default_offers_runbook(self):
        # The default value of the new parameter must preserve the
        # legacy behaviour (always offer runbook) so callers that have
        # not yet been migrated continue working.
        suggestions = _resolved_suggestions(remaining=2)
        labels = [s["label"] for s in suggestions]
        assert "Generate runbook from this case" in labels

    def test_runbook_suggestion_payload_unchanged(self):
        s = _runbook_suggestion()
        assert s["payload"] == GENERATE_RUNBOOK_PAYLOAD

    def test_both_caps_exhausted_yields_empty_list(self):
        suggestions = _resolved_suggestions(remaining=0, runbook_already_exists=True)
        assert suggestions == []
