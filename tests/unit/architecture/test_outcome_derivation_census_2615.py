"""Every place that derives a case's outcome, reads an outcome field raw, or
enters a closure is declared, per function (#2615 stage S0, pins 3 and 5).

#2615 moves every reader of a case's outcome onto one record (``CaseOutcome``),
stage by stage. Until then nothing stopped one more reader from deciding the
outcome on its own. This census stops that: a new site, a moved one or a
changed count fails until it is declared here, and each stage of #2615 shrinks
the tables as it moves its readers.

The census watches the derivations, fields and entries it names. A wrapper
outside those lists (a function that returns one of them) is not seen. Pin 2's
poisoned-case test (stage S2) backstops what a census of names misses.

Each site is keyed by ``(path, qualname of the enclosing function)``, the path
as the scan writes it (``rel.as_posix()``), with an exact count per name.
Module level is ``<module>``. Three tables:

* ``DERIVATION_SITES``: every Load reference to a function in ``DERIVATIONS``,
  the functions #2615's tables name as deciding an outcome, and four one-line
  wrappers of them (``has_resolution_confirmation``,
  ``_resolution_confirmed``, ``_cause_identified`` and
  ``solution_licence_leg``). Stage S1 (#2636) adds the record's own:
  ``compute_case_outcome``, ``closure_outcome``, ``record_case_outcome`` (its
  callers are the write points) and the three codes the record reads at the
  deciders. A call counts, and so does a bare reference, as a
  name or an attribute (``ca.fix_windows``, ``callback=fix_windows``); an
  import or the ``def`` does not. ``case.contracts`` re-exports
  ``classify_solution_outcome``, so the narration package's import contract
  cannot see a narrator's use of it; only this census does. Stage S4 removes
  the re-export.
* ``FIELD_READS``: every attribute Load of a field in ``FIELDS``:
  ``completed_milestones``, ``solution_verified``, its alias property
  ``resolution_complete`` (no reader today), a mitigation's ``verified`` and
  ``closure_reason``. ``verified`` has no receiver heuristic: all 12 reads
  today are ``MitigationRecord.verified``, and a future reader of another
  ``verified`` gets a row with its reason.
* ``ENTRY_CALLERS`` (pin 5): every reference to an entry in ``ENTRIES``: the
  offer record's three writers (``open_offer``, ``answer_offer``,
  ``lapse_offers``), the transitions' two executors, and the entries that
  open, answer or close through them; and the narrators' one accessor of a
  case's text, ``content_by_id`` (#2811: an action's description, a need's
  request text; by kind, a row's text and established turn). Each row's note says what that
  caller opens, answers, closes or reads. An entry referenced other than by a
  call fails, so every way in stays visible here.

Beside them (#3053 S3d-3): ``CYCLE_SOURCES``, the in-cycle deciders declared
the cycle record's sources, permanently (ruling Q4), whose derivation
references are counted as ``DERIVATION_SITES``' are, with the typed-code
pins; and ``CONTENT_ACCESSOR_CALLERS``, every caller of a content predicate
(``CONTENT_ACCESSORS``, the summary gate, ruling Q6), outside
``DERIVATIONS``.

Also counted: a call of the builtin ``getattr(x, "name")`` whose name is a
constant string, as a reference to that name (for an entry, one that is not a
call). An import of a watched function under another name fails, since every
later call would go unseen.

In doubt, the census counts. A Load of a watched name is dropped as a local
variable (``open_offer = case.offer_record.open``) only when all of these
hold:

* the nearest enclosing scope of the load is a function (a ``def`` or a
  ``lambda``), and the load sits in that function's body;
* that function's own body assigns the name;
* nothing else in that function binds the name: no parameter, no
  function-local import, no ``global`` or ``nonlocal`` declaration;
* no scope nested in that function declares the name ``nonlocal``.

Every other Load counts. That includes a load in a class body, a
comprehension, a decorator, a default or an annotation, and a name that a
parameter or an import binds, rebound or not. For example,
``_close_on_explicit_intent``'s ``assess_closure_readiness`` is passed in. An
alias assigned from a watched name counts through its right-hand side.

Not counted:

* ``problem_status``, ``cause_state`` and ``mitigation.disposition``: progress
  inputs the record reads from (#2615, *The record*). Under ``prompts/``,
  ``PROMPT_PROGRESS_READS`` watches each name in ``PROGRESS_INPUTS`` per
  function (#2811 S2b-1c-i): a prompt line reads them off the record;
* a read through ``builtins.getattr``, ``operator.attrgetter`` or
  ``object.__getattribute__``, or by a class pattern
  (``case Case(closure_reason=r)``): none exists today;
* a read by a computed name (``getattr(case, name)``);
* a read by a key: a subscript, or a milestone map or turn record keyed
  ``"solution_verified"`` or ``"mitigation_verified"``;
* readers in the clients.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import pytest

from faultmaven.core.investigation.narration.codes import (
    DECIDED_FROM,
    NoticeCode,
    OfferCode,
)
from tests.source_tree import parse_file, parse_source

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE = _ROOT / "faultmaven"

_INV = "faultmaven/core/investigation/"
_ME = _INV + "milestone_engine/"
_CB = _INV + "prompts/context_builder/"
_CASE = "faultmaven/modules/case/"
_CA = _INV + "cause_assurance.py"
_TT = _INV + "terminal_transitions.py"
_DISCONF = _INV + "causal_graph/disconfirmation.py"
_OFFERS = _INV + "offers.py"
_PROBLEM = _INV + "problem_status.py"
_OUTCOME = _INV + "outcome.py"
_MONITOR = _INV + "progress_monitor.py"
_VALIDATOR = _INV + "state_validator.py"
_SG = _ME + "stage_gates.py"
_PROPOSALS = _ME + "terminal_proposals.py"
_TERMINAL_TURNS = _ME + "terminal_turns.py"
_TRANSITION_TURNS = _ME + "transition_turns.py"
_TRANSITIONS = _ME + "transitions.py"
_PROGRESS = _CASE + "domain/models/progress.py"
_UI = _CASE + "domain/services/case_ui_adapter.py"
_PG = _CASE + "infrastructure/postgresql_hybrid_case_repository/"
_SQLITE = _CASE + "infrastructure/sqlite_case_repository/"
_API = "faultmaven/models/api_models.py"
_REPORT = "faultmaven/modules/report/domain/services/report_generation_service.py"

#: Each function that decides a case's outcome, by the module that defines it.
DERIVATIONS: dict[str, str] = {
    **dict.fromkeys(
        (
            "fix_windows",
            "confirming_window",
            "resolution_confirmation_rows",
            "confirmation_stamp_rows",
            "confirming_polarity_rows",
            "mitigation_observation",
            "_pool_legs",
            "_cause_pool",
            "cause_recheck_status",
            "uncited_recheck_why",
            "uncited_recheck",
            "_no_confirming_window",
            "has_resolution_confirmation",
        ),
        _CA,
    ),
    **dict.fromkeys(
        (
            "assess_resolution_readiness",
            "resolution_record_lines",
            "shown_milestones",
            "closure_verdict",
            "assess_closure_readiness",
            "derive_closure_reason",
            "derive_disposition_eligibility",
            "mitigation_closure_line",
            "_mitigation_verified",
            "_mitigation_declared_stable",
            "assess_close_summary",
            "_resolution_confirmed",
        ),
        _TT,
    ),
    "failed_fix_of": _DISCONF,
    "classify_solution_outcome": _CASE + "domain/models/solution.py",
    # Stage S1 (#2636): the record, and the codes it reads at the deciders.
    "cause_pool_empty_reason": _CA,
    "mitigation_refusal_reason": _CA,
    "unbound_post_fix_rows": _CA,
    "compute_case_outcome": _OUTCOME,
    "closure_outcome": _OUTCOME,
    # Its callers are the record's write points.
    "record_case_outcome": _OUTCOME,
    # #2811: the two save helpers' write, and the record narration reads.
    "record_before_save": _OUTCOME,
    "narration_outcome": _OUTCOME,
    # #2811 S2b-1a: the derivations the fix-verification block read, which
    # it now reads off the record.
    "off_cause_attempt": _CA,
    "symptom_elimination_rows": _CA,
    "interception_relief": _CA,
    # #2811 S2b-1b-i: the held need and the resolve decline, which the block's
    # need lines read off the record (``resolution.pending_check_need_id``,
    # ``pending_check_reason``, ``declined_stands``).
    "pending_symptom_check": _CA,
    "declined_resolve_entry": _TT,
    # #2811 S2b-1b-ii: the checklist rows and the dated fix the
    # ``<evidence_needs>`` block read, which it now reads off the record
    # (``bindable``, the dated attempts). ``bindable_rows`` is a method,
    # ``SubjectIndex.bindable_rows`` (``_METHODS``).
    "fix_application_turn": _CA,
    "bindable_rows": _CA,
    # #2811 S2b-1c-i: whether the problem takes cause work, which the
    # exclusion rule now reads off the record (``problem.status``), and the
    # false-alarm close declined, which the focus emphasis reads off the offer
    # record (``OfferView``, S2b-2d).
    "cause_work_accepted": _PROBLEM,
    "cause_work_staged": _PROBLEM,
    "false_alarm_close_declined_at": _PROBLEM,
    # #2811 S2b-1c-ii: why the held check holds, the record's one derivation,
    # which the PENDING_CHECK decision's params share.
    "held_check_reason": _CA,
    # #2811 S2b-2a: the held-open and MECE-contested roots, which the graph
    # block's recovery notes and the working conclusion's NOTE read off the
    # record (``cause.held_open_root_ids``, ``cause.contested_root_ids``).
    "held_open_root_ids": _INV + "causal_graph/projection.py",
    "mece_contested_root_ids": _INV + "causal_graph/clusters.py",
    # #2811 S2b-2b: whether a cause is identified, which the resolve
    # confirmation reads off the record (``cause.identification_leg``), and
    # its two one-line wrappers.
    "cause_identification_leg": _INV + "cause_identification.py",
    "_cause_identified": _TT,
    "solution_licence_leg": _SG,
}

#: The watched names that are methods, by the class that defines each.
_METHODS: dict[str, str] = {"bindable_rows": "SubjectIndex"}

#: Each outcome field read raw, by the module and class that define it.
FIELDS: dict[str, tuple[str, str]] = {
    "completed_milestones": (_PROGRESS, "InvestigationProgress"),
    "solution_verified": (_PROGRESS, "InvestigationProgress"),
    "resolution_complete": (_PROGRESS, "InvestigationProgress"),
    "verified": (_PROGRESS, "MitigationRecord"),
    "closure_reason": (_CASE + "domain/models/case.py", "Case"),
}

#: The progress inputs the record reads from (``problem.status``,
#: ``milestones_shown``, ``mitigation.disposition``), watched where the
#: prompt is built (#2811 S2b-1c-i): ``FIELDS`` leaves them out package-wide,
#: so a prompt line that read one raw would be seen by nothing.
PROGRESS_INPUTS = (
    "symptom_verified",
    "problem_status",
    "cause_state",
    "solution_proposed",
    "solution_accepted",
    "disposition",
    # #2811 S2b-2a: the working conclusion's NOTE reads the record's
    # ``cause.contested_root_ids`` in its place.
    "cause_identification_contested",
)
_PROMPTS = _INV + "prompts/"

#: (path, qualname) -> {input: attribute reads}, under ``prompts/``: the
#: prompt sites that still read a progress input raw, each until the stage
#: that switches it. None since S2b-1c-i.
PROMPT_PROGRESS_READS: dict[tuple[str, str], dict[str, int]] = {}


#: The offer record's writers, the transitions' executors, the entries that
#: open, answer or close through them (#2615, *Closure on demand*), and the
#: narrators' text accessor (#2811), by the module that defines each.
ENTRIES: dict[str, str] = {
    "_execute_closed_transition": _TT,
    "_execute_resolved_transition": _TT,
    "open_disposition_offer": _TT,
    "disposition_refusal": _TT,
    "confirm_open_offer": _TT,
    "execute_user_closure": _TT,
    "open_offer": _OFFERS,
    "answer_offer": _OFFERS,
    "lapse_offers": _OFFERS,
    # #2811 S2b-1a: not a closure entry. The narrators' one accessor of a
    # case's text, by id (ruling 5 of 2026-11-15; a need's request text can
    # be rewritten, S2b-1b-i; a row's, by kind, S2b-1b-ii): every caller is
    # enumerated, so a narrator reading the case through it is seen here.
    "content_by_id": _INV + "narration/fix_verification.py",
}

#: (path, qualname) -> {derivation: references}.
DERIVATION_SITES: dict[tuple[str, str], dict[str, int]] = {
    # The derivations' own modules: a derivation built from others.
    (_DISCONF, "_disconfirmed_cause_trigger"): {"failed_fix_of": 1},
    (_DISCONF, "_fix_failure_refusal"): {"fix_application_turn": 1},
    (_CA, "_cause_pool"): {"_pool_legs": 1},
    (_CA, "cause_pool_empty_reason"): {"_cause_pool": 1, "_pool_legs": 1},
    (_CA, "cause_elimination_rows"): {"cause_recheck_status": 1},
    (_CA, "cause_recheck_status"): {
        "_no_confirming_window": 1,
        "confirming_window": 1,
        "uncited_recheck": 1,
    },
    # #2811: the typed parameters, rendered by ``narration.rechecks``.
    (_CA, "uncited_recheck_why"): {"uncited_recheck": 1},
    # #2811 S2b-1b-ii: the dated fix and the bindable rows, which the
    # ``<evidence_needs>`` block now reads off the record.
    (_CA, "unbound_post_fix_rows"): {"fix_application_turn": 1},
    (_CA, "uncited_recheck"): {"bindable_rows": 1},
    # #2811 S2b-1a: the three names the fix-verification block switched to
    # the record, where the deciders still derive them.
    (_CA, "_no_confirming_window"): {"off_cause_attempt": 1},
    (_CA, "mitigation_relief"): {"interception_relief": 1},
    (_CA, "pending_symptom_check"): {"symptom_elimination_rows": 1},
    (_CA, "resolution_confirmation_rows"): {"symptom_elimination_rows": 1},
    # #3046 S3a-2: the carried view lays the record's verdicts over the
    # fresh read; every window decider reads it (``CARRIED_VIEW_SITES``).
    (_CA, "carried_fix_windows"): {"fix_windows": 1},
    (_CA, "fix_windows"): {"_pool_legs": 1},
    (_CA, "has_actionable_solution"): {"classify_solution_outcome": 1},
    (_CA, "has_resolution_confirmation"): {"resolution_confirmation_rows": 1},
    (_CA, "off_cause_attempt"): {"_cause_pool": 1},
    (_CA, "problem_leg_anchor"): {"confirming_window": 1},
    (_CA, "subject_index"): {"_cause_pool": 1},
    (_TT, "_resolution_confirmed"): {"has_resolution_confirmation": 1},
    (_TT, "_suggest_resolve_params"): {
        "cause_identification_leg": 1,
        "resolution_record_lines": 1,
    },
    (_TT, "assess_close_summary"): {
        "_cause_identified": 1,
        "cause_identification_leg": 1,
        "shown_milestones": 1,
    },
    (_TT, "assess_closure_readiness"): {
        "_resolution_confirmed": 1,
        "assess_close_summary": 1,
    },
    (_TT, "assess_resolution_readiness"): {
        "_cause_identified": 1,
        "_resolution_confirmed": 1,
        "off_cause_attempt": 1,
        "pending_symptom_check": 1,
    },
    (_TT, "closure_verdict"): {"assess_closure_readiness": 1},
    # The four closure entries compute the closure on demand (#2636, pin 5).
    (_TT, "confirm_open_offer"): {
        "assess_closure_readiness": 1,
        "closure_outcome": 1,
    },
    (_TT, "declined_resolve_entry"): {"closure_verdict": 1},
    (_TT, "derive_closure_reason"): {
        "_mitigation_declared_stable": 1,
        "_mitigation_verified": 1,
        "declined_resolve_entry": 1,
    },
    # #2811 S2b-1b-i: the resolve decline read at its owner, the offer record
    # (ruling Q1 of 2026-11-25: an input is read at its owner), by the
    # closure entries' pivot and the reopen admission, outside the decision
    # cycle. ``pending_check_params`` is a cycle source (``CYCLE_SOURCES``).
    (_TT, "close_pivots_to_resolve"): {"declined_resolve_entry": 1},
    (_TT, "resolve_reopen_admitted"): {"declined_resolve_entry": 1},
    (_TT, "derive_disposition_eligibility"): {
        "assess_resolution_readiness": 1,
        "closure_verdict": 1,
    },
    (_TT, "_execute_closed_transition"): {"record_case_outcome": 1},
    (_TT, "_execute_resolved_transition"): {"record_case_outcome": 1},
    (_TT, "disposition_refusal"): {"closure_outcome": 1},
    (_TT, "execute_user_closure"): {
        "assess_closure_readiness": 1,
        "closure_outcome": 1,
        "closure_verdict": 1,
    },
    (_TT, "mitigation_closure_line"): {"mitigation_observation": 1},
    (_TT, "open_disposition_offer"): {"closure_outcome": 1},
    (_TT, "resolution_record_lines"): {
        "cause_identification_leg": 1,
        "resolution_confirmation_rows": 1,
    },
    # #2811 S2b-2b: whether a cause is identified, and its two wrappers, where
    # it is derived outside the cycle's record: the readiness gates' own
    # building blocks (``_cause_identified``, the documented-but-unapplied
    # fix that ``derive_closure_reason`` reads), the resolve's truth surface
    # (written at the resolve transition, which folds its own record), M5's
    # licence (the apply step, before the cycle's record; the deciders' leg
    # keeps the recompute's flag, ruling of 2026-11-26 (3)) and the offer
    # record's lapse rule (read at each lapse point, before the fold).
    (_TT, "_cause_identified"): {"cause_identification_leg": 1},
    (_TT, "_fix_documented_not_applied"): {"_cause_identified": 1},
    (_TT, "finalize_resolution_truth_surface"): {"cause_identification_leg": 1},
    (_SG, "solution_licence_leg"): {"cause_identification_leg": 1},
    (_SG, "_solution_cause_validated"): {"solution_licence_leg": 1},
    (_OFFERS, "_premise_gone"): {"cause_identification_leg": 1},
    (_TT, "shown_milestones"): {"_mitigation_verified": 1},
    # The engine. ``assess_closure_readiness`` is passed, not called, to
    # ``_close_on_explicit_intent``: the one bare reference.
    (_ME + "engine.py", "MilestoneEngine._process_turn_impl"): {
        "assess_closure_readiness": 1,
        "closure_verdict": 1,
    },
    (_ME + "hypothesis_updates.py", "_refutation_grounds"): {"failed_fix_of": 1},
    (_SG, "_apply_stage_gate_side_effects"): {
        "closure_verdict": 1,
        "mitigation_closure_line": 1,
    },
    # #3046 S3a-2: the pair before and after the re-dating, off the fresh
    # read, or the guard could never fire; the verdict is the carried view's.
    (_SG, "_date_to_the_report"): {"fix_windows": 2},
    (_SG, "_flag_unbound_post_fix_rows"): {
        "bindable_rows": 1,
        "fix_application_turn": 1,
        "unbound_post_fix_rows": 1,
        "uncited_recheck_why": 1,
    },
    (_SG, "_mitigation_verify_refusal"): {
        "bindable_rows": 1,
        "mitigation_refusal_reason": 1,
    },
    (_SG, "fix_cause"): {
        "_cause_pool": 1,
        "_pool_legs": 1,
        "solution_licence_leg": 1,
    },
    (_SG, "licence_leg_ids"): {"_cause_pool": 1},
    (_SG, "licence_subject"): {"_pool_legs": 1},
    (_SG, "standing_fix_action"): {
        "fix_application_turn": 1,
        "interception_relief": 1,
    },
    # #2811 S2b-2b: the reopen chip's confirmation (#3053 S3d-2) and the
    # closed reply read the record: the fold of the moment.
    (_ME + "terminal_replies.py", "_reply_outcome"): {"narration_outcome": 1},
    (_TRANSITION_TURNS, "_close_on_explicit_intent"): {
        "assess_close_summary": 1,
        "assess_closure_readiness": 1,
    },
    # The record's write points (#2636): the end of a decision cycle and each
    # bundle of a statement-revision replay; the transitions are above.
    (_ME + "turn_application.py", "_apply_turn_response"): {"record_case_outcome": 1},
    # The outcome computed in memory for the narration alone when the cycle's
    # record write failed, never stored and counting nothing (#2636 B1b).
    (_ME + "turn_narration.py", "_outcome"): {"narration_outcome": 1},
    # #2811 S2c-3a: the record the outcome statement reads when the cycle's
    # write failed, the narration's fold, stored nowhere.
    (_ME + "turn_completion.py", "_statement_record"): {"narration_outcome": 1},
    # The two save helpers (#2811): a turn's commit and the case service's
    # write fold the record after the save's lapse; the metadata write, whose
    # description is the statement in force outside an investigation, writes
    # it in its own scoped update.
    (_ME + "turn_commit.py", "commit_turn_plan"): {"record_before_save": 1},
    (_CASE + "utils/retry.py", "update_case_with_retry"): {"record_before_save": 1},
    (_CASE + "domain/services/case_service.py", "CaseService._write_metadata"): {
        "record_case_outcome": 1
    },
    # #2811: a new case's first record, and the out-of-band reclassification,
    # which re-dates its file's rows.
    (_CASE + "domain/services/case_service.py", "CaseService.create_case"): {
        "record_before_save": 1
    },
    (
        "faultmaven/modules/agent/domain/services/investigation_service/"
        "reclassification.py",
        "EvidenceReclassifier.reclassify_out_of_band",
    ): {"record_before_save": 1},
    (_ME + "statement_revision.py", "confirm_revision"): {"record_case_outcome": 1},
    # #2811 S2b-1b-i: the reopen chip, the offer projection's: the resolve
    # decline read at its owner, the offer record (ruling Q1).
    (_INV + "offer_projection.py", "_reopen_chip"): {"declined_resolve_entry": 1},
    # #2811 S2b-1c-i: the cause-work gates, which read the live problem
    # status, an input, at its owner (ruling Q1's premise correction), and
    # M5's licence beside them; and the false-alarm decline's bare reply,
    # which reads the offer record (``false_alarm_close_declined_at``).
    (_ME + "chain_emission.py", "_apply_chain_emission"): {
        "cause_work_accepted": 1,
        "cause_work_staged": 1,
    },
    (_ME + "response_application.py", "ResponseApplier._apply_cause_claims"): {
        "cause_work_accepted": 1
    },
    (
        _ME + "response_application.py",
        "ResponseApplier._apply_investigation_updates",
    ): {
        "cause_work_accepted": 1,
        "cause_work_staged": 1,
        "solution_licence_leg": 1,
    },
    # #3046 S3a-2: a write point, the same-cycle mint before the chain
    # emission's re-root (a cycle's, or a replayed bundle's; ``pre_chain``).
    (_ME + "response_application.py", "_mint_before_the_chain"): {
        "record_case_outcome": 1
    },
    (_ME + "response_application.py", "_reclassify_premature_causal_absence"): {
        "cause_work_accepted": 1,
        "cause_work_staged": 1,
    },
    (_TRANSITION_TURNS, "_decline_bare_reply"): {"false_alarm_close_declined_at": 1},
    (_INV + "offer_projection.py", "OfferProjection.disposition_eligibility"): {
        "derive_disposition_eligibility": 1
    },
    (_OFFERS, "_confirmation_ids"): {"resolution_confirmation_rows": 1},
    # The case outcome record (#2636, stage S1): the one place every outcome
    # is computed for the record, and its write point.
    # #2811 S2b-2a: the held-open and contested roots, derived fresh at the
    # write.
    (_OUTCOME, "_cause"): {
        "_pool_legs": 1,
        "cause_identification_leg": 1,
        "cause_pool_empty_reason": 1,
        "held_open_root_ids": 1,
        "mece_contested_root_ids": 1,
    },
    (_OUTCOME, "_closure"): {"derive_closure_reason": 1},
    (_OUTCOME, "_closure_of"): {"closure_verdict": 1, "mitigation_closure_line": 1},
    (_OUTCOME, "_mitigation"): {
        "_mitigation_declared_stable": 1,
        "_mitigation_verified": 1,
        "mitigation_observation": 1,
        "mitigation_refusal_reason": 1,
    },
    (_OUTCOME, "_attempts"): {"interception_relief": 1},
    (_OUTCOME, "_resolution"): {
        "assess_resolution_readiness": 1,
        "confirmation_stamp_rows": 1,
        "declined_resolve_entry": 1,
        "held_check_reason": 1,
        "off_cause_attempt": 1,
        "pending_symptom_check": 1,
        "resolution_record_lines": 1,
        "symptom_elimination_rows": 1,
    },
    (_OUTCOME, "_bindable"): {"bindable_rows": 1, "uncited_recheck_why": 1},
    (_OUTCOME, "_unbound_rechecks"): {"unbound_post_fix_rows": 1},
    (_OUTCOME, "closure_outcome"): {"derive_closure_reason": 1},
    (_OUTCOME, "compute_case_outcome"): {
        "cause_recheck_status": 1,
        "fix_windows": 1,
        "shown_milestones": 1,
    },
    (_OUTCOME, "narration_outcome"): {"compute_case_outcome": 1},
    (_OUTCOME, "record_before_save"): {"record_case_outcome": 1},
    (_OUTCOME, "record_case_outcome"): {"compute_case_outcome": 1},
    # The backfill: once per stored case without a record, from the case.
    ("faultmaven/cli/backfill_case_outcome.py", "backfill_case_outcome._apply"): {
        "compute_case_outcome": 1
    },
    # The prompt. The fix-verification block renders from the record
    # (#2811 S2b-1a and S2b-1b-i, ``narration.fix_verification``): the
    # assembly's one fold; its window verdicts and ``fix_confirmed`` read the
    # carried view (#3046 S3a-2); the held need is the record's.
    (_CB + "narration_fold.py", "NarrationFold.outcome"): {"narration_outcome": 1},
    # #2811 S2b-2a: not the predicate but the record's field of the same
    # name, ``outcome.cause.held_open_root_ids``, which a census of names
    # cannot tell apart; ``test_contest_record_2811``'s record-wins tests
    # guard that the block reads the record.
    (_CB + "causal_graph_block.py", "_root_recovery_reasons"): {
        "held_open_root_ids": 1
    },
    # The API, the knowledge harvest and the report.
    (
        "faultmaven/modules/knowledge/domain/models/conversion.py",
        "CaseConversionRequest.from_case",
    ): {"classify_solution_outcome": 1},
    (_REPORT, "ReportGenerationService._fix_and_confirming_evidence"): {
        "confirmation_stamp_rows": 1,
        "confirming_polarity_rows": 1,
    },
    (_REPORT, "ReportGenerationService._generate_closure_summary"): {
        "mitigation_closure_line": 1,
        "shown_milestones": 1,
    },
    (_REPORT, "ReportGenerationService._solutions_by_outcome"): {
        "classify_solution_outcome": 1
    },
}

#: (path, qualname) -> {field: attribute reads}.
FIELD_READS: dict[tuple[str, str], dict[str, int]] = {
    (_INV + "case_telemetry.py", "_frontier"): {"solution_verified": 1},
    (_ME + "milestone_inference.py", "_milestone_already_recorded"): {"verified": 1},
    (_SG, "_apply_stage_gate_side_effects"): {"closure_reason": 1},
    (_SG, "_apply_stage_gate_signals"): {"verified": 1},
    (_SG, "_register_disposition"): {"verified": 1},
    (_SG, "justification_skipped"): {"solution_verified": 1},
    # #2811 S2b-2b: the closed reply when no record can be read (the
    # terminal write failed and the fold raised): the reason's line alone.
    (_ME + "terminal_replies.py", "_record_free_reply"): {"closure_reason": 1},
    (_OFFERS, "_premise_gone"): {"closure_reason": 1},
    (_OFFERS, "_scope"): {"closure_reason": 1},
    (_OFFERS, "answer_offer"): {"closure_reason": 2},
    (_OFFERS, "lapse_offers"): {"closure_reason": 3},
    (_PROBLEM, "_acted_on_bar"): {"solution_verified": 1, "verified": 1},
    (_PROBLEM, "is_false_alarm_close"): {"closure_reason": 1},
    # The record (#2636): a CLOSED case's stored reason, and the stored verdict
    # the refusal reason is read beside.
    (_OUTCOME, "_closure"): {"closure_reason": 1},
    (_OUTCOME, "_mitigation"): {"verified": 1},
    (_OUTCOME, "record_case_outcome"): {"closure_reason": 1},
    (_MONITOR, "ProgressMonitor._find_pending_milestone"): {"completed_milestones": 1},
    # #2811 S2b-1c-i: the milestone status reads the record; the stored
    # ``mitigation.verified`` stays a fresh input (ruling Q1 (a)) until S3d-4 (#3053).
    (_CB + "narration_fold.py", "_stored_mitigation_verified"): {"verified": 1},
    (_VALIDATOR, "StateValidator._validate_milestone_ordering"): {
        "solution_verified": 2,
        "verified": 1,
    },
    (_VALIDATOR, "StateValidator._validate_state_consistency"): {
        "solution_verified": 1
    },
    # By ``getattr(mitigation, "verified", False)``.
    (_TT, "_mitigation_verified"): {"verified": 1},
    (_TT, "confirm_open_offer"): {"closure_reason": 1},
    (_TT, "derive_solution_surface"): {"solution_verified": 1},
    (_TT, "should_generate_terminal_summary"): {"completed_milestones": 1},
    (_TT, "shown_milestones"): {"completed_milestones": 1},
    (_INV + "working_conclusion_generator.py", "_generate_next_steps"): {
        "solution_verified": 1
    },
    (_API, "AdminCaseMetadata.from_case_metadata"): {"closure_reason": 1},
    (_API, "AdminCaseMetadata.from_summary"): {"closure_reason": 1},
    (_API, "CaseDetail.from_case"): {"closure_reason": 1, "completed_milestones": 1},
    (_API, "CaseSummary.from_case"): {"closure_reason": 1},
    # The models themselves: a validator, and the properties built on the fields.
    (_CASE + "domain/models/case.py", "Case.validate_state_timestamp_consistency"): {
        "closure_reason": 2
    },
    (_PROGRESS, "InvestigationProgress.completed_milestones"): {
        "solution_verified": 1,
        "verified": 1,
    },
    (_PROGRESS, "InvestigationProgress.current_stage"): {"solution_verified": 1},
    (_PROGRESS, "InvestigationProgress.resolution_complete"): {"solution_verified": 1},
    (_PROGRESS, "InvestigationProgress.solution_ordering"): {"solution_verified": 1},
    (_PROGRESS, "MitigationRecord._verified_requires_accepted"): {"verified": 2},
    # The one statement of when a mitigation is settled (#3053 Q2): the stage,
    # ``cause_assurance.mitigation_settled``, the monitor and the disposition
    # writer read it.
    (_PROGRESS, "MitigationRecord.settled"): {"verified": 1},
    (_CASE + "domain/services/case_service.py", "CaseService.close_case"): {
        "closure_reason": 1
    },
    (_UI, "_compute_progress_transparency"): {"completed_milestones": 1},
    (_UI, "_transform_investigating"): {"completed_milestones": 1},
    (_UI, "_transform_resolved"): {"completed_milestones": 1},
    # Persistence: the column written and loaded.
    (_PG + "loading.py", "_row_to_case"): {"closure_reason": 1},
    (_PG + "rows.py", "_case_record_params"): {"closure_reason": 2},
    (_SQLITE + "rows.py", "_case_record_params"): {"closure_reason": 2},
    (_SQLITE + "rows.py", "_row_to_case"): {"closure_reason": 1},
    # The report, by ``getattr(case, "closure_reason", None)``.
    (_REPORT, "ReportGenerationService._generate_closure_summary"): {
        "closure_reason": 1
    },
}

#: (path, qualname) -> ({entry: calls}, what the caller answers or closes).
ENTRY_CALLERS: dict[tuple[str, str], tuple[dict[str, int], str]] = {
    (_CB + "fix_verification_block.py", "build_fix_verification_block"): (
        {"content_by_id": 1},
        "binds the narration's text accessor to the case for the "
        "fix-verification renderer, which reads an action's description and "
        "a need's request text by id (#2811 S2b-1a, S2b-1b-i)",
    ),
    (_CB + "evidence_needs.py", "_build_evidence_needs_block"): (
        {"content_by_id": 2},
        "binds the narration's text accessor to the case for the "
        "evidence-needs renderer, once per kind: a need's request text for "
        "the need sections, a row's text and established turn for the "
        "checklist (row=True); its one fresh input, the "
        "symptom rows where none verified, is read beside it from "
        "case.evidence (#2811 S2b-1b-ii)",
    ),
    (_ME + "terminal_replies.py", "resolution_confirmation_params"): (
        {"content_by_id": 1},
        "binds the narration's text accessor to the case for the reopen "
        "chip's resolve confirmation, which reads the confirming attempt's "
        "description by its action id (the attempt picked from the record's "
        "attempts) (#2811 S2c-1)",
    ),
    (_ME + "turn_narration.py", "_narrate_offer"): (
        {"content_by_id": 1},
        "binds the narration's text accessor to the case for the cycle's "
        "resolve confirmations, as resolution_confirmation_params does (#3053 "
        "S3d-3)",
    ),
    (_ME + "turn_completion.py", "_ready_confirmation"): (
        {"content_by_id": 1},
        "binds the narration's text accessor to the case for the met "
        "request's confirmation in the reply, as resolution_confirmation_params "
        "does (#3053 S3d-3)",
    ),
    (_ME + "turn_narration.py", "narrate_turn"): (
        {"content_by_id": 1},
        "reads, through the narration's text accessor, the request text of "
        "the need the cycle's record holds the resolution for "
        "(resolution.pending_check_need_id), once per narration, for the held "
        "check's notice and the readiness asks it renders from the record "
        "(#2811 S2b-2c)",
    ),
    (_ME + "engine.py", "MilestoneEngine._process_turn_impl._answer"): (
        {"answer_offer": 1},
        "answers the open resolve or close offer declined or withdrawn, on a "
        "turn that does not accept it",
    ),
    (_ME + "response_application.py", "ResponseApplier._withdraw_false_alarm_close"): (
        {"answer_offer": 1},
        "answers an open false-alarm close withdrawn, when its finding no "
        "longer stands or a revision takes its place",
    ),
    (_SG, "_apply_stage_gate_side_effects"): (
        {"open_disposition_offer": 1},
        "opens the close offer for a mitigation on an infeasible root-cause "
        "analysis (basis rca_infeasible)",
    ),
    (_PROPOSALS, "_maybe_propose_confirmed_resolution"): (
        {"open_disposition_offer": 1},
        "opens the resolve offer on a case whose cause is confirmed gone "
        "(basis backstop)",
    ),
    (_PROPOSALS, "_maybe_propose_deferred_close"): (
        {"open_disposition_offer": 1},
        "opens the resolve or close offer for a fix deferred out of band "
        "(basis deferred)",
    ),
    (_PROPOSALS, "_maybe_propose_false_alarm_close"): (
        {"open_disposition_offer": 1},
        "opens the close offer on a false-alarm finding (basis false_alarm)",
    ),
    (_TRANSITION_TURNS, "_confirm_pending_transition"): (
        {"confirm_open_offer": 1},
        "executes the resolve or close offer the user accepted in the chat",
    ),
    (_TRANSITION_TURNS, "_close_on_explicit_intent"): (
        {"open_disposition_offer": 2},
        "opens the menu's close offer, or the resolve offer it pivots to "
        "(basis menu)",
    ),
    (_TRANSITION_TURNS, "_resolve_on_reopen_chip"): (
        {"content_by_id": 1, "open_disposition_offer": 1},
        "re-opens the declined resolve offer from the reopen chip (basis "
        "reopen), or reads the held check's request text its refusal states",
    ),
    (_TRANSITIONS, "TransitionManager._apply_model_proposal"): (
        {"disposition_refusal": 1, "open_disposition_offer": 1},
        "opens the model's resolve or close offer (basis model), or reads why "
        "the writer refused it",
    ),
    (_TRANSITIONS, "TransitionManager._recheck_resolve_request"): (
        {"open_disposition_offer": 2},
        "opens the resolve offer on a ready recheck (basis model), or the close "
        "offer on an unmet request (basis needs_info_unmet)",
    ),
    (_OUTCOME, "record_before_save"): (
        {"lapse_offers": 1},
        "lapses, at a save helper before its record is folded (#2811), what "
        "the save's own lapse would: an open resolve or close offer whose "
        "premise has gone, and each decline whose premise moved",
    ),
    (_ME + "turn_application.py", "_lapse_and_record"): (
        {"lapse_offers": 1},
        "lapses, before step 4 and after the final recompute, an open resolve "
        "or close offer whose premise has gone (a deferred close whose M5 "
        "licence fell included), and each decline whose premise moved",
    ),
    (_PROBLEM, "_answer_pending"): (
        {"answer_offer": 1},
        "answers the open statement-revision offer (declined or cancelled); "
        "not a closure",
    ),
    (_PROBLEM, "commit_revision"): (
        {"answer_offer": 1},
        "answers the open statement-revision offer accepted; not a closure",
    ),
    (_PROBLEM, "propose_revision"): (
        {"answer_offer": 1, "open_offer": 1},
        "opens a statement-revision offer, answering a standing one superseded; "
        "not a closure",
    ),
    (_TT, "confirm_open_offer"): (
        {
            "_execute_closed_transition": 1,
            "_execute_resolved_transition": 1,
            "answer_offer": 2,
            "open_disposition_offer": 1,
        },
        "executes the accepted resolve or close offer and answers it accepted; "
        "a close on a resolvable case is answered pivoted and the resolve "
        "offer opened instead (INV-37)",
    ),
    (_TT, "open_disposition_offer"): (
        {"open_offer": 1},
        "opens a resolve or close offer, a close with the reason derived from "
        "the case",
    ),
    (_TT, "execute_user_closure"): (
        {
            "_execute_closed_transition": 1,
            "answer_offer": 2,
            "open_disposition_offer": 1,
        },
        "the REST close: lapses an open offer, or declines the resolve offer "
        "(opening it first), then executes the close",
    ),
    (_CASE + "domain/services/case_service.py", "CaseService.close_case.apply"): (
        {"execute_user_closure": 1},
        "the REST close: closes the case through execute_user_closure",
    ),
    (_CASE + "infrastructure/case_repository.py", "CaseRepository.prepare_for_save"): (
        {"lapse_offers": 1},
        "lapses, at every save, what lapse_offers lapses, so a write outside "
        "the engine lapses what it moved",
    ),
}


class Source(NamedTuple):
    """An in-cycle decider, declared a source of the cycle's record (#3053
    S3d-3, ruling Q4 of 2026-11-25): it decides before the record and folds
    none. ``serves``: for a helper that records no code, the code-recording
    rows it serves."""

    counts: dict[str, int]
    note: str
    serves: tuple[tuple[str, str], ...] = ()


_RECHECK = (_TRANSITIONS, "TransitionManager._recheck_resolve_request")
_MODEL = (_TRANSITIONS, "TransitionManager._apply_model_proposal")
_BACKSTOP = (_PROPOSALS, "_maybe_propose_confirmed_resolution")
_RCC = _INV + "causal_graph/rcc.py"

#: (path, qualname) -> the in-cycle decider's derivations, what it decides,
#: and the rows it serves. Permanent: the cycle decides everything, then
#: records once after the last decider (#2615), and the record derives
#: ``resolution`` from these same functions.
CYCLE_SOURCES: dict[tuple[str, str], Source] = {
    _RECHECK: Source(
        {
            "assess_resolution_readiness": 1,
            "cause_identification_leg": 1,
            "resolution_record_lines": 1,
        },
        "step 0, the remembered request: READY records REQUEST_READY with the "
        "leg and record lines it was decided on, else NEEDS_INFO_UNMET_CLOSE",
    ),
    _MODEL: Source(
        {
            "assess_closure_readiness": 1,
            "assess_resolution_readiness": 1,
            "cause_identification_leg": 1,
            "resolution_record_lines": 1,
        },
        "step 2, the model's proposal through readiness: MODEL_PROPOSAL by "
        "composer, or RESOLUTION_NEEDS_INFO and PENDING_CHECK",
    ),
    (_PROPOSALS, "_maybe_propose_deferred_close"): Source(
        {
            "assess_closure_readiness": 1,
            "cause_identification_leg": 1,
            "pending_symptom_check": 1,
            "resolution_record_lines": 1,
        },
        "the deferred proposer: DEFERRED_RESOLVE, held by a pending check, "
        "or DEFERRED_CLOSE",
    ),
    _BACKSTOP: Source(
        {
            "assess_resolution_readiness": 1,
            "cause_identification_leg": 1,
            "resolution_record_lines": 1,
        },
        "the backstop: BACKSTOP_RESOLVE, or PENDING_CHECK while a check holds it",
    ),
    (_TRANSITIONS, "declined_resolution_reask"): Source(
        {"declined_resolve_entry": 1, "pending_symptom_check": 1},
        "the params of DECLINED_RESOLUTION_REASK, the refusal while a resolve "
        "decline stands, and whether its chips are attached",
        serves=(_RECHECK, _MODEL),
    ),
    (_TRANSITIONS, "declined_close_reask"): Source(
        {"false_alarm_close_declined_at": 1},
        "the params of DECLINED_CLOSE_REASK, the refusal while a close "
        "decline stands",
        serves=(_MODEL,),
    ),
    (_TT, "pending_check_params"): Source(
        {"declined_resolve_entry": 1, "held_check_reason": 1},
        "the params of PENDING_CHECK; why the check holds through the "
        "record's one derivation (held_check_reason)",
        serves=(_MODEL, _BACKSTOP),
    ),
    # The recompute writers log no code, so the typed-code pin cannot apply.
    (_ME + "cause_state.py", "_recompute_cause_state_from_chain"): Source(
        {"held_open_root_ids": 1, "mece_contested_root_ids": 1},
        "the cause recompute, the cycle's last after every decider: writes "
        "cause_identification_contested and _held_open, which the deciders' "
        "identification leg reads (ruling of 2026-11-26 (3))",
    ),
    (_RCC, "retract_stale_engine_rcc"): Source(
        {"mece_contested_root_ids": 1},
        "retracts the engine's conclusion mirror under a contest, in the " "recompute",
    ),
    (_RCC, "synthesize_rcc_from_validated_root"): Source(
        {"mece_contested_root_ids": 1},
        "mints the engine's conclusion mirror, refused under a contest, in the "
        "recompute",
    ),
}

#: The cycle sources that record no code and serve no row: they write the
#: inputs the deciders read.
RECOMPUTE_WRITERS = frozenset(
    {
        (_ME + "cause_state.py", "_recompute_cause_state_from_chain"),
        (_RCC, "retract_stale_engine_rcc"),
        (_RCC, "synthesize_rcc_from_validated_root"),
    }
)

#: The notices the cycle sources record outside ``DECIDED_FROM`` (ruling of
#: 2026-11-26 (2): the typed-code pin covers offers only), with the reason.
CYCLE_NOTICES = {
    "engine_offer_stands": "the model's proposal dropped for the engine's offer "
    "this turn: it decides no outcome, the offer it names does",
    "declined_close_reask": "a refusal of the model's re-proposal while the "
    "user's close decline stands, read off the offer record",
    "declined_resolution_reask": "a refusal of a resolution while the user's "
    "resolve decline stands, read off the offer record",
}

#: The helpers outside ``CYCLE_SOURCES`` that record a ``CYCLE_NOTICES`` code
#: for a source.
CYCLE_NOTICE_HELPERS = {(_TRANSITIONS, "_refuse_declined_reask"): _MODEL}

#: Content predicates (ruling Q6 of 2026-11-25), outside ``DERIVATIONS``: the
#: summary gate is a predicate over the case's content, for which the record
#: has no field; every caller is enumerated, as the narrators' text accessor's.
CONTENT_ACCESSORS: dict[str, str] = {
    "should_generate_terminal_summary": _TT,
    "terminal_summary_skip_reason": _TT,
}

#: (path, qualname) -> ({accessor: references}, what the caller reads).
CONTENT_ACCESSOR_CALLERS: dict[tuple[str, str], tuple[dict[str, int], str]] = {
    (_TT, "terminal_summary_skip_reason"): (
        {"should_generate_terminal_summary": 1},
        "the skip note, on the same predicate",
    ),
    (_ME + "terminal_replies.py", "_closed_suggestions"): (
        {"should_generate_terminal_summary": 1},
        "the offer half of the summary gate (#698, one predicate)",
    ),
    (_TERMINAL_TURNS, "TerminalTurnHandler._handle_report_regeneration"): (
        {"should_generate_terminal_summary": 1, "terminal_summary_skip_reason": 1},
        "the enforcement half, on a regeneration",
    ),
    (_TERMINAL_TURNS, "TerminalTurnHandler.auto_generate_report"): (
        {"should_generate_terminal_summary": 1, "terminal_summary_skip_reason": 1},
        "the enforcement half, at the terminal transition",
    ),
    (_UI, "_transform_resolved"): (
        {"terminal_summary_skip_reason": 1},
        "the Report tab's note when no summary exists",
    ),
}


#: The carried window view (#3046 S3a-2) and its two selects, outside
#: ``DERIVATIONS``: every decider of a fix window reads the record's final
#: verdicts laid over the fresh read. Watched so a new reader stays visible
#: to S3b-S4.
CARRIED_VIEW = (
    "carried_fix_windows",
    "carry_verdicts",
    "latest_fix_window",
    "latest_decided_fix_window",
)

#: (path, qualname) -> references to ``CARRIED_VIEW``.
CARRIED_VIEW_SITES: dict[tuple[str, str], int] = {
    **{
        (_CA, qualname): 1
        for qualname in (
            "attempt_window",
            "carried_fix_windows",
            "cause_recheck_status",
            "confirming_polarity_rows",
            "confirming_window",
            "latest_decided_fix_window",
            "latest_fix_window",
            "off_cause_attempt",
        )
    },
    (_DISCONF, "_fix_failure_refusal"): 1,
    (_DISCONF, "failed_fix_of"): 1,
    (_DISCONF, "m6_disconfirmation_basis"): 1,
    (_TT, "assess_resolution_readiness"): 1,
    # The cause re-check ask's names, the reopen chip's refusal's content.
    (_TT, "cause_recheck_names"): 1,
    (_MONITOR, "ProgressMonitor._detect_fix_failure_cycle"): 1,
    (_MONITOR, "ProgressMonitor._repair_fix_failure_cycle"): 1,
    (_PROBLEM, "invalidation_refusal"): 1,
    (_SG, "_date_to_the_report"): 1,
    (_SG, "_flag_absence_inside_a_failed_fix_window"): 1,
    (_SG, "_flag_unbound_post_fix_rows"): 1,
    (_SG, "standing_fix_action"): 1,
    (_CB + "fix_verification_block.py", "build_fix_verification_block"): 1,
    (_CB + "causal_graph_block.py", "fix_confirmed"): 1,
    # The mint names the FAILED windows a failed write did not mint.
    (_ME + "response_application.py", "_mint_before_the_chain"): 1,
    # The fold seeds the view it carries for the rest of its computation.
    (_OUTCOME, "compute_case_outcome"): 1,
}


class Mark(NamedTuple):
    """A site no table may hold: ``what`` says why it fails."""

    path: str
    qualname: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.qualname}: {self.what}"


class Sites(NamedTuple):
    """What a walk found: three tables keyed by ``(path, qualname)``, and the
    marks that fail wherever they are."""

    derivations: dict[tuple[str, str], Counter]
    fields: dict[tuple[str, str], Counter]
    entries: dict[tuple[str, str], Counter]
    #: An entry referenced other than by a call.
    uncalled_entries: list[Mark]
    #: A watched function imported under another name.
    aliased_imports: list[Mark]


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_NESTED_SCOPES = _FUNCTIONS + (
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _in_body(child: ast.AST, scope: ast.AST) -> bool:
    """Whether ``child``, a child node of ``scope``, is in its body: not a
    decorator, a default, an annotation or a base."""
    body = scope.body
    if isinstance(body, list):
        return any(child is statement for statement in body)
    return child is body


def _locals_of(function: ast.AST) -> frozenset[str]:
    """The names ``function``'s body assigns that nothing else binds: not a
    parameter, not a function-local import, not a ``global`` or ``nonlocal``
    declaration in it, and not a ``nonlocal`` in any scope nested in it. A
    name bound any of those ways may be the watched function, so its loads
    stay counted."""
    arguments = function.args
    bound_otherwise = {
        arg.arg
        for arg in (
            *arguments.posonlyargs,
            *arguments.args,
            arguments.vararg,
            *arguments.kwonlyargs,
            arguments.kwarg,
        )
        if arg is not None
    }
    stored: set[str] = set()
    body = function.body
    stack = list(body) if isinstance(body, list) else [body]
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED_SCOPES):
            # Its own names are its own, but a ``nonlocal`` in it rebinds this
            # function's name.
            bound_otherwise.update(
                name
                for inner in ast.walk(node)
                if isinstance(inner, ast.Nonlocal)
                for name in inner.names
            )
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            stored.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound_otherwise.update(node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound_otherwise.update(
                (alias.asname or alias.name).split(".")[0] for alias in node.names
            )
        stack.extend(ast.iter_child_nodes(node))
    return frozenset(stored - bound_otherwise)


def _scope_of(node: ast.AST, parents: dict) -> str:
    """The qualname of the function or class enclosing ``node``."""
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return ".".join(reversed(names)) or "<module>"


def _constant_getattr(node: ast.AST) -> str | None:
    """The name ``getattr(x, "name")`` reads, or None."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[1].value
    return None


