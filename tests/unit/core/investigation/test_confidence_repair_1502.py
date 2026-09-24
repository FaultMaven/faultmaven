"""fm#1502: an out-of-range confidence costs at most the field that carried it.

A provider that does not enforce ``Field(ge=0, le=1)`` (FUNCTION_CALLING,
BEST_EFFORT) can answer ``likelihood: 90`` meaning 90%. On ``main`` Pydantic
rejected it and the backstop pruned the record — or, on a single-object
sub-record with no list index, dropped every ``state_updates``. The ruling
(2026-09-19, amended 2026-09-24) is asymmetric by the field's ROLE:

- ADD-shaped: rescale ``(1, 100]`` as a percentage, coerce a ``bool``;
  anything else prunes that record only.
- UPDATE-shaped, where absence means "keep": drop the field, keep the record.
- Links: set the value aside; ingest decides new (rescale/coerce, else prune)
  versus re-emitted (keep the stored value).

The census below is the declaration of WHERE the rule can be violated: every
[0, 1]-bounded field reachable from the six engine schemas must carry one of
the three policies, and must keep its bound on the schema (#355's fix).
"""

from __future__ import annotations

import json
import math
import typing
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import annotated_types
import pytest
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from faultmaven.core.investigation import milestone_engine as me
from faultmaven.core.investigation import reliability_metrics, schemas
from faultmaven.core.investigation.causal_graph import ingest_emitted_chain
from faultmaven.core.investigation.confidence_repair import (
    CONFIDENCE_REPAIRS_CONTEXT_KEY,
    ConfidenceAction,
    ConfidenceRepair,
    classify,
    decide_link_at_ingest,
    set_aside_link_confidence,
)
from faultmaven.core.investigation.confidence_repair import (
    count as count_confidence_repair,
)
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeType,
    ProblemVerification,
)

pytestmark = pytest.mark.unit

ENGINE_SCHEMAS = [
    schemas.InquiryResponse,
    schemas.TerminalResponse,
    schemas.InvestigationResponse_Diagnosis,
    schemas.InvestigationResponse_Mitigation,
    schemas.InvestigationResponse_Treatment,
    schemas.InvestigationResponse_General,
]

#: Every [0, 1] confidence field and its ROLE, exactly as the ruling assigns it.
#: N = 10. ``test_census_*`` fails if a field is added, removed or loses its
#: bound without this map moving with it.
POLICY = {
    ("ReasoningConclusion", "confidence"): "add",
    ("KnowledgeMatch", "match_likelihood"): "add",
    ("EvidenceToAdd", "likelihood"): "add",
    ("HypothesisToAdd", "likelihood"): "add",
    # Absence means the 0.7 DEFAULT here, not "keep" — so ADD-shaped.
    ("RootCauseConclusionUpdate", "likelihood"): "add",
    ("MilestoneUpdates", "root_cause_likelihood"): "update",
    ("HypothesisUpdate", "likelihood"): "update",
    ("WorkingConclusionUpdate", "likelihood"): "update",
    ("HypothesisEvidenceLinkToAdd", "stance_confidence"): "link",
    ("NodeEvidenceLinkToAdd", "stance_confidence"): "link",
}

HYP_ID = "hyp_aaaaaaaaaaaa"
EV_ID = "ev_aaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# Census: where the rule can be violated
# ---------------------------------------------------------------------------


#: Every shape a numeric bound takes in field metadata: ``Field(ge=, le=)``
#: and ``gt``/``lt`` produce the single-bound types; ``confloat`` and
#: ``annotated_types.Interval`` produce an ``Interval``.
_BOUND_TYPES = (
    annotated_types.Ge,
    annotated_types.Le,
    annotated_types.Gt,
    annotated_types.Lt,
    annotated_types.Interval,
)


def _carries_bound(metadata: Any) -> bool:
    for item in metadata:
        if isinstance(item, _BOUND_TYPES):
            return True
        # ``Annotated[float, Field(ge=0, le=1)]`` keeps the bound on a nested
        # FieldInfo, not on the field's own metadata.
        if isinstance(item, FieldInfo) and _carries_bound(item.metadata):
            return True
        if isinstance(item, annotated_types.GroupedMetadata) and _carries_bound(
            list(item)
        ):
            return True
    return False


def _annotation_is_bounded(annotation: Any) -> bool:
    """A bound anywhere in the annotation — including an ``Annotated`` alias
    inside ``Optional``/``List``/``Dict``, the idiom ``IdRef`` already uses
    eight times in the schemas. The field's top-level ``metadata`` alone misses
    those."""
    if typing.get_origin(annotation) is typing.Annotated:
        base, *metadata = typing.get_args(annotation)
        return _carries_bound(metadata) or _annotation_is_bounded(base)
    return any(_annotation_is_bounded(arg) for arg in typing.get_args(annotation))


def _forward_refs_in(annotation: Any) -> list:
    if typing.get_origin(annotation) is typing.Literal:
        return []  # its strings are values, not references
    if isinstance(annotation, (typing.ForwardRef, str)):
        return [annotation]
    return [ref for arg in typing.get_args(annotation) for ref in _forward_refs_in(arg)]


def _models_in(annotation: Any):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for arg in typing.get_args(annotation):
        yield from _models_in(arg)


def _walk(schemas_to_walk) -> tuple[dict[tuple[str, str], Any], list]:
    """Every bounded field reachable from ``schemas_to_walk``, and every field
    whose annotation the walk could not read (an unresolved forward reference).
    """
    seen: set[type] = set()
    found: dict[tuple[str, str], Any] = {}
    unreadable: list = []

    def visit(model: type) -> None:
        if model in seen:
            return
        seen.add(model)
        for name, info in model.model_fields.items():
            if _carries_bound(info.metadata) or _annotation_is_bounded(info.annotation):
                found[(model.__name__, name)] = info
            refs = _forward_refs_in(info.annotation)
            if refs:
                unreadable.append((model.__name__, name, refs))
            for sub in _models_in(info.annotation):
                visit(sub)

    for schema in schemas_to_walk:
        visit(schema)
    return found, unreadable


