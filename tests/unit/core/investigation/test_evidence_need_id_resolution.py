"""Phase 6 tests: ``evidence_need_id`` resolution at the follow-up
flattening seam.

The engine's ``_flatten_follow_ups`` helper sits between the LLM-emitted
``SuggestedFollowUp`` objects (which may carry ``new_index_N``
placeholders on ``evidence_need_id`` for same-turn refs) and the
wire-level ``SuggestedActionResponse`` dicts. Resolution rides on the
existing ``_resolve_id_ref`` helper against
``metadata["evidence_needs_updated"]``.

Pinned shape (design §6.2 + §8.5):

- Real ``eneed_xxxxxxxxxxxx`` IDs pass through unchanged.
- ``new_index_N`` placeholders resolve to the Nth entry in
  ``metadata["evidence_needs_updated"]``.
- Unresolvable refs (index out of range) are dropped silently — graceful
  degradation, matches the apply-layer pattern for dangling motivator /
  evidence IDs.
- Non-EVIDENCE suggestions never carry the field (schema-side validator
  in Phase 2 rejects the combination; this layer just doesn't generate
  one for them).
- The two callers (``_process_terminal_qa`` and ``_process_turn_impl``)
  both route through the same helper — refactor pin.

Run:
    pytest tests/unit/core/investigation/test_evidence_need_id_resolution.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine


def _make_engine() -> MilestoneEngine:
    """Bare engine — only the pure helper is exercised."""
    return MilestoneEngine.__new__(MilestoneEngine)


def _make_follow_up(**overrides):
    """SimpleNamespace mimicking SuggestedFollowUp for the helper (it
    reads attributes only — no instance check)."""
    defaults = dict(
        label="Upload metrics",
        action_type="EVIDENCE",
        payload="kubectl top pods",
        body=None,
        cooperative_action=None,
        hints=None,
        evidence_need_id=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _empty_metadata() -> dict:
    return {"evidence_needs_updated": []}


# ============================================================
# Real eneed ID passes through unchanged
# ============================================================


@pytest.mark.unit
class TestRealNeedIdPassthrough:
    def test_real_eneed_id_propagated_to_output_dict(self):
        engine = _make_engine()
        real_id = "eneed_aabbccdd1234"
        follow_ups = [_make_follow_up(evidence_need_id=real_id)]
        out = engine._flatten_follow_ups(follow_ups, _empty_metadata())
        assert len(out) == 1
        assert out[0]["evidence_need_id"] == real_id

    def test_real_eneed_id_passes_even_when_metadata_empty(self):
        """The helper doesn't validate that a real ID is in the
        metadata list — real IDs are LLM-emitted refs to needs that
        may have been created on a prior turn (not this turn's
        ``evidence_needs_updated`` snapshot)."""
        engine = _make_engine()
        real_id = "eneed_aabbccdd1234"
        follow_ups = [_make_follow_up(evidence_need_id=real_id)]
        out = engine._flatten_follow_ups(follow_ups, {"evidence_needs_updated": []})
        assert out[0]["evidence_need_id"] == real_id


# ============================================================
# new_index_N resolution against metadata
# ============================================================


@pytest.mark.unit
class TestNewIndexResolution:
    def test_new_index_0_resolves_to_first_created_need(self):
        engine = _make_engine()
        created = ["eneed_aaaa11112222", "eneed_bbbb33334444"]
        meta = {"evidence_needs_updated": created}
        follow_ups = [_make_follow_up(evidence_need_id="new_index_0")]
        out = engine._flatten_follow_ups(follow_ups, meta)
        assert out[0]["evidence_need_id"] == "eneed_aaaa11112222"

    def test_new_index_1_resolves_to_second_created_need(self):
        engine = _make_engine()
        created = ["eneed_aaaa11112222", "eneed_bbbb33334444"]
        meta = {"evidence_needs_updated": created}
        follow_ups = [_make_follow_up(evidence_need_id="new_index_1")]
        out = engine._flatten_follow_ups(follow_ups, meta)
        assert out[0]["evidence_need_id"] == "eneed_bbbb33334444"

    def test_bare_int_string_coercion_handled_by_schema_not_helper(self):
        """The schema's field_validator coerces bare ints (``0``) →
        ``"new_index_0"`` before the engine ever sees the value. By the
        time ``_flatten_follow_ups`` runs, the value is already a
        string. This test is a contract reminder, not an exercise of
        the helper."""
        # Pass a plain int to confirm the helper does NOT call str()
        # on it — coercion is the schema's job, not the helper's.
        engine = _make_engine()
        meta = {"evidence_needs_updated": ["eneed_aaaa11112222"]}
        follow_ups = [_make_follow_up(evidence_need_id=0)]  # bypassing schema
        out = engine._flatten_follow_ups(follow_ups, meta)
        # ``0`` is falsy → the helper's `if f.evidence_need_id:` guard
        # skips it. Documented behavior: the schema is responsible for
        # coercing bare ints to strings BEFORE this helper runs.
        assert "evidence_need_id" not in out[0]


# ============================================================
# Drop on unresolvable references
# ============================================================


@pytest.mark.unit
class TestUnresolvableRefDropped:
    """Index out of range, empty metadata list, or malformed indices
    all produce the same outcome: drop the field silently. The whole
    turn must not fail just because one suggestion references a stale
    placeholder."""

    def test_new_index_out_of_range_dropped(self):
        engine = _make_engine()
        meta = {"evidence_needs_updated": ["eneed_aaaa11112222"]}  # only index 0
        follow_ups = [_make_follow_up(evidence_need_id="new_index_5")]
        out = engine._flatten_follow_ups(follow_ups, meta)
        # Field dropped; other fields preserved.
        assert "evidence_need_id" not in out[0]
        assert out[0]["label"] == "Upload metrics"

    def test_new_index_with_empty_metadata_dropped(self):
        engine = _make_engine()
        meta = {"evidence_needs_updated": []}
        follow_ups = [_make_follow_up(evidence_need_id="new_index_0")]
        out = engine._flatten_follow_ups(follow_ups, meta)
        assert "evidence_need_id" not in out[0]

    def test_new_index_missing_metadata_key_dropped(self):
        """``metadata.get("evidence_needs_updated", [])`` covers a
        defensive default — Phase 6 turns always carry the key, but a
        future caller might not. Missing key behaves the same as
        empty list."""
        engine = _make_engine()
        follow_ups = [_make_follow_up(evidence_need_id="new_index_0")]
        out = engine._flatten_follow_ups(follow_ups, {})
        assert "evidence_need_id" not in out[0]


# ============================================================
# Drop-counter observability (pairs with evidence_need_rejected_total)
# ============================================================


@pytest.mark.unit
class TestDropCounterObservability:
    """Drops at the response-flattening seam increment
    ``evidence_need_id_dropped_total{reason}``. Symmetric with the
    apply-layer ``evidence_need_rejected_total`` so every drop along
    the evidence-needs pipeline is observable as a ratio, not just a
    log grep."""

    def test_out_of_range_drop_counted_with_out_of_range_label(self):
        from unittest.mock import MagicMock, patch

        engine = _make_engine()
        meta = {"evidence_needs_updated": ["eneed_aaaa11112222"]}
        follow_ups = [_make_follow_up(evidence_need_id="new_index_5")]

        mock_counter = MagicMock()
        with patch(
            "faultmaven.core.investigation.milestone_engine."
            "evidence_need_id_dropped_total",
            mock_counter,
        ):
            engine._flatten_follow_ups(follow_ups, meta)

        mock_counter.labels.assert_called_once_with(reason="out_of_range")
        mock_counter.labels.return_value.inc.assert_called_once()

    def test_missing_metadata_drop_counted_with_missing_metadata_label(self):
        from unittest.mock import MagicMock, patch

        engine = _make_engine()
        follow_ups = [_make_follow_up(evidence_need_id="new_index_0")]

        mock_counter = MagicMock()
        with patch(
            "faultmaven.core.investigation.milestone_engine."
            "evidence_need_id_dropped_total",
            mock_counter,
        ):
            engine._flatten_follow_ups(follow_ups, {})

        mock_counter.labels.assert_called_once_with(reason="missing_metadata")
        mock_counter.labels.return_value.inc.assert_called_once()

    def test_successful_resolution_does_not_increment_counter(self):
        """Real IDs and resolved ``new_index_N`` placeholders should
        not increment the drop counter — only actual drops do."""
        from unittest.mock import MagicMock, patch

        engine = _make_engine()
        meta = {"evidence_needs_updated": ["eneed_aaaa11112222"]}
        follow_ups = [
            _make_follow_up(evidence_need_id="eneed_bbbb33334444"),
            _make_follow_up(evidence_need_id="new_index_0"),
        ]

        mock_counter = MagicMock()
        with patch(
            "faultmaven.core.investigation.milestone_engine."
            "evidence_need_id_dropped_total",
            mock_counter,
        ):
            engine._flatten_follow_ups(follow_ups, meta)

        mock_counter.labels.assert_not_called()


# ============================================================
# Other-field flattening preserved (regression guard)
# ============================================================


@pytest.mark.unit
class TestExistingFieldsFlattenedUnchanged:
    """The Phase 6 refactor extracted the duplicated flattening loops
    into a single helper. The original behavior on label / type /
    payload / body / cooperative_action / hints must round-trip
    unchanged."""

    def test_label_type_payload_required_fields(self):
        engine = _make_engine()
        follow_ups = [
            _make_follow_up(label="L", action_type="COOPERATIVE", payload="P")
        ]
        out = engine._flatten_follow_ups(follow_ups, _empty_metadata())
        assert out[0]["label"] == "L"
        assert out[0]["action_type"] == "COOPERATIVE"
        assert out[0]["payload"] == "P"

    def test_body_propagated_when_present(self):
        engine = _make_engine()
        follow_ups = [_make_follow_up(body="extra context")]
        out = engine._flatten_follow_ups(follow_ups, _empty_metadata())
        assert out[0]["body"] == "extra context"

    def test_body_omitted_when_none(self):
        engine = _make_engine()
        follow_ups = [_make_follow_up(body=None)]
        out = engine._flatten_follow_ups(follow_ups, _empty_metadata())
        assert "body" not in out[0]

    def test_cooperative_action_propagated_when_present(self):
        engine = _make_engine()
        follow_ups = [_make_follow_up(cooperative_action="query_submit")]
        out = engine._flatten_follow_ups(follow_ups, _empty_metadata())
        assert out[0]["cooperative_action"] == "query_submit"

    def test_hints_propagated_when_present(self):
        engine = _make_engine()
        follow_ups = [_make_follow_up(hints=["timeline", "symptoms"])]
        out = engine._flatten_follow_ups(follow_ups, _empty_metadata())
        assert out[0]["hints"] == ["timeline", "symptoms"]

    def test_empty_input_returns_empty_list(self):
        engine = _make_engine()
        assert engine._flatten_follow_ups([], _empty_metadata()) == []


# ============================================================
# Mixed list — multiple suggestions in one turn
# ============================================================


@pytest.mark.unit
class TestMixedSuggestionList:
    def test_real_id_plus_new_index_both_resolve(self):
        engine = _make_engine()
        meta = {"evidence_needs_updated": ["eneed_cccc55556666"]}
        follow_ups = [
            _make_follow_up(label="prior need", evidence_need_id="eneed_aaaa11112222"),
            _make_follow_up(label="this turn", evidence_need_id="new_index_0"),
        ]
        out = engine._flatten_follow_ups(follow_ups, meta)
        assert len(out) == 2
        assert out[0]["evidence_need_id"] == "eneed_aaaa11112222"
        assert out[1]["evidence_need_id"] == "eneed_cccc55556666"

    def test_one_resolves_one_drops_others_unaffected(self):
        engine = _make_engine()
        meta = {"evidence_needs_updated": ["eneed_aaaa11112222"]}
        follow_ups = [
            _make_follow_up(label="a", evidence_need_id="new_index_0"),
            _make_follow_up(label="b", evidence_need_id="new_index_99"),
            _make_follow_up(label="c", evidence_need_id=None),  # no field
        ]
        out = engine._flatten_follow_ups(follow_ups, meta)
        assert out[0]["evidence_need_id"] == "eneed_aaaa11112222"
        assert "evidence_need_id" not in out[1]
        assert "evidence_need_id" not in out[2]


# ============================================================
# Call-site refactor pin — static-source guard
# ============================================================


@pytest.mark.unit
class TestBothCallSitesUseFlattener:
    """Pins that both follow-up flattening seams route through
    ``_flatten_follow_ups``. Without this, a future partial revert
    at one site would re-create the duplication this PR collapsed —
    and the helper-only tests above wouldn't catch it. Same shape as
    the static-source pins used in the lifecycle invariant matrix
    (INV-10, INV-16, etc.)."""

    def test_both_seams_call_flatten_follow_ups(self):
        import inspect

        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        src_terminal = inspect.getsource(MilestoneEngine._process_terminal_qa)
        src_turn = inspect.getsource(MilestoneEngine._process_turn_impl)
        assert "self._flatten_follow_ups(" in src_terminal, (
            "_process_terminal_qa no longer calls _flatten_follow_ups — a "
            "partial revert has re-introduced the duplicated flattening "
            "loop. Either restore the call or update this pin to match a "
            "deliberate redesign."
        )
        assert "self._flatten_follow_ups(" in src_turn, (
            "_process_turn_impl no longer calls _flatten_follow_ups — a "
            "partial revert has re-introduced the duplicated flattening "
            "loop. Either restore the call or update this pin to match a "
            "deliberate redesign."
        )
