"""The shared reference-set verdict (issue #1232).

Both irreversible-delete sweeps route their safety decision through this, so
it is tested directly rather than only through the two jobs: a verdict matrix
is exactly the kind of logic where the interesting cases are the ones neither
caller happens to exercise.

Run with:
    pytest tests/unit/jobs/test_reference_set.py -v
"""

from __future__ import annotations

import pytest

from faultmaven.jobs.reference_set import (
    REASON_REFERENCE_SET_DISJOINT,
    assess_reference_set,
)


def _assess(candidates, referenced, **kw):
    return assess_reference_set(
        candidates=candidates, referenced=referenced, **{"dry_run": False, **kw}
    )


class TestOverlapIsTheDiscriminator:
    """Emptiness was the first attempt and it left the worse half open."""

    def test_any_overlap_permits_deletion(self):
        v = _assess(["a", "b"], ["a", "zzz"])
        assert not v.disjoint
        assert v.may_delete
        assert not v.refuse
        assert v.overlap_count == 1

    def test_non_empty_but_disjoint_refuses(self):
        """THE hole: a populated reference set sharing nothing.

        Guarding on emptiness passes this and deletes every candidate.
        """
        v = _assess(["a", "b"], ["/var/data/conversions/x.md", "/srv/y.csv"])
        assert v.disjoint
        assert v.refuse
        assert not v.may_delete
        assert v.reason == REASON_REFERENCE_SET_DISJOINT
        assert v.referenced_count == 2, "the set is NOT empty — that is the point"
        assert v.overlap_count == 0

    def test_empty_reference_set_is_just_one_instance_of_disjoint(self):
        v = _assess(["a", "b"], [])
        assert v.disjoint
        assert v.refuse
        assert v.reason == REASON_REFERENCE_SET_DISJOINT

    def test_no_candidates_is_never_suspect(self):
        """An empty candidate set is trivially disjoint from everything.

        Checked before the overlap test on purpose: reporting it as suspect
        would refuse every clean deployment, every night.
        """
        v = _assess([], [])
        assert not v.disjoint
        assert not v.refuse
        v2 = _assess([], ["a"])
        assert not v2.disjoint
        assert not v2.refuse

    def test_duplicate_candidates_do_not_change_the_verdict(self):
        v = _assess(["a", "a", "a"], ["a"])
        assert not v.disjoint
        assert v.candidate_count == 1


class TestReportingIsSeparatedFromDeleting:
    """How the guard avoids deadlocking the deployments it protects."""

    def test_a_dry_run_is_never_refused(self):
        """It deletes nothing, so it cannot lose anything — and it is the
        documented way to diagnose a disjoint set, plus the only way to
        complete the mandatory pre-arming canary."""
        v = _assess(["a"], [], dry_run=True)
        assert v.disjoint
        assert not v.refuse
        assert not v.may_delete, "a dry run must still not delete"
        assert "DRY RUN" in v.message

    def test_acknowledgment_permits_a_live_run(self):
        v = _assess(["a"], [], acknowledged=True)
        assert v.disjoint
        assert not v.refuse
        assert v.may_delete

    def test_acknowledgment_is_off_by_default(self):
        assert _assess(["a"], []).refuse

    def test_acknowledgment_does_not_manufacture_an_overlap(self):
        """It licenses the deletion; it does not claim the sets correspond.

        A caller reading `disjoint` to decide whether to shout must still get
        True, or the acknowledgment would silence the diagnostic as well as
        the refusal.
        """
        v = _assess(["a"], ["zzz"], acknowledged=True)
        assert v.disjoint
        assert v.overlap_count == 0
        assert v.message


class TestTheVerdictIsInternallyConsistent:
    @pytest.mark.parametrize(
        "candidates,referenced,dry_run,acknowledged",
        [
            ([], [], False, False),
            (["a"], ["a"], False, False),
            (["a"], ["a"], True, False),
            (["a"], [], False, False),
            (["a"], [], True, False),
            (["a"], [], False, True),
            (["a"], ["z"], False, False),
            (["a"], ["z"], True, True),
        ],
    )
    def test_refuse_and_may_delete_are_never_both_true(
        self, candidates, referenced, dry_run, acknowledged
    ):
        v = _assess(candidates, referenced, dry_run=dry_run, acknowledged=acknowledged)
        assert not (v.refuse and v.may_delete)
        assert (v.reason is not None) == v.refuse
        if v.disjoint:
            assert v.message, "a suspect verdict must always explain itself"

    def test_a_refusal_always_names_the_override(self):
        v = _assess(["a"], [], acknowledge_flag="--some-flag")
        assert "--some-flag" in v.message
