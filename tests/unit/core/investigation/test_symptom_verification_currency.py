"""The symptom's observation window must survive as a tracked fact.

The prompt has always said the symptom timeline "becomes the anchor for all
Zone 2 searches — every evidence request in Zone 2 references this window", and
in the same breath called it "an extracted fact, not a tracked variable". So it
lived in prose for one turn and vanished, and evidence requests defaulted to
the present: an investigation of a symptom observed two hours ago asks for
`--since=30m`, inspects a period the problem was never in, finds nothing.

This is a reading about WHERE TO LOOK. A problem is investigable while it
EXISTS — evidence collectible, cause unidentified, solution unknown —
regardless of whether it is firing right now.
"""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.core.investigation.symptom_currency import (
    STALE_AFTER,
    SymptomCurrency,
    assess_symptom_currency,
    newest_symptom_observation,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    ProblemVerification,
    TemporalState,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 4, 19, 36, 17, tzinfo=timezone.utc)


def _case(*, verified=True, temporal=TemporalState.ONGOING, evidence=()) -> Case:
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="checkout 500s",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout 500s",
            severity=CaseSeverity.HIGH,
            temporal_state=temporal,
        ),
    )
    case.progress.symptom_verified = verified
    case.evidence = list(evidence)
    return case


def _ev(category, observed, source="iso8601") -> Evidence:
    return Evidence(
        summary="s",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        primary_purpose="p",
        collected_by="u",
        collected_at_turn=1,
        coverage_start_ts=observed,
        coverage_end_ts=observed,
        # Currency counts VOUCHED provenance only, so a span here needs one to
        # date anything. These fixtures are about the currency RULES; the
        # provenance rules have their own tests below.
        coverage_source=source,
    )


def _symptom(observed):
    return _ev(EvidenceCategory.SYMPTOM_EVIDENCE, observed)


