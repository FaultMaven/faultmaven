"""Gate overrides compose WITH the LLM's reply — they never hide analysis.

Regression for #656 (case_5db5417fe445, turns 10-11): on gate turns
(resolution needs-info, close pivot) the response override replaced the
LLM's ``agent_response`` wholesale. The model had analyzed the user's
pasted configmap — its hypotheses and solutions were persisted — but the
transcript showed only the canned resolution ask, so the user reasonably
concluded the engine never looked.

Contract pinned here:

- ``_prose_with_gate_notice`` preserves the LLM prose and appends the
  engine gate message below a separator; empty prose yields the gate
  message alone.
- The override branches in the response assembly route their PROSE through
  the composer (static-source guard — the branches live inline in
  ``_process_turn_impl``).
- Follow-up SUGGESTIONS on gate turns remain engine-owned REPLACEMENTS.
  That is a separate, deliberate ownership decision (the #428 "augment"
  experiment was reverted by #430); the prose fix must not re-open it
  (static-source guard).
"""

import inspect
import re

from faultmaven.core.investigation import milestone_engine
from faultmaven.core.investigation.milestone_engine import (
    _prose_with_gate_notice,
)


class TestProseComposition:
    def test_llm_prose_is_preserved_above_gate_message(self):
        composed = _prose_with_gate_notice(
            "The configmap has 5 nameservers; pods allow at most 3.",
            "To mark this **resolved** I need confirmation.",
        )
        assert composed.startswith(
            "The configmap has 5 nameservers; pods allow at most 3."
        )
        assert composed.endswith("To mark this **resolved** I need confirmation.")
        assert "\n\n---\n\n" in composed

    def test_empty_prose_yields_gate_message_only(self):
        gate = "You can **close** the case instead."
        assert _prose_with_gate_notice(None, gate) == gate
        assert _prose_with_gate_notice("", gate) == gate
        assert _prose_with_gate_notice("   \n", gate) == gate

    def test_prose_whitespace_is_trimmed_not_duplicated(self):
        composed = _prose_with_gate_notice("Analysis.\n\n", "Gate.")
        assert composed == "Analysis.\n\n---\n\nGate."


class TestOverrideBranchesUseComposer:
    """Static-source guards on the inline override branches.

    The four gate branches (ready-for-confirmation, suggest-close,
    needs-info first pass, rca-infeasible closure) must feed their prose
    through ``_prose_with_gate_notice``; a refactor that reverts any of
    them to bare assignment silently reintroduces the #656 analysis-hiding
    bug.
    """

    def _impl_source(self):
        return inspect.getsource(milestone_engine.MilestoneEngine._process_turn_impl)

    def test_all_gate_branches_route_prose_through_composer(self):
        src = self._impl_source()
        # One composer call per gate branch, anchored by each branch's
        # engine-text argument.
        for anchor in (
            "_build_resolution_confirmation(case_updated),",
            'metadata["resolution_readiness_message"],',
            'metadata["resolution_needs_info_message"],',
            'metadata["rca_infeasible_closure_message"],',
        ):
            assert anchor in src, f"gate branch anchor missing: {anchor}"
        assert src.count("_prose_with_gate_notice(") >= 4, (
            "an override branch stopped composing prose with the LLM reply "
            "— that re-hides the model's analysis on gate turns (#656)"
        )
        # INVARIANT (not a denylist): NO branch — present or future — may
        # assign an engine-authored metadata message directly to the
        # user-visible prose. A new prose-replacement key added without
        # the composer must fail here, not slip through because it wasn't
        # enumerated.
        direct_assignments = re.findall(r"agent_response_text\s*=\s*metadata\[", src)
        assert not direct_assignments, (
            "a gate branch replaces prose wholesale from metadata — route "
            "it through _prose_with_gate_notice so the LLM's analysis "
            "stays visible (#656)"
        )

    def test_gate_suggestions_remain_engine_owned_replacements(self):
        """The #430 boundary: prose composes, suggestions REPLACE.

        The gate branches must still assign follow_ups outright from the
        engine-owned source — never merge/extend them with the LLM's own
        suggestions.
        """
        src = self._impl_source()
        assert 'follow_ups = metadata["override_suggestions"]' in src
        assert "follow_ups = _close_confirmation_suggestions()" in src
        assert "follow_ups = _resolution_confirmation_suggestions()" in src
        # An "augment" regression would extend rather than assign.
        assert "follow_ups.extend" not in src
