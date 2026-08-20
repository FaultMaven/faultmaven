"""Tests for broadened progress definition.

Verifies that investigative behaviors (data requests, hypothesis testing,
evidence linking) count as progress, not just structural artifacts.
"""

from unittest.mock import MagicMock

import pytest

from faultmaven.modules.case.contracts import TurnOutcome


class TestBroadenedProgressDefinition:
    """Test that investigative behaviors count as progress."""

    @pytest.fixture
    def engine(self):
        """Create a MilestoneEngine instance for testing _check_if_progress_made."""
        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        instance = MilestoneEngine.__new__(MilestoneEngine)
        return instance

    @pytest.fixture
    def empty_metadata(self):
        """Metadata with no structural progress."""
        return {
            "milestones_completed": [],
            "evidence_added": [],
            "hypotheses_generated": [],
            "hypotheses_validated": [],
            "solutions_proposed": [],
            "files_uploaded": [],
            "status_transitioned": False,
            "outcome": TurnOutcome.CONVERSATION,
        }

    def test_data_requested_counts_as_progress(self, engine, empty_metadata):
        """DATA_REQUESTED outcome should count as progress — agent asking for data IS forward motion."""
        empty_metadata["outcome"] = TurnOutcome.DATA_REQUESTED

        assert engine._check_if_progress_made(empty_metadata) is True

    def test_hypothesis_tested_counts_as_progress(self, engine, empty_metadata):
        """HYPOTHESIS_TESTED outcome should count as progress."""
        empty_metadata["outcome"] = TurnOutcome.HYPOTHESIS_TESTED

        assert engine._check_if_progress_made(empty_metadata) is True

    def test_data_provided_alone_does_not_count(self, engine, empty_metadata):
        """DATA_PROVIDED is NOT a standalone progress arm (#1136).

        It is set from ``evidence_added`` in ``determine_turn_outcome``, so honouring
        it here would readmit through the outcome label exactly the duplicate rows
        ``novel_evidence_added`` exists to exclude — a user re-submitting a snapshot
        they already sent would keep resetting ``turns_without_progress``.
        Genuinely new evidence still counts, via the novel key (below).
        """
        empty_metadata["outcome"] = TurnOutcome.DATA_PROVIDED

        assert engine._check_if_progress_made(empty_metadata) is False

    def test_conversation_only_does_not_count(self, engine, empty_metadata):
        """Pure CONVERSATION outcome without structural progress should NOT count."""
        empty_metadata["outcome"] = TurnOutcome.CONVERSATION

        assert engine._check_if_progress_made(empty_metadata) is False

    def test_evidence_links_count_as_progress(self, engine, empty_metadata):
        """A NEW or materially revised evidence link counts as progress even
        without new artifacts.

        Post-#1136 the caller gates this counter on what ``link_evidence``
        reports: storage upserts by ``evidence_id``, so re-emitting a standing
        link leaves the link set unchanged and no longer increments. See
        ``test_stall_net_arming_1136.py`` for the restatement cases.
        """
        empty_metadata["hypothesis_evidence_links_applied"] = 2

        assert engine._check_if_progress_made(empty_metadata) is True

    def test_structural_progress_still_counts(self, engine, empty_metadata):
        """A NEW structural artifact still counts as progress.

        Post-#1136 the arm reads ``novel_evidence_added`` rather than
        ``evidence_added``: the raw list keeps every minted id for positional
        ``new_index_N`` resolution and milestone attribution, while the progress
        reading narrows to rows carrying a datum the case did not already hold.
        """
        empty_metadata["evidence_added"] = ["ev_000000000001"]
        empty_metadata["novel_evidence_added"] = ["ev_000000000001"]

        assert engine._check_if_progress_made(empty_metadata) is True

    def test_duplicate_structural_artifacts_do_not_count(self, engine, empty_metadata):
        """A minted row that only RESTATES what the case holds is not progress (#1136).

        This is the arm that disarmed every stall net: the LLM restates constantly
        while it waits on the user — re-quoting the same log lines, re-proposing the
        standing fix — and each restatement minted a row that reset the counter.
        """
        empty_metadata["evidence_added"] = ["ev_000000000001"]
        empty_metadata["solutions_proposed"] = ["sol_000000000001"]
        empty_metadata["files_uploaded"] = ["file_000000000001"]

        assert engine._check_if_progress_made(empty_metadata) is False

    def test_status_transition_still_counts(self, engine, empty_metadata):
        """Status transitions should still count as progress."""
        empty_metadata["status_transitioned"] = True

        assert engine._check_if_progress_made(empty_metadata) is True

    def test_no_outcome_key_does_not_crash(self, engine):
        """Missing outcome key should not cause an error."""
        metadata = {
            "milestones_completed": [],
            "evidence_added": [],
            "hypotheses_generated": [],
            "hypotheses_validated": [],
            "solutions_proposed": [],
            "files_uploaded": [],
        }

        assert engine._check_if_progress_made(metadata) is False

    def test_zero_evidence_links_does_not_count(self, engine, empty_metadata):
        """Zero evidence links should not count as progress."""
        empty_metadata["hypothesis_evidence_links_applied"] = 0

        assert engine._check_if_progress_made(empty_metadata) is False