# -- the defect ---------------------------------------------------------------
def test_old_symptom_observation_on_an_ongoing_problem_is_stale():
    """The failure this exists for, in its general form: the problem was shown
    to have EXISTED; nothing establishes it still does."""

    case = _case(evidence=[_symptom(NOW - timedelta(hours=2))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


def test_recent_symptom_observation_is_current():
    case = _case(evidence=[_symptom(NOW - timedelta(minutes=5))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.CURRENT


def test_the_newest_observation_decides_not_the_oldest():
    """A stale row plus a fresh re-check reads CURRENT — re-verification is
    exactly the action the stale reading asks for, so it has to clear it."""

    case = _case(
        evidence=[
            _symptom(NOW - timedelta(hours=6)),
            _symptom(NOW - timedelta(minutes=2)),
        ]
    )
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.CURRENT


def test_boundary_is_not_stale_until_past_the_window():
    case = _case(evidence=[_symptom(NOW - STALE_AFTER)])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.CURRENT
    case = _case(evidence=[_symptom(NOW - STALE_AFTER - timedelta(seconds=1))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


# -- scoping: keyed on being under investigation ------------------------------
def test_a_historical_incident_still_needs_its_window():
    """An inactive incident is investigable while it EXISTS, and it needs the
    anchor MORE than a live one — there is no current state to fall back on."""

    case = _case(
        temporal=TemporalState.HISTORICAL,
        evidence=[_symptom(NOW - timedelta(days=30))],
    )
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


def test_unset_temporal_state_does_not_drop_the_anchor():
    """``temporal_state`` is populated only when the LLM happened to emit
    preliminary_urgency during INQUIRY — unrelated to whether a window exists."""

    case = _case(temporal=None, evidence=[_symptom(NOW - timedelta(days=1))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


def test_a_case_not_under_investigation_has_no_window_to_anchor():
    case = _case(evidence=[_symptom(NOW - timedelta(days=1))])
    case.state = CaseState.INQUIRY
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.NOT_APPLICABLE


def test_unverified_symptom_has_no_currency_question():
    case = _case(verified=False, evidence=[_symptom(NOW - timedelta(hours=9))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.NOT_APPLICABLE


# -- unknown is not an assurance ---------------------------------------------
def test_undated_evidence_is_distinct_from_current():
    """Most content has no parseable timestamps. That is the absence of an
    answer, and must never collapse into "recent"."""

    case = _case(evidence=[_symptom(None)])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.UNDATED


def test_no_symptom_evidence_at_all_is_undated_not_current():
    case = _case(evidence=[])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.UNDATED


# -- absence rows must not invert the reading ---------------------------------
def test_absence_evidence_is_not_read_as_a_symptom_observation():
    """A symptom-absence row evidences the problem is GONE. Counting it would
    make the strongest proof the problem stopped register as proof it is
    present — the exact inversion."""

    case = _case(
        evidence=[
            _symptom(NOW - timedelta(hours=4)),
            _ev(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, NOW),
        ]
    )
    assert newest_symptom_observation(case) == NOW - timedelta(hours=4)
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


def test_causal_evidence_does_not_establish_symptom_currency():
    """Causal evidence explains WHY, not that the problem is still occurring."""

    case = _case(
        evidence=[
            _symptom(NOW - timedelta(hours=3)),
            _ev(EvidenceCategory.CAUSAL_EVIDENCE, NOW),
        ]
    )
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


def test_naive_timestamps_do_not_raise():
    """SQLite round-trips coverage columns without tzinfo; a mixed set must not
    blow up prompt assembly."""

    case = _case(evidence=[_symptom(datetime(2026, 8, 4, 12, 0, 0))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


# -- what the model actually sees --------------------------------------------
def test_progress_indicator_no_longer_reports_a_bare_flag_when_stale():
    """The named defect: the indicator stated a conclusion while withholding
    everything needed to weigh it, so it read as settled fact."""

    from faultmaven.core.investigation.prompts.context_builder import (
        _symptom_currency_note,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(hours=2))])
    note = _symptom_currency_note(case, "symptom_verified")
    assert "investigation window" in note
    assert "--since=30m" in note  # names the concrete failure mode
    assert "not read a clean current-state reading as counter-evidence" in note


def test_undated_is_reported_as_unknown_not_as_recent():
    from faultmaven.core.investigation.prompts.context_builder import (
        _symptom_currency_note,
    )

    note = _symptom_currency_note(_case(evidence=[_symptom(None)]), "symptom_verified")
    assert "UNKNOWN" in note
    assert "not confirmed recent" in note


def test_other_indicators_are_untouched():
    from faultmaven.core.investigation.prompts.context_builder import (
        _symptom_currency_note,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(hours=2))])
    assert _symptom_currency_note(case, "root_cause_identified") == ""
    assert _symptom_currency_note(case, "solution_proposed") == ""


def test_inquiry_case_gets_no_note_at_all():
    from faultmaven.core.investigation.prompts.context_builder import (
        _symptom_currency_note,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(days=30))])
    case.state = CaseState.INQUIRY
    assert _symptom_currency_note(case, "symptom_verified") == ""


def test_zone2_emphasis_anchors_the_window_without_calling_the_case_dead():
    """Zone 2 asserted "Symptoms are confirmed" flatly on every remaining turn,
    with nothing to say about WHERE to look. It must now name the window — and
    must not imply a non-firing problem is not worth investigating."""

    from faultmaven.core.investigation.prompts.templates import (
        _get_diagnosis_focus_emphasis,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(hours=2))])
    text = _get_diagnosis_focus_emphasis(case.progress, case)
    assert "anchor to the symptom's window" in text.lower()
    assert "ABSOLUTE timestamps" in text
    assert "not counter-evidence" in text
    assert "while it EXISTS" in text
    assert "do not retract on it" in text


def test_zone2_emphasis_is_unchanged_when_current():
    from faultmaven.core.investigation.prompts.templates import (
        _get_diagnosis_focus_emphasis,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(minutes=1))])
    text = _get_diagnosis_focus_emphasis(case.progress, case)
    assert "Symptoms are confirmed" in text


def test_progress_only_callers_keep_working():
    """The optional ``case`` parameter must not change existing call sites."""

    from faultmaven.core.investigation.prompts.templates import (
        _get_diagnosis_focus_emphasis,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(hours=9))])
    assert "Symptoms are confirmed" in _get_diagnosis_focus_emphasis(case.progress)