def collect_progress_reads(tree: ast.AST, path: str) -> dict[tuple[str, str], Counter]:
    """Each attribute Load of a ``PROGRESS_INPUTS`` name in ``tree``, and each
    builtin ``getattr`` of one by a constant name, by enclosing function."""
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    found: dict[tuple[str, str], Counter] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name = node.attr
        else:
            name = _constant_getattr(node)
        if name in PROGRESS_INPUTS:
            key = (path, _scope_of(node, parents))
            found.setdefault(key, Counter())[name] += 1
    return found


def collect(tree: ast.AST, path: str) -> Sites:
    """The sites in ``tree``, the module at ``path``.

    A Load of a derivation or an entry as a name or an attribute, an attribute
    Load of a field, and a builtin ``getattr`` of any of them by a constant
    name. A load in a function's body, of a name that is that function's
    local variable (``_locals_of``), is not a site; every other Load is. An
    import, a ``def``, an assignment or a ``del`` is not a Load; an import
    under another name is a mark.
    """
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    locals_memo: dict[ast.AST, frozenset[str]] = {}

    def scope_of(node: ast.AST) -> str:
        return _scope_of(node, parents)

    def is_local(node: ast.Name) -> bool:
        # The nearest enclosing scope decides. Only a load in a function's
        # body is judged against its locals; a load in a class body, a
        # comprehension or a function's signature is counted.
        child, scope = node, parents.get(node)
        while scope is not None and not isinstance(scope, _NESTED_SCOPES):
            child, scope = scope, parents.get(scope)
        if scope is None or not isinstance(scope, _FUNCTIONS):
            return False
        if not _in_body(child, scope):
            return False
        function = scope
        if function not in locals_memo:
            locals_memo[function] = _locals_of(function)
        return node.id in locals_memo[function]

    sites = Sites({}, {}, {}, [], [])
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (
                    alias.name in DERIVATIONS or alias.name in ENTRIES
                ) and alias.asname not in (None, alias.name):
                    sites.aliased_imports.append(
                        Mark(
                            path,
                            scope_of(node),
                            node.lineno,
                            f"{alias.name} imported as {alias.asname}",
                        )
                    )
            continue
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name, attribute = node.attr, True
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if is_local(node):
                continue
            name, attribute = node.id, False
        elif _constant_getattr(node) is not None:
            # ``getattr(x, "name")`` reads what ``x.name`` reads; it is never
            # a call of that name.
            name, attribute = _constant_getattr(node), True
        else:
            continue
        key = (path, scope_of(node))
        if name in DERIVATIONS:
            sites.derivations.setdefault(key, Counter())[name] += 1
        elif attribute and name in FIELDS:
            sites.fields.setdefault(key, Counter())[name] += 1
        elif name in ENTRIES:
            sites.entries.setdefault(key, Counter())[name] += 1
            parent = parents.get(node)
            if isinstance(node, ast.Call) or not (
                isinstance(parent, ast.Call) and parent.func is node
            ):
                sites.uncalled_entries.append(
                    Mark(path, key[1], node.lineno, f"{name}, not called")
                )
    return sites


