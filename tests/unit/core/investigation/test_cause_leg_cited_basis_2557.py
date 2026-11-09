"""The cause leg binds a post-fix re-check by citation (#2557, ruled
2026-11-06: strict citation).

After a lasting fix, a ``causal_absence`` row confirms the cause only when the
row it re-observes is one the identified root's terminal verdict cites
(``root_terminal_evidence_ids``, #2036 D6: the cited basis, the rows that show
why the root arose), by citation alone. A re-check of the root's consequence,
or of a relief's own result, holds equally when the fix was never applied, so
it confirms nothing — on the confirmation read and on a kinded window's
SUCCEEDED read alike. The window reads that FAIL a fix (M6's pair, window 0's
relapse) keep binding through the SUPPORTS links, so a relief-row re-check can
still fail the fix. The model is told which rows are the cited basis; the user
is asked for the fix's state at the root, with no row ids.

The fixture mirrors the stored case (``case_34c150b5cab6``): the chain
deepened twice (certificate expired → the AppRole secret_id expired → nothing
rotates it), an interception (a new secret_id) whose read-back is a row
SUPPORTS-linked to the final root, then the lasting fix (Kubernetes auth).
"""

import re

import pytest

from faultmaven.core.investigation.causal_graph.disconfirmation import failed_fix_of
from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_SUCCEEDED,
    SUBJECT_CAUSE,
    SUBJECT_UNBOUND,
    cause_recheck_status,
    fix_windows,
    remediations_at,
    subject_index,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _flag_unbound_post_fix_rows,
)
from faultmaven.core.investigation.prompts.context_builder.budget import TokenBudget
from faultmaven.core.investigation.prompts.context_builder.fix_verification_block import (
    build_fix_verification_block,
    cap_fix_verification_block,
)
from faultmaven.core.investigation.schemas import EvidenceToAdd
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    CauseState,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisState,
    InvestigationActionType,
    NeedPriority,
    NeedPurpose,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProposedAction,
    ValidationMethod,
)
from tests.unit.core.investigation.narrated import narrated
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _ROOT_ID,
    _UNNAMED,
    VERIFYING_ID,
    _assert_demoted,
    _eid,
    _engine_rows,
    _identified_case,
    _recompute_at,
    _row,
)
from tests.utils import terminal_verdict

pytestmark = pytest.mark.unit

_CAUSAL = EvidenceCategory.CAUSAL_EVIDENCE
_CAUSE_GONE = EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
_GONE = EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
_PRESENT = EvidenceCategory.SYMPTOM_EVIDENCE

_SECRET_ID = "cn_00000000255b"
_FINAL_ID = "cn_00000000255c"
_FINAL = "nothing rotates the fixed-life AppRole secret_id the issuer logs in with"
_RELIEF = "write a new secret_id to the vault-approle secret"
_FIX = "switch the issuer to Vault Kubernetes auth"
_UNCITED = "re-checks a row the root's verdict does not cite"
_NO_CITATION = "the root cause's terminal verdict cites no row yet"


def _root(node_id, statement, supports, *, validated, cites=()) -> CausalNode:
    """A ROOT on ``supports``; its terminal verdict cites ``cites`` (D6)."""
    return CausalNode(
        metadata=terminal_verdict(*(r.evidence_id for r in cites)) if cites else {},
        node_id=node_id,
        statement=statement,
        node_type=NodeType.ROOT,
        node_state=NodeState.VALIDATED if validated else NodeState.CANDIDATE,
        validation_method=(
            ValidationMethod.EMPIRICAL if validated else ValidationMethod.NONE
        ),
        belief=0.9 if validated else 0.5,
        actionable=True,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=r.evidence_id,
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on it",
                linked_at_turn=4,
            )
            for r in supports
        ],
        generated_at_turn=3,
    )


def _fix(case, turn, kind, targets, description) -> ProposedAction:
    """A dated attempt, kinded as the settle records it (#2036 D2.3)."""
    action = ProposedAction(
        case_id=case.case_id,
        action_type=InvestigationActionType.SOLUTION,
        description=description,
        proposed_in_turn=turn - 1,
        accepted_in_turn=turn,
        state="accepted",
        target_ids=list(targets),
        fix_kind=kind,
    )
    case.proposed_actions.append(action)
    case.progress.solution_accepted = True
    return action