# -- closure reasons: every close must say WHY, never merely when -------------
class TestClosureReasonsAreReasons:
    """`closed_after_investigation` named the state a case closed FROM, and hid
    three unrelated situations behind one label: a successful mitigation, a
    structurally unreachable cause, and a plain failure to establish anything.
    """

    @staticmethod
    def _derive(case):
        from faultmaven.core.investigation.terminal_transitions import (
            derive_closure_reason,
        )

        return derive_closure_reason(case)

    @staticmethod
    def _mitigated(case):
        from faultmaven.modules.case.contracts import MitigationRecord

        case.progress.mitigation = MitigationRecord(
            proposed_at_turn=1, accepted=True, verified=True, completed_at_turn=2
        )
        return case

    @staticmethod
    def _with_cause(case):
        """A documented fix presupposes an established cause — the license
        `_maybe_propose_deferred_close` requires via `_solution_cause_validated`."""
        from faultmaven.modules.case.contracts import (
            ConfidenceLevel,
            RootCauseConclusion,
        )

        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="connection pool exhausted",
            mechanism="m",
            evidence_basis=[],
            likelihood=0.8,
            confidence_level=ConfidenceLevel.from_score(0.8),
        )
        return case

    @staticmethod
    def _infeasible(case, rationale="Black-box 3rd-party API, no internal telemetry"):
        case.problem_verification.rca_infeasible = True
        case.problem_verification.rca_infeasible_rationale = rationale
        return case

    def test_nothing_established_is_insufficient_evidence(self):
        """The case the old derivation could not express: a problem entered
        investigation and could not be verified. `closed_insufficient_evidence`
        sat behind the INSUFFICIENT_EVIDENCE cell, which needs work_gate_passed
        (>=2 hypotheses across >=2 categories) — CAUSE work a case stuck at
        symptom verification never does — so it fell to the generic bucket."""

        assert self._derive(_case(verified=False)) == "closed_insufficient_evidence"

    def test_verified_symptom_but_no_grounded_cause_is_also_insufficient_evidence(self):
        """Same reason, later gap. Insufficient evidence covers the shortfall
        wherever it sits."""

        assert self._derive(_case(verified=True)) == "closed_insufficient_evidence"

    def test_verified_mitigation_is_not_a_failure(self):
        """The one closure in the set that is not a failure of any kind: the
        symptom is relieved and RCA was deferred BY CHOICE."""

        assert self._derive(self._mitigated(_case())) == "mitigation_sufficient"

    def test_structural_infeasibility_outranks_the_mitigation(self):
        """Not hypothetical: the engine's only path to an rca-infeasible close
        fires on mitigation_verified, so both always hold. The label that also
        explains why RCA stopped must win, or it is shadowed every time."""

        case = self._infeasible(self._mitigated(_case()))
        assert self._derive(case) == "closed_rca_infeasible"

    def test_infeasibility_without_a_rationale_is_not_honored(self):
        """A self-declared boolean that forecloses future work has to carry its
        own justification. Unjustified, it must fall through rather than tell a
        future reader the cause cannot be found."""

        case = self._infeasible(self._mitigated(_case()), rationale=None)
        assert self._derive(case) == "mitigation_sufficient"

    def test_blank_rationale_is_not_a_rationale(self):
        case = self._infeasible(_case(verified=False), rationale="   ")
        assert self._derive(case) == "closed_insufficient_evidence"

    def test_inquiry_still_outranks_everything(self):
        case = self._infeasible(self._mitigated(_case()))
        case.state = CaseState.INQUIRY
        assert self._derive(case) == "inquiry_only"

    def test_every_derivable_reason_passes_the_case_validator(self):
        from faultmaven.modules.case.domain.models import VALID_CLOSURE_REASONS

        for reason in (
            "inquiry_only",
            "closed_rca_infeasible",
            "mitigation_sufficient",
            "closed_insufficient_evidence",
        ):
            assert reason in VALID_CLOSURE_REASONS

    def test_the_retired_reason_is_gone_entirely(self):
        """No legacy, no back-compat: the system is still under development, so
        an obsolete value is removed rather than carried."""

        from faultmaven.modules.case.domain.models import VALID_CLOSURE_REASONS

        assert "closed_after_investigation" not in VALID_CLOSURE_REASONS

    def test_a_documented_but_deferred_fix_is_not_an_evidence_failure(self):
        """The fourth situation the generic bucket was hiding: the cause IS
        identified and the fix IS documented — implementation just happens
        out-of-band. Nothing was missing."""

        from faultmaven.modules.case.contracts import SolutionFeasible

        case = self._with_cause(_case())
        case.progress.solution_feasible = SolutionFeasible.DEFERRED
        case.progress.solution_proposed = True
        assert self._derive(case) == "solution_deferred"

    def test_deferred_without_a_fix_on_record_describes_no_outcome(self):
        """`solution_feasible` can be set before any fix exists; "deferred" with
        nothing deferred is not an outcome."""

        from faultmaven.modules.case.contracts import SolutionFeasible

        case = _case()
        case.progress.solution_feasible = SolutionFeasible.DEFERRED
        case.progress.solution_proposed = False
        assert self._derive(case) == "closed_insufficient_evidence"

    def test_a_documented_fix_outranks_a_mitigation(self):
        """Both can hold — an interim workaround plus a known permanent fix.
        The label carrying the most established knowledge wins."""

        from faultmaven.modules.case.contracts import SolutionFeasible

        case = self._with_cause(self._mitigated(_case()))
        case.progress.solution_feasible = SolutionFeasible.DEFERRED
        case.progress.solution_proposed = True
        assert self._derive(case) == "solution_deferred"