@lru_cache(maxsize=1)
def _scan() -> tuple[Sites, int]:
    sites = Sites({}, {}, {}, [], [])
    files = 0
    for path in sorted(_PACKAGE.rglob("*.py")):
        files += 1
        found = collect(parse_file(path), path.relative_to(_ROOT).as_posix())
        sites.derivations.update(found.derivations)
        sites.fields.update(found.fields)
        sites.entries.update(found.entries)
        sites.uncalled_entries.extend(found.uncalled_entries)
        sites.aliased_imports.extend(found.aliased_imports)
    return sites, files


@lru_cache(maxsize=1)
def _scan_prompts() -> tuple[dict[tuple[str, str], Counter], int]:
    found: dict[tuple[str, str], Counter] = {}
    files = 0
    for path in sorted((_ROOT / _PROMPTS).rglob("*.py")):
        files += 1
        found.update(
            collect_progress_reads(parse_file(path), path.relative_to(_ROOT).as_posix())
        )
    return found, files


def _render(counts: dict) -> str:
    return str(dict(sorted(counts.items()))) if counts else "nothing"


def _differences(found: dict, declared: dict) -> list[str]:
    """One line per ``path::qualname`` where the scan and the table differ."""
    lines = []
    for key in sorted(found.keys() | declared.keys()):
        got, want = dict(found.get(key, {})), declared.get(key, {})
        if got != want:
            path, qualname = key
            lines.append(
                f"{path}::{qualname}: declared {_render(want)}, found {_render(got)}"
            )
    return lines


