"""Behavior of the differential-intake evaluate bodies (matcher-owned).

The frozen seam SHAPE is pinned in ``test_differential_intake_contract.py``; this
covers what the bodies DO once filled: telemetry resolution (trusted digest, never
``Evidence.summary``), content-addressed dispatch, subset-trust evaluation, the
one-verdict-per-(datum, cause) aggregation, and provenance tagging.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from faultmaven.core.investigation.cause_schemas import CauseRecord
from faultmaven.core.investigation.differential_intake import (
    ActiveCause,
    _resolve_stance,
    assemble_active_causes,
    evaluate_datum_against_differential,
    recheck_proposed_predicate,
)
from faultmaven.core.investigation.runbook_cause_matcher import resolve_datum_text
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    EvidenceStance,
    InquiryData,
    ProblemVerification,
)
from faultmaven.modules.case.domain.models import UploadedFile
from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _digest(file_extract: str) -> str:
    """A real structural_index blob carrying ``file_extract`` as its digest."""
    return ExtractResult(file_extract=file_extract).to_json()


def _case_with_datum(file_extract, *, structural_index=None):
    """A case whose single datum's backing file has the given digest.

    ``file_extract=None`` and ``structural_index`` unset → the datum has no
    backing file at all. ``structural_index`` overrides the blob (to test the
    non-JSON tolerant path).
    """
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="Deploy fails",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="Deploy fails",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="Deploy fails",
            severity=CaseSeverity.HIGH,
        ),
    )
    file_id = f"file_{uuid4().hex[:12]}"
    if file_extract is not None or structural_index is not None:
        blob = (
            structural_index if structural_index is not None else _digest(file_extract)
        )
        case.uploaded_files.append(
            UploadedFile(
                file_id=file_id,
                filename="f.log",
                size_bytes=1,
                uploaded_at_turn=1,
                structural_index=blob,
            )
        )
        evidence = SimpleNamespace(source_file_id=file_id)
    else:
        evidence = SimpleNamespace(source_file_id=None)
    return case, evidence


def _candidate(letter, predicates):
    return ActiveCause(
        candidate_id=f"rb1:{letter}",
        record=CauseRecord(cause_letter=letter, match_predicates=predicates),
    )


# ---------------------------------------------------------------------------
# resolve_datum_text — the trust boundary
# ---------------------------------------------------------------------------


class TestResolveDatumText:
    def test_returns_file_extract_from_structural_index(self):
        case, ev = _case_with_datum("the crime scene: OOMKilled")
        assert resolve_datum_text(ev, case) == "the crime scene: OOMKilled"

    def test_none_when_no_backing_file(self):
        case, ev = _case_with_datum(None)
        assert resolve_datum_text(ev, case) is None

    def test_none_when_file_not_found(self):
        case, _ = _case_with_datum("x")
        ev = SimpleNamespace(source_file_id="file_doesnotexist")
        assert resolve_datum_text(ev, case) is None

    def test_tolerant_fallback_on_non_json_index(self):
        # A pre-schema / non-JSON index is treated as the extract text itself.
        case, ev = _case_with_datum(None, structural_index="raw non-json blob")
        assert resolve_datum_text(ev, case) == "raw non-json blob"

    def test_never_reads_evidence_summary(self):
        # The digest has no "secret"; even if an LLM summary did, it must not leak.
        case, ev = _case_with_datum("digest only")
        ev.summary = "secret OOMKilled claim"
        assert "secret" not in (resolve_datum_text(ev, case) or "")


# ---------------------------------------------------------------------------
# evaluate_datum_against_differential — content-addressed dispatch
# ---------------------------------------------------------------------------


class TestEvaluateDatum:
    def test_no_trusted_content_yields_no_verdicts(self):
        case, ev = _case_with_datum(None)
        cands = [_candidate("A", [{"predicate": "contains", "target": "x"}])]
        assert (
            evaluate_datum_against_differential(
                evidence=ev, active_causes=cands, case=case
            )
            == []
        )

    def test_match_emits_one_supports_with_runbook_provenance(self):
        case, ev = _case_with_datum("pod OOMKilled, restarting")
        cands = [_candidate("A", [{"predicate": "contains", "target": "OOMKilled"}])]
        verdicts = evaluate_datum_against_differential(
            evidence=ev, active_causes=cands, case=case
        )
        assert len(verdicts) == 1
        v = verdicts[0]
        assert v.cause_id == "rb1:A"
        assert v.stance == EvidenceStance.SUPPORTS
        assert v.provenance == "runbook"
        assert v.predicate == {"predicate": "contains", "target": "OOMKilled"}

    def test_present_target_refutes_an_absent_predicate(self):
        case, ev = _case_with_datum("ERROR: disk full")
        cands = [_candidate("A", [{"predicate": "absent", "target": "ERROR"}])]
        verdicts = evaluate_datum_against_differential(
            evidence=ev, active_causes=cands, case=case
        )
        assert [v.stance for v in verdicts] == [EvidenceStance.REFUTES]

    def test_contains_miss_against_digest_is_untested_not_refute(self):
        # Subset-trust: the substring may live outside the digest, so absence is
        # never a refutation — the candidate gets no verdict.
        case, ev = _case_with_datum("unrelated digest content")
        cands = [_candidate("A", [{"predicate": "contains", "target": "OOMKilled"}])]
        assert (
            evaluate_datum_against_differential(
                evidence=ev, active_causes=cands, case=case
            )
            == []
        )

    def test_conflicting_predicates_for_one_cause_abstain(self):
        # One predicate supports, another refutes the SAME cause on the SAME datum
        # → abstain (no verdict), honoring one-verdict-per-(datum, cause).
        case, ev = _case_with_datum("OOMKilled and ERROR both here")
        cands = [
            _candidate(
                "A",
                [
                    {"predicate": "contains", "target": "OOMKilled"},  # SUPPORTS
                    {"predicate": "absent", "target": "ERROR"},  # REFUTES (present)
                ],
            )
        ]
        assert (
            evaluate_datum_against_differential(
                evidence=ev, active_causes=cands, case=case
            )
            == []
        )

    def test_agreeing_predicates_yield_single_verdict(self):
        case, ev = _case_with_datum("OOMKilled, rc=137")
        cands = [
            _candidate(
                "A",
                [
                    {"predicate": "contains", "target": "OOMKilled"},
                    {"predicate": "exit_code", "target": 137},
                ],
            )
        ]
        verdicts = evaluate_datum_against_differential(
            evidence=ev, active_causes=cands, case=case
        )
        assert len(verdicts) == 1
        assert verdicts[0].stance == EvidenceStance.SUPPORTS

    def test_candidate_without_predicates_is_skipped(self):
        case, ev = _case_with_datum("anything")
        cands = [_candidate("A", [])]
        assert (
            evaluate_datum_against_differential(
                evidence=ev, active_causes=cands, case=case
            )
            == []
        )

    def test_dispatch_is_content_addressed_across_candidates(self):
        # One datum, two candidates: each judged independently against it.
        case, ev = _case_with_datum("OOMKilled here")
        cands = [
            _candidate("A", [{"predicate": "contains", "target": "OOMKilled"}]),
            _candidate("B", [{"predicate": "contains", "target": "ConnRefused"}]),
        ]
        verdicts = evaluate_datum_against_differential(
            evidence=ev, active_causes=cands, case=case
        )
        # A fires (present); B is untested (subset-trust miss) → only A.
        assert [v.cause_id for v in verdicts] == ["rb1:A"]

    def test_refutes_stance_predicate_eliminates_cause_on_present_token(self):
        # M-A / T2: a firing stance="refutes" predicate (a sibling's signature is
        # present) ELIMINATES the cause → REFUTES, not SUPPORTS.
        case, ev = _case_with_datum("cgroup medium: Memory (tmpfs) mount")
        cands = [
            _candidate(
                "A",
                [
                    {
                        "predicate": "contains",
                        "target": "medium: Memory",
                        "stance": "refutes",
                    }
                ],
            )
        ]
        verdicts = evaluate_datum_against_differential(
            evidence=ev, active_causes=cands, case=case
        )
        assert [v.stance for v in verdicts] == [EvidenceStance.REFUTES]

    def test_default_stance_still_supports_on_present_token(self):
        # A predicate with no stance behaves exactly as before (SUPPORTS on match).
        case, ev = _case_with_datum("pod OOMKilled")
        cands = [_candidate("A", [{"predicate": "contains", "target": "OOMKilled"}])]
        verdicts = evaluate_datum_against_differential(
            evidence=ev, active_causes=cands, case=case
        )
        assert [v.stance for v in verdicts] == [EvidenceStance.SUPPORTS]


class TestResolveStance:
    """M-A: stance-aware verdict→EvidenceStance mapping (default 'supports')."""

    def test_truth_table(self):
        supports = {"predicate": "contains", "target": "x"}  # default stance
        refutes = {"predicate": "contains", "target": "x", "stance": "refutes"}
        assert _resolve_stance("matched", supports) == EvidenceStance.SUPPORTS
        assert _resolve_stance("refuted", supports) == EvidenceStance.REFUTES
        assert _resolve_stance("matched", refutes) == EvidenceStance.REFUTES
        assert _resolve_stance("refuted", refutes) == EvidenceStance.SUPPORTS

    def test_untested_is_silent(self):
        assert _resolve_stance("untested", {"predicate": "contains"}) is None

    def test_unknown_stance_value_defaults_to_supports(self):
        p = {"predicate": "contains", "stance": "bogus"}
        assert _resolve_stance("matched", p) == EvidenceStance.SUPPORTS


# ---------------------------------------------------------------------------
# recheck_proposed_predicate — the LLM-fallback tier
# ---------------------------------------------------------------------------


class TestRecheckProposedPredicate:
    def test_match_emits_llm_fallback_provenance(self):
        case, ev = _case_with_datum("connection refused on :8090")
        v = recheck_proposed_predicate(
            evidence=ev,
            cause_id="rb1:A",
            proposed_predicate={
                "predicate": "contains",
                "target": "connection refused",
            },
            case=case,
        )
        assert v is not None
        assert v.provenance == "llm_fallback"
        assert v.stance == EvidenceStance.SUPPORTS
        assert v.cause_id == "rb1:A"

    def test_untested_against_digest_returns_none(self):
        case, ev = _case_with_datum("unrelated content")
        assert (
            recheck_proposed_predicate(
                evidence=ev,
                cause_id="rb1:A",
                proposed_predicate={"predicate": "contains", "target": "ghost"},
                case=case,
            )
            is None
        )

    def test_malformed_predicate_returns_none(self):
        case, ev = _case_with_datum("x")
        assert (
            recheck_proposed_predicate(
                evidence=ev,
                cause_id="rb1:A",
                proposed_predicate={"predicate": "unknown_op"},
                case=case,
            )
            is None
        )

    def test_no_content_returns_none(self):
        case, ev = _case_with_datum(None)
        assert (
            recheck_proposed_predicate(
                evidence=ev,
                cause_id="rb1:A",
                proposed_predicate={"predicate": "contains", "target": "x"},
                case=case,
            )
            is None
        )


# ---------------------------------------------------------------------------
# assemble_active_causes — the candidate differential
# ---------------------------------------------------------------------------


def _record(letter, *, fallback=False):
    return CauseRecord(cause_letter=letter, is_fallback_cause=fallback)


class TestAssembleActiveCauses:
    def test_mints_cross_runbook_unique_candidate_ids(self):
        matched = [("rbA", [_record("A"), _record("B")]), ("rbB", [_record("A")])]
        active = assemble_active_causes(matched)
        assert [a.candidate_id for a in active] == ["rbA:A", "rbA:B", "rbB:A"]

    def test_excludes_fallback_cause(self):
        matched = [("rbA", [_record("A"), _record("Z", fallback=True)])]
        active = assemble_active_causes(matched)
        assert [a.candidate_id for a in active] == ["rbA:A"]

    def test_dedupes_repeated_candidate_id_keeping_first(self):
        first, second = _record("A"), _record("A")
        active = assemble_active_causes([("rbA", [first, second])])
        assert [a.candidate_id for a in active] == ["rbA:A"]
        assert active[0].record is first

    def test_carries_the_full_record(self):
        rec = _record("A")
        active = assemble_active_causes([("rbA", [rec])])
        assert active[0].record is rec

    def test_empty_input_yields_empty_differential(self):
        assert assemble_active_causes([]) == []
