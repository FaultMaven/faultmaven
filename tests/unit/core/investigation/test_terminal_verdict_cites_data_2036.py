"""A terminal verdict cites data (#2036 D6), and a refuted root holds nothing
(#2059).

A root used to be identified on a bare ``root_is_terminal: true``, with a
free-text basis nothing read: a run marked its root terminal because renewing
the credential "resolves the authentication error". Now a ``true`` stands only
on a cited non-engine causal_evidence row recorded at or after the root's first
attach (the same turn, and on a staged replay the same emission, count). One
citing none is refused at write time, with feedback, and the root's why stays
open. This is a floor: it cannot judge what the row says.

The open-WHY hold admitted a root its hypothesis marked ``false`` whatever its
evidence, a REFUTED one included, and rendered recovery advice for it.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.causal_graph.projection import held_open_root_ids
from faultmaven.core.investigation.cause_assurance import (
    ENGINE_EVIDENCE_AUTHOR,
    cause_recheck_status,
    confirming_polarity_rows,
)
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_cause_state_from_chain,
)
from faultmaven.core.investigation.milestone_engine.response_application import (
    WHY_OPEN_DOWNGRADE_REASON,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _withdraw_unlicensed_solution_offers,
)
from faultmaven.core.investigation.prompts.context_builder import (
    causal_graph_block as cgb,
)
from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
    _build_causal_graph_block,
)
from faultmaven.core.investigation.prompts.templates.diagnosis import (
    _CHAIN_EMISSION_BLOCK,
)
from faultmaven.core.investigation.schemas import (
    CausalNodeToAdd,
    EvidenceToAdd,
    HypothesisUpdate,
)
from faultmaven.core.investigation.terminal_transitions import (
    cause_identification_leg,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    CauseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisState,
    InvestigationActionType,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProposedAction,
    RootCauseConclusion,
)
from tests.unit.core.investigation.bound_rows import bound_evidence, upload
from tests.unit.core.investigation.narrated import narrated
from tests.unit.core.investigation.test_terminal_root_identification_2036 import (
    _open_chain_case,
)

pytestmark = pytest.mark.unit

_ATTACH = 4
_REFUSED = "was NOT marked terminal"


def _row(case, turn, label, *, category=EvidenceCategory.CAUSAL_EVIDENCE, **kw):
    row = Evidence(
        evidence_id="ev_" + hashlib.md5(label.encode()).hexdigest()[:12],
        summary=label,
        primary_purpose="diagnosis",
        category=category,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=kw.pop("source_file_id", "file_0123456789ab"),
        collected_by=kw.pop("collected_by", "llm"),
        collected_at_turn=turn,
        collected_at=datetime.now(UTC),
    )
    case.evidence.append(row)
    return row.evidence_id


def _d6_case():
    """A root validated on two supports, first posited at turn 4, with no
    verdict yet; the turn is 5."""
    case, root, hyp = _open_chain_case()
    root.metadata["root_first_attached_turn"] = _ATTACH
    case.current_turn = 5
    return case, root, hyp


def _apply(case, **fields) -> dict:
    from tests.unit.core.investigation.test_m5_recovery_1679 import (
        _make_engine,
        _meta,
        _Updates,
    )

    meta = _meta()
    asyncio.run(
        _make_engine().responses._apply_investigation_updates(
            case, _Updates(**fields), meta
        )
    )
    # The apply step records codes; the narration step renders them (#2636).
    narrated(case, meta)
    return meta


def _mark_terminal(case, hyp, cited, **fields) -> dict:
    return _apply(
        case,
        hypotheses_to_update=[
            HypothesisUpdate(
                hypothesis_id=hyp.hypothesis_id,
                root_is_terminal=True,
                terminal_basis_evidence_ids=cited,
            )
        ],
        **fields,
    )


def _assert_held(case, root):
    assert root.root_terminal is None
    assert root.node_id in held_open_root_ids(case)
    assert case.progress.cause_identification_held_open is True
    assert case.progress.cause_state == CauseState.CANDIDATES


# ---------------------------------------------------------------------------
# D6: refused at write time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cite, reason",
    [
        ("nothing", "None cited"),
        ("before the attach", "recorded at turn 3, before the root was posited"),
        ("an engine row", "recorded by the engine"),
        ("a symptom row", "symptom_evidence, not causal_evidence"),
        ("no row", "not on record"),
    ],
)
def test_a_true_verdict_with_no_qualifying_row_is_refused_and_held(cite, reason):
    case, root, hyp = _d6_case()
    cited = {
        "nothing": [],
        "before the attach": [_row(case, 3, "the secret_id has a fixed TTL")],
        "an engine row": [
            _row(case, 5, "engine note", collected_by=ENGINE_EVIDENCE_AUTHOR)
        ],
        "a symptom row": [
            _row(case, 5, "401s", category=EvidenceCategory.SYMPTOM_EVIDENCE)
        ],
        "no row": ["ev_00000000dead"],
    }[cite]
    meta = _mark_terminal(case, hyp, cited)
    _assert_held(case, root)
    feedback = "\n".join(meta["system_notices"])
    assert f"Root {root.node_id} {_REFUSED}" in feedback
    assert "terminal_basis_evidence_ids" in feedback
    assert reason in feedback
    # The graph block's WHY note asks for the datum.
    assert "terminal_basis_evidence_ids" in _build_causal_graph_block(case)


# ---------------------------------------------------------------------------
# D6: a cited row identifies
# ---------------------------------------------------------------------------


def test_a_row_recorded_on_the_attach_turn_identifies():
    case, root, hyp = _d6_case()
    basis = _row(case, _ATTACH, "no CronJob or rotation job renews the secret_id")
    before = _row(case, 3, "the secret_id expired")
    meta = _mark_terminal(case, hyp, [before, basis])
    assert root.root_terminal is True
    assert root.metadata["root_terminal_evidence_ids"] == [basis]
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _REFUSED not in "\n".join(meta.get("system_notices") or [])


def test_a_same_turn_new_index_row_identifies():
    case, root, hyp = _d6_case()
    fresh = upload(case, 5)
    _mark_terminal(
        case,
        hyp,
        ["new_index_0"],
        evidence_to_add=[
            EvidenceToAdd(
                summary="the secret_id was issued by hand with a 30-day TTL",
                extract="secret_id_ttl=720h; no rotation job in the namespace",
                category=EvidenceCategory.CAUSAL_EVIDENCE,
                source_type=EvidenceSourceType.LOGS,
                source_file_id=fresh,
            )
        ],
    )
    (new,) = [e for e in case.evidence if e.collected_at_turn == 5]
    assert root.metadata["root_terminal_evidence_ids"] == [new.evidence_id]
    assert case.progress.cause_state == CauseState.IDENTIFIED


def test_a_row_recorded_after_the_attach_from_an_early_upload_qualifies():
    """The floor is the turn the row was recorded, not its data's date."""
    case, root, hyp = _d6_case()
    early = upload(case, 1, "bundle")
    basis = _row(case, 5, "TTL read from the turn-1 bundle", source_file_id=early)
    _mark_terminal(case, hyp, [basis])
    assert case.progress.cause_state == CauseState.IDENTIFIED