def _with(source: str, path: str) -> Sites:
    """The real scan with ``source``, as the module at ``path``, laid over it.

    A row the probe reports replaces the real row with the same key, so a new
    function adds a row and a declared function's row is recounted. Every row
    the probe omits is kept, the other functions of the module at ``path``
    included. The probe's marks are added to the real scan's. The comparisons
    then see what they would on that tree.
    """
    real = _scan()[0]
    probe = collect(parse_source(source, _ROOT / path), path)
    return Sites(
        {**real.derivations, **probe.derivations},
        {**real.fields, **probe.fields},
        {**real.entries, **probe.entries},
        real.uncalled_entries + probe.uncalled_entries,
        real.aliased_imports + probe.aliased_imports,
    )


_ENTRY_COUNTS = {key: counts for key, (counts, _) in ENTRY_CALLERS.items()}

#: Every declared derivation site: the census's and the cycle's sources.
_DECLARED = {
    **DERIVATION_SITES,
    **{key: source.counts for key, source in CYCLE_SOURCES.items()},
}

_HELP = (
    "#2615 moves every reader of a case's outcome onto one record "
    "(CaseOutcome). A new reader reads the outcome from it. Until the stage "
    "that serves it lands, declare the site in {table} with a comment naming "
    "the #2615 stage that moves it; a changed count is updated only after "
    "reading the new reference."
)