def _bounded_fields() -> dict[tuple[str, str], Any]:
    return _walk(ENGINE_SCHEMAS)[0]


def test_census_every_bounded_field_has_a_policy():
    """A new ``Field(ge=, le=)`` in an engine schema must be given a role; the
    whole-record prune (or worse, the drop-all) is what it silently gets
    otherwise."""
    found = _bounded_fields()
    assert len(found) == 10, sorted(found)
    assert set(found) == set(POLICY)


def test_census_can_read_every_reachable_annotation():
    """A quoted forward reference that pydantic leaves unresolved on a
    reachable field hides whatever it names from BOTH walkers that read field
    annotations: this census, and the ladder's
    ``_optional_sub_record_prefix``, which would then drop every
    ``state_updates`` for an error inside it. Neither can see through one, so
    none may exist."""
    _, unreadable = _walk(ENGINE_SCHEMAS)
    assert unreadable == []


def test_the_census_walker_sees_every_shape_a_bound_is_written_in():
    """The census is only as good as its walker. Each shape here is one a
    confidence field could be declared in; the walker must report all of
    them, and must stay quiet on an unbounded float."""
    from typing import Annotated, Dict, List, Optional

    from pydantic import confloat

    Conf = Annotated[float, Field(ge=0.0, le=1.0)]

    class Sub(BaseModel):
        plain: float = Field(0.5, ge=0.0, le=1.0)

    class Probe(BaseModel):
        top_alias: Conf = 0.5
        optional_alias: Optional[Conf] = None
        list_alias: List[Conf] = []
        dict_alias: Dict[str, Conf] = {}
        constrained: Optional[confloat(ge=0.0, le=1.0)] = None
        interval: Annotated[float, annotated_types.Interval(ge=0, le=1)] = 0.5
        strict_bounds: float = Field(0.5, gt=0.0, lt=1.0)
        nested: Optional[Sub] = None
        unbounded: float = 0.5

    found, _ = _walk([Probe])
    assert set(found) == {
        ("Probe", "top_alias"),
        ("Probe", "optional_alias"),
        ("Probe", "list_alias"),
        ("Probe", "dict_alias"),
        ("Probe", "constrained"),
        ("Probe", "interval"),
        ("Probe", "strict_bounds"),
        ("Sub", "plain"),
    }


@pytest.mark.parametrize("key", sorted(POLICY), ids=lambda k: ".".join(k))
def test_the_bound_itself_is_not_relaxed(key):
    """fm#355's fix is the bound on the wire. The repair must live beside it,
    never replace it: ``le=100`` would make every percentage a valid
    ``likelihood`` and the model's meaning ambiguous."""
    model = getattr(schemas, key[0])
    info = model.model_fields[key[1]]
    assert any(
        isinstance(m, annotated_types.Ge) and m.ge == 0 for m in info.metadata
    ), info.metadata
    assert any(
        isinstance(m, annotated_types.Le) and m.le == 1 for m in info.metadata
    ), info.metadata
    prop = model.model_json_schema()["properties"][key[1]]
    numbers = [prop] if "anyOf" not in prop else prop["anyOf"]
    number = next(b for b in numbers if b.get("type") == "number")
    assert (number["minimum"], number["maximum"]) == (0, 1)


def test_the_field_action_vocabulary_is_the_metric_label_set():
    assert {a.value for a in ConfidenceAction} - {"set_aside"} == set(
        reliability_metrics.SCHEMA_FIELD_REPAIR_ACTIONS
    )


def test_a_deferral_is_never_counted_as_an_action():
    """``set_aside`` is not a label: ingest counts the decision it defers to.
    Pinned on ``count`` itself, because the ladder also filters deferrals
    before counting and would hide a regression here."""
    with patch.object(reliability_metrics, "schema_field_repairs_total") as fields:
        count_confidence_repair(
            ConfidenceRepair(
                schema="NodeEvidenceLinkToAdd",
                field="stance_confidence",
                action=ConfidenceAction.SET_ASIDE,
                raw=90,
            )
        )
    assert fields.labels.call_args_list == []


# ---------------------------------------------------------------------------
# classify / decide_link_at_ingest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (0, ("conforming", 0.0)),
        (1, ("conforming", 1.0)),
        (0.42, ("conforming", 0.42)),
        (" 0.5 ", ("conforming", 0.5)),
        (1.5, ("rescaled", 0.015)),
        (90, ("rescaled", 0.9)),
        ("95", ("rescaled", 0.95)),
        (100, ("rescaled", 1.0)),
        (True, ("coerced", 1.0)),
        (False, ("coerced", 0.0)),
        (100.5, ("unrepairable", None)),
        (-0.1, ("unrepairable", None)),
        (float("nan"), ("unrepairable", None)),
        (float("inf"), ("unrepairable", None)),
        (float("-inf"), ("unrepairable", None)),
        ("high", ("unrepairable", None)),
        ("nan", ("unrepairable", None)),
        ([0.9], ("unrepairable", None)),
        ({"v": 1}, ("unrepairable", None)),
        # JSON integers are unbounded; float() raises on this rather than
        # returning inf, and an unwrapped exception would escape the ladder.
        (10**400, ("unrepairable", None)),
        ("1e400", ("unrepairable", None)),
    ],
)
def test_classify(raw, expected):
    kind, value = classify(raw)
    assert kind == expected[0]
    if expected[1] is None:
        assert value is None
    else:
        assert value == pytest.approx(expected[1])


@pytest.mark.parametrize(
    "raw, exists, expected",
    [
        (90, False, (ConfidenceAction.RESCALED, 0.9)),
        (True, False, (ConfidenceAction.COERCED, 1.0)),
        (False, False, (ConfidenceAction.COERCED, 0.0)),
        (float("nan"), False, (ConfidenceAction.PRUNED, None)),
        (-3, False, (ConfidenceAction.PRUNED, None)),
        (90, True, (ConfidenceAction.DROPPED, None)),
        (True, True, (ConfidenceAction.DROPPED, None)),
        (float("nan"), True, (ConfidenceAction.DROPPED, None)),
    ],
)
def test_decide_link_at_ingest(raw, exists, expected):
    action, value = decide_link_at_ingest(raw, re_emitted=exists)
    assert action is expected[0]
    assert value == (pytest.approx(expected[1]) if expected[1] is not None else None)