def _mirror(*, cited=True):
    """The stored case's shape. Returns the case, the final root and the rows
    a re-check could name: the relief's read-back (SUPPORTS-linked to the
    secret_id root and the final root), the consequence (the Vault login
    failure, linked to both too) and the final root's own datum (the TTL,
    linked to it alone), which its verdict cites. ``cited=False`` leaves the
    final root with a verdict citing nothing, as a case identified before D6
    stores it, standing as the recorded cause."""
    case, cert, hyp = _identified_case()
    cert.statement = "the api-gateway client certificate expired"
    problem = next(
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    login = _row(
        "login_fail",
        _CAUSAL,
        4,
        "cert-manager: Vault login fails: invalid role or secret ID",
    )
    ttl = _row(
        "ttl", _CAUSAL, 4, "approle secret_id_ttl is 720h; no rotation job exists"
    )
    relief = _row(
        "issuer_ready",
        _CAUSAL,
        5,
        "ClusterIssuer/vault-pki Ready after the new secret_id was written",
    )
    case.evidence += [login, ttl, relief]
    secret = _root(
        _SECRET_ID, "the AppRole secret_id is expired", [login, relief], validated=True
    )
    final = _root(_FINAL_ID, _FINAL, [ttl, login, relief], validated=True, cites=[ttl])
    case.causal_nodes[secret.node_id] = secret
    case.causal_nodes[final.node_id] = final
    case.causal_edges = [
        CausalEdge(cause_node_id=final.node_id, effect_node_id=secret.node_id),
        CausalEdge(cause_node_id=secret.node_id, effect_node_id=cert.node_id),
        CausalEdge(cause_node_id=cert.node_id, effect_node_id=problem.node_id),
    ]
    hyp.root_node_id = final.node_id
    hyp.path = [final.node_id, secret.node_id, cert.node_id, problem.node_id]
    for old, new in ((cert, secret), (secret, final)):
        # As the attach writer records a re-root past a root (pruning).
        old.record_root_terminal(False, None, 4)
        old.metadata["deepened_to"] = new.node_id
    _recompute_at(case, 5)
    assert case.progress.cause_state == CauseState.IDENTIFIED, "premise"
    if not cited:
        final.metadata = {"root_terminal": True}
        case.progress.identified_cause_root_ids = [final.node_id]
    assert subject_index(case).pool_roots == {final.node_id}, "premise: the pool"
    return case, final, relief, login, ttl


def _fixed(case, final):
    """The interception at 5 (the relief), the lasting fix at 6, the problem
    seen gone at 7."""
    _fix(case, 5, "interception", [_SECRET_ID], _RELIEF)
    fix = _fix(case, 6, "remediation", [final.node_id], _FIX)
    case.evidence.append(
        _row("gone", _GONE, 7, "503s at 0.0/s; GET /v1/users/me returns 200")
    )
    return fix


def _cites_unlinked(case, final, ttl):
    """The final root's verdict also cites a row it does not SUPPORTS-link."""
    why = _row("why", _CAUSAL, 4, "the AppRole secret_id was issued once, by hand")
    case.evidence.append(why)
    final.metadata.update(terminal_verdict(ttl.evidence_id, why.evidence_id))
    assert why.evidence_id not in {l.evidence_id for l in final.evidence_links}
    return why


def _recheck(case, named, turn=8, label="recheck"):
    row = _row(label, _CAUSE_GONE, turn, "the re-check after the fix", rechecks=named)
    case.evidence.append(row)
    case.current_turn = turn
    return row


def _verdict(case):
    return assess_resolution_readiness(case).verdict


def _latest(case):
    return fix_windows(case)[-1]


def _feedback(case) -> str:
    meta: dict = {}
    _flag_unbound_post_fix_rows(case, meta)
    # The decider records a code; the narration step renders it (#2636).
    return "\n".join(narrated(case, meta))


# ---------------------------------------------------------------------------
# 1. The stored case: only the cited basis confirms the cause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["relief", "consequence"])
def test_a_recheck_of_the_relief_or_the_consequence_confirms_nothing(which):
    """The relief's read-back and the login failure both hold when the fix
    was never applied: a re-check of either is unbound on the confirmation
    read, the remediation's window is not SUCCEEDED, and no resolution is
    proposed. The window still saw it (SUPPORTS-linked to the cause the fix
    addressed), so the status says why it does not count."""
    case, final, relief, login, ttl = _mirror()
    _fixed(case, final)
    row = _recheck(case, (relief if which == "relief" else login).evidence_id)

    subjects = subject_index(case)
    assert subjects.subject(row) == SUBJECT_UNBOUND
    assert subjects.subject(row, windows=True) == SUBJECT_CAUSE
    window = _latest(case)
    assert row in window.cause_rows and row not in window.confirmed_cause_rows
    assert window.state != FIX_SUCCEEDED
    assert _verdict(case) == ResolutionReadiness.NEEDS_INFO
    assert [(e, why) for e, why in cause_recheck_status(case)] == [
        (row, f"it {_UNCITED}; name one of {ttl.evidence_id} (the cited basis)")
    ]


def test_a_recheck_of_the_cited_basis_confirms_the_cause():
    case, final, _relief, _login, ttl = _mirror()
    _fixed(case, final)
    row = _recheck(case, ttl.evidence_id)

    assert subject_index(case).subject(row) == SUBJECT_CAUSE
    window = _latest(case)
    assert row in window.confirmed_cause_rows and window.state == FIX_SUCCEEDED
    assert cause_recheck_status(case) == [(row, None)]
    assert _verdict(case) == ResolutionReadiness.READY


def test_bindable_rows_lists_the_cited_basis_alone():
    case, _final, _relief, _login, ttl = _mirror()
    _problem, cause = subject_index(case).bindable_rows()
    assert [e.evidence_id for e in cause] == [ttl.evidence_id]


# ---------------------------------------------------------------------------
# 2. Citation alone binds: no SUPPORTS link is needed
# ---------------------------------------------------------------------------


def test_a_cited_row_the_root_does_not_link_binds_end_to_end():
    """The model cited a row as the root's basis without linking it. It is the
    cited basis, so a re-check of it binds, is inside the fix's window, and
    confirms the cause (INV-48 rejected a link requirement)."""
    case, final, _relief, _login, ttl = _mirror()
    why = _cites_unlinked(case, final, ttl)
    _fixed(case, final)
    row = _recheck(case, why.evidence_id)

    subjects = subject_index(case)
    assert subjects.subject(row) == SUBJECT_CAUSE
    assert subjects.cause_targets(why.evidence_id) == {final.node_id}
    window = _latest(case)
    assert row in window.cause_rows and row in window.confirmed_cause_rows
    assert window.state == FIX_SUCCEEDED
    assert _verdict(case) == ResolutionReadiness.READY


def test_the_residual_a_relief_row_the_model_cites_binds():
    """D6's floor (INV-48): nothing judges what a cited row says, so a relief's
    read-back the model cites as the root's basis binds, as ruled."""
    case, final, relief, _login, ttl = _mirror()
    final.metadata.update(terminal_verdict(ttl.evidence_id, relief.evidence_id))
    _fixed(case, final)
    row = _recheck(case, relief.evidence_id)

    assert subject_index(case).subject(row) == SUBJECT_CAUSE
    assert row in _latest(case).confirmed_cause_rows
    assert _verdict(case) == ResolutionReadiness.READY


# ---------------------------------------------------------------------------
# 3. A cluster: a row cited by one member binds for the cluster
# ---------------------------------------------------------------------------


def test_a_row_cited_by_one_member_of_a_cluster_binds_for_the_cluster():
    """A second identified root on the final root's causal line (one cause,
    ``_one_cause``) cites a row of its own. The fix targeted the final root
    only; a re-check of the other member's cited row still confirms it."""
    case, final, _relief, _login, ttl = _mirror()
    policy = _row("policy", _CAUSAL, 4, "the Vault AppRole policy pins a fixed TTL")
    case.evidence.append(policy)
    deeper = _root(
        "cn_00000000255d",
        "the Vault AppRole policy forbids secret_id renewal",
        [policy],
        validated=True,
        cites=[policy],
    )
    case.causal_nodes[deeper.node_id] = deeper
    case.causal_edges.append(
        CausalEdge(cause_node_id=deeper.node_id, effect_node_id=final.node_id)
    )
    subjects = subject_index(case)
    assert subjects.pool_roots == {final.node_id, deeper.node_id}, "premise"
    _fixed(case, final)
    row = _recheck(case, policy.evidence_id)

    subjects = subject_index(case)
    assert subjects.subject(row) == SUBJECT_CAUSE
    assert subjects.cause_targets(policy.evidence_id) == {deeper.node_id}
    window = _latest(case)
    assert window.targets == (final.node_id,)
    assert row in window.confirmed_cause_rows and window.state == FIX_SUCCEEDED
    assert _verdict(case) == ResolutionReadiness.READY
    assert ttl.evidence_id in {e.evidence_id for e in subjects.bindable_rows()[1]}


# ---------------------------------------------------------------------------
# 4. A pool root citing nothing binds nothing
# ---------------------------------------------------------------------------


def test_an_uncited_pool_root_binds_nothing_until_the_verdict_is_re_sent():
    """The root was identified before D6 (a verdict citing no row), standing as
    the recorded cause. Nothing confirms it: a re-check of its own support is
    unbound, the case cannot resolve, and the model is asked to re-send the
    verdict citing the rows that show why the root arose."""
    case, final, _relief, _login, ttl = _mirror(cited=False)
    _fixed(case, final)
    row = _recheck(case, ttl.evidence_id)

    subjects = subject_index(case)
    assert subjects.subject(row) == SUBJECT_UNBOUND
    assert subjects.bindable_rows()[1] == []
    assert _latest(case).state != FIX_SUCCEEDED
    assert _verdict(case) == ResolutionReadiness.NEEDS_INFO
    assert cause_recheck_status(case) == [(row, _NO_CITATION)]
    feedback = _feedback(case)
    assert f"Post-fix row(s) {row.evidence_id} confirm nothing" in feedback
    assert _NO_CITATION in feedback
    assert "re-send root_is_terminal true citing the causal_evidence rows" in feedback
    assert "RE-ROOT" in feedback


def test_with_no_cause_in_the_pool_the_feedback_keeps_its_line():
    """No pool (the root has no verdict, nothing recorded): the unnamed row's
    line says no row supports the root cause alone yet, as before."""
    case, root, _hyp = _identified_case()
    root.metadata = {}
    _recompute_at(case, 5)
    assert subject_index(case).pool_roots == frozenset(), "premise: no pool"
    _fix(case, 6, None, [], "raise max_connections to 100")
    row = _recheck(case, _UNNAMED)

    assert subject_index(case).subject(row, windows=True) == SUBJECT_UNBOUND
    feedback = _feedback(case)
    assert f"Post-fix row(s) {row.evidence_id} name nothing" in feedback
    assert "none supports the root cause alone yet." in feedback
    assert _NO_CITATION not in feedback


# ---------------------------------------------------------------------------
# 5. The window reads that FAIL a fix are unchanged
# ---------------------------------------------------------------------------


def test_a_relief_row_recheck_still_fails_the_fix_on_a_relapse():
    """M6's pair: the cause re-checked gone through the relief's read-back,
    then the problem back. The window read binds through the SUPPORTS links,
    so the remediation's window FAILS as before (#1927)."""
    case, final, relief, _login, _ttl = _mirror()
    _fixed(case, final)
    _recheck(case, relief.evidence_id)
    case.evidence.append(
        _row("back", _PRESENT, 9, "503s at the gateway again", rechecks=None)
    )
    _recompute_at(case, 9)

    window = _latest(case)
    assert (window.state, window.failure_turn) == (FIX_FAILED, 9)


def test_window_zero_relapse_on_a_relief_row_recheck_is_unchanged():
    """Before any dated fix (#2501): the cause re-checked gone through a row
    the root supports but does not cite, the problem gone, then back: window
    0 FAILED, as before."""
    case, _final, relief, _login, _ttl = _mirror()
    _recheck(case, relief.evidence_id, turn=6)
    case.evidence += [
        _row("gone0", _GONE, 7, "503s at 0.0/s"),
        _row("back0", _PRESENT, 8, "503s at the gateway again", rechecks=None),
    ]
    _recompute_at(case, 8)

    window = fix_windows(case)[0]
    assert window.fix_turn is None
    assert (window.state, window.relief_turn, window.failure_turn) == (
        FIX_FAILED,
        7,
        8,
    )


# ---------------------------------------------------------------------------
# 6. The texts: the model is shown the cited basis, the user the fix's state
# ---------------------------------------------------------------------------


def test_the_feedback_names_the_cited_basis():
    """An unnamed causal_absence row and one re-checking the relief, in the
    fix's window: the first is told which rows bind (the cited basis), the
    second that it re-checks a row the verdict does not cite, naming them."""
    case, final, relief, _login, ttl = _mirror()
    _fixed(case, final)
    unnamed = _recheck(case, _UNNAMED, label="unnamed")
    uncited = _recheck(case, relief.evidence_id, label="uncited")

    feedback = _feedback(case)
    assert (
        f"Post-fix row(s) {unnamed.evidence_id} name nothing they re-check" in feedback
    )
    assert (
        f"causal_absence rows: one of {ttl.evidence_id} (the cited basis of the "
        "root cause)." in feedback
    )
    assert (
        f"Post-fix row(s) {uncited.evidence_id} confirm nothing: it {_UNCITED}; "
        f"name one of {ttl.evidence_id} (the cited basis)." in feedback
    )


def test_the_schema_tells_the_model_to_name_the_cited_basis():
    """Every turn's schema says what ``rechecks`` names for the cause, so it
    states the rule the feedback enforces."""
    description = EvidenceToAdd.model_fields["rechecks"].description
    assert "terminal verdict cites (the cited basis)" in description
    assert (
        "An observation naming no row confirms and disconfirms nothing" in description
    )
    assert (
        "does not cite confirms nothing, though it can show a fix failed" in description
    )
    assert "supports the root cause alone" not in description


def test_the_block_names_the_cited_basis_on_a_missing_cause_leg():
    case, final, relief, _login, ttl = _mirror()
    _fixed(case, final)
    row = _recheck(case, relief.evidence_id)

    block = build_fix_verification_block(case)
    assert (
        f"  - cause: missing; re-checked, not counted yet: {row.evidence_id} "
        f"(it {_UNCITED}; name one of {ttl.evidence_id} (the cited basis))\n" in block
    )


def test_the_readiness_ask_names_the_root_and_the_fix_without_ids():
    """The problem shown gone, the cause not: the ask names what the fix
    addressed and what to show changed, in the user's terms — no row ids."""
    case, final, _relief, _login, _ttl = _mirror()
    _fixed(case, final)
    case.current_turn = 8

    readiness = assess_resolution_readiness(case)
    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    ask = next(
        line
        for line in readiness.message.splitlines()
        if "The cause re-checked after the fix" in line
    )
    assert ask == (
        "- **The cause re-checked after the fix** — what you reported is shown "
        f'gone; the cause the fix addressed, "{_FINAL}", needs to be seen removed '
        f'too: the output of a check showing what "{_FIX}" changed at it.'
    )
    assert "ev_" not in ask and _eid("ttl") not in ask


def test_an_attempt_with_nothing_to_name_keeps_the_generic_ask():
    """Window 0 never asks for the cause re-check (an out-of-band fix has no
    window to owe it), so the named ask is a dated window's. A dated attempt
    recorded with no description leaves nothing to name: today's line."""
    case, final, _relief, _login, _ttl = _mirror()
    _fix(case, 6, "remediation", [final.node_id], "")
    case.evidence.append(_row("gone", _GONE, 7, "503s at 0.0/s"))
    case.current_turn = 7

    message = assess_resolution_readiness(case).message
    assert (
        "the cause the fix addressed needs to be seen removed too: the output of "
        "a check showing it (e.g. the setting now reads the new value" in message
    )
    assert "changed at it" not in message


# ---------------------------------------------------------------------------
# 7. A cited row that confirms can also fail: window 0's relapse and M6
# ---------------------------------------------------------------------------


def test_window_zero_relapses_through_a_cited_unlinked_row():
    """No dated fix. The cited row confirms window 0, so the problem coming back
    after it is that window's relapse: it does not stay READY (#2501)."""
    case, final, _relief, _login, ttl = _mirror()
    why = _cites_unlinked(case, final, ttl)
    _recheck(case, why.evidence_id, turn=6)
    case.evidence.append(_row("gone0", _GONE, 7, "503s at 0.0/s"))
    case.current_turn = 7
    assert _verdict(case) == ResolutionReadiness.READY, "premise: it confirms"

    case.evidence.append(_row("back0", _PRESENT, 8, "503s at the gateway again"))
    case.current_turn = 8

    window = fix_windows(case)[0]
    assert (window.state, window.relief_turn, window.failure_turn) == (
        FIX_FAILED,
        7,
        8,
    )
    assert _verdict(case) == ResolutionReadiness.NEEDS_INFO


def test_m6_demotes_on_a_failed_fix_seen_through_a_cited_unlinked_row():
    """The discriminating datum disconfirms as a relief row does: the fix's
    window FAILS, and M6 attributes that failure to the root (``failed_fix_of``)."""
    case, final, _relief, _login, ttl = _mirror()
    hyp = next(iter(case.hypotheses.values()))
    why = _cites_unlinked(case, final, ttl)
    _fix(case, 6, "remediation", [final.node_id], _FIX)
    _recheck(case, why.evidence_id, turn=7)
    case.evidence.append(_row("back", _PRESENT, 8, "503s at the gateway again"))

    _recompute_at(case, 8)

    _assert_demoted(case, final, hyp)
    assert len(_engine_rows(case)) == 1
    assert fix_windows(case)[-1].state == FIX_FAILED


# ---------------------------------------------------------------------------
# 8. A refused re-check with no dated fix is not silent, and only that shape
# ---------------------------------------------------------------------------


def test_a_refused_recheck_with_no_dated_fix_is_told_and_asked_for():
    """An unregistered fix: a re-check naming a root support the verdict does
    not cite. The model is told which rows bind, and the user is asked for the
    cause, not again for the problem they just showed gone."""
    case, _final, _relief, login, ttl = _mirror()
    row = _recheck(case, login.evidence_id, turn=6)

    assert (
        f"SYSTEM: Row(s) {row.evidence_id} confirm nothing: it {_UNCITED}; "
        f"name one of {ttl.evidence_id} (the cited basis)." in _feedback(case)
    )
    assert "Post-fix" not in _feedback(case), "no fix is on record"

    case.evidence.append(_row("gone0", _GONE, 7, "503s at 0.0/s"))
    case.current_turn = 7
    readiness = assess_resolution_readiness(case)
    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    assert "**The cause re-checked after the fix**" in readiness.message
    assert "**Confirmation it's resolved**" not in readiness.message
    assert "ev_" not in readiness.message


def _assert_keeps_mains_ask(case) -> None:
    message = assess_resolution_readiness(case).message
    assert "**Confirmation it's resolved**" in message
    assert "**The cause re-checked after the fix**" not in message


def test_window_zero_with_no_recheck_named_keeps_its_ask_and_no_feedback():
    """Every other window-0 shape reads as on ``main``: a causal_absence row
    naming nothing, a symptom row naming nothing."""
    case, _final, _relief, _login, ttl = _mirror()
    _recheck(case, _UNNAMED, turn=6)
    case.evidence += [
        _row("sym0", _PRESENT, 6, "a probe", rechecks=_UNNAMED),
        _row("sym1", _PRESENT, 6, "a probe", rechecks=ttl.evidence_id),
    ]
    assert _feedback(case) == ""

    case.evidence.append(_row("gone0", _GONE, 7, "503s at 0.0/s"))
    case.current_turn = 7
    message = assess_resolution_readiness(case).message
    assert "**Confirmation it's resolved**" in message
    assert "**The cause re-checked after the fix**" not in message
    assert _feedback(case) == ""


def test_a_named_recheck_with_no_dated_fix_and_no_pool_stays_silent():
    case, root, _hyp = _identified_case()
    root.metadata = {}
    _recompute_at(case, 5)
    assert subject_index(case).pool_roots == frozenset(), "premise: no pool"
    _recheck(case, _eid("pool_log"), turn=6)
    assert _feedback(case) == ""
    case.evidence.append(_row("gone0", _GONE, 7, "503s at 0.0/s"))
    case.current_turn = 7
    _assert_keeps_mains_ask(case)


# ---------------------------------------------------------------------------
# 9. The block offers the cited basis only where a re-check can count
# ---------------------------------------------------------------------------

_CLAUSE = "the cited basis to re-check"


def _failed(case, final, relief):
    _fix(case, 6, "remediation", [final.node_id], _FIX)
    _recheck(case, relief.evidence_id, turn=7)
    case.evidence.append(_row("back", _PRESENT, 8, "503s at the gateway again"))
    case.current_turn = 8


def _interception_only(case, _final, _relief):
    _fix(case, 5, "interception", [_SECRET_ID], _RELIEF)


def _no_fix(case, _final, _relief):
    case.progress.solution_accepted = True


@pytest.mark.parametrize("shape", [_failed, _interception_only, _no_fix])
def test_the_block_offers_no_basis_where_no_recheck_can_count(shape):
    case, final, relief, _login, ttl = _mirror()
    shape(case, final, relief)
    block = build_fix_verification_block(case)
    assert "  - cause: missing" in block, "premise: the leg is rendered"
    assert _CLAUSE not in block and ttl.evidence_id not in block


def test_the_block_offers_the_basis_after_a_remediation_with_the_problem_gone():
    case, final, _relief, _login, ttl = _mirror()
    _fixed(case, final)
    block = build_fix_verification_block(case)
    assert f"  - cause: missing; {_CLAUSE}: {ttl.evidence_id}\n" in block


def test_the_block_names_the_basis_once_beside_a_pending_uncited_row():
    case, final, relief, _login, ttl = _mirror()
    _fixed(case, final)
    _recheck(case, relief.evidence_id)
    block = build_fix_verification_block(case)
    assert _CLAUSE not in block
    assert block.count(ttl.evidence_id) == 1


# ---------------------------------------------------------------------------
# 10. A real target of the fix is told why, in the true words
# ---------------------------------------------------------------------------


def test_a_recheck_of_a_deepened_past_target_is_told_the_cited_basis():
    """The fix ran against a root since deepened past as well as the final one.
    A re-check of that root's own support is the fix's, not another cause's:
    it does not count because the verdict does not cite it."""
    case, final, _relief, _login, ttl = _mirror()
    _fix(case, 6, "remediation", [_ROOT_ID, final.node_id], _FIX)
    row = _recheck(case, _eid("pool_log"), turn=7)
    assert cause_recheck_status(case) == [
        (row, f"it {_UNCITED}; name one of {ttl.evidence_id} (the cited basis)")
    ]


# ---------------------------------------------------------------------------
# 11. The ask carries no record id, and quotes what it names
# ---------------------------------------------------------------------------


def _ask(case) -> str:
    message = assess_resolution_readiness(case).message
    return next(
        line
        for line in message.splitlines()
        if "The cause re-checked after the fix" in line
    )


@pytest.mark.parametrize(
    "where, record_id",
    [
        ("statement", "ev_0a65b4b839ca"),
        ("statement", "cn_51f2ee0b4a99"),
        ("description", "act_0123456789ab"),
        ("description", "hyp_0123456789ab"),
    ],
)
def test_the_ask_falls_back_when_a_name_carries_a_record_id(where, record_id):
    case, final, _relief, _login, _ttl = _mirror()
    fix = _fixed(case, final)
    if where == "statement":
        final.statement = f"{_FINAL}, as {record_id} shows"
    else:
        fix.description = f"{_FIX} (see {record_id})"
    ask = _ask(case)
    assert record_id not in ask
    assert "the output of a check showing it (e.g. the setting" in ask


def test_the_ask_quotes_a_statement_holding_markup():
    case, final, _relief, _login, _ttl = _mirror()
    _fixed(case, final)
    final.statement = "the pool is sized workers * 2 by hand"
    assert '"the pool is sized workers * 2 by hand"' in _ask(case)


def test_the_ask_clips_a_long_statement():
    case, final, _relief, _login, _ttl = _mirror()
    _fixed(case, final)
    final.statement = "nothing rotates it " * 20
    ask = _ask(case)
    assert "…" in ask and final.statement.strip() not in ask


# ---------------------------------------------------------------------------
# 12. What the rule rests on
# ---------------------------------------------------------------------------


def test_remediations_at_excludes_a_same_turn_relief():
    case, final, _relief, _login, _ttl = _mirror()
    _fix(case, 6, "interception", [_SECRET_ID], _RELIEF)
    _fix(case, 6, "remediation", [final.node_id], _FIX)
    assert [a.description for a in remediations_at(case, 6)] == [_FIX]
    case.evidence.append(_row("gone", _GONE, 7, "503s at 0.0/s"))
    case.current_turn = 7
    assert _FIX in _ask(case) and _RELIEF not in _ask(case)


def _with_deeper_member(case, final):
    """A second identified root on the final root's line, on its own datum."""
    policy = _row("policy", _CAUSAL, 4, "the Vault AppRole policy pins a fixed TTL")
    case.evidence.append(policy)
    deeper = _root(
        "cn_00000000255d",
        "the Vault AppRole policy forbids secret_id renewal",
        [policy],
        validated=True,
        cites=[policy],
    )
    case.causal_nodes[deeper.node_id] = deeper
    case.causal_edges.append(
        CausalEdge(cause_node_id=deeper.node_id, effect_node_id=final.node_id)
    )
    assert subject_index(case).pool_roots == {final.node_id, deeper.node_id}
    return deeper


def test_the_ask_narrows_to_the_targets_of_a_kinded_window():
    case, final, _relief, _login, _ttl = _mirror()
    deeper = _with_deeper_member(case, final)
    _fixed(case, final)
    assert _FINAL in _ask(case) and deeper.statement not in _ask(case)


def test_a_citation_on_a_node_outside_the_pool_binds_nothing():
    """A candidate on the cause's own line, so only the pool test refuses it:
    the rule reads the pool's citations, not every node's."""
    case, final, _relief, _login, ttl = _mirror()
    other = _row("other", _CAUSAL, 4, "a row a candidate root's verdict cites")
    case.evidence.append(other)
    candidate = _root(
        "cn_00000000255e", "a candidate above it", [], validated=False, cites=[other]
    )
    case.causal_nodes[candidate.node_id] = candidate
    case.causal_edges.append(
        CausalEdge(cause_node_id=candidate.node_id, effect_node_id=final.node_id)
    )
    subjects = subject_index(case)
    assert subjects.pool_roots == {final.node_id}, "premise: outside the pool"
    assert subjects.cause_targets(other.evidence_id) == frozenset()
    assert [e.evidence_id for e in subjects.bindable_rows()[1]] == [ttl.evidence_id]


def test_a_rootless_validated_hypothesis_still_binds():
    """Leg 4 has no verdict to cite: the row binds through the hypothesis."""
    case, root, hyp = _identified_case()
    case.causal_nodes.pop(root.node_id)
    case.progress.identified_cause_root_ids = []
    hyp.root_node_id, hyp.path = None, []
    hyp.state = HypothesisState.VALIDATED
    hyp.evidence_links = [
        HypothesisEvidenceLink(
            hypothesis_id=hyp.hypothesis_id,
            evidence_id=_eid("pool_log"),
            stance=EvidenceStance.SUPPORTS,
            reasoning="bears on it",
            stance_confidence=0.9,
        )
    ]
    subjects = subject_index(case)
    assert subjects.pool_roots == frozenset(), "premise: no root"
    row = _recheck(case, _eid("pool_log"), turn=6)
    assert subject_index(case).subject(row) == SUBJECT_CAUSE


# ---------------------------------------------------------------------------
# 13. Delta of 6f45da107: what a window reads of its own citation
# ---------------------------------------------------------------------------


def _stands(case, node_id, statement, *, cites, turn, generated_at_turn=None):
    """A second cause on its own two supports, its verdict citing ``cites`` (its
    own first support when None), rooted by a standing hypothesis."""
    labels = (f"{node_id}_a", f"{node_id}_b")
    rows = [_row(label, _CAUSAL, turn, f"{label} reading") for label in labels]
    case.evidence += rows
    root = _root(node_id, statement, rows, validated=True, cites=cites or rows[:1])
    case.causal_nodes[root.node_id] = root
    problem = next(
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    case.causal_edges.append(
        CausalEdge(cause_node_id=root.node_id, effect_node_id=problem.node_id)
    )
    hyp = Hypothesis(
        hypothesis_id=f"hyp_{node_id[3:]}",
        statement=statement,
        category=HypothesisCategory.CODE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=root.node_id,
        generated_at_turn=turn if generated_at_turn is None else generated_at_turn,
    )
    case.hypotheses[hyp.hypothesis_id] = hyp
    return root, hyp, rows


@pytest.mark.parametrize("through", ["cited-unlinked", "linked-control"])
def test_a_failure_read_through_a_cited_row_stays_failed_once_the_cause_falls(through):
    """An unkinded fix, re-checked, the problem still there: M6 demotes on it and
    mints its row. A second cause is identified and its cited row re-checked,
    the problem gone. The failure M6 acted on still stands (the gate and M6 do
    not disagree), through a cited row as through a linked one."""
    case, final, relief, _login, ttl = _mirror()
    hyp = next(iter(case.hypotheses.values()))
    named = _cites_unlinked(case, final, ttl) if through == "cited-unlinked" else relief
    _fix(case, 6, None, [], _FIX)
    _recheck(case, named.evidence_id, turn=7, label="rc_a")
    case.evidence.append(_row("back", _PRESENT, 8, "503s at the gateway again"))
    _recompute_at(case, 8)
    _assert_demoted(case, final, hyp)
    assert len(_engine_rows(case)) == 1

    second, _hyp2, rows = _stands(
        case,
        "cn_00000000255f",
        "a memory leak in the gunicorn worker",
        cites=None,
        turn=9,
    )
    _recompute_at(case, 9)
    assert second.is_identified_root, "premise: the second cause is identified"
    _recheck(case, rows[0].evidence_id, turn=10, label="rc_b")
    case.evidence.append(_row("gone", _GONE, 11, "503s at 0.0/s"))
    case.current_turn = 11

    window = fix_windows(case)[-1]
    assert (window.state, window.failure_turn) == (FIX_FAILED, 8)
    assert _verdict(case) == ResolutionReadiness.NEEDS_INFO


def _kinded_a_fell_older_b_stands(b_cites=None):
    """A remediation kinded to A (6) seen failing (7, 8): M6 demotes A. Then B, a
    hypothesis older than the fix, is identified."""
    case, a, hyp_a = _identified_case()
    _fix(case, 6, "remediation", [a.node_id], _FIX)
    _recheck(case, _eid("pool_log"), turn=7, label="rc_a")
    case.evidence.append(_row("back", _PRESENT, 8, "503s at the gateway again"))
    _recompute_at(case, 8)
    _assert_demoted(case, a, hyp_a)
    assert len(_engine_rows(case)) == 1
    b, hyp_b, rows = _stands(
        case,
        "cn_00000000256a",
        "a memory leak in the gunicorn worker",
        cites=b_cites,
        turn=9,
        generated_at_turn=1,
    )
    _recompute_at(case, 9)
    assert b.is_identified_root, "premise: B is identified"
    return case, b, hyp_b, rows


def test_m6_leaves_a_cause_the_kinded_fix_did_not_target():
    """B cites a row A SUPPORTS-links, and that row is re-checked with the problem
    present. The window is A's: the failure is not B's, whatever B cites."""
    shared = _row("pool_metric", _CAUSAL, 4, "unused; the row is A's own")
    case, b, hyp_b, _rows = _kinded_a_fell_older_b_stands(b_cites=[shared])
    _recheck(case, shared.evidence_id, turn=10, label="rc_b")
    case.evidence.append(_row("back2", _PRESENT, 11, "503s at the gateway again"))
    _recompute_at(case, 11)

    assert b.node_state == NodeState.VALIDATED
    assert hyp_b.state != HypothesisState.REFUTED
    assert len(_engine_rows(case)) == 1


def test_m6_attributes_a_cited_row_to_the_root_citing_it():
    """Two identified roots of distinct causes (two clusters) and an unkinded fix,
    whose window confirms through either. It fails through A's cited, unlinked row:
    that failure is A's, and B does not cite the row. Roots of one cluster count
    as one cause, so this holds only across clusters."""
    case, a, hyp_a = _identified_case()
    why = _row("why", _CAUSAL, 4, "max_connections was set by hand, once")
    case.evidence.append(why)
    a.metadata.update(
        terminal_verdict(*a.metadata["root_terminal_evidence_ids"], why.evidence_id)
    )
    b, hyp_b, _rows = _stands(
        case,
        "cn_00000000256f",
        "a worker leak",
        cites=None,
        turn=4,
        generated_at_turn=1,
    )
    _recompute_at(case, 5)
    assert subject_index(case).pool_roots == {a.node_id, b.node_id}, "premise"
    assert case.progress.cause_identification_contested is True, "premise: distinct"
    _fix(case, 6, None, [], _FIX)
    row = _recheck(case, why.evidence_id, turn=7, label="rc_a")
    case.evidence.append(_row("back", _PRESENT, 8, "503s at the gateway again"))
    case.current_turn = 8

    assert row in fix_windows(case)[-1].confirmed_cause_rows, "premise: it confirms"
    assert failed_fix_of(case, hyp_a) is not None
    assert failed_fix_of(case, hyp_b) is None


def test_a_kinded_window_reads_only_its_own_roots_citations():
    """A row B alone cites is no re-check of what A's fix addressed: it stays
    out of A's window, and a citation of A's own does not widen what confirms
    it."""
    case, b, _hyp_b, rows = _kinded_a_fell_older_b_stands()
    of_b = _recheck(case, rows[0].evidence_id, turn=10, label="rc_b")
    window = fix_windows(case)[-1]
    assert of_b not in window.cause_rows
    assert subject_index(case).subject(of_b) == SUBJECT_CAUSE, "premise: B's basis"
    assert window.confirmed_cause_rows == ()


def test_a_kinded_windows_own_citation_is_read_but_does_not_confirm():
    """The fallen target's cited, unlinked row is inside its window, and with the
    target out of the pool it confirms nothing."""
    case, final, _relief, _login, ttl = _mirror()
    hyp = next(iter(case.hypotheses.values()))
    why = _cites_unlinked(case, final, ttl)
    _fix(case, 6, "remediation", [final.node_id], _FIX)
    _recheck(case, why.evidence_id, turn=7, label="rc_a")
    case.evidence.append(_row("back", _PRESENT, 8, "503s at the gateway again"))
    _recompute_at(case, 8)
    _assert_demoted(case, final, hyp)
    _stands(case, "cn_00000000256b", "a worker leak", cites=None, turn=9)
    _recompute_at(case, 9)

    again = _recheck(case, why.evidence_id, turn=10, label="rc_a2")
    window = fix_windows(case)[-1]
    assert again in window.cause_rows and window.confirmed_cause_rows == ()


# ---------------------------------------------------------------------------
# 14. Window 0 reads only a row its window binds
# ---------------------------------------------------------------------------


def _names_a_competitors_support(case) -> str:
    support = _row("comp_sup", _CAUSAL, 4, "dns errors in the resolver log")
    case.evidence.append(support)
    node = _root(
        "cn_00000000256c", "the resolver drops queries", [support], validated=False
    )
    case.causal_nodes[node.node_id] = node
    return support.evidence_id


def _names_a_symptom_row(_case) -> str:
    return VERIFYING_ID


def _names_a_rungs_support(case) -> str:
    support = _row("rung_sup", _CAUSAL, 4, "a rung's own reading")
    case.evidence.append(support)
    rung = _root("cn_00000000256d", "a rung", [support], validated=False)
    rung.node_type = NodeType.INTERMEDIATE
    case.causal_nodes[rung.node_id] = rung
    return support.evidence_id


@pytest.mark.parametrize(
    "names",
    [_names_a_competitors_support, _names_a_symptom_row, _names_a_rungs_support],
)
def test_a_row_window_zero_does_not_bind_is_silent_and_keeps_mains_ask(names):
    """In DIAGNOSIS, with no fix, a causal_absence row is as likely an exclusion as
    a fix's outcome: naming what the window does not bind, it is not told to name
    the cited basis, which would read READY with no fix on record."""
    case, _final, _relief, _login, _ttl = _mirror()
    _recheck(case, names(case), turn=6)
    assert _feedback(case) == ""
    case.evidence.append(_row("gone0", _GONE, 7, "503s at 0.0/s"))
    case.current_turn = 7
    assert _verdict(case) == ResolutionReadiness.NEEDS_INFO
    _assert_keeps_mains_ask(case)


def _no_root_identified(case, root, hyp) -> None:
    root.metadata = {}
    case.progress.identified_cause_root_ids = []
    _recompute_at(case, 5)


def _no_graph(case, root, hyp) -> None:
    case.causal_nodes = {
        n: v for n, v in case.causal_nodes.items() if v.node_type == NodeType.PROBLEM
    }
    case.causal_edges = []
    hyp.root_node_id, hyp.path = None, []
    case.progress.identified_cause_root_ids = []


def _interception_with_no_pool(case, root, hyp) -> None:
    _no_root_identified(case, root, hyp)
    _fix(case, 8, "interception", [], _RELIEF)


@pytest.mark.parametrize(
    "shape", [_no_root_identified, _no_graph, _interception_with_no_pool]
)
def test_a_rootless_pool_keeps_mains_ask(shape):
    """Nothing binds on an empty pool, so "the cause the fix addressed" could not
    be met."""
    case, root, hyp = _identified_case()
    shape(case, root, hyp)
    assert subject_index(case).pool_roots == frozenset(), "premise: no pool"
    _recheck(case, _eid("pool_log"), turn=6)
    case.evidence.append(_row("gone0", _GONE, 7, "503s at 0.0/s"))
    case.current_turn = 9
    _assert_keeps_mains_ask(case)


def test_a_window_bound_row_with_an_empty_pool_keeps_mains_ask():
    """A fallen cause past another standing hypothesis: the window still binds the
    re-check, and no root cause is identified to ask about."""
    case, root, _hyp = _identified_case()
    root.metadata = {}
    case.progress.identified_cause_root_ids = [root.node_id]
    case.causal_nodes["cn_00000000256e"] = _root(
        "cn_00000000256e", "a standing candidate", [], validated=False
    )
    case.hypotheses["hyp_00000000256e"] = Hypothesis(
        hypothesis_id="hyp_00000000256e",
        statement="a standing candidate",
        category=HypothesisCategory.CODE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id="cn_00000000256e",
        generated_at_turn=2,
    )
    row = _recheck(case, _eid("pool_log"), turn=6)
    subjects = subject_index(case)
    assert subjects.pool_roots == frozenset(), "premise: no pool"
    assert subjects.window_roots == {root.node_id}, "premise: the window binds it"
    assert row in fix_windows(case)[0].cause_rows
    assert _feedback(case) == ""
    case.evidence.append(_row("gone0", _GONE, 7, "503s at 0.0/s"))
    case.current_turn = 7
    _assert_keeps_mains_ask(case)


def test_a_dated_window_keeps_post_fix_in_the_uncited_line():
    case, final, relief, _login, ttl = _mirror()
    _fixed(case, final)
    row = _recheck(case, relief.evidence_id)
    assert f"SYSTEM: Post-fix row(s) {row.evidence_id} confirm nothing" in _feedback(
        case
    )


# ---------------------------------------------------------------------------
# 15. The block stays inside its budget, and says what it hides
# ---------------------------------------------------------------------------


def _long_pending(pending, needs, cited):
    """The block's largest shape: three attempts, ``pending`` uncited re-checks,
    ``needs`` outstanding asks and ``cited`` rows in the basis."""
    case, final, relief, _login, _ttl = _mirror()
    basis = [_row(f"cit{i}", _CAUSAL, 4, f"datum {i}") for i in range(cited)]
    case.evidence += basis
    final.metadata.update(terminal_verdict(*(row.evidence_id for row in basis)))
    fix = "switch the issuer to Kubernetes auth and rotate every secret_id, in all clusters "
    _fix(case, 5, "interception", [_SECRET_ID], fix)
    _fix(case, 6, "remediation", [final.node_id], fix)
    _fix(case, 7, "remediation", [final.node_id], fix)
    case.evidence.append(_row("gone", _GONE, 8, "503s at 0.0/s"))
    for i in range(pending):
        _recheck(case, relief.evidence_id, turn=8 + i, label=f"pend{i}")
    ask = "Paste the check showing every route still returns 200 after the change, " * 3
    for _ in range(needs):
        case.evidence_needs.append(
            EvidenceNeed(
                case_id=case.case_id,
                purpose=NeedPurpose.SYMPTOM_VERIFICATION,
                request_text=ask,
                rationale="r",
                priority=NeedPriority.HIGH,
                created_at_turn=case.current_turn,
            )
        )
    return case, basis


def test_six_pending_uncited_rows_cut_no_need_and_name_the_basis_once():
    """The full why on the first row; the later rows with the same why keep its
    clause before the ``;``. The block then fits, with every need kept."""
    case, basis = _long_pending(pending=6, needs=5, cited=5)
    block = build_fix_verification_block(case)
    assert block.count(f"it {_UNCITED}") == 6, "premise: six rows, each says why"
    assert cap_fix_verification_block(block, TokenBudget(100000)) == block
    assert block.count("[eneed_") == 5
    for row in basis:
        assert block.count(row.evidence_id) == 1
    assert len(re.findall(r"ev_[0-9a-f]{12}", block)) <= 12


def test_the_block_names_the_basis_when_the_list_hides_the_row_that_would():
    """Six rows from outside the window, then one uncited: the list ends (+1
    more), so the seventh row's why is not shown. The clause must say it."""
    case, final, relief, _login, ttl = _mirror()
    for i in range(6):
        _recheck(case, ttl.evidence_id, turn=5, label=f"before{i}")
    _fix(case, 6, "remediation", [final.node_id], _FIX)
    _recheck(case, relief.evidence_id, turn=7, label="uncited")
    block = build_fix_verification_block(case)
    assert "(+1 more)" in block, "premise: the seventh row is cut from the list"
    assert f"{_CLAUSE}: {ttl.evidence_id}" in block


# ---------------------------------------------------------------------------
# 16. The ask, for every id the case can mint and the ref the model writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefix",
    [
        "act",
        "att",
        "case",
        "cdo",
        "ce",
        "cn",
        "doc",
        "eneed",
        "ev",
        "file",
        "hyp",
        "sol",
    ],
)
def test_the_ask_falls_back_for_every_prefix_a_record_id_carries(prefix):
    case, final, _relief, _login, _ttl = _mirror()
    _fixed(case, final)
    record_id = f"{prefix}_0123456789ab"
    final.statement = f"{_FINAL}, as {record_id} shows"
    ask = _ask(case)
    assert record_id not in ask
    assert "the output of a check showing it (e.g. the setting" in ask


def test_the_ask_falls_back_for_the_ref_the_model_writes():
    case, final, _relief, _login, _ttl = _mirror()
    _fixed(case, final)
    final.statement = "the row new_index_2 shows nothing rotates it"
    ask = _ask(case)
    assert "new_index_2" not in ask
    assert "the output of a check showing it (e.g. the setting" in ask


def test_the_ask_clips_a_long_fix_description():
    case, final, _relief, _login, _ttl = _mirror()
    fix = _fixed(case, final)
    fix.description = "switch the issuer to Kubernetes auth " * 20
    ask = _ask(case)
    assert "…" in ask and fix.description.strip() not in ask


# ---------------------------------------------------------------------------
# 17. A window reads only its own roots' citations (N10)
# ---------------------------------------------------------------------------

_KINDS = [
    ("remediation", "remediation"),
    (None, None),
    ("remediation", None),
    (None, "remediation"),
]
_KIND_IDS = ["kinded", "unkinded", "kinded-a-unkinded-b", "unkinded-a-kinded-b"]


def _fix_of(case, turn, kind, root) -> None:
    _fix(case, turn, kind, [root.node_id] if kind else [], f"a fix at turn {turn}")


def _a_fell_then_b_identified(kind_a):
    """A's verdict also cites a row it does not link. A's fix (6) is seen failing
    through A's linked, cited row (7, 8), so M6 demotes A on every tree. Then B
    is identified (9)."""
    case, a, hyp_a = _identified_case()
    why = _row("why_a", _CAUSAL, 4, "max_connections was set by hand, once")
    case.evidence.append(why)
    a.metadata.update(
        terminal_verdict(*a.metadata["root_terminal_evidence_ids"], why.evidence_id)
    )
    _fix_of(case, 6, kind_a, a)
    _recheck(case, _eid("pool_log"), turn=7, label="rc_a")
    case.evidence.append(_row("back_a", _PRESENT, 8, "503s at the gateway again"))
    _recompute_at(case, 8)
    _assert_demoted(case, a, hyp_a)
    b, hyp_b, rows = _stands(
        case,
        "cn_0000000025b0",
        "a memory leak in the gunicorn worker",
        cites=None,
        turn=9,
    )
    _recompute_at(case, 9)
    assert b.is_identified_root, "premise: B is identified"
    return case, why, b, hyp_b, rows


def _turns(case, *steps) -> None:
    """Each ``(turn, row)`` recorded, the case recomputed in its turn."""
    for turn, row in steps:
        case.evidence.append(row)
        _recompute_at(case, turn)


def _rc(label, turn, named):
    return _row(label, _CAUSE_GONE, turn, "the re-check after the fix", rechecks=named)


@pytest.mark.parametrize("kind_a, kind_b", _KINDS, ids=_KIND_IDS)
def test_a_fallen_causes_citation_does_not_fail_the_next_fix(kind_a, kind_b):
    """A re-check of the fallen A's cited, unlinked row in B's window is not B's:
    the problem present after it fails nothing and M6 does not refute B. B's own
    cited row re-checked, then the problem gone, confirms B, as on main."""
    case, why, b, hyp_b, rows = _a_fell_then_b_identified(kind_a)
    _fix_of(case, 10, kind_b, b)
    _turns(
        case,
        (11, _rc("rc_a_why", 11, why.evidence_id)),
        (12, _row("back_b", _PRESENT, 12, "503s at the gateway again")),
        (13, _rc("rc_b", 13, rows[0].evidence_id)),
        (14, _row("gone_b", _GONE, 14, "503s at 0.0/s")),
    )

    window = fix_windows(case)[-1]
    assert (window.fix_turn, window.state) == (10, FIX_SUCCEEDED)
    assert b.node_state == NodeState.VALIDATED
    assert hyp_b.state != HypothesisState.REFUTED
    assert len(_engine_rows(case)) == 1, "only A's demotion"
    assert _verdict(case) == ResolutionReadiness.READY


@pytest.mark.parametrize("kind", ["remediation", None], ids=["kinded", "unkinded"])
def test_a_fallen_causes_citation_does_not_observe_a_newer_fix(kind):
    """B's fix confirmed, then a newer fix nobody has looked at yet. A re-check of
    the fallen A's cited row is not the newer fix's outcome: the confirmed one
    still answers."""
    case, why, b, _hyp_b, rows = _a_fell_then_b_identified(kind)
    _fix_of(case, 10, kind, b)
    _turns(
        case,
        (11, _rc("rc_b", 11, rows[0].evidence_id)),
        (12, _row("gone_b", _GONE, 12, "503s at 0.0/s")),
    )
    assert _verdict(case) == ResolutionReadiness.READY, "premise: B confirmed"
    _fix_of(case, 13, kind, b)
    _turns(case, (14, _rc("rc_a_why", 14, why.evidence_id)))

    assert not fix_windows(case)[-1].observed
    assert _verdict(case) == ResolutionReadiness.READY


@pytest.mark.parametrize("named", ["fallen-linked", "own-uncited"])
def test_a_kinded_window_is_failed_by_its_target_and_a_fallen_cause_alike(named):
    """What a kinded window observes through SUPPORTS is unchanged: a re-check of
    its target's linked, uncited row, or of the fallen A's linked row, with the
    problem present, fails it. M6 refutes only the cause the row is about."""
    case, _why, b, hyp_b, rows = _a_fell_then_b_identified("remediation")
    _fix_of(case, 10, "remediation", b)
    target = _eid("pool_metric") if named == "fallen-linked" else rows[1].evidence_id
    _turns(
        case,
        (11, _rc("rc_11", 11, target)),
        (12, _row("back_b", _PRESENT, 12, "503s at the gateway again")),
    )

    window = fix_windows(case)[-1]
    assert (window.fix_turn, window.state, window.failure_turn) == (10, FIX_FAILED, 12)
    if named == "fallen-linked":
        assert b.node_state == NodeState.VALIDATED and len(_engine_rows(case)) == 1
    else:
        assert hyp_b.state == HypothesisState.REFUTED and len(_engine_rows(case)) == 2
