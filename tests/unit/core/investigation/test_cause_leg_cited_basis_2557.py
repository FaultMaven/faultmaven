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

import pytest

from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_SUCCEEDED,
    SUBJECT_CAUSE,
    SUBJECT_UNBOUND,
    cause_recheck_status,
    fix_windows,
    subject_index,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _flag_unbound_post_fix_rows,
)
from faultmaven.core.investigation.prompts.context_builder.fix_verification_block import (
    build_fix_verification_block,
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
    EvidenceStance,
    InvestigationActionType,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProposedAction,
    ValidationMethod,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _eid,
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
    return meta.get("system_feedback") or ""


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
    why = _row("why", _CAUSAL, 4, "the AppRole secret_id was issued once, by hand")
    case.evidence.append(why)
    final.metadata.update(terminal_verdict(ttl.evidence_id, why.evidence_id))
    assert why.evidence_id not in {l.evidence_id for l in final.evidence_links}
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
    assert cause_recheck_status(case) == [
        (row, f"it {_UNCITED}, and the verdict cites no row yet")
    ]
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
    row = _recheck(case, None)

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
    unnamed = _recheck(case, None, label="unnamed")
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
        f"Post-fix row(s) {uncited.evidence_id} confirm nothing: each {_UNCITED}; "
        f"name one of {ttl.evidence_id} (the cited basis)." in feedback
    )


def test_the_schema_tells_the_model_to_name_the_cited_basis():
    """Every turn's schema says what ``rechecks`` names for the cause, so it
    states the rule the feedback enforces."""
    description = EvidenceToAdd.model_fields["rechecks"].description
    assert "terminal verdict cites (the cited basis)" in description
    assert "supports the root cause alone" not in description


def test_the_block_names_the_cited_basis_on_a_missing_cause_leg():
    case, final, relief, _login, ttl = _mirror()
    _fixed(case, final)
    row = _recheck(case, relief.evidence_id)

    block = build_fix_verification_block(case)
    assert (
        f"  - cause: missing; re-checked, not counted yet: {row.evidence_id} "
        f"(it {_UNCITED}; name one of {ttl.evidence_id} (the cited basis)); "
        f"the cited basis to re-check: {ttl.evidence_id}\n" in block
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
        f"gone; the cause the fix addressed, *{_FINAL}*, needs to be seen removed "
        f"too: the output of a check showing what *{_FIX}* changed at it."
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