def _check_only_called(sites: Sites) -> None:
    uncalled = [str(mark) for mark in sites.uncalled_entries]
    assert not uncalled, (
        "an entry is referenced other than by a call, so its callers are out "
        "of the enumeration's sight:\n  " + "\n  ".join(uncalled)
    )


def _check_no_alias(sites: Sites) -> None:
    aliased = [str(mark) for mark in sites.aliased_imports]
    assert not aliased, (
        "a watched function is imported under another name, which hides every "
        "later call from this census; import it under its own name:\n  "
        + "\n  ".join(aliased)
    )


def test_the_scan_covers_the_package():
    """A positive control: a walk that stopped matching would report nothing
    to declare, which reads as a clean census."""
    sites, files = _scan()
    assert files > 400, f"scanned {files} files under {_PACKAGE}"
    found = {
        name
        for table in (sites.derivations, sites.fields, sites.entries)
        for counts in table.values()
        for name in counts
    }
    declared = {
        name
        for table in (_DECLARED, FIELD_READS, _ENTRY_COUNTS)
        for counts in table.values()
        for name in counts
    }
    assert declared <= found, f"never matched: {sorted(declared - found)}"


@pytest.mark.parametrize("name", sorted({**DERIVATIONS, **FIELDS, **ENTRIES}))
def test_the_collector_matches_each_watched_name(name):
    """A positive control per name, including one with no reader today
    (``resolution_complete``), which the real scan alone cannot show."""
    if name in FIELDS:
        source, table = f"def f(x):\n    return x.{name}", "fields"
    else:
        source = f"def f(x):\n    return {name}(x)"
        table = "derivations" if name in DERIVATIONS else "entries"
    found = getattr(collect(ast.parse(source), "m.py"), table)
    assert found == {("m.py", "f"): Counter({name: 1})}