# -- review findings ----------------------------------------------------------
class TestEvidenceCoverageResolution:
    """`_evidence_coverage` inherited the whole FILE's span, and the docstring's
    justification was inverted: it claimed widening "can only make evidence look
    OLDER", but `coverage_end_ts` is what the staleness read consults, and
    widening moves the END LATER. A dump spanning 12:00-19:45 containing a 17:36
    symptom reported "last observed 19:45" and read CURRENT — masking exactly the
    staleness this machinery exists to surface.
    """

    @staticmethod
    def _case_with_file(start, end):
        from faultmaven.modules.case.contracts import UploadedFile

        case = _case()
        f = UploadedFile(
            file_id="file_0123456789ab",
            filename="app.log",
            size_bytes=10,
            uploaded_at_turn=1,
        )
        f.coverage_start_ts = start
        f.coverage_end_ts = end
        case.uploaded_files = [f]
        return case, f.file_id

    def test_a_ranged_file_is_not_inherited(self):
        """The reported bug. Unknown is honest; a fabricated recent end is not."""
        from faultmaven.core.investigation.milestone_engine import _evidence_coverage

        case, fid = self._case_with_file(
            datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 4, 19, 45, tzinfo=timezone.utc),
        )
        assert _evidence_coverage(case, fid)[:2] == (None, None)

    def test_a_point_in_time_file_is_inherited(self):
        """The motivating path: an alert stamped from a forwarding caller's
        observed_at describes ONE moment, so the slice can only be that moment."""
        from faultmaven.core.investigation.milestone_engine import _evidence_coverage

        instant = datetime(2026, 8, 4, 17, 36, 17, tzinfo=timezone.utc)
        case, fid = self._case_with_file(instant, instant)
        assert _evidence_coverage(case, fid)[:2] == (instant, instant)

    def test_the_extract_beats_the_file(self):
        """An evidence row is a SLICE; its own quoted lines are the authority."""
        from faultmaven.core.investigation.milestone_engine import _evidence_coverage

        case, fid = self._case_with_file(
            datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 4, 19, 45, tzinfo=timezone.utc),
        )
        extract = "2026-08-04 17:36:17 ERROR etcdserver: no leader\n"
        start, end, _ = _evidence_coverage(case, fid, extract)
        assert end is not None and end.hour == 17
        # ...and crucially NOT the file's 19:45 end.
        assert end.hour != 19