def test_a_stored_true_without_a_citation_reads_none_and_is_held():
    case, root, _hyp = _d6_case()
    root.metadata["root_terminal"] = True
    _recompute_cause_state_from_chain(case)
    assert root.is_identified_root is False
    _assert_held(case, root)


def test_a_true_re_sent_on_a_cited_root_is_absorbed_or_extends_it():
    case, root, hyp = _d6_case()
    basis = _row(case, _ATTACH, "no rotation job renews the secret_id")
    _mark_terminal(case, hyp, [basis])
    case.current_turn = 6
    meta = _mark_terminal(case, hyp, [])  # nothing qualifying: absorbed
    assert _REFUSED not in "\n".join(meta.get("system_notices") or [])
    assert root.metadata["root_terminal_evidence_ids"] == [basis]
    before = _row(case, 3, "the secret_id expired")
    meta = _mark_terminal(case, hyp, [before])  # named, still absorbed
    feedback = "\n".join(meta["system_notices"])
    assert _REFUSED not in feedback
    assert f"{before} (recorded at turn 3, before the root was posited)" in feedback
    assert "(the root's verdict stands on its cited rows)" in feedback
    assert root.metadata["root_terminal_evidence_ids"] == [basis]
    assert "root_terminal_refused_turn" not in root.metadata
    assert case.progress.cause_state == CauseState.IDENTIFIED
    more = _row(case, 6, "the issuer role pins secret_id_ttl")
    _mark_terminal(case, hyp, [more])
    assert root.metadata["root_terminal_evidence_ids"] == [basis, more]


def _root_line(block, node_id):
    return next(ln for ln in block.splitlines() if ln.strip().startswith(node_id))


def test_a_refused_true_on_a_root_outside_the_hold_still_renders_the_ask():
    """Render only (#2036 review): the root is not validated, so not held open,
    and the refusal moves no hold."""
    case, root, hyp = _d6_case()
    root.evidence_links = root.evidence_links[:1]  # count-held, not validated
    _recompute_cause_state_from_chain(case)
    assert root.node_state == NodeState.INCONCLUSIVE
    held = (held_open_root_ids(case), case.progress.cause_identification_held_open)
    _mark_terminal(case, hyp, [])
    assert root.metadata["root_terminal_refused_turn"] == 5
    assert (
        held_open_root_ids(case),
        case.progress.cause_identification_held_open,
    ) == held
    line = _root_line(_build_causal_graph_block(case), root.node_id)
    assert cgb._RECOVERY_NOTES[cgb._VERDICT_REFUSED] in line
    assert "terminal_basis_evidence_ids" in line
    # A recorded verdict clears the mark.
    _mark_terminal(case, hyp, [_row(case, 5, "TTL fixed at issue")])
    assert "root_terminal_refused_turn" not in root.metadata