def test_every_derivation_site_is_declared():
    problems = _differences(_scan()[0].derivations, _DECLARED)
    assert not problems, (
        "a case's outcome is derived where the census does not say:\n  "
        + "\n  ".join(problems)
        + "\n"
        + _HELP.format(table="DERIVATION_SITES")
    )


def test_every_carried_view_read_is_declared():
    found: Counter = Counter()
    for path in sorted(_PACKAGE.rglob("*.py")):
        tree = parse_file(path)
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for node in ast.walk(tree):
            name = getattr(node, "id", None) or getattr(node, "attr", None)
            if name in CARRIED_VIEW and isinstance(node.ctx, ast.Load):
                rel = path.relative_to(_ROOT).as_posix()
                found[(rel, _scope_of(node, parents))] += 1
    assert dict(found) == CARRIED_VIEW_SITES, (
        "a reader of the carried window view is not declared in "
        "CARRIED_VIEW_SITES (#3046 S3a-2)"
    )


def test_every_raw_field_read_is_declared():
    problems = _differences(_scan()[0].fields, FIELD_READS)
    assert not problems, (
        "an outcome field is read raw where the census does not say:\n  "
        + "\n  ".join(problems)
        + "\n"
        + _HELP.format(table="FIELD_READS")
    )


def test_every_raw_progress_read_in_the_prompt_is_declared():
    """Pin 3 where the prompt is built (#2811 S2b-1c-i): a prompt line reads
    whether the problem is verified, its cause identified or a solution
    proposed off the record, never raw, but where declared."""
    found, files = _scan_prompts()
    assert files > 20, f"scanned {files} files under {_PROMPTS}"
    problems = _differences(found, PROMPT_PROGRESS_READS)
    assert not problems, (
        "a progress input the record holds is read raw in the prompt where "
        "the census does not say:\n  "
        + "\n  ".join(problems)
        + "\nRead it off the record (narration.prompt_state), or declare the "
        "site in PROMPT_PROGRESS_READS with the #2615 stage that moves it."
    )


@pytest.mark.parametrize("name", PROGRESS_INPUTS)
@pytest.mark.parametrize(
    "source",
    [
        "def f(case):\n    return case.progress.{name}",
        "def f(p):\n    return getattr(p, '{name}', None)",
    ],
)
def test_the_prompt_watch_matches_each_input(name, source):
    """A positive control per name and shape."""
    tree = ast.parse(source.format(name=name))
    assert collect_progress_reads(tree, "m.py") == {("m.py", "f"): Counter({name: 1})}


# ---------------------------------------------------------------------------
# The cycle's sources (#3053 S3d-3, ruling Q4) and the content accessors (Q6)
# ---------------------------------------------------------------------------


def _function(path: str, qualname: str) -> ast.AST:
    """The ``def`` of ``qualname`` in the module at ``path``."""
    node: ast.AST = parse_file(_ROOT / path)
    for part in qualname.split("."):
        node = next(
            child
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and child.name == part
        )
    return node


_CODE_ENUMS = {"NoticeCode": NoticeCode, "OfferCode": OfferCode}


def _recorded(function: ast.AST, where: str) -> tuple[set[str], list[str]]:
    """The codes ``function`` records (``record_decision(m, <Enum>.<X>,
    ...)``), by value, and one problem per call whose code is not such a
    literal."""
    codes: set[str] = set()
    problems = []
    for node in ast.walk(function):
        if not (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None))
            == "record_decision"
        ):
            continue
        code = node.args[1] if len(node.args) > 1 else None
        enum = getattr(getattr(code, "value", None), "id", None)
        if not isinstance(code, ast.Attribute) or enum not in _CODE_ENUMS:
            problems.append(f"{where}:{node.lineno}: a code that is not a literal")
            continue
        codes.add(str(getattr(_CODE_ENUMS[enum], code.attr)))
    return codes, problems


def _composers(function: ast.AST) -> set[str]:
    """The ``"composer": <const>`` literals ``function`` writes."""
    return {
        value.value
        for node in ast.walk(function)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant)
        and key.value == "composer"
        and isinstance(value, ast.Constant)
    }


def _cycle_source_problems(
    sources=CYCLE_SOURCES,
    notices=CYCLE_NOTICES,
    helpers=CYCLE_NOTICE_HELPERS,
    resolve=lambda key: _function(*key),
) -> list[str]:
    """Each way the cycle's sources break the table's pins: a source under
    ``narration/`` or ``prompts/``, in ``DERIVATION_SITES`` too, counting
    ``fix_windows``, recording an offer code or ``PENDING_CHECK`` that is not
    a ``DECIDED_FROM`` key, another code that is not a declared cycle notice,
    a code that is not a literal, or a composer ``DECIDED_FROM`` lacks; a row
    recording no code that serves no code-recording row (but a recompute
    writer); and a declared notice no source or helper records."""
    decided = {str(code) for code, _composer in DECIDED_FROM}
    problems: list[str] = []
    recording: set = set()
    found: set[str] = set()
    for key, source in sources.items():
        where = "::".join(key)
        if key[0].startswith((_INV + "narration/", _PROMPTS)):
            problems.append(f"{where}: a narrator or a prompt builder")
        if key in DERIVATION_SITES:
            problems.append(f"{where}: in DERIVATION_SITES too")
        if "fix_windows" in source.counts:
            problems.append(f"{where}: counts fix_windows, not the carried view")
        function = resolve(key)
        codes, bad = _recorded(function, where)
        problems += bad
        for code in codes:
            if code in notices:
                found.add(code)
            elif code not in decided:
                problems.append(f"{where}: records {code}, undeclared")
        for composer in _composers(function):
            if (OfferCode.MODEL_PROPOSAL, composer) not in DECIDED_FROM:
                problems.append(f"{where}: composer {composer!r} is not decided from")
        if codes:
            recording.add(key)
    for key, source in sources.items():
        if key in recording:
            continue
        if not source.serves and key not in RECOMPUTE_WRITERS:
            problems.append(f"{'::'.join(key)}: records no code and serves no row")
        for served in source.serves:
            if served not in recording:
                problems.append(f"{'::'.join(key)}: serves {served}, no source")
    for key, served in helpers.items():
        codes, bad = _recorded(resolve(key), "::".join(key))
        problems += bad
        found |= codes & set(notices)
        if served not in recording:
            problems.append(f"{'::'.join(key)}: serves {served}, no source")
    missing = [code for code in notices if code not in found]
    return problems + [f"{code}: declared, recorded by no source" for code in missing]


