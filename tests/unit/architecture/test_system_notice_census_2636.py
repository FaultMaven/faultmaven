"""Every ``_add_system_feedback`` call and every recorded decision, by class
(#2636, #2615 stage R).

A decider records an outcome-bearing notice as a code
(``stage_gates.record_decision``); the narration step renders it after the
cycle's record (``milestone_engine/turn_narration.py``). Three classes:

* **R**: rendered from the record (the text states a verdict the record holds);
* **D**: a decision on this response's emission, rendered after the record,
  its reason a param;
* **text**: a notice that states no outcome the cycle can change, declared
  here with the reason. Most report an ordering or schema error in the
  response; the problem-status sites stay text because ``problem.status`` has
  one writer, ``problem_status.py``, which runs only in step 1 and in step
  0b's replay.

A new call, a moved one or a changed count fails until it is declared here,
and a converted function (one listed in ``DECISIONS``) that calls
``_add_system_feedback`` fails. Sites are keyed by ``(module, qualname)``.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from tests.source_tree import parse_file

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

_ROOT = Path(__file__).resolve().parents[3]
_ME = "faultmaven/core/investigation/milestone_engine/"
_SG = _ME + "stage_gates.py"
_RA = _ME + "response_application.py"
_HU = _ME + "hypothesis_updates.py"
_CE = _ME + "chain_emission.py"
_TR = _ME + "transitions.py"
_TP = _ME + "terminal_proposals.py"
_TA = _ME + "turn_application.py"

_ORDER = "an ordering or schema error in the response"
_STATUS = (
    "a problem-status site: problem.status has one writer, run only in step 1 "
    "and step 0b's replay"
)
_EMISSION = "a refusal of this response's emission that states no outcome"

#: (module, qualname) -> (calls, reason): the notices that stay text.
TEXT: dict[tuple[str, str], tuple[int, str]] = {
    (_ME + "cause_work.py", "report_refused_cause_work"): (1, _STATUS),
    (_CE, "_apply_chain_emission"): (1, "one cause = one chain: " + _ORDER),
    (_CE, "_nudge_ambiguous_orphan_chains"): (1, _ORDER),
    (_ME + "engine.py", "MilestoneEngine._process_turn_impl"): (
        2,
        "the user's decline of a revision (step 0b) and a problem-status "
        "refusal: " + _STATUS,
    ),
    (_HU, "_apply_hypothesis_action_intent"): (3, _EMISSION),
    (_HU, "_apply_hypothesis_updates"): (1, _EMISSION),
    (_HU, "_apply_deferred_likelihood_updates"): (1, "a likelihood cap: " + _ORDER),
    (_HU, "_apply_hypothesis_evidence_links"): (1, "a link to a terminal hypothesis"),
    (_RA, "_reclassify_premature_causal_absence"): (1, _STATUS),
    (_RA, "_settle_fix_attempts"): (1, "a node_ref naming no node: " + _ORDER),
    (_RA, "ResponseApplier.process_response_structured"): (2, _ORDER),
    (_RA, "ResponseApplier._apply_verification_updates"): (
        2,
        "a withdrawal with no basis, and a revision beside a false alarm: " + _ORDER,
    ),
    (_RA, "ResponseApplier._apply_investigation_updates"): (
        6,
        "a data blocker, a duplicate hypothesis, the cause work held for a "
        "revision, and three problem-status refusals: " + _STATUS,
    ),
    (_SG, "record_decision"): (1, "the token a notice code holds its place with"),
    (_SG, "add_evidence_validation_feedback"): (1, "the reasoning-first strip"),
    (_SG, "_refuse_fix_ref"): (1, "an accepted_action_ref naming no fix: " + _ORDER),
    (_SG, "_note_unnamed_acceptance"): (1, _EMISSION),
    (_SG, "_resolve_fix_identity"): (
        2,
        "a fix held for a revision, and no SOLUTION pending: " + _EMISSION,
    ),
    (_SG, "_carry_fix"): (1, "a fix acceptance held for a revision"),
    (_SG, "decide_fix_acceptance"): (1, "a ref without solution_accepted: " + _ORDER),
    (_SG, "_apply_stage_gate_signals"): (3, "milestone order errors: " + _ORDER),
    (_TR, "TransitionManager._apply_model_proposal"): (
        5,
        "a revision pending, an invalid edge, a non-disposition, the engine's "
        "offer standing, and the turn quiet (TRANSITION_QUIET_FEEDBACK, "
        "amendment 9): " + _EMISSION,
    ),
    (_TA, "_apply_turn_response"): (
        2,
        "the restatement stall, and the progress monitor's injection (its "
        "FIX_FAILURE_CYCLE branch names only the stage, A14)",
    ),
    (_ME + "turn_records.py", "_perform_hypothesis_housekeeping"): (
        1,
        "anti-anchoring: its retirements are HYPOTHESIS_RETIRED codes",
    ),
}

#: (module, qualname) -> {code: (references, class)}. ``offer`` is an offer
#: code; ``context`` a code other renderers read.
DECISIONS: dict[tuple[str, str], dict[str, tuple[int, str]]] = {
    (_SG, "_withdraw_unlicensed_solution_offers"): {
        "NoticeCode.SOLUTION_OFFER_WITHDRAWN": (1, "R")
    },
    (_SG, "_flag_absence_inside_a_failed_fix_window"): {
        "NoticeCode.ABSENCE_IN_FAILED_WINDOW": (1, "R")
    },
    (_SG, "_flag_unbound_post_fix_rows"): {"NoticeCode.UNBOUND_RECHECKS": (1, "R")},
    (_SG, "_note_failed_fix"): {"NoticeCode.FAILED_FIX_NOT_REREGISTERED": (1, "R")},
    (_SG, "_refuse_withdrawn"): {"NoticeCode.FIX_LICENCE_REFUSED": (1, "D")},
    (_SG, "_bearer_registers"): {"NoticeCode.FIX_LICENCE_REFUSED": (1, "D")},
    (_SG, "_register_carried_fix"): {"NoticeCode.FIX_LICENCE_REFUSED": (1, "D")},
    (_SG, "_ignore_fix_report"): {"NoticeCode.FIX_REPORT_IGNORED": (1, "D")},
    (_SG, "_apply_stage_gate_side_effects"): {
        "OfferCode.RCA_INFEASIBLE_CLOSE": (1, "offer")
    },
    (_SG, "_apply_stage_gate_signals"): {
        "NoticeCode.MITIGATION_VERIFY_PROCEEDED": (1, "R"),
        "NoticeCode.MITIGATION_VERIFY_REFUSED": (1, "R"),
    },
    (_SG, "_register_disposition"): {
        "NoticeCode.MITIGATION_DISPOSITION_REFUSED": (1, "R"),
        "NoticeCode.MITIGATION_DISPOSED": (1, "context"),
    },
    (_CE, "_record_verdict_on_root"): {
        "NoticeCode.ROOT_TERMINAL_NOT_QUALIFYING": (1, "D"),
        "NoticeCode.ROOT_TERMINAL_REFUSED": (1, "D"),
    },
    (_HU, "_apply_pending_refutations"): {"NoticeCode.REFUTATION_REFUSED": (1, "D")},
    (_RA, "_settle_fix_attempts"): {"NoticeCode.FIX_UNTARGETED": (1, "R")},
    (_RA, "ResponseApplier._apply_verification_updates"): {
        "NoticeCode.FALSE_ALARM_REFUSED": (1, "D"),
        "NoticeCode.REVISION_REFUSED": (1, "D"),
    },
    (_RA, "ResponseApplier._apply_investigation_updates"): {
        "NoticeCode.SIGNALS_ON_INVALIDATED_PROBLEM": (1, "R")
    },
    (_TP, "_maybe_propose_deferred_close"): {"code": (1, "offer")},
    (_TP, "_maybe_propose_confirmed_resolution"): {
        "NoticeCode.PENDING_CHECK": (1, "R"),
        "OfferCode.BACKSTOP_RESOLVE": (1, "offer"),
    },
    (_TP, "_maybe_propose_false_alarm_close"): {
        "OfferCode.FALSE_ALARM_CLOSE": (1, "offer")
    },
    (_TR, "_refuse_declined_reask"): {"NoticeCode.DECLINED_CLOSE_REASK": (1, "D")},
    (_TR, "TransitionManager._recheck_resolve_request"): {
        "NoticeCode.DECLINED_RESOLUTION_REASK": (1, "R"),
        "OfferCode.REQUEST_READY": (1, "offer"),
        "OfferCode.NEEDS_INFO_UNMET_CLOSE": (1, "offer"),
    },
    (_TR, "TransitionManager._apply_model_proposal"): {
        "NoticeCode.PENDING_CHECK": (1, "R"),
        "OfferCode.RESOLUTION_NEEDS_INFO": (1, "offer"),
        "NoticeCode.DECLINED_RESOLUTION_REASK": (1, "R"),
        "OfferCode.MODEL_PROPOSAL": (1, "offer"),
    },
    (_TA, "_apply_turn_response"): {"NoticeCode.HYPOTHESIS_RETIRED": (1, "context")},
    (_TA, "_lapse_unlicensed_offer"): {"NoticeCode.OFFER_LAPSED": (1, "context")},
}

#: The functions whose every notice became a code: none may call
#: ``_add_system_feedback`` again.
CONVERTED = frozenset(DECISIONS) - frozenset(TEXT)

_CLASSES = {"R", "D", "offer", "context"}


def _qualified_calls(path: Path, name: str):
    """``(qualname, call)`` for each call of ``name`` (a name or an attribute)."""
    out = []

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, stack + [child.name])
                continue
            if isinstance(child, ast.Call):
                func = child.func
                called = getattr(func, "id", None) or getattr(func, "attr", None)
                if called == name:
                    out.append((".".join(stack) or "<module>", child))
            walk(child, stack)

    walk(parse_file(path), [])
    return out


def _scan(name: str):
    for path in sorted((_ROOT / "faultmaven").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        for qualname, call in _qualified_calls(path, name):
            yield rel, qualname, call


def test_every_text_notice_is_declared_with_its_reason():
    found = Counter((rel, qual) for rel, qual, _ in _scan("_add_system_feedback"))
    declared = {site: calls for site, (calls, _reason) in TEXT.items()}
    assert dict(found) == declared, (
        "An _add_system_feedback call was added, moved or removed. An "
        "outcome-bearing notice is a code (record_decision), rendered after "
        "the record; declare a text site in TEXT with its reason."
    )
    assert all(reason for _calls, reason in TEXT.values())


def test_every_decision_is_declared_by_class():
    found: dict[tuple[str, str], Counter] = {}
    for rel, qual, call in _scan("record_decision"):
        if len(call.args) < 2:
            continue  # the definition's own recursion has none
        found.setdefault((rel, qual), Counter())[ast.unparse(call.args[1])] += 1
    declared = {
        site: Counter({code: n for code, (n, _cls) in codes.items()})
        for site, codes in DECISIONS.items()
    }
    assert found == declared
    assert {cls for codes in DECISIONS.values() for _n, cls in codes.values()} <= (
        _CLASSES
    )


def test_a_converted_function_writes_no_text():
    calling = {(rel, qual) for rel, qual, _ in _scan("_add_system_feedback")}
    assert not (CONVERTED & calling)


def test_the_census_names_every_notice_code():
    """Each code a census site records exists, and every code is recorded
    somewhere."""
    from faultmaven.core.investigation.narration.codes import NoticeCode, OfferCode

    named = {
        code.split(".", 1)[1]
        for codes in DECISIONS.values()
        for code in codes
        if "." in code
    }
    members = {m.name for m in NoticeCode} | {m.name for m in OfferCode}
    assert named <= members
    deferred = {"DEFERRED_RESOLVE", "DEFERRED_CLOSE"}  # recorded through `code`
    assert members - named == deferred