def _refused_count_held():
    """A count-held (INCONCLUSIVE) root whose ``true`` was refused."""
    case, root, hyp = _d6_case()
    root.evidence_links = root.evidence_links[:1]
    _recompute_cause_state_from_chain(case)
    _mark_terminal(case, hyp, [])
    assert "root_terminal_refused_turn" in root.metadata
    return case, root, hyp


@pytest.mark.parametrize("change", ["refuted", "retired", "moved off the root"])
def test_the_ask_renders_only_on_a_standing_hypothesis_current_root(change):
    case, root, hyp = _refused_count_held()
    assert cgb._RECOVERY_NOTES[cgb._VERDICT_REFUSED] in _build_causal_graph_block(case)
    if change == "refuted":
        hyp.state = HypothesisState.REFUTED
        hyp.refutation_reason = "the fix of it failed"
    elif change == "retired":
        hyp.state = HypothesisState.RETIRED
    else:
        other = root.model_copy(update={"node_id": "cn_0000000000b2", "metadata": {}})
        case.causal_nodes[other.node_id] = other
        hyp.root_node_id = other.node_id
    assert cgb._RECOVERY_NOTES[cgb._VERDICT_REFUSED] not in (
        _build_causal_graph_block(case)
    )


@pytest.mark.parametrize("how", ["a false verdict", "a deepening"])
def test_a_false_verdict_clears_the_refused_mark(how):
    case, root, hyp = _refused_count_held()
    if how == "a false verdict":
        update = HypothesisUpdate(
            hypothesis_id=hyp.hypothesis_id, root_is_terminal=False
        )
        _apply(case, hypotheses_to_update=[update])
    else:  # the deepening writer marks the root deepened past not terminal
        _apply(
            case,
            causal_nodes_to_add=[
                CausalNodeToAdd(
                    statement="nothing rotates the secret_id",
                    node_type="root",
                    produces=root.node_id,
                )
            ],
            hypotheses_to_update=[
                HypothesisUpdate(
                    hypothesis_id=hyp.hypothesis_id, root_node_ref="new_index_0"
                )
            ],
        )
        assert hyp.root_node_id != root.node_id
    assert root.root_terminal is False
    assert "root_terminal_refused_turn" not in root.metadata


def test_an_attached_root_line_shows_the_turn_it_was_posited():
    case, root, _hyp = _d6_case()
    d = next(n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM)
    orphan = CausalNode(
        node_id="cn_00000000000f",
        statement="an orphan cause",
        node_type=NodeType.ROOT,
        generated_at_turn=2,
    )
    case.causal_nodes[orphan.node_id] = orphan
    case.causal_edges.append(
        CausalEdge(cause_node_id=orphan.node_id, effect_node_id=d.node_id)
    )
    _recompute_cause_state_from_chain(case)
    block = _build_causal_graph_block(case)
    assert f"(posited turn {_ATTACH})" in _root_line(block, root.node_id)
    # No hypothesis attached to the orphan: it has no floor to show yet.
    assert "posited turn" not in _root_line(block, orphan.node_id)


def test_the_prompt_example_basis_row_is_a_valid_evidence_to_add():
    """#1930: a model copying the example's row must not be refused."""
    import json
    import re

    block = _CHAIN_EMISSION_BLOCK.split("terminal_basis_evidence_ids:[")[0]
    body = block.rsplit("evidence_to_add:", 1)[1].split("causal_nodes_to_add:")[0]
    raw = re.sub(r"\s+", " ", body.strip().removeprefix("[0]").strip())
    row = EvidenceToAdd.model_validate(json.loads(re.sub(r"(\w+):\"", r'"\1":"', raw)))
    assert row.category == EvidenceCategory.CAUSAL_EVIDENCE
    assert row.source_file_id and row.source_type and row.extract


# ---------------------------------------------------------------------------
# D6 across a staged revision (ruled 2026-11-05: the same emission counts)
# ---------------------------------------------------------------------------


async def _staged_then_confirmed(cited=None):
    from tests.unit.core.investigation import (
        test_problem_statement_revision_and_false_alarm as revision,
    )

    engine, case = revision._engine(), revision._case()
    dsu = revision._revision_turn_with_cause_work()  # cites new_index_2
    if cited is not None:
        dsu.hypotheses_to_add[0].terminal_basis_evidence_ids = [cited(case)]
    await revision._apply(engine, case, dsu)
    case.current_turn += 1
    result = await engine.process_turn(
        case=case, user_message="yes", redaction_ctx=None
    )
    case = result["case_updated"]
    (hyp,) = case.hypotheses.values()
    return case, case.causal_nodes[hyp.root_node_id]


