"""Every place that derives a case's outcome, reads an outcome field raw, or
enters a closure is declared, per function (#2615 stage S0, pins 3 and 5).

#2615 moves every reader of a case's outcome onto one record (``CaseOutcome``),
stage by stage. Until then nothing stopped one more reader from deciding the
outcome on its own. This census stops that: a new site, a moved one or a
changed count fails until it is declared here, and each stage of #2615 shrinks
the tables as it moves its readers.

Each site is keyed by ``(path, qualname of the enclosing function)``, the path
as the scan writes it (``rel.as_posix()``), with an exact count per name.
Module level is ``<module>``. A ``getattr`` by a constant name counts as the
attribute would: ``getattr(case, "closure_reason", None)`` is a field read, and
an entry reached that way is a reference that is not a call. Three tables:

* ``DERIVATION_SITES``: every Load reference to a function in ``DERIVATIONS``,
  the functions #2615's tables name as deciding an outcome. A call counts, and
  so does a bare reference, as a name or an attribute (``ca.fix_windows``,
  ``callback=fix_windows``); an import or the ``def`` does not.
  ``case.contracts`` re-exports ``classify_solution_outcome``, so the narration
  package's import contract cannot see a narrator's use of it; only this
  census does. Stage S4 removes the re-export.
* ``FIELD_READS``: every attribute Load of a field in ``FIELDS``:
  ``completed_milestones``, ``solution_verified``, a mitigation's ``verified``
  and ``closure_reason``. ``verified`` has no receiver heuristic: all 14 reads
  today are ``MitigationRecord.verified``, and a future reader of another
  ``verified`` gets a row with its reason.
* ``ENTRY_CALLERS`` (pin 5): every reference to a closure entry in
  ``ENTRIES``, the transitions' executors and the offer record's writers. Each
  row's note says what that caller answers or closes. Whether it must reach
  ``closure_outcome`` is stage S1's to declare. An entry referenced other than
  by a call fails, so every way in stays visible here.

Not counted:

* ``problem_status``, ``cause_state`` and ``mitigation.disposition``: progress
  inputs the record reads from (#2615, *The record*). Pin 2's poisoned-case
  test (stage S2) is #2615's answer for raw reads by a narrator;
* a read by a computed name (``getattr(case, name)``), or by a key: a
  subscript, or a milestone map or turn record keyed ``"solution_verified"``
  or ``"mitigation_verified"``;
* readers in the clients.
"""

from __future__ import annotations

import ast
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import pytest

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
            "_no_confirming_window",
            "latest_fix_window",
            "latest_decided_fix_window",
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
            "should_generate_terminal_summary",
            "terminal_summary_skip_reason",
        ),
        _TT,
    ),
    "failed_fix_of": _DISCONF,
    "classify_solution_outcome": _CASE + "domain/models/solution.py",
}

#: Each outcome field read raw, by the module and class that define it.
FIELDS: dict[str, tuple[str, str]] = {
    "completed_milestones": (_PROGRESS, "InvestigationProgress"),
    "solution_verified": (_PROGRESS, "InvestigationProgress"),
    "verified": (_PROGRESS, "MitigationRecord"),
    "closure_reason": (_CASE + "domain/models/case.py", "Case"),
}

#: Each closure entry (#2615, *Closure on demand*), by the module that defines it.
ENTRIES: dict[str, str] = {
    "_execute_closed_transition": _TT,
    "_execute_resolved_transition": _TT,
    "open_disposition_offer": _TT,
    "disposition_refusal": _TT,
    "answer_offer": _OFFERS,
    "lapse_offers": _OFFERS,
}