# ---------------------------------------------------------------------------
# Per-field bodies, driven through the real degradation ladder
# ---------------------------------------------------------------------------


def _hyp(likelihood: Any, statement: str = "h") -> dict:
    return {
        "statement": statement,
        "category": "config",
        "likelihood": likelihood,
        "rationale": "r",
    }


def _ev(likelihood: Any, summary: str = "e") -> dict:
    return {
        "summary": summary,
        "category": "symptom_evidence",
        "source_type": "logs",
        "source_file_id": "file_aaaaaaaaaaaa",
        "likelihood": likelihood,
    }


def _conclusion(confidence: Any, observation: str = "o") -> dict:
    return {"observation": observation, "inference": "i", "confidence": confidence}


def _hlink(confidence: Any, reasoning: str = "r") -> dict:
    return {
        "hypothesis_id_ref": HYP_ID,
        "evidence_id_ref": EV_ID,
        "stance": "refutes",
        "reasoning": reasoning,
        "stance_confidence": confidence,
    }


def _nlink(confidence: Any, reasoning: str = "r") -> dict:
    return {
        "node_ref": "cn_aaaaaaaaaaaa",
        "evidence_id_ref": EV_ID,
        "stance": "refutes",
        "reasoning": reasoning,
        "stance_confidence": confidence,
    }


def _diag(state_updates: dict, **top: Any) -> dict:
    return {"agent_response": "a", "state_updates": state_updates, **top}


class _Case(typing.NamedTuple):
    schema: type
    body: Callable[[Any], dict]
    # the record carrying the value (None when pruned/nulled), and the value
    record: Callable[[Any], Any]
    value: Callable[[Any], Any]
    # a sibling that must survive whatever happens to the record
    sibling_survives: Callable[[Any], bool]


D = schemas.InvestigationResponse_Diagnosis

CASES: dict[tuple[str, str], _Case] = {
    ("ReasoningConclusion", "confidence"): _Case(
        D,
        lambda v: _diag(
            {},
            internal_reasoning={"conclusions": [_conclusion(v), _conclusion(0.5, "s")]},
        ),
        lambda p: next(
            (c for c in p.internal_reasoning.conclusions if c.observation == "o"),
            None,
        ),
        lambda r: r.confidence,
        lambda p: any(c.observation == "s" for c in p.internal_reasoning.conclusions),
    ),
    ("KnowledgeMatch", "match_likelihood"): _Case(
        schemas.InquiryResponse,
        lambda v: {
            "agent_response": "a",
            "state_updates": {
                "knowledge_match": {
                    "match_type": "runbook",
                    "match_likelihood": v,
                    "match_summary": "m",
                },
                "user_confirmed_investigation": True,
            },
        },
        lambda p: p.state_updates.knowledge_match,
        lambda r: r.match_likelihood,
        lambda p: p.state_updates.user_confirmed_investigation is True,
    ),
    ("EvidenceToAdd", "likelihood"): _Case(
        D,
        lambda v: _diag({"evidence_to_add": [_ev(v), _ev(0.5, "s")]}),
        lambda p: next(
            (e for e in p.state_updates.evidence_to_add if e.summary == "e"), None
        ),
        lambda r: r.likelihood,
        lambda p: any(e.summary == "s" for e in p.state_updates.evidence_to_add),
    ),
    ("HypothesisToAdd", "likelihood"): _Case(
        D,
        lambda v: _diag({"hypotheses_to_add": [_hyp(v), _hyp(0.5, "s")]}),
        lambda p: next(
            (h for h in p.state_updates.hypotheses_to_add if h.statement == "h"),
            None,
        ),
        lambda r: r.likelihood,
        lambda p: any(h.statement == "s" for h in p.state_updates.hypotheses_to_add),
    ),
    ("RootCauseConclusionUpdate", "likelihood"): _Case(
        D,
        lambda v: _diag(
            {
                "root_cause_conclusion": {
                    "root_cause": "rc",
                    "mechanism": "m",
                    "likelihood": v,
                },
                "hypotheses_to_add": [_hyp(0.5, "s")],
            }
        ),
        lambda p: p.state_updates.root_cause_conclusion,
        lambda r: r.likelihood,
        lambda p: [h.statement for h in p.state_updates.hypotheses_to_add] == ["s"],
    ),
    ("MilestoneUpdates", "root_cause_likelihood"): _Case(
        D,
        lambda v: _diag(
            {"milestones": {"symptom_verified": True, "root_cause_likelihood": v}}
        ),
        lambda p: p.state_updates.milestones,
        lambda r: r.root_cause_likelihood,
        lambda p: p.state_updates.milestones.symptom_verified is True,
    ),
    ("HypothesisUpdate", "likelihood"): _Case(
        D,
        lambda v: _diag(
            {
                "hypotheses_to_update": [
                    {
                        "hypothesis_id": HYP_ID,
                        "likelihood": v,
                        "state": "refuted",
                        "refutation_reason": "disproved by ev",
                    }
                ]
            }
        ),
        lambda p: next(iter(p.state_updates.hypotheses_to_update), None),
        lambda r: r.likelihood,
        # The refutation riding on the same entry is the sibling that matters.
        lambda p: p.state_updates.hypotheses_to_update[0].state
        == HypothesisState.REFUTED,
    ),
    ("WorkingConclusionUpdate", "likelihood"): _Case(
        D,
        lambda v: _diag(
            {
                "working_conclusion": {"summary": "wc", "likelihood": v},
                "hypotheses_to_add": [_hyp(0.5, "s")],
            }
        ),
        lambda p: p.state_updates.working_conclusion,
        lambda r: r.likelihood,
        lambda p: p.state_updates.working_conclusion.summary == "wc"
        and len(p.state_updates.hypotheses_to_add) == 1,
    ),
    ("HypothesisEvidenceLinkToAdd", "stance_confidence"): _Case(
        D,
        lambda v: _diag({"hypothesis_evidence_links": [_hlink(v), _hlink(0.5, "s")]}),
        lambda p: next(
            (
                link
                for link in p.state_updates.hypothesis_evidence_links
                if link.reasoning == "r"
            ),
            None,
        ),
        lambda r: r.stance_confidence,
        lambda p: any(
            link.reasoning == "s" for link in p.state_updates.hypothesis_evidence_links
        ),
    ),
    ("NodeEvidenceLinkToAdd", "stance_confidence"): _Case(
        D,
        lambda v: _diag({"node_evidence_links": [_nlink(v), _nlink(0.5, "s")]}),
        lambda p: next(
            (
                link
                for link in p.state_updates.node_evidence_links
                if link.reasoning == "r"
            ),
            None,
        ),
        lambda r: r.stance_confidence,
        lambda p: any(
            link.reasoning == "s" for link in p.state_updates.node_evidence_links
        ),
    ),
}