async def test_a_staged_revision_citing_new_index_identifies_on_confirmation():
    case, root = await _staged_then_confirmed()
    # Dated at its staged turn, not the confirmation (#2537).
    assert root.root_first_attached_turn == case.current_turn - 1
    assert root.root_terminal is True
    assert case.progress.cause_state == CauseState.IDENTIFIED


async def test_a_row_from_before_the_staged_turn_is_still_refused_on_replay():
    case, root = await _staged_then_confirmed(
        lambda case: _row(case, case.current_turn - 1, "pool size is 20")
    )
    assert root.root_terminal is None
    assert case.progress.cause_state == CauseState.CANDIDATES


# ---------------------------------------------------------------------------
# #2059: a refuted root holds nothing
# ---------------------------------------------------------------------------


def _marked_false(refutes: int):
    """#2059's repro: a root with one SUPPORTS link and ``refutes`` REFUTES
    links, marked not terminal, under an ACTIVE hypothesis."""
    from tests.unit.core.investigation.test_mece_arbitration import (
        _case,
        _evidence,
        _hyp,
        _problem,
        _root,
    )

    d = _problem()
    root = _root(
        "cn_00000000000a",
        "the deploy removed the release call",
        support_labels=["ev_s1"],
    )
    root.metadata = {"root_terminal": False}
    evidence = [_evidence("ev_s1")]
    for i in range(refutes):
        row = _evidence(f"ev_r{i}")
        evidence.append(row)
        root.evidence_links.append(
            NodeEvidenceLink(
                evidence_id=row.evidence_id,
                stance=EvidenceStance.REFUTES,
                reasoning="contradicts it",
                linked_at_turn=2,
            )
        )
    case = _case(
        [d, root],
        edges=[CausalEdge(cause_node_id=root.node_id, effect_node_id=d.node_id)],
        evidence=evidence,
        hyps=[_hyp(root.node_id, root.statement, hypothesis_id="hyp_0000000000aa")],
    )
    _recompute_cause_state_from_chain(case)
    return case, root


def test_a_refuted_root_marked_not_terminal_holds_nothing():
    case, root = _marked_false(refutes=2)
    assert root.node_state == NodeState.REFUTED
    assert held_open_root_ids(case) == set()
    assert case.progress.cause_identification_held_open is False
    assert "its WHY is open" not in _build_causal_graph_block(case)


def test_an_inconclusive_root_marked_not_terminal_stays_held():
    case, root = _marked_false(refutes=1)
    assert root.node_state == NodeState.INCONCLUSIVE
    assert held_open_root_ids(case) == {root.node_id}
    assert case.progress.cause_identification_held_open is True


def test_no_consumer_reads_a_refuted_root_as_held():
    """The five readers of ``cause_identification_held_open`` take their
    not-held branch."""
    case, root = _marked_false(refutes=2)
    # cause_assurance: a re-check of the fallen cause is not "held open", and
    # the report's polarity rows are not emptied by the hold.
    case.progress.identified_cause_root_ids = [root.node_id]
    file_id = upload(case, 5)
    case.evidence.append(
        bound_evidence(
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            turn=5,
            evidence_id="ev_0000000ab5e1",
            rechecks=root.evidence_links[0].evidence_id,
            collected_by="user",
            source_file_id=file_id,
        )
    )
    # Not "held open": the fallen cause's reversed verdict cites no row, so
    # the window-bound re-check confirms nothing (#2557).
    assert [why for _e, why in cause_recheck_status(case)] == [
        "the root cause's terminal verdict cites no row yet"
    ]
    assert [e.evidence_id for e in confirming_polarity_rows(case)] == [
        "ev_0000000ab5e1"
    ]
    # terminal_transitions: the conclusion backstop is not stood down.
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="the pool is undersized",
        mechanism="m",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
    )
    assert cause_identification_leg(case) == "rcc"
    case.root_cause_conclusion = None
    # stage_gates: a withdrawn offer is not told the cause's why is open.
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description="raise the pool size",
            proposed_in_turn=case.current_turn,
        )
    )
    metadata: dict = {}
    assert _withdraw_unlicensed_solution_offers(case, metadata=metadata) == 1
    assert "no longer established" in "\n".join(narrated(case, metadata))
    # response_application: M5 at proposal gives the generic recovery.
    from tests.unit.core.investigation.test_m5_recovery_1679 import _fix

    _apply(case, solutions_to_add=_fix())
    action = case.proposed_actions[-1]
    assert action.action_type == InvestigationActionType.DIAGNOSTIC
    assert action.downgrade_reason != WHY_OPEN_DOWNGRADE_REASON