def test_the_cycle_sources_hold_the_tables_pins():
    """Ruling Q4: each in-cycle decider is a declared source of the record;
    its offer codes and ``PENDING_CHECK`` are ``DECIDED_FROM`` keys, its other
    codes declared notices, and it reads windows only through the carried
    view (``CARRIED_VIEW_SITES``)."""
    assert _cycle_source_problems() == []
    recording = [k for k in CYCLE_SOURCES if _recorded(_function(*k), "")[0]]
    assert len(recording) == 4, "premise: the four code-recording deciders"
    assert {"readiness", "confirmation", "closure"} <= _composers(_function(*_MODEL))


#: A probe source's module: not under ``narration/``, as a source never is.
_CYCLE_PROBE = _ME + "probe.py"
_PROBE_KEY = (_CYCLE_PROBE, "decide")


def _probe_problems(source: str, **tables) -> list[str]:
    """``_cycle_source_problems`` with the probe ``decide`` (``source``) as a
    source serving nothing, beside the real ones."""
    probe = Source({}, "a probe")
    function = ast.parse(source).body[0]
    sources = {**CYCLE_SOURCES, _PROBE_KEY: probe, **tables.pop("sources", {})}
    return _cycle_source_problems(
        sources,
        resolve=lambda key: function if key == _PROBE_KEY else _function(*key),
        **tables,
    )


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        (
            "def decide(m):\n    record_decision(m, NoticeCode.FIX_UNTARGETED)",
            f"{_CYCLE_PROBE}::decide: records fix_untargeted, undeclared",
        ),
        (
            "def decide(m, code):\n    record_decision(m, code)",
            f"{_CYCLE_PROBE}::decide:2: a code that is not a literal",
        ),
        (
            "def decide(m):\n    c = {'composer': 'summary'}\n"
            "    record_decision(m, OfferCode.MODEL_PROPOSAL, **c)",
            f"{_CYCLE_PROBE}::decide: composer 'summary' is not decided from",
        ),
        (
            "def decide(m):\n    return None",
            f"{_CYCLE_PROBE}::decide: records no code and serves no row",
        ),
    ],
)
def test_a_source_breaking_a_pin_is_named(source, problem):
    assert problem in _probe_problems(source)


def test_a_declared_notice_no_source_records_is_named():
    notices = {**CYCLE_NOTICES, "fix_report_ignored": "a probe"}
    problems = _probe_problems(
        "def decide(m):\n    record_decision(m, OfferCode.MODEL_PROPOSAL)",
        notices=notices,
    )
    assert problems == ["fix_report_ignored: declared, recorded by no source"]


@pytest.mark.parametrize(
    ("key", "counts", "problem"),
    [
        (
            (_TT, "assess_closure_readiness"),
            {},
            f"{_TT}::assess_closure_readiness: in DERIVATION_SITES too",
        ),
        (
            (_INV + "narration/offers.py", "render_offer"),
            {},
            f"{_INV}narration/offers.py::render_offer: a narrator or a prompt "
            "builder",
        ),
        (
            (_CA, "carried_fix_windows"),
            {"fix_windows": 1},
            f"{_CA}::carried_fix_windows: counts fix_windows, not the carried view",
        ),
    ],
)
def test_a_misplaced_source_is_named(key, counts, problem):
    sources = {key: Source(counts, "a probe", serves=(_MODEL,))}
    assert problem in _probe_problems(
        "def decide(m):\n    record_decision(m, OfferCode.MODEL_PROPOSAL)",
        sources=sources,
    )


def test_no_fresh_fallback_row_remains():
    """Ruling Q1: no ``*_fresh`` row after S3d-3, and the confirmation folds
    nothing in the cycle (``resolution_confirmation_params`` takes the fold
    its caller holds)."""
    tables = (_DECLARED, FIELD_READS, _ENTRY_COUNTS, CONTENT_ACCESSOR_CALLERS)
    fresh = [key for table in tables for key in table if key[1].endswith("_fresh")]
    assert fresh == []
    confirmation = _function(
        _ME + "terminal_replies.py", "resolution_confirmation_params"
    )
    named = {getattr(n, "id", None) for n in ast.walk(confirmation)}
    assert not named & {"_reply_outcome", "narration_outcome"}


def _content_reads() -> dict[tuple[str, str], Counter]:
    found: dict[tuple[str, str], Counter] = {}
    for path in sorted(_PACKAGE.rglob("*.py")):
        tree = parse_file(path)
        rel = path.relative_to(_ROOT).as_posix()
        for key, counts in _content_reads_of(tree, rel).items():
            found[key] = counts
    return found


def _content_reads_of(tree: ast.AST, path: str) -> dict[tuple[str, str], Counter]:
    """Each Load of a ``CONTENT_ACCESSORS`` name in ``tree``, as a name or an
    attribute, and a builtin ``getattr`` of one by a constant name."""
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    found: dict[tuple[str, str], Counter] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(
            node.ctx, ast.Load
        ):
            name = getattr(node, "id", None) or node.attr
        else:
            name = _constant_getattr(node)
        if name in CONTENT_ACCESSORS:
            key = (path, _scope_of(node, parents))
            found.setdefault(key, Counter())[name] += 1
    return found


def test_every_content_accessor_caller_is_declared():
    """Ruling Q6: the summary gate is a content predicate, not an outcome
    derivation; each caller is declared with what it reads."""
    declared = {key: counts for key, (counts, _) in CONTENT_ACCESSOR_CALLERS.items()}
    problems = _differences(_content_reads(), declared)
    assert not problems, "\n  ".join(problems)
    assert not set(CONTENT_ACCESSORS) & set(DERIVATIONS)


@pytest.mark.parametrize("name", sorted(CONTENT_ACCESSORS))
@pytest.mark.parametrize(
    "source",
    ["def f(x):\n    return {name}(x)", "def f(m):\n    return getattr(m, '{name}')"],
)
def test_the_content_watch_matches_each_accessor(name, source):
    """A positive control per accessor and shape."""
    tree = ast.parse(source.format(name=name))
    assert _content_reads_of(tree, "m.py") == {("m.py", "f"): Counter({name: 1})}


def test_every_closure_entry_caller_is_declared():
    problems = _differences(_scan()[0].entries, _ENTRY_COUNTS)
    assert not problems, (
        "an entry is called where the enumeration does not say:\n  "
        + "\n  ".join(problems)
        + "\nDeclare the caller in ENTRY_CALLERS with what it opens, answers or "
        "closes (#2615 pin 5)."
    )


#: The four closure entries that decide a closure reason (#2615, *Closure on
#: demand*), each of which computes it through ``closure_outcome`` (#2636).
_CLOSURE_ENTRIES = (
    "open_disposition_offer",
    "disposition_refusal",
    "confirm_open_offer",
    "execute_user_closure",
)


@pytest.mark.parametrize("entry", _CLOSURE_ENTRIES)
def test_each_closure_entry_reaches_closure_outcome(entry):
    """Pin 5's other half: an entry that decides a closure computes it on
    demand, and none derives the reason around the record's function."""
    found = _scan()[0].derivations.get((_TT, entry), {})
    assert found.get("closure_outcome") == 1
    assert "derive_closure_reason" not in found


def test_a_closure_entry_is_only_called():
    _check_only_called(_scan()[0])


def test_no_watched_function_is_imported_under_another_name():
    _check_no_alias(_scan()[0])


_NARRATION = _PACKAGE / "core" / "investigation" / "narration"


def _unwalked_directories(root: Path) -> list[str]:
    """Each directory under ``root``, itself included, that holds a module and
    has no ``__init__.py``. grimp does not walk into it, so contract 16 does
    not see what a module there imports."""
    unwalked = set()
    for module in root.rglob("*.py"):
        for directory in module.parents:
            if not (directory / "__init__.py").is_file():
                unwalked.add(directory.relative_to(root.parent).as_posix())
            if directory == root:
                break
    return sorted(unwalked)


def test_every_narration_directory_is_a_regular_package():
    unwalked = _unwalked_directories(_NARRATION)
    assert not unwalked, (
        "a directory under narration/ holds a module but no __init__.py, so "
        "the import contract cannot see what it imports (#2615 pin 1); add "
        "the __init__.py:\n  " + "\n  ".join(unwalked)
    )


def test_the_regular_package_check_bites(tmp_path):
    root = tmp_path / "narration"
    (root / "sub").mkdir(parents=True)
    (root / "__init__.py").touch()
    (root / "sub" / "narrator.py").touch()
    assert _unwalked_directories(root) == ["narration/sub"]
    (root / "sub" / "__init__.py").touch()
    assert _unwalked_directories(root) == []


def _module_functions(path: str) -> set[str]:
    """The module's functions, and its classes' methods as ``Class.method``."""
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    body = parse_file(_ROOT / path).body
    return {node.name for node in body if isinstance(node, functions)} | {
        f"{node.name}.{method.name}"
        for node in body
        if isinstance(node, ast.ClassDef)
        for method in node.body
        if isinstance(method, functions)
    }


@pytest.mark.parametrize(
    "name", sorted({**DERIVATIONS, **ENTRIES, **CONTENT_ACCESSORS})
)
def test_each_watched_function_is_defined_where_the_census_says(name):
    """A rename would otherwise leave the census watching a name nothing has."""
    path = {**DERIVATIONS, **ENTRIES, **CONTENT_ACCESSORS}[name]
    qualname = f"{_METHODS[name]}.{name}" if name in _METHODS else name
    assert qualname in _module_functions(path), f"{qualname} is not defined in {path}"


@pytest.mark.parametrize("name", sorted(FIELDS))
def test_each_field_is_defined_on_its_model(name):
    path, model = FIELDS[name]
    cls = next(
        node
        for node in parse_file(_ROOT / path).body
        if isinstance(node, ast.ClassDef) and node.name == model
    )
    defined = {
        node.target.id
        for node in cls.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    } | {node.name for node in cls.body if isinstance(node, ast.FunctionDef)}
    assert name in defined, f"{model}.{name} is not defined in {path}"