def test_every_policy_field_has_a_driven_case():
    assert set(CASES) == set(POLICY)


def _ladder(body: dict, schema: type):
    """Run the real ladder; return (parsed, outcomes, field-counter labels)."""
    engine = MilestoneEngine.__new__(MilestoneEngine)
    with (
        patch.object(me, "schema_validation_total") as outcomes,
        patch.object(reliability_metrics, "schema_field_repairs_total") as fields,
    ):
        parsed = engine._validate_with_degradation(body, schema)
    return (
        parsed,
        [c.kwargs["outcome"] for c in outcomes.labels.call_args_list],
        [c.kwargs for c in fields.labels.call_args_list],
    )


def _keys(role: str):
    return sorted(k for k, r in POLICY.items() if r == role)


@pytest.mark.parametrize("key", sorted(POLICY), ids=lambda k: ".".join(k))
def test_a_conforming_value_is_untouched_and_clean(key):
    case = CASES[key]
    parsed, outcomes, fields = _ladder(case.body(0.42), case.schema)
    assert case.value(case.record(parsed)) == pytest.approx(0.42)
    assert outcomes == ["clean"]
    assert fields == []


@pytest.mark.parametrize(
    "raw, expected, action",
    [
        (90, 0.9, "rescaled"),
        ("95", 0.95, "rescaled"),
        (100, 1.0, "rescaled"),
        (True, 1.0, "coerced"),
        (False, 0.0, "coerced"),
    ],
)
@pytest.mark.parametrize("key", _keys("add"), ids=lambda k: ".".join(k))
def test_add_shaped_repairs_keep_the_record(key, raw, expected, action):
    case = CASES[key]
    parsed, outcomes, fields = _ladder(case.body(raw), case.schema)
    record = case.record(parsed)
    assert record is not None, "the record was pruned instead of repaired"
    assert case.value(record) == pytest.approx(expected)
    assert case.sibling_survives(parsed)
    assert outcomes == ["repaired"]
    assert fields == [{"schema": key[0], "field": key[1], "action": action}]
    assert [r.action.value for r in parsed._confidence_repairs] == [action]


@pytest.mark.parametrize(
    "raw", [float("nan"), float("inf"), -0.5, 100.5, "high", [0.9], 10**400]
)
@pytest.mark.parametrize("key", _keys("add"), ids=lambda k: ".".join(k))
def test_add_shaped_unrepairable_prunes_its_own_record_only(key, raw):
    """Never a sibling, never every state_updates — the single-object rows
    (``root_cause_conclusion``, ``knowledge_match``) included, which on
    ``main`` fell through to the drop-all rung."""
    case = CASES[key]
    parsed, outcomes, fields = _ladder(case.body(raw), case.schema)
    assert case.record(parsed) is None
    assert case.sibling_survives(parsed)
    assert outcomes == ["pruned"]
    assert fields == [{"schema": key[0], "field": key[1], "action": "pruned"}]


@pytest.mark.parametrize("raw", [90, True, False, float("nan"), -1, "high", 10**400])
@pytest.mark.parametrize("key", _keys("update"), ids=lambda k: ".".join(k))
def test_update_shaped_drops_the_field_and_keeps_the_record(key, raw):
    """``None`` is what the consumer reads as "keep the stored value". A
    rescalable ``90`` is dropped too: the ruling keeps the stored value rather
    than guess, and a ``bool`` must not become the ``1.0`` lax coercion made
    it on ``main``."""
    case = CASES[key]
    parsed, outcomes, fields = _ladder(case.body(raw), case.schema)
    record = case.record(parsed)
    assert record is not None, "the record was pruned; only the field may go"
    assert case.value(record) is None
    assert case.sibling_survives(parsed)
    # A field was discarded, so the body is not ``repaired``.
    assert outcomes == ["pruned"]
    assert fields == [{"schema": key[0], "field": key[1], "action": "dropped"}]


@pytest.mark.parametrize("raw", [90, True, float("nan"), -1, "high", 10**400])
@pytest.mark.parametrize("key", _keys("link"), ids=lambda k: ".".join(k))
def test_links_set_the_value_aside_for_ingest(key, raw):
    case = CASES[key]
    parsed, outcomes, fields = _ladder(case.body(raw), case.schema)
    record = case.record(parsed)
    assert record is not None
    assert "stance_confidence" not in record.model_fields_set
    aside = set_aside_link_confidence(record)
    if isinstance(raw, float) and math.isnan(raw):
        assert math.isnan(aside)
    else:
        assert aside == raw
    assert case.sibling_survives(parsed)
    assert outcomes == ["pruned"]
    # Counted at ingest, which is where the decision is made.
    assert fields == []
    assert parsed._confidence_repairs == []


def test_a_conforming_link_sets_nothing_aside():
    case = CASES[("NodeEvidenceLinkToAdd", "stance_confidence")]
    parsed, _, _ = _ladder(case.body("0.7"), case.schema)
    record = case.record(parsed)
    assert record.stance_confidence == pytest.approx(0.7)
    assert set_aside_link_confidence(record) is None


# ---------------------------------------------------------------------------
# The ladder's own guarantees
# ---------------------------------------------------------------------------


