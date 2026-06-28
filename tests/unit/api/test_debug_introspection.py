"""Unit tests for the debug causal-graph payload builder.

The fm-sre-simulator chain probe consumes this payload to validate the runbook
Cause matcher. The load-bearing requirement: each hypothesis's ``rationale`` is
exposed, because that is the matcher's firing signal — a matcher-seeded
hypothesis carries the ``RUNBOOK_MATCH_RATIONALE_PREFIX`` marker, an LLM-emitted
one does not, and the probe must tell them apart.
"""

from types import SimpleNamespace

import pytest

from faultmaven.api.debug_introspection import build_causal_graph_debug_payload
from faultmaven.core.investigation.runbook_cause_matcher import (
    RUNBOOK_MATCH_RATIONALE_PREFIX,
    is_runbook_match_hypothesis,
)
from faultmaven.modules.case.domain.models import (
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
)

pytestmark = pytest.mark.unit


def _hyp(statement: str, rationale: str, **kw) -> Hypothesis:
    kw.setdefault("generated_at_turn", 1)
    kw.setdefault("generation_mode", HypothesisGenerationMode.OPPORTUNISTIC)
    return Hypothesis(
        statement=statement,
        category=HypothesisCategory.OTHER,
        rationale=rationale,
        **kw,
    )


def _case(hypotheses: dict) -> SimpleNamespace:
    # The builder is duck-typed; a namespace with the read attrs is enough.
    return SimpleNamespace(
        case_id="case_abc123",
        current_turn=3,
        causal_nodes={},
        causal_edges=[],
        hypotheses=hypotheses,
        progress=None,
        root_cause_conclusion=None,
    )


class TestRationaleExposure:
    def test_matcher_hypothesis_rationale_is_exposed(self):
        matcher_rationale = (
            f"{RUNBOOK_MATCH_RATIONALE_PREFIX} kb_runbook_pvc_pending "
            "(cause A: storage class missing)"
        )
        h = _hyp("The PVC has no matching StorageClass", matcher_rationale)
        payload = build_causal_graph_debug_payload(_case({h.hypothesis_id: h}))

        dumped = payload["hypotheses"][h.hypothesis_id]
        assert dumped["rationale"] == matcher_rationale
        # The exposed rationale round-trips through the matcher's own detector —
        # the probe can rely on the same prefix the engine uses.
        assert dumped["rationale"].startswith(RUNBOOK_MATCH_RATIONALE_PREFIX)

    def test_probe_can_distinguish_matcher_from_llm_hypothesis(self):
        seeded = _hyp(
            "Seeded cause",
            f"{RUNBOOK_MATCH_RATIONALE_PREFIX} kb_x (cause A)",
        )
        llm = _hyp("LLM cause", "Derived from the user's stack trace")
        payload = build_causal_graph_debug_payload(
            _case({seeded.hypothesis_id: seeded, llm.hypothesis_id: llm})
        )

        # Reconstruct the firing check the way the probe would, from exposed data.
        is_matcher = {
            hid: str(h["rationale"] or "").startswith(RUNBOOK_MATCH_RATIONALE_PREFIX)
            for hid, h in payload["hypotheses"].items()
        }
        assert is_matcher[seeded.hypothesis_id] is True
        assert is_matcher[llm.hypothesis_id] is False
        # And it agrees with the engine's own helper on the live objects.
        assert is_runbook_match_hypothesis(seeded) is True
        assert is_runbook_match_hypothesis(llm) is False


class TestPayloadShape:
    def test_core_fields_and_chain_link_present(self):
        h = _hyp(
            "cause",
            "why",
            likelihood=0.5,
            initial_likelihood=0.5,
            state=HypothesisState.ACTIVE,
            root_node_id="cn_root1",
        )
        payload = build_causal_graph_debug_payload(_case({h.hypothesis_id: h}))

        assert payload["case_id"] == "case_abc123"
        assert payload["current_turn"] == 3
        assert payload["cause_state"] is None  # progress=None
        dumped = payload["hypotheses"][h.hypothesis_id]
        # Chain link + capped-prior soundness fields the sim asserts on.
        assert dumped["root_node_id"] == "cn_root1"
        assert dumped["likelihood"] == 0.5
        assert dumped["initial_likelihood"] == 0.5
        assert dumped["state"] == "active"

    def test_empty_graph_serializes_clean(self):
        payload = build_causal_graph_debug_payload(_case({}))
        assert payload["hypotheses"] == {}
        assert payload["causal_nodes"] == {}
        assert payload["causal_edges"] == []
        assert payload["problem_node_id"] is None
        assert payload["root_cause_conclusion"] is None