class TestTheCollector:
    """Reach first: a collector that misses a shape the code writes reports a
    clean census over sites it never saw."""

    @staticmethod
    def _count(source: str) -> int:
        sites = collect(ast.parse(source), "m.py")
        tables = (sites.derivations, sites.fields, sites.entries)
        return sum(sum(c.values()) for table in tables for c in table.values())

    @pytest.mark.parametrize(
        "source",
        [
            "def f(case):\n    return fix_windows(case)",
            "def f(case):\n    return ca.fix_windows(case)",
            "def f(run):\n    return run(callback=fix_windows)",
            "def f(case):\n    return case.progress.completed_milestones",
            "def f(case):\n    return case.progress.mitigation.verified",
            "def f(case):\n    return case.closure_reason",
            "def f(case):\n    return answer_offer(case, proposal_id='p')",
            "async def f(case):\n    return terminal_transitions.lapse_offers(case)",
            # A parameter named after a derivation: the derivation, passed in.
            "def f(case, assess_closure_readiness):\n"
            "    return assess_closure_readiness(case)",
        ],
    )
    def test_each_shape_is_one_site(self, source):
        assert self._count(source) == 1, source

    @pytest.mark.parametrize(
        "source",
        [
            "from faultmaven.core.investigation.cause_assurance import fix_windows",
            "from faultmaven.core.investigation.offers import answer_offer",
            "def fix_windows(case):\n    return []",
            "def f(case, reason):\n    case.closure_reason = reason",
            "def f(m):\n    del m.verified",
            "def f(verified):\n    return verified",
            "def f(case):\n    return 'solution_verified' in case.milestones",
            # A computed name: the collector cannot know what it reads.
            "def f(case, name):\n    return getattr(case, name)",
            # A local variable named after an entry is not the entry.
            "def f(case):\n    open_offer = case.offer_record.open\n"
            "    return open_offer is not None and open_offer.kind",
        ],
    )
    def test_an_import_a_def_a_write_a_local_or_a_string_is_not_a_site(self, source):
        assert self._count(source) == 0, source

    def test_a_global_declaration_keeps_the_module_function(self):
        source = (
            "def f(case):\n    global fix_windows\n"
            "    fix_windows = wrap(fix_windows)\n    return fix_windows(case)"
        )
        assert collect(ast.parse(source), "m.py").derivations == {
            ("m.py", "f"): Counter({"fix_windows": 2})
        }

    @pytest.mark.parametrize(
        ("source", "scope", "count"),
        [
            # Bound by a function-local import, then rebound: still the
            # watched function where it is not rebound.
            (
                "def f(case, strict):\n"
                "    from faultmaven.core.investigation.cause_assurance"
                " import fix_windows\n"
                "    if strict:\n        fix_windows = wrap(fix_windows)\n"
                "    return fix_windows(case)",
                "f",
                2,
            ),
            (
                "def f(case):\n"
                "    from faultmaven.core.investigation.cause_assurance"
                " import fix_windows\n"
                "    if (fix_windows := fix_windows):\n"
                "        return fix_windows(case)",
                "f",
                2,
            ),
            # A parameter, then reassigned.
            (
                "def f(case, fix_windows):\n"
                "    fix_windows = fix_windows or wrap\n    return fix_windows(case)",
                "f",
                2,
            ),
            # Defaults and decorators are evaluated in the enclosing scope:
            # the body's own assignment does not make them local.
            (
                "def f(case, cb=fix_windows):\n"
                "    fix_windows = cb\n    return fix_windows(case)",
                "f",
                1,
            ),
            (
                "def f(case, fix_windows=fix_windows):\n"
                "    fix_windows = wrap(fix_windows)\n    return fix_windows(case)",
                "f",
                3,
            ),
            (
                "@fix_windows\ndef f(case):\n"
                "    fix_windows = 1\n    return fix_windows",
                "f",
                1,
            ),
            (
                "H = lambda case, cb=fix_windows: (fix_windows := cb)(case)",
                "<module>",
                1,
            ),
        ],
    )
    def test_a_name_bound_otherwise_or_read_by_the_signature_is_counted(
        self, source, scope, count
    ):
        compile(source, "m.py", "exec")
        assert collect(ast.parse(source), "m.py").derivations == {
            ("m.py", scope): Counter({"fix_windows": count})
        }

    @pytest.mark.parametrize(
        ("source", "scope"),
        [
            # A class body resolves a name itself: an import, a ``global`` or
            # an assignment after the load leaves the function's local unread.
            (
                "def run(case):\n    fix_windows = None\n    class C:\n"
                "        from ca import fix_windows\n"
                "        r = fix_windows(case)\n    return C.r",
                "run.C",
            ),
            (
                "def run(case):\n    fix_windows = None\n    class C:\n"
                "        global fix_windows\n        r = fix_windows(case)\n"
                "    return C.r",
                "run.C",
            ),
            (
                "def run(case):\n    fix_windows = None\n    class C:\n"
                "        r = fix_windows(case)\n        fix_windows = 1\n"
                "    return C.r",
                "run.C",
            ),
            # A nested scope's ``nonlocal`` rebinds the function's name.
            (
                "def run(case):\n    fix_windows = None\n    def setter():\n"
                "        nonlocal fix_windows\n        from ca import fix_windows\n"
                "    setter()\n    return fix_windows(case)",
                "run",
            ),
            (
                "def run(case):\n    fix_windows = None\n    class C:\n"
                "        nonlocal fix_windows\n        from ca import fix_windows\n"
                "    return fix_windows(case)",
                "run",
            ),
            # A comprehension, a nested def's default and a class decorator
            # are counted even where the function assigns the name.
            (
                "def run(cases):\n    fix_windows = None\n"
                "    return [fix_windows(c) for c in cases]",
                "run",
            ),
            (
                "def run(case):\n    fix_windows = None\n"
                "    def g(cb=fix_windows):\n        return cb\n    return g()",
                "run.g",
            ),
            (
                "def run(case):\n    fix_windows = None\n"
                "    @fix_windows\n    class C:\n        pass\n    return C",
                "run.C",
            ),
        ],
    )
    def test_a_load_outside_the_function_body_proper_is_counted(self, source, scope):
        compile(source, "m.py", "exec")
        assert collect(ast.parse(source), "m.py").derivations == {
            ("m.py", scope): Counter({"fix_windows": 1})
        }

    @pytest.mark.parametrize(
        ("source", "field"),
        [
            (
                "def f(case):\n    return getattr(case, 'closure_reason', None)",
                "closure_reason",
            ),
            ("def f(m):\n    return getattr(m, 'verified')", "verified"),
        ],
    )
    def test_a_getattr_by_a_constant_name_is_a_field_read(self, source, field):
        assert collect(ast.parse(source), "m.py").fields == {
            ("m.py", "f"): Counter({field: 1})
        }

    def test_a_getattr_of_an_entry_is_an_uncalled_reference(self):
        sites = collect(
            ast.parse("def f(mod):\n    return getattr(mod, 'answer_offer')"), "m.py"
        )
        assert sites.entries == {("m.py", "f"): Counter({"answer_offer": 1})}
        assert [str(m) for m in sites.uncalled_entries] == [
            "m.py:2 f: answer_offer, not called"
        ]

    @pytest.mark.parametrize(
        ("source", "marked"),
        [
            (
                "from x import fix_windows as fw",
                ["m.py:1 <module>: fix_windows imported as fw"],
            ),
            (
                "from x import answer_offer as ao",
                ["m.py:1 <module>: answer_offer imported as ao"],
            ),
            ("from x import fix_windows as fix_windows", []),
            ("from x import shown_milestones, fix_windows", []),
            ("import faultmaven.core.investigation.cause_assurance as ca", []),
        ],
    )
    def test_an_import_under_another_name_is_marked(self, source, marked):
        sites = collect(ast.parse(source), "m.py")
        assert [str(m) for m in sites.aliased_imports] == marked

    def test_a_site_is_attributed_to_its_enclosing_function(self):
        source = (
            "x = shown_milestones\n"
            "class C:\n    def m(self, case):\n"
            "        def inner():\n            return fix_windows(case)\n"
        )
        assert collect(ast.parse(source), "m.py").derivations == {
            ("m.py", "<module>"): Counter({"shown_milestones": 1}),
            ("m.py", "C.m.inner"): Counter({"fix_windows": 1}),
        }


_PROBE = "faultmaven/core/investigation/narration/probe.py"


def test_a_new_derivation_reference_fails_the_census():
    """A new function's call, and a reference passed as a value."""
    found = _with(
        "def summarise(case):\n    return fix_windows(case)\n"
        "def wire(run):\n    return run(callback=fix_windows)\n",
        _PROBE,
    ).derivations
    assert _differences(found, _DECLARED) == [
        f"{_PROBE}::summarise: declared nothing, found {{'fix_windows': 1}}",
        f"{_PROBE}::wire: declared nothing, found {{'fix_windows': 1}}",
    ]


def test_a_changed_count_fails_the_census():
    """One more call in a declared function. The probe holds only
    ``derive_closure_reason``; ``_with`` keeps the module's other rows, so the
    one difference is that function's."""
    found = _with(
        "def derive_closure_reason(case):\n"
        "    return _mitigation_verified(case) and _mitigation_verified(case)"
        " or _mitigation_declared_stable(case) or declined_resolve_entry(case)\n",
        _TT,
    ).derivations
    assert _differences(found, _DECLARED) == [
        f"{_TT}::derive_closure_reason: declared "
        "{'_mitigation_declared_stable': 1, '_mitigation_verified': 1, "
        "'declined_resolve_entry': 1}, found "
        "{'_mitigation_declared_stable': 1, '_mitigation_verified': 2, "
        "'declined_resolve_entry': 1}"
    ]


def test_a_new_raw_field_read_fails_the_census():
    found = _with(
        "def label(case):\n"
        "    return case.progress.completed_milestones, case.closure_reason\n",
        _PROBE,
    ).fields
    assert _differences(found, FIELD_READS) == [
        f"{_PROBE}::label: declared nothing, found "
        "{'closure_reason': 1, 'completed_milestones': 1}"
    ]


def test_a_new_entry_caller_fails_the_enumeration():
    found = _with(
        "def offer(case, kind, basis):\n"
        "    return open_disposition_offer(case, kind=kind, basis=basis)\n",
        _PROBE,
    ).entries
    assert _differences(found, _ENTRY_COUNTS) == [
        f"{_PROBE}::offer: declared nothing, found {{'open_disposition_offer': 1}}"
    ]


def test_an_entry_referenced_other_than_by_a_call_fails():
    sites = _with("HANDLERS = {'close': _execute_closed_transition}\n", _PROBE)
    with pytest.raises(
        AssertionError,
        match=re.escape(f"{_PROBE}:1 <module>: _execute_closed_transition, not called"),
    ):
        _check_only_called(sites)
    assert _differences(sites.entries, _ENTRY_COUNTS) == [
        f"{_PROBE}::<module>: declared nothing, found "
        "{'_execute_closed_transition': 1}"
    ]


def test_an_import_under_another_name_fails():
    sites = _with(
        "from faultmaven.core.investigation.cause_assurance import fix_windows as fw\n"
        "def summarise(case):\n    return fw(case)\n",
        _PROBE,
    )
    with pytest.raises(
        AssertionError,
        match=re.escape(f"{_PROBE}:1 <module>: fix_windows imported as fw"),
    ):
        _check_no_alias(sites)