def test_a_failed_attempt_reports_nothing():
    """Validators report into the attempt's context. The first attempt here
    rescales entry 0 and then fails on entry 1; only the attempt that
    succeeded may be counted, or entry 0's rescale is counted twice."""
    parsed, outcomes, fields = _ladder(
        _diag({"hypotheses_to_add": [_hyp(90), _hyp(float("nan"), "bad")]}), D
    )
    assert [h.likelihood for h in parsed.state_updates.hypotheses_to_add] == [
        pytest.approx(0.9)
    ]
    assert outcomes == ["pruned"]
    assert sorted(f["action"] for f in fields) == ["pruned", "rescaled"]
    assert sorted(r.action.value for r in parsed._confidence_repairs) == [
        "pruned",
        "rescaled",
    ]


def test_repaired_is_never_counted_as_clean():
    _, outcomes, _ = _ladder(_diag({"hypotheses_to_add": [_hyp(75)]}), D)
    assert outcomes == ["repaired"]


def test_repaired_yields_to_a_drop_in_the_same_body():
    """``repaired`` applies only when nothing was pruned or dropped."""
    _, outcomes, _ = _ladder(
        _diag(
            {
                "hypotheses_to_add": [_hyp(75)],
                "working_conclusion": {"likelihood": 80},
            }
        ),
        D,
    )
    assert outcomes == ["pruned"]


def test_an_optional_sub_object_is_nulled_for_any_error_not_just_confidence():
    """The prune extension is general: an invalid optional sub-object costs
    that sub-object. ``milestones`` here, which on ``main`` took every
    ``state_updates`` with it."""
    parsed, outcomes, _ = _ladder(
        _diag(
            {
                "milestones": {"symptom_verified": "sometimes"},
                "hypotheses_to_add": [_hyp(0.5, "s")],
            }
        ),
        D,
    )
    assert parsed.state_updates.milestones is None
    assert [h.statement for h in parsed.state_updates.hypotheses_to_add] == ["s"]
    assert outcomes == ["pruned"]


def test_a_non_object_field_error_still_falls_through_to_the_drop_all_rung():
    """Only an OPTIONAL sub-object is nulled. ``outcome`` is an enum, not an
    object; the extension must not swallow it."""
    parsed, outcomes, _ = _ladder(
        _diag({"outcome": "bogus", "hypotheses_to_add": [_hyp(0.5)]}), D
    )
    assert parsed.state_updates.hypotheses_to_add == []
    assert outcomes == ["state_dropped"]


def test_the_drop_all_rung_builds_on_the_pruned_body():
    """A conclusion pruned for an unrepairable confidence must stay pruned
    when a second, unprunable error sends the body to the drop-all rung — on
    ``main`` that rung rebuilt from the ORIGINAL body, the conclusion came
    back, and the turn failed outright."""
    body = _diag(
        {"evidence_to_add": "not-a-list"},
        internal_reasoning={"conclusions": [_conclusion(float("nan"))]},
    )
    parsed, outcomes, fields = _ladder(body, D)
    assert parsed.agent_response == "a"
    assert parsed.internal_reasoning.conclusions == []
    assert outcomes == ["state_dropped"]
    assert fields == [
        {"schema": "ReasoningConclusion", "field": "confidence", "action": "pruned"}
    ]


# ---------------------------------------------------------------------------
# Ingest: node links (causal_graph.ingest_emitted_chain)
# ---------------------------------------------------------------------------


def _graph_case() -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="e",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="Deploy fails",
            problem_statement_confirmed=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="Deploy to on-prem job fails",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.current_turn = 4
    case.evidence.append(
        Evidence(
            evidence_id=EV_ID,
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            primary_purpose="p",
            summary="s",
            extract="x",
            source_type=EvidenceSourceType.LOGS,
            source_file_id="file_aaaaaaaaaaaa",
            collected_by="u",
            collected_at_turn=1,
        )
    )
    return case


def _parsed_node_link(raw: Any, stance: str = "refutes", node_ref: str = "new_index_0"):
    return schemas.NodeEvidenceLinkToAdd.model_validate(
        {
            "node_ref": node_ref,
            "evidence_id_ref": EV_ID,
            "stance": stance,
            "reasoning": "r",
            "stance_confidence": raw,
        }
    )


def _ingest(case: Case, links: list, repairs: list | None = None):
    root = SimpleNamespace(
        statement="root cause", node_type=NodeType.ROOT, produces="D", and_group=None
    )
    with patch.object(reliability_metrics, "schema_field_repairs_total") as fields:
        created = ingest_emitted_chain(
            case, [root], [], links, case.current_turn, validation_repairs=repairs
        )
    return created, [c.kwargs for c in fields.labels.call_args_list]


def _node_links(case: Case, node_id: str):
    return case.causal_nodes[node_id].evidence_links


@pytest.mark.parametrize(
    "raw, expected, action",
    [(90, 0.9, "rescaled"), (True, 1.0, "coerced"), (False, 0.0, "coerced")],
)
def test_new_node_link_is_repaired_at_ingest(raw, expected, action):
    case = _graph_case()
    repairs: list[str] = []
    created, fields = _ingest(case, [_parsed_node_link(raw)], repairs)
    links = _node_links(case, created[0])
    assert [link.stance_confidence for link in links] == [pytest.approx(expected)]
    assert fields == [
        {
            "schema": "NodeEvidenceLinkToAdd",
            "field": "stance_confidence",
            "action": action,
        }
    ]
    assert len(repairs) == 1 and action in repairs[0]


@pytest.mark.parametrize("raw", [float("nan"), -2, 250, "high", 10**400])
def test_new_node_refutes_link_with_unrepairable_value_is_pruned_not_defaulted(raw):
    """The refuting direction: ``None`` on a NEW link means full confidence,
    and a REFUTES link at 1.0 is a decisive disconfirmation. Garbage must not
    buy one — the link is not written at all."""
    case = _graph_case()
    repairs: list[str] = []
    created, fields = _ingest(case, [_parsed_node_link(raw)], repairs)
    assert _node_links(case, created[0]) == []
    assert fields == [
        {
            "schema": "NodeEvidenceLinkToAdd",
            "field": "stance_confidence",
            "action": "pruned",
        }
    ]
    assert len(repairs) == 1 and "pruned" in repairs[0]