#: (path, qualname) -> {derivation: references}.
DERIVATION_SITES: dict[tuple[str, str], dict[str, int]] = {
    # The derivations' own modules: a derivation built from others.
    (_DISCONF, "_disconfirmed_cause_trigger"): {"failed_fix_of": 1},
    (_DISCONF, "_fix_failure_refusal"): {"fix_windows": 1},
    (_DISCONF, "failed_fix_of"): {"latest_decided_fix_window": 1},
    (_DISCONF, "m6_disconfirmation_basis"): {"latest_decided_fix_window": 1},
    (_CA, "_cause_pool"): {"_pool_legs": 1},
    (_CA, "attempt_window"): {"fix_windows": 1},
    (_CA, "cause_elimination_rows"): {"cause_recheck_status": 1},
    (_CA, "cause_recheck_status"): {
        "_no_confirming_window": 1,
        "confirming_window": 1,
        "fix_windows": 1,
        "uncited_recheck_why": 1,
    },
    (_CA, "confirming_polarity_rows"): {"fix_windows": 1},
    (_CA, "confirming_window"): {"fix_windows": 1},
    (_CA, "fix_windows"): {"_pool_legs": 1},
    (_CA, "has_actionable_solution"): {"classify_solution_outcome": 1},
    (_CA, "has_resolution_confirmation"): {"resolution_confirmation_rows": 1},
    (_CA, "latest_decided_fix_window"): {"fix_windows": 1},
    (_CA, "latest_fix_window"): {"fix_windows": 1},
    (_CA, "off_cause_attempt"): {"_cause_pool": 1, "fix_windows": 1},
    (_CA, "problem_leg_anchor"): {"confirming_window": 1},
    (_CA, "subject_index"): {"_cause_pool": 1},
    (_TT, "_suggest_resolve_message"): {"resolution_record_lines": 1},
    (_TT, "assess_close_summary"): {"shown_milestones": 1},
    (_TT, "assess_closure_readiness"): {"assess_close_summary": 1},
    (_TT, "assess_resolution_readiness"): {"latest_fix_window": 1},
    (_TT, "closure_verdict"): {"assess_closure_readiness": 1},
    (_TT, "confirm_open_offer"): {
        "assess_closure_readiness": 1,
        "derive_closure_reason": 1,
    },
    (_TT, "declined_resolve_entry"): {"closure_verdict": 1},
    (_TT, "derive_closure_reason"): {
        "_mitigation_declared_stable": 1,
        "_mitigation_verified": 1,
    },
    (_TT, "derive_disposition_eligibility"): {
        "assess_resolution_readiness": 1,
        "closure_verdict": 1,
    },
    (_TT, "disposition_refusal"): {"derive_closure_reason": 1},
    (_TT, "execute_user_closure"): {
        "assess_closure_readiness": 1,
        "closure_verdict": 1,
        "derive_closure_reason": 1,
    },
    (_TT, "mitigation_closure_line"): {"mitigation_observation": 1},
    (_TT, "open_disposition_offer"): {"derive_closure_reason": 1},
    (_TT, "resolution_record_lines"): {"resolution_confirmation_rows": 1},
    (_TT, "shown_milestones"): {"_mitigation_verified": 1},
    (_TT, "terminal_summary_skip_reason"): {"should_generate_terminal_summary": 1},
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
    (_SG, "_date_to_the_report"): {"fix_windows": 2},
    (_SG, "_flag_absence_inside_a_failed_fix_window"): {"latest_fix_window": 1},
    (_SG, "_flag_unbound_post_fix_rows"): {"fix_windows": 1, "uncited_recheck_why": 1},
    (_SG, "fix_cause"): {"_cause_pool": 1, "_pool_legs": 1},
    (_SG, "licence_leg_ids"): {"_cause_pool": 1},
    (_SG, "licence_subject"): {"_pool_legs": 1},
    (_SG, "standing_fix_action"): {"fix_windows": 1},
    (_PROPOSALS, "_maybe_propose_confirmed_resolution"): {
        "assess_resolution_readiness": 1,
        "resolution_record_lines": 1,
    },
    (_PROPOSALS, "_maybe_propose_deferred_close"): {
        "assess_closure_readiness": 1,
        "resolution_record_lines": 1,
    },
    (_ME + "terminal_replies.py", "_build_resolution_confirmation"): {
        "resolution_record_lines": 1
    },
    (_ME + "terminal_replies.py", "_closed_suggestions"): {
        "should_generate_terminal_summary": 1
    },
    (_ME + "terminal_replies.py", "_terminal_confirmation_response"): {
        "mitigation_closure_line": 1
    },
    (_TERMINAL_TURNS, "TerminalTurnHandler._handle_report_regeneration"): {
        "should_generate_terminal_summary": 1,
        "terminal_summary_skip_reason": 1,
    },
    (_TERMINAL_TURNS, "TerminalTurnHandler.auto_generate_report"): {
        "should_generate_terminal_summary": 1,
        "terminal_summary_skip_reason": 1,
    },
    (_TRANSITION_TURNS, "_close_on_explicit_intent"): {
        "assess_close_summary": 1,
        "assess_closure_readiness": 1,
    },
    (_TRANSITION_TURNS, "_resolve_on_reopen_chip"): {"assess_resolution_readiness": 1},
    (_TRANSITIONS, "TransitionManager._apply_model_proposal"): {
        "assess_closure_readiness": 1,
        "assess_resolution_readiness": 1,
    },
    (_TRANSITIONS, "TransitionManager._recheck_resolve_request"): {
        "assess_resolution_readiness": 1
    },
    (_INV + "offer_projection.py", "OfferProjection.disposition_eligibility"): {
        "derive_disposition_eligibility": 1
    },
    (_OFFERS, "_confirmation_ids"): {"resolution_confirmation_rows": 1},
    (_PROBLEM, "invalidation_refusal"): {"fix_windows": 1},
    (_MONITOR, "ProgressMonitor._detect_fix_failure_cycle"): {"fix_windows": 1},
    (_MONITOR, "ProgressMonitor._repair_fix_failure_cycle"): {"fix_windows": 1},
    # The prompt.
    (_CB + "causal_graph_block.py", "fix_confirmed"): {"latest_decided_fix_window": 1},
    (_CB + "fix_verification_block.py", "_mitigation_lines"): {
        "mitigation_observation": 1
    },
    (_CB + "fix_verification_block.py", "build_fix_verification_block"): {
        "cause_recheck_status": 1,
        "fix_windows": 1,
    },
    # The API, the knowledge harvest and the report.
    (_UI, "_transform_resolved"): {"terminal_summary_skip_reason": 1},
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
    (_SG, "_mitigation_settled"): {"verified": 1},
    (_SG, "_register_disposition"): {"verified": 2},
    (_SG, "justification_skipped"): {"solution_verified": 1},
    (_ME + "terminal_replies.py", "_terminal_confirmation_response"): {
        "closure_reason": 1
    },
    (_OFFERS, "_premise_gone"): {"closure_reason": 1},
    (_OFFERS, "_scope"): {"closure_reason": 1},
    (_OFFERS, "answer_offer"): {"closure_reason": 2},
    (_OFFERS, "lapse_offers"): {"closure_reason": 3},
    (_PROBLEM, "_acted_on_bar"): {"solution_verified": 1, "verified": 1},
    (_PROBLEM, "is_false_alarm_close"): {"closure_reason": 1},
    (_MONITOR, "ProgressMonitor._detect_fix_failure_cycle"): {"verified": 1},
    (_MONITOR, "ProgressMonitor._find_pending_milestone"): {"completed_milestones": 1},
    (_CB + "assembly.py", "build_investigation_context"): {
        "solution_verified": 1,
        "verified": 1,
    },
    (_INV + "prompts/templates/fallback.py", "_fallback_body"): {
        "closure_reason": 1,
        "solution_verified": 1,
    },
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
    (_PROGRESS, "InvestigationProgress.current_stage"): {
        "solution_verified": 1,
        "verified": 1,
    },
    (_PROGRESS, "InvestigationProgress.resolution_complete"): {"solution_verified": 1},
    (_PROGRESS, "InvestigationProgress.solution_ordering"): {"solution_verified": 1},
    (_PROGRESS, "MitigationRecord._verified_requires_accepted"): {"verified": 2},
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
    (_TRANSITION_TURNS, "_close_on_explicit_intent"): (
        {"open_disposition_offer": 2},
        "opens the menu's close offer, or the resolve offer it pivots to "
        "(basis menu)",
    ),
    (_TRANSITION_TURNS, "_resolve_on_reopen_chip"): (
        {"open_disposition_offer": 1},
        "re-opens the declined resolve offer from the reopen chip (basis reopen)",
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
    (_ME + "turn_application.py", "_apply_turn_response"): (
        {"lapse_offers": 1},
        "lapses, before step 4, an open resolve or close offer whose premise "
        "has gone, and each decline whose premise moved",
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
        {"answer_offer": 1},
        "answers a standing statement-revision offer superseded; not a closure",
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
    (_TT, "execute_user_closure"): (
        {
            "_execute_closed_transition": 1,
            "answer_offer": 2,
            "open_disposition_offer": 1,
        },
        "the REST close: lapses an open offer, or declines the resolve offer "
        "(opening it first), then executes the close",
    ),
    (_CASE + "infrastructure/case_repository.py", "CaseRepository.prepare_for_save"): (
        {"lapse_offers": 1},
        "lapses, at every save, what lapse_offers lapses, so a write outside "
        "the engine lapses what it moved",
    ),
}


class Sites(NamedTuple):
    """What a walk found, each table keyed by ``(path, qualname)``."""

    derivations: dict[tuple[str, str], Counter]
    fields: dict[tuple[str, str], Counter]
    entries: dict[tuple[str, str], Counter]
    #: ``path:line qualname name`` for each entry referenced other than by a call.
    uncalled_entries: list[str]


def collect(tree: ast.AST, path: str) -> Sites:
    """The sites in ``tree``, the module at ``path``.

    A Load of a derivation or an entry as a name or an attribute, an attribute
    Load of a field, and a ``getattr`` of any of them by a constant name. An
    import, a ``def``, an assignment or a ``del`` is not a Load.
    """
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }

    def scope_of(node: ast.AST) -> str:
        names = []
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
        return ".".join(reversed(names)) or "<module>"

    sites = Sites({}, {}, {}, [])
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name, attribute = node.attr, True
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name, attribute = node.id, False
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            # ``getattr(x, "name")`` reads what ``x.name`` reads; it is never
            # a call of that name.
            name, attribute = node.args[1].value, True
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
                sites.uncalled_entries.append(f"{path}:{node.lineno} {key[1]} {name}")
    return sites


@lru_cache(maxsize=1)
def _scan() -> tuple[Sites, int]:
    sites = Sites({}, {}, {}, [])
    files = 0
    for path in sorted(_PACKAGE.rglob("*.py")):
        files += 1
        found = collect(parse_file(path), path.relative_to(_ROOT).as_posix())
        sites.derivations.update(found.derivations)
        sites.fields.update(found.fields)
        sites.entries.update(found.entries)
        sites.uncalled_entries.extend(found.uncalled_entries)
    return sites, files


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


def _with(table: str, source: str, path: str) -> dict:
    """The real scan's ``table`` with ``source``'s rows, as the module at
    ``path``, laid over it: a new function adds a row, a declared one's row is
    replaced. The comparison then sees what it would on that tree."""
    found = dict(getattr(_scan()[0], table))
    found.update(getattr(collect(parse_source(source, _ROOT / path), path), table))
    return found


_ENTRY_COUNTS = {key: counts for key, (counts, _) in ENTRY_CALLERS.items()}

_HELP = (
    "#2615 moves every reader of a case's outcome onto one record "
    "(CaseOutcome). A new reader reads the outcome from it. Until the stage "
    "that serves it lands, declare the site in {table} with a comment naming "
    "the #2615 stage that moves it; a changed count is updated only after "
    "reading the new reference."
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
    watched = set(DERIVATIONS) | set(FIELDS) | set(ENTRIES)
    assert watched <= found, f"never matched: {sorted(watched - found)}"


def test_every_derivation_site_is_declared():
    problems = _differences(_scan()[0].derivations, DERIVATION_SITES)
    assert not problems, (
        "a case's outcome is derived where the census does not say:\n  "
        + "\n  ".join(problems)
        + "\n"
        + _HELP.format(table="DERIVATION_SITES")
    )


def test_every_raw_field_read_is_declared():
    problems = _differences(_scan()[0].fields, FIELD_READS)
    assert not problems, (
        "an outcome field is read raw where the census does not say:\n  "
        + "\n  ".join(problems)
        + "\n"
        + _HELP.format(table="FIELD_READS")
    )


def test_every_closure_entry_caller_is_declared():
    problems = _differences(_scan()[0].entries, _ENTRY_COUNTS)
    assert not problems, (
        "a closure entry is called where the enumeration does not say:\n  "
        + "\n  ".join(problems)
        + "\nDeclare the caller in ENTRY_CALLERS with what it answers or closes "
        "(#2615 pin 5); stage S1 declares which must reach closure_outcome."
    )


def test_a_closure_entry_is_only_called():
    uncalled = _scan()[0].uncalled_entries
    assert not uncalled, (
        "a closure entry is referenced other than by a call, so its callers "
        "are out of the enumeration's sight:\n  " + "\n  ".join(uncalled)
    )


def _module_functions(path: str) -> set[str]:
    return {
        node.name
        for node in parse_file(_ROOT / path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


@pytest.mark.parametrize("name", sorted({**DERIVATIONS, **ENTRIES}))
def test_each_watched_function_is_defined_where_the_census_says(name):
    """A rename would otherwise leave the census watching a name nothing has."""
    path = {**DERIVATIONS, **ENTRIES}[name]
    assert name in _module_functions(path), f"{name} is not defined in {path}"


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
        ],
    )
    def test_an_import_a_def_a_write_or_a_string_is_not_a_site(self, source):
        assert self._count(source) == 0, source

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
        assert sites.uncalled_entries == ["m.py:2 f answer_offer"]

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
        "derivations",
        "def summarise(case):\n    return fix_windows(case)\n"
        "def wire(run):\n    return run(callback=fix_windows)\n",
        _PROBE,
    )
    assert _differences(found, DERIVATION_SITES) == [
        f"{_PROBE}::summarise: declared nothing, found {{'fix_windows': 1}}",
        f"{_PROBE}::wire: declared nothing, found {{'fix_windows': 1}}",
    ]


def test_a_changed_count_fails_the_census():
    """One more call in a declared function."""
    found = _with(
        "derivations",
        "def derive_closure_reason(case):\n"
        "    return _mitigation_verified(case) and _mitigation_verified(case)"
        " or _mitigation_declared_stable(case)\n",
        _TT,
    )
    assert _differences(found, DERIVATION_SITES) == [
        f"{_TT}::derive_closure_reason: declared "
        "{'_mitigation_declared_stable': 1, '_mitigation_verified': 1}, found "
        "{'_mitigation_declared_stable': 1, '_mitigation_verified': 2}"
    ]


def test_a_new_raw_field_read_fails_the_census():
    found = _with(
        "fields",
        "def label(case):\n"
        "    return case.progress.completed_milestones, case.closure_reason\n",
        _PROBE,
    )
    assert _differences(found, FIELD_READS) == [
        f"{_PROBE}::label: declared nothing, found "
        "{'closure_reason': 1, 'completed_milestones': 1}"
    ]


def test_a_new_entry_caller_fails_the_enumeration():
    found = _with(
        "entries",
        "def offer(case, kind, basis):\n"
        "    return open_disposition_offer(case, kind=kind, basis=basis)\n",
        _PROBE,
    )
    assert _differences(found, _ENTRY_COUNTS) == [
        f"{_PROBE}::offer: declared nothing, found {{'open_disposition_offer': 1}}"
    ]


def test_an_entry_referenced_other_than_by_a_call_is_reported():
    source = "HANDLERS = {'close': _execute_closed_transition}\n"
    sites = collect(parse_source(source, _ROOT / _PROBE), _PROBE)
    assert sites.uncalled_entries == [f"{_PROBE}:1 <module> _execute_closed_transition"]