class TestFixDocumentedNotApplied:
    """Two findings at once: the predicate omitted the established-cause license
    that `_maybe_propose_deferred_close` requires, and a FEASIBLE (not deferred)
    documented fix closed without executing fell to insufficient_evidence."""

    @staticmethod
    def _derive(case):
        from faultmaven.core.investigation.terminal_transitions import (
            derive_closure_reason,
        )

        return derive_closure_reason(case)

    @staticmethod
    def _with_cause(case):
        from faultmaven.modules.case.contracts import (
            ConfidenceLevel,
            RootCauseConclusion,
        )

        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="connection pool exhausted",
            mechanism="m",
            evidence_basis=[],
            likelihood=0.8,
            confidence_level=ConfidenceLevel.from_score(0.8),
        )
        return case

    def test_a_stale_deferred_flag_without_a_cause_is_not_solution_deferred(self):
        """Solution records are monotone and never withdrawn, so the flag
        outlives the cause it describes. Reporting "cause identified and fix
        documented" for a case with no cause would be a fabricated claim."""
        from faultmaven.modules.case.contracts import SolutionFeasible

        case = _case()
        case.progress.solution_feasible = SolutionFeasible.DEFERRED
        case.progress.solution_proposed = True
        assert self._derive(case) == "closed_insufficient_evidence"

    def test_a_deferred_fix_with_an_established_cause_is_solution_deferred(self):
        from faultmaven.modules.case.contracts import SolutionFeasible

        case = self._with_cause(_case())
        case.progress.solution_feasible = SolutionFeasible.DEFERRED
        case.progress.solution_proposed = True
        assert self._derive(case) == "solution_deferred"

    def test_a_feasible_fix_closed_without_executing_is_also_solution_deferred(self):
        """Same position: cause found, fix written down, nobody ran it. Whether
        the user formally declared it deferred is bookkeeping."""
        from faultmaven.modules.case.contracts import SolutionFeasible

        case = self._with_cause(_case())
        case.progress.solution_feasible = SolutionFeasible.NOW
        case.progress.solution_proposed = True
        assert self._derive(case) == "solution_deferred"