@pytest.mark.parametrize("raw", [90, True, float("nan")])
def test_re_emitted_node_link_keeps_its_stored_value(raw):
    case = _graph_case()
    created, _ = _ingest(case, [_parsed_node_link(0.5)])
    node_id = created[0]
    # Re-emit against the now-existing node.
    repairs: list[str] = []
    _, fields = _ingest(case, [_parsed_node_link(raw, node_ref=node_id)], repairs)
    assert [link.stance_confidence for link in _node_links(case, node_id)] == [0.5]
    assert fields == [
        {
            "schema": "NodeEvidenceLinkToAdd",
            "field": "stance_confidence",
            "action": "dropped",
        }
    ]
    assert len(repairs) == 1 and "stored value kept" in repairs[0]


# ---------------------------------------------------------------------------
# Ingest: hypothesis links (_apply_hypothesis_evidence_links + link_evidence)
# ---------------------------------------------------------------------------


def _hyp_case() -> tuple[Case, Hypothesis]:
    case = _graph_case()
    case.evidence[0] = case.evidence[0].model_copy(
        update={"category": EvidenceCategory.SYMPTOM_EVIDENCE}
    )
    h = Hypothesis(
        hypothesis_id=HYP_ID,
        statement="Test hypothesis",
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.ACTIVE,
        likelihood=0.6,
        initial_likelihood=0.6,
        generated_at_turn=case.current_turn,
        last_updated_turn=case.current_turn,
        last_progress_at_turn=case.current_turn,
        iterations_without_progress=0,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        rationale="test",
    )
    case.hypotheses[h.hypothesis_id] = h
    return case, h


def _parsed_hyp_link(raw: Any = ..., stance: str = "refutes"):
    body = {
        "hypothesis_id_ref": HYP_ID,
        "evidence_id_ref": EV_ID,
        "stance": stance,
        "reasoning": "r",
    }
    if raw is not ...:
        body["stance_confidence"] = raw
    return schemas.HypothesisEvidenceLinkToAdd.model_validate(body)


def _apply_links(case: Case, links: list):
    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine.hypothesis_manager = HypothesisManager()
    metadata: dict = {}
    with patch.object(reliability_metrics, "schema_field_repairs_total") as fields:
        engine._apply_hypothesis_evidence_links(case, links, metadata)
    return metadata, [c.kwargs for c in fields.labels.call_args_list]


@pytest.mark.parametrize("raw", [float("nan"), -2, 250, "high"])
def test_new_hypothesis_refutes_link_with_unrepairable_value_is_pruned(raw):
    """``HypothesisEvidenceLinkToAdd`` defaults to 1.0; letting that default
    stand in would record a decisive refutation the model never asserted."""
    case, h = _hyp_case()
    metadata, fields = _apply_links(case, [_parsed_hyp_link(raw)])
    assert h.evidence_links == []
    assert [f["action"] for f in fields] == ["pruned"]
    assert "pruned" in metadata["validation_repairs"][0]


@pytest.mark.parametrize(
    "raw, expected", [(90, 0.9), (True, 1.0), (False, 0.0), ("40", 0.4)]
)
def test_new_hypothesis_link_is_repaired(raw, expected):
    case, h = _hyp_case()
    _apply_links(case, [_parsed_hyp_link(raw)])
    assert [link.stance_confidence for link in h.evidence_links] == [
        pytest.approx(expected)
    ]


@pytest.mark.parametrize("raw", [90, True, float("nan")])
def test_re_emitted_hypothesis_link_keeps_its_stored_value(raw):
    case, h = _hyp_case()
    _apply_links(case, [_parsed_hyp_link(0.4)])
    metadata, fields = _apply_links(case, [_parsed_hyp_link(raw)])
    assert [link.stance_confidence for link in h.evidence_links] == [0.4]
    assert [f["action"] for f in fields] == ["dropped"]
    assert "stored value kept" in metadata["validation_repairs"][0]


@pytest.mark.parametrize("omitted", [..., None])
@pytest.mark.parametrize("stance", ["refutes", "supports"])
def test_an_omitted_confidence_on_a_true_re_emission_keeps_the_stored_value(
    omitted, stance
):
    """The 2026-09-24 ruling, §3: a re-emitted link keeps its stored value,
    "including HypothesisEvidenceLinkToAdd, whose 1.0 default already
    overwrites a stored value today when the field is omitted". A re-emission
    is the same evidence AT THE SAME STANCE. ``main`` wrote 1.0 here, promoting
    a deliberate 0.3 hedge into causal grounding on a routine re-listing (or a
    strict-mode ``null``)."""
    case, h = _hyp_case()
    _apply_links(case, [_parsed_hyp_link(0.3, stance=stance)])
    metadata, fields = _apply_links(case, [_parsed_hyp_link(omitted, stance=stance)])
    assert [
        (link.stance.value, link.stance_confidence) for link in h.evidence_links
    ] == [(stance, 0.3)]
    # Nothing was out of range, so nothing is counted or noted.
    assert fields == []
    assert metadata.get("validation_repairs", []) == []
    # A restatement, not a revision: the #1136 stall arm stays put.
    assert "hypothesis_evidence_links_applied" not in metadata


@pytest.mark.parametrize("omitted", [..., None])
def test_an_omitted_confidence_on_a_stance_flip_is_full_confidence(omitted):
    """A flip is a new claim, so omitted means what it means on a new link —
    full confidence, the schema default — exactly as on ``main`` (H3). The
    stored 0.3 was confidence in the OTHER stance."""
    case, h = _hyp_case()
    _apply_links(case, [_parsed_hyp_link(0.3, stance="refutes")])
    _apply_links(case, [_parsed_hyp_link(omitted, stance="supports")])
    assert [
        (link.stance.value, link.stance_confidence) for link in h.evidence_links
    ] == [("supports", 1.0)]


def test_new_hypothesis_link_without_a_value_gets_full_confidence():
    case, h = _hyp_case()
    _apply_links(case, [_parsed_hyp_link()])
    assert [link.stance_confidence for link in h.evidence_links] == [1.0]