class TestReviewFindingsResidual:
    """The remaining findings: a fabricated closure claim, a stale prompt zone,
    a misleading log, and a classifier input that was widened by accident."""

    @staticmethod
    def _boundary(case, hypotheses=()):
        from faultmaven.modules.report.domain.services.report_generation_service import (
            ReportGenerationService,
        )

        svc = ReportGenerationService.__new__(ReportGenerationService)
        return "".join(
            svc._insufficient_evidence_boundary_block(case, list(hypotheses))
        )

    def test_closure_summary_does_not_claim_an_elimination_pass_that_never_ran(self):
        """`closed_insufficient_evidence` is now the DEFAULT, not a cell behind
        work_gate_passed (>=2 hypotheses). The block's "did enough work to rule
        things out" was licensed by that gate, so a turn-2 close with zero
        hypotheses would otherwise carry a fabricated claim."""

        text = self._boundary(_case(verified=False))
        assert "rule things out" not in text
        assert "never established from the data" in text

    def test_a_verified_symptom_with_no_hypotheses_says_so_plainly(self):
        text = self._boundary(_case(verified=True))
        assert "rule things out" not in text
        assert "candidate causes were put forward" in text

    def test_real_diagnostic_work_keeps_the_original_claim(self):
        """When the work DID happen the statement is true and must survive."""

        from faultmaven.modules.case.contracts import Hypothesis

        case = _case(verified=True)
        hyps = [
            Hypothesis(
                statement="pool exhausted",
                category="config",
                generated_at_turn=2,
                generation_mode="opportunistic",
                rationale="r",
            ),
            Hypothesis(
                statement="dns flap",
                category="network",
                generated_at_turn=2,
                generation_mode="opportunistic",
                rationale="r",
            ),
        ]
        case.hypotheses = {h.hypothesis_id: h for h in hyps}
        assert "rule things out" in self._boundary(case, hyps)

    def test_zone1_no_longer_withholds_verification_for_past_evidence(self):
        """The pre-revert framing told Zone 1 to ask for a current check rather
        than treat the question as settled. Every currency surface is gated on
        symptom_verified, so suppressing it there kept them all dormant."""

        from faultmaven.core.investigation.prompts.templates import (
            _get_diagnosis_focus_emphasis,
        )

        case = _case(verified=False)
        text = _get_diagnosis_focus_emphasis(case.progress, case)
        assert "DOES verify it" in text
        assert "while it EXISTS" in text
        assert "absolute timestamps" in text
        # The repudiated instruction must be gone.
        assert "rather than treating the question as\nsettled" not in text

    def test_an_undeclared_rationale_stores_mitigation_sufficient(self):
        """Finding 8's substance: the rca-infeasible proposal logged
        "closed_rca_infeasible" while using the GENERIC fallback rationale, which
        derive_closure_reason's guard rejects — so the stored reason was
        mitigation_sufficient and the log disagreed with the record. The log now
        derives the same way; this pins the behaviour it must report."""

        from faultmaven.core.investigation.terminal_transitions import (
            derive_closure_reason,
        )
        from faultmaven.modules.case.contracts import MitigationRecord

        case = _case()
        case.progress.mitigation = MitigationRecord(
            proposed_at_turn=1, accepted=True, verified=True, completed_at_turn=2
        )
        case.problem_verification.rca_infeasible = True
        case.problem_verification.rca_infeasible_rationale = None
        assert derive_closure_reason(case) == "mitigation_sufficient"

    def test_relay_provenance_does_not_reach_the_content_classifier(self):
        """`source_url` means the URL the CONTENT came from — the classifier
        consults it at Priority 3, ahead of the content rules. A relay link is a
        different thing: the Slack agent's permalink points at the message that
        forwarded an alert, not at where the alert's text came from. Recording it
        there would let a pasted excerpt be typed by whatever page someone copied
        it out of, so the field stays scoped to page captures."""

        from faultmaven.modules.case.api.routes import resolve_paste_source_meta

        meta, _ = resolve_paste_source_meta("paste", "https://slack/archives/C1/p1")
        assert "source_url" not in meta

        meta, _ = resolve_paste_source_meta(
            "page_capture", "https://grafana.example.com/d/abc"
        )
        assert meta["source_url"] == "https://grafana.example.com/d/abc"


# -- currency is stricter than the prompt, deliberately -----------------------
def test_an_inferred_year_does_not_establish_symptom_currency():
    """The prompt can render an inferred-year instant with `observed_basis` and
    let the model weigh it. Currency feeds a binary CURRENT/STALE call with
    nowhere to put a caveat, and a syslog year taken from the wall clock can be
    wrong by a whole year — the one error size that flips that call."""

    from faultmaven.core.investigation.symptom_currency import (
        newest_symptom_observation,
    )

    observed = datetime.now(timezone.utc) - timedelta(minutes=5)
    case = _case(
        evidence=[
            _ev(
                EvidenceCategory.SYMPTOM_EVIDENCE,
                observed,
                source="syslog_bsd_noyear",
            )
        ]
    )
    assert newest_symptom_observation(case) is None


def test_unrecorded_provenance_does_not_establish_symptom_currency():
    from faultmaven.core.investigation.symptom_currency import (
        newest_symptom_observation,
    )

    observed = datetime.now(timezone.utc) - timedelta(minutes=5)
    case = _case(
        evidence=[_ev(EvidenceCategory.SYMPTOM_EVIDENCE, observed, source=None)]
    )
    assert newest_symptom_observation(case) is None