def test_an_explicit_conforming_value_still_overwrites():
    case, h = _hyp_case()
    _apply_links(case, [_parsed_hyp_link(0.4)])
    _apply_links(case, [_parsed_hyp_link(0.8)])
    assert [link.stance_confidence for link in h.evidence_links] == [0.8]


# ---------------------------------------------------------------------------
# A stance FLIP is a new claim, not a re-emission
# ---------------------------------------------------------------------------
#
# "Re-emitted" keyed on evidence_id alone kept the stored confidence under the
# NEW stance: a confident REFUTES re-emitted as SUPPORTS with garbage became a
# confident SUPPORTS — causal grounding on the node axis, and a "material"
# revision that reset the #1136 stall counter. The stored value is confidence
# in the other claim, so absence cannot mean "keep" it: the flip is decided as
# a new link, and pruning it leaves the stored link exactly as ``main`` did.

GARBAGE = [float("nan"), -1, "high", 10**400]


def _stored_hyp_link(case: Case, h: Hypothesis, stance: str, confidence: float):
    from faultmaven.modules.case.contracts import HypothesisEvidenceLink

    h.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=h.hypothesis_id,
            evidence_id=EV_ID,
            stance=EvidenceStance(stance),
            reasoning="r",
            stance_confidence=confidence,
        )
    )


def _hyp_links(h: Hypothesis):
    return [(link.stance.value, link.stance_confidence) for link in h.evidence_links]


@pytest.mark.parametrize("raw", GARBAGE)
def test_a_garbage_stance_flip_leaves_the_stored_hypothesis_link_alone(raw):
    case, h = _hyp_case()
    _stored_hyp_link(case, h, "refutes", 0.9)
    metadata, fields = _apply_links(case, [_parsed_hyp_link(raw, stance="supports")])
    assert _hyp_links(h) == [("refutes", 0.9)]  # main's outcome
    # Not written, so not a material revision: the #1136 stall arm stays put.
    assert "hypothesis_evidence_links_applied" not in metadata
    assert [f["action"] for f in fields] == ["pruned"]


@pytest.mark.parametrize("raw, expected", [(90, 0.9), (True, 1.0)])
def test_a_repairable_stance_flip_is_decided_as_a_new_link(raw, expected):
    case, h = _hyp_case()
    _stored_hyp_link(case, h, "refutes", 0.9)
    metadata, fields = _apply_links(case, [_parsed_hyp_link(raw, stance="supports")])
    assert _hyp_links(h) == [("supports", pytest.approx(expected))]
    assert metadata["hypothesis_evidence_links_applied"] == 1
    assert [f["action"] for f in fields] == ["rescaled" if raw == 90 else "coerced"]


def test_one_body_that_flips_its_own_link_keeps_the_repaired_first_claim():
    """``[REFUTES 90, SUPPORTS 'high']`` in ONE body: the first entry is new
    and rescaled; the second flips the link the first just wrote, with no
    usable number, so it is pruned rather than inheriting 0.9 as SUPPORTS."""
    case, h = _hyp_case()
    parsed, _, _ = _ladder(
        _diag(
            {
                "hypothesis_evidence_links": [
                    {**_hlink(90), "stance": "refutes"},
                    {**_hlink("high"), "stance": "supports"},
                ]
            }
        ),
        D,
    )
    metadata, fields = _apply_links(
        case, parsed.state_updates.hypothesis_evidence_links
    )
    assert _hyp_links(h) == [("refutes", pytest.approx(0.9))]
    assert metadata["hypothesis_evidence_links_applied"] == 1
    assert [f["action"] for f in fields] == ["rescaled", "pruned"]


def _node_with_stored_link(stance: str, confidence: float) -> tuple[Case, str]:
    case = _graph_case()
    created, _ = _ingest(case, [_parsed_node_link(confidence, stance=stance)])
    return case, created[0]


def _node_support_ev_ids(case: Case, node_id: str) -> list:
    from faultmaven.core.investigation.causal_graph import _node_evidence_tally

    return _node_evidence_tally(
        case.causal_nodes[node_id], {EV_ID: EvidenceCategory.CAUSAL_EVIDENCE}
    )[2]


@pytest.mark.parametrize("raw", GARBAGE)
def test_a_garbage_stance_flip_manufactures_no_node_grounding(raw):
    case, node_id = _node_with_stored_link("refutes", 0.9)
    _, fields = _ingest(
        case, [_parsed_node_link(raw, stance="supports", node_ref=node_id)]
    )
    links = _node_links(case, node_id)
    assert [(link.stance.value, link.stance_confidence) for link in links] == [
        ("refutes", 0.9)
    ]
    assert _node_support_ev_ids(case, node_id) == []
    assert [f["action"] for f in fields] == ["pruned"]


def test_a_repairable_node_stance_flip_is_decided_as_a_new_link():
    case, node_id = _node_with_stored_link("refutes", 0.9)
    _, fields = _ingest(
        case, [_parsed_node_link(90, stance="supports", node_ref=node_id)]
    )
    links = _node_links(case, node_id)
    assert [(link.stance.value, link.stance_confidence) for link in links] == [
        ("supports", pytest.approx(0.9))
    ]
    assert [f["action"] for f in fields] == ["rescaled"]


def test_an_omitted_node_stance_flip_is_unchanged_from_main():
    """On the node axis ``main`` already lets an omitted confidence inherit
    the stored value, flip or not. That is an OMITTED value, outside #1502, so
    it is pinned as it stands rather than changed here."""
    case, node_id = _node_with_stored_link("refutes", 0.9)
    _ingest(case, [_parsed_node_link(None, stance="supports", node_ref=node_id)])
    links = _node_links(case, node_id)
    assert [(link.stance.value, link.stance_confidence) for link in links] == [
        ("supports", 0.9)
    ]


# ---------------------------------------------------------------------------
# Apply: an UPDATE-shaped drop leaves the stored value and the rest of the entry
# ---------------------------------------------------------------------------


def _update_engine() -> MilestoneEngine:
    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine.hypothesis_manager = HypothesisManager()
    return engine


def _parsed_update(raw: Any, **extra: Any):
    return schemas.HypothesisUpdate.model_validate(
        {"hypothesis_id": HYP_ID, "likelihood": raw, **extra}
    )


@pytest.mark.parametrize("raw", [90, True, float("nan")])
def test_a_dropped_likelihood_leaves_the_stored_value_and_the_progress_counter(raw):
    """The sentinel attempt demoted a CONFIDENT hypothesis to SPECULATION and
    read the delta as progress. A dropped field writes nothing."""
    case, h = _hyp_case()
    h.likelihood = 0.85
    h.iterations_without_progress = 2
    engine = _update_engine()
    metadata: dict = {}
    engine._apply_hypothesis_updates(
        case, [_parsed_update(raw)], metadata, case.current_turn
    )
    engine._apply_deferred_likelihood_updates(case, metadata, case.current_turn)
    assert h.likelihood == 0.85
    assert h.iterations_without_progress == 2


@pytest.mark.parametrize("raw", [90, True, float("nan")])
def test_a_refutation_is_not_lost_to_its_own_bad_likelihood(raw):
    """The refuting direction on the UPDATE side: on ``main`` the whole entry
    was pruned, so a model certain a hypothesis is dead lost the refutation
    to the number beside it."""
    case, h = _hyp_case()
    engine = _update_engine()
    engine._apply_hypothesis_updates(
        case,
        [_parsed_update(raw, state="refuted", refutation_reason="ev contradicts it")],
        {},
        case.current_turn,
    )
    assert h.state == HypothesisState.REFUTED


# ---------------------------------------------------------------------------
# The tool loop carries the repairs out too (the other way a body arrives)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["done", ""])
async def test_the_schema_tool_path_keeps_the_repairs_on_the_response(answer):
    """The schema-tool call is the tool loop's answer; its parsed response then
    passes through ``_synthesize_agent_response``, which COPIES it when the
    answer is blank. The repairs ride on a private attribute, so both the
    plain and the copied response must still carry them to the apply step."""
    from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall

    body = _diag({"hypotheses_to_add": [_hyp(90)]})
    body["agent_response"] = answer
    call = ToolCall(
        id="call_schema",
        type="function",
        function={"name": D.__name__, "arguments": json.dumps(body)},
    )
    provider = AsyncMock()
    provider.generate = AsyncMock(
        return_value=LLMResponse(
            content="",
            confidence=0.9,
            provider="test",
            model="test-model",
            tokens_used=1,
            response_time_ms=1,
            tool_calls=[call],
        )
    )
    registry = MagicMock()
    registry.get_all_tools.return_value = []
    repo = MagicMock()
    repo.save = AsyncMock()
    engine = MilestoneEngine(
        llm_provider=provider, repository=repo, investigation_tools=registry
    )
    parsed = await engine._tool_augmented_generate(
        prompt="p",
        schema_model=D,
        investigation_tools=[
            {"type": "function", "function": {"name": "search_file", "parameters": {}}}
        ],
        tool_context=MagicMock(),
    )
    assert [h.likelihood for h in parsed.state_updates.hypotheses_to_add] == [
        pytest.approx(0.9)
    ]
    assert [r.action for r in parsed._confidence_repairs] == [ConfidenceAction.RESCALED]


# ---------------------------------------------------------------------------
# End to end: the notes land on the persisted turn record
# ---------------------------------------------------------------------------


class _StrictLLM:
    """A provider double answering one fixed body through the JSON path."""

    def __init__(self, body: dict):
        from faultmaven.infrastructure.llm.structured_output_capability import (
            StructuredOutputCapability,
            StructuredOutputMode,
            StructuredOutputStrategy,
        )

        self._strategy = StructuredOutputStrategy(
            capability=StructuredOutputCapability.BEST_EFFORT,
            mode=StructuredOutputMode.JSON_OBJECT,
            include_schema_in_prompt=True,
            response_format={"type": "json_object"},
        )
        self.generate = AsyncMock(return_value=json.dumps(body))

    def get_structured_output_strategy(self, schema):
        return self._strategy


@pytest.mark.asyncio
async def test_a_turn_records_every_repair_on_its_turn_history():
    """Through ``process_turn``, not a direct call: the validation-time notes
    travel on the response to the apply step, the ingest-time notes are
    appended there, and both reach ``TurnProgress.validation_repairs`` — the
    channel that on ``main`` nothing read."""
    case, h = _hyp_case()
    body = _diag(
        {
            "hypotheses_to_add": [_hyp(80, "new theory")],
            "hypothesis_evidence_links": [
                {
                    "hypothesis_id_ref": "new_index_0",
                    "evidence_id_ref": EV_ID,
                    "stance": "supports",
                    "reasoning": "r",
                    "stance_confidence": True,
                }
            ],
            "root_cause_conclusion": {
                "root_cause": "rc",
                "mechanism": "m",
                "likelihood": 500,
            },
            "working_conclusion": {"summary": "wc", "likelihood": "high"},
        }
    )
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    engine = MilestoneEngine(_StrictLLM(body), repo, investigation_tools=None)

    result = await engine.process_turn(case, "what now?")

    updated = result["case_updated"]
    notes = updated.turn_history[-1].validation_repairs
    joined = "\n".join(notes)
    assert "HypothesisToAdd.likelihood: 80 read as a percentage" in joined
    assert "RootCauseConclusionUpdate.likelihood: 500 unrepairable" in joined
    assert "WorkingConclusionUpdate.likelihood: 'high'" in joined
    assert "HypothesisEvidenceLinkToAdd.stance_confidence: True coerced" in joined
    # The sibling survived the null of root_cause_conclusion — on ``main`` the
    # 500 cost every state_updates, this hypothesis included.
    new = [x for x in updated.hypotheses.values() if x.statement == "new theory"]
    assert len(new) == 1
    assert [link.stance_confidence for link in new[0].evidence_links] == [1.0]


def test_validation_context_key_is_what_the_ladder_passes():
    """A validator reporting under a different key would repair silently."""
    sink: list = []
    schemas.HypothesisToAdd.model_validate(
        _hyp(90), context={CONFIDENCE_REPAIRS_CONTEXT_KEY: sink}
    )
    assert [r.action for r in sink] == [ConfidenceAction.RESCALED]
