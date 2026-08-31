"""Calibration pin for the §7.1 restatement guard (INV-27).

Runs the REAL guard predicate over the shipped 91-runbook corpus: every
expert-authored (non-fallback) cause Statement, anchored on its own runbook's
title-as-symptom, must PASS the guard — a guard that blocks real mechanism
statements trades the #656 false-positive for false-negatives on correct
grounding. The pin is FP == 0 on the whole corpus, so any change to the
novelty bar, the tokenizer, or the corpus that starts blocking real causes
fails CI loudly instead of silently degrading grounding.

The positive side pins the #656 incident shape (the disjunction root against
its real case frame) and verbatim symptom-as-cause.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph import root_restates_case_frame
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeType,
    ProblemVerification,
)

pytestmark = pytest.mark.unit

PACK_JSON = (
    Path(__file__).resolve().parents[4]
    / "resources"
    / "knowledge"
    / "pack"
    / "pack.json"
)


def _case(
    symptom: str,
    *,
    hyp_statements: list[str] | None = None,
    chains: list[tuple[str, list[str]]] | None = None,
) -> Case:
    """A case with the given symptom anchor.

    ``hyp_statements`` are UNATTACHED standing hypotheses (no ``root_node_id``).
    ``chains`` are ``(root_statement, [hypothesis_statement, ...])`` pairs: each
    mints a ROOT node and anchors those hypotheses to it, which is the shape a
    live case actually has — ``templates.py`` mandates ``root_node_ref`` on
    every hypothesis, so an unattached one only survives the fm#1091 refusal.
    """
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement=symptom,
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement=symptom, severity=CaseSeverity.HIGH
        ),
    )

    def _add_hyp(stmt: str, root_node_id: str | None) -> None:
        h = Hypothesis(
            statement=stmt,
            category=HypothesisCategory.OTHER,
            state=HypothesisState.ACTIVE,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="posited",
            generated_at_turn=1,
            root_node_id=root_node_id,
        )
        case.hypotheses[h.hypothesis_id] = h

    for stmt in hyp_statements or []:
        _add_hyp(stmt, None)
    for root_stmt, stmts in chains or []:
        sibling_root = _root(root_stmt)
        case.causal_nodes[sibling_root.node_id] = sibling_root
        for stmt in stmts:
            _add_hyp(stmt, sibling_root.node_id)
    return case


def _root(statement: str) -> CausalNode:
    return CausalNode(
        statement=statement,
        node_type=NodeType.ROOT,
        generated_at_turn=1,
    )


def test_no_shipped_mechanism_statement_is_blocked():
    """FP pin: every non-fallback cause Statement in the shipped corpus passes
    the guard against its own runbook's symptom anchor."""
    pack = json.loads(PACK_JSON.read_text(encoding="utf-8"))
    blocked: list[str] = []
    checked = 0
    for rb in pack["runbooks"]:
        case = _case(rb["title"])
        for cause in rb.get("causes", []):
            if cause.get("is_fallback_cause"):
                continue
            stmt = cause.get("cause_statement") or ""
            if not stmt:
                continue
            checked += 1
            if root_restates_case_frame(_root(stmt), case):
                blocked.append(f"[{rb['title'][:40]}] {stmt[:80]}")
    assert checked > 300  # the corpus is really being swept
    assert not blocked, (
        f"{len(blocked)}/{checked} real mechanism statements blocked "
        f"(guard FP regression):\n" + "\n".join(blocked[:10])
    )


def test_incident_disjunction_root_is_blocked_against_its_frame():
    """TP pin: the #656 turn-6 root — a disjunction of the case's two ACTIVE
    hypotheses restating the symptom — is blocked against its real frame."""
    case = _case(
        "Intermittent 502 errors under load",
        hyp_statements=[
            "Transient network congestion",
            "Resource contention on the backend",
        ],
    )
    root = _root(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors"
    )
    assert root_restates_case_frame(root, case) is True


def test_verbatim_symptom_as_cause_is_blocked():
    case = _case("Kafka consumer group lag keeps increasing")
    root = _root("Kafka consumer group lag increasing")
    assert root_restates_case_frame(root, case) is True


def test_known_boundary_cause_contaminated_anchor_blocks_prenamed_cause():
    """KNOWN BOUNDARY (documented, not endorsed — #661 gate, pg-postmortem run):
    when the PROBLEM framing itself embeds the cause (a postmortem user opens
    with 'the audit_events table is missing an index, causing ...'), the case
    frame contains the true cause's tokens, so a root restating that
    user-pre-named cause has ~zero novelty and the guard holds it INCONCLUSIVE
    even when real evidence supports it. This pins the CURRENT behavior so any
    future change is conscious. If live runs show this shape actually reaching
    would-validate and being blocked, the anchor definition (symptom-shaped
    text only) is the tuning lever — tracked on #656/#661."""
    case = _case(
        "The audit_events table is missing an index on created_at, causing "
        "pool exhaustion and request timeouts"
    )
    root = _root("audit_events table missing index on created_at")
    assert root_restates_case_frame(root, case) is True  # blocked — the boundary


def test_known_limit_filler_padding_escapes():
    """KNOWN LIMIT (documented, not endorsed): novelty is lexical, so padding a
    pure restatement with generic filler tokens crosses the bar — the incident
    root scores 0.11 (blocked) but the same text + 'in the production
    environment cluster' scores ~0.33 and passes. No lexical bar closes this
    (filler tokens ARE novel); the semantic layers (multi-support validation,
    grade caps) tracked on #656 own it. Pinned so the escape is a conscious,
    visible property rather than a surprise."""
    case = _case(
        "Intermittent 502 errors under load",
        hyp_statements=[
            "Transient network congestion",
            "Resource contention on the backend",
        ],
    )
    padded = _root(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors in the production environment cluster"
    )
    assert root_restates_case_frame(padded, case) is False  # escapes — known limit


def test_dilution_fp_bounded_under_realistic_sibling_frames():
    """Frame-dilution bound: with the HARSHEST realistic frame — every sibling
    cause of the same runbook standing simultaneously as unattached hypotheses
    — the corpus FP rate stays ≤ 2% (measured 1.1%, dense-vocabulary IAM/RBAC
    tails). Under the entry-bar semantics this is an upper bound on validation
    DELAY, not a permanent block: an already-validated root is ruled by
    evidence alone, and the frame shrinks as siblings are refuted/retired."""
    from faultmaven.core.investigation.causal_graph import (
        _content_tokens,
        _node_restates,
    )

    pack = json.loads(PACK_JSON.read_text(encoding="utf-8"))
    blocked = checked = 0
    for rb in pack["runbooks"]:
        anchors = _content_tokens(rb["title"])
        causes = [
            c
            for c in rb.get("causes", [])
            if not c.get("is_fallback_cause") and c.get("cause_statement")
        ]
        for i, cause in enumerate(causes):
            st = _content_tokens(cause["cause_statement"])
            if not st:
                continue
            siblings = [
                (None, _content_tokens(o["cause_statement"]))
                for j, o in enumerate(causes)
                if j != i
            ]
            checked += 1
            if _node_restates(st, "cn_under_test", anchors, siblings):
                blocked += 1
    assert checked > 400
    assert (
        blocked / checked <= 0.02
    ), f"dilution FP regressed: {blocked}/{checked} ({blocked/checked:.1%})"


# ---------------------------------------------------------------------------
# fm#1137 — the guard's known limit, and the #656 TP that bounds any fix
# ---------------------------------------------------------------------------


def test_unattached_duplicate_no_longer_frames_its_own_root():
    """fm#1122 (was the fm#1137 KNOWN LIMIT). A hypothesis that DUPLICATES this
    root's own cause, left standing and unattached, used to enter the root's
    frame and hold it there permanently — a true duplicate is never refuted or
    retired, so the hold never lifted. In the live incident that cost nine turns
    on a root carrying three confident independent causal supports (novelty 1/9
    against a 0.30 bar) and ended in ``cause_assurance=no_root`` with a NULL
    ``root_cause_conclusion``.

    Released by the §7.1 ATTRIBUTION test: the problem anchors plus this ONE
    hypothesis already account for everything the root says, so the two are the
    same claim worded twice, not a root restating the case frame. The #656
    shape is untouched because no single disjunct can account for a disjunction
    (next test)."""
    case = _case(
        "The production payment-processor deployment in the `payments` "
        "namespace is currently unavailable or unstable because its v2.1.4 "
        "pods enter CrashLoopBackOff after 2-3 minutes, causing customer "
        "payment failures.",
        hyp_statements=[
            "The v2.1.4 JVM configuration sets a 512MB maximum heap inside a "
            "400Mi container, leaving insufficient headroom for JVM "
            "native/non-heap memory; total RSS reaches the cgroup limit, the "
            "kernel kills the process with SIGKILL/exit 137, and Kubernetes "
            "restarts it into CrashLoopBackOff."
        ],
    )
    root = _root(
        "JVM heap and native/non-heap memory exceed the 400Mi container " "cgroup limit"
    )
    assert root_restates_case_frame(root, case) is False  # released


def test_656_disjunction_root_stays_blocked_against_verbose_siblings():
    """REGRESSION PIN — the bound on any fm#1137 fix. The #656 TP must survive
    siblings written at the length real hypotheses are written at. A
    disjunction root is CONTAINED IN each verbose sibling (0.667 here) exactly
    as each terse sibling is contained in the root, so a one-way-containment
    ownership arm in EITHER direction releases the incident shape. An earlier
    cut of fm#1137 shipped one and this fixture is what caught it."""
    case = _case(
        "Intermittent 502 errors under load",
        hyp_statements=[
            "Transient network congestion between the ingress controller and "
            "the backend pods causes intermittent 502 errors whenever request "
            "volume rises under load",
            "Resource contention on the backend host - CPU and memory pressure "
            "from co-tenant workloads - causes intermittent 502 errors under "
            "load",
        ],
    )
    root = _root(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors"
    )
    from faultmaven.core.investigation.causal_graph import _content_tokens

    st = _content_tokens(root.statement)
    covers = [
        len(st & _content_tokens(h.statement)) / len(st)
        for h in case.hypotheses.values()
    ]
    assert max(covers) >= 0.6, f"fixture no longer exercises containment: {covers}"
    assert root_restates_case_frame(root, case) is True


# ---------------------------------------------------------------------------
# fm#1122 — the ATTRIBUTION test, and the #656 shapes that bound it
#
# Every pin below was verified by mutation: the production predicate was broken
# in a scratch copy and each pin confirmed to fail. The mutations and what each
# one killed are recorded in the fm#1122 PR body.
#
# Each pin asserts its own PREMISES first. A pin on a guard verdict passes just
# as well when the fixture stopped exercising the arm under test — the sibling
# count drifted to zero (``all()`` over an empty set is vacuously true), or the
# anchors alone came to cover the root so the verdict comes from the anchors
# clause instead. Both would pin nothing while staying green.
# ---------------------------------------------------------------------------


def _assert_sibling_held_premises(case, root, *, attached: int, unattached: int):
    """Premises shared by the sibling-held #656 pins: the frame really has the
    elements the fixture intends, in the intended attachment state, and the
    anchors alone do NOT account for the root — so a True verdict can only be
    coming from the sibling arm."""
    from faultmaven.core.investigation.causal_graph import (
        ROOT_NOVELTY_MIN_FRACTION,
        _content_tokens,
    )

    standing = [h for h in case.hypotheses.values() if h.statement]
    got_attached = sum(1 for h in standing if h.root_node_id)
    got_unattached = sum(1 for h in standing if not h.root_node_id)
    assert (got_attached, got_unattached) == (attached, unattached), (
        f"fixture drifted: expected {attached} attached / {unattached} "
        f"unattached siblings, got {got_attached}/{got_unattached}"
    )
    assert got_attached + got_unattached >= 2, "not a multi-sibling frame"
    st = _content_tokens(root.statement)
    anchors = _content_tokens(case.problem_verification.symptom_statement)
    assert len(st - anchors) / len(st) >= ROOT_NOVELTY_MIN_FRACTION, (
        "fixture no longer exercises the sibling arm: the anchors alone leave "
        "this root non-novel, so it would be held by the anchors clause"
    )


def test_attached_sibling_rooted_elsewhere_no_longer_holds_a_real_root():
    """fm#1122 population pin — distilled VERBATIM from ``case_b2033be22a6d``
    (dev corpus), one of the five real ``would_validate AND restating`` holds.

    The pollutant is not the unattached duplicate of the fm#1137 write-up: it is
    a sibling hypothesis ANCHORED TO ITS OWN ROOT, which ``_node_restates`` used
    to admit unconditionally. The held root carries a real mechanism the frame
    cannot see ("a code change causing connection leaks") and scored 2/9 = 0.222
    novel; on the pre-fix engine that case derived ``cause_assurance=no_root``
    with zero VALIDATED roots.

    Released because the anchors plus that ONE sibling chain account for the
    whole of the frame's coverage — residue ``{code, leak}`` either way."""
    case = _case(
        "checkout-service is experiencing 500 errors due to Postgres connection "
        "pool exhaustion following the 14:00 deployment.",
        chains=[
            (
                "14:00 deployment introduced a change causing connection pool "
                "exhaustion",
                [
                    "14:00 deployment introduced a change causing connection "
                    "pool exhaustion"
                ],
            )
        ],
    )
    root = _root(
        "14:00 deployment introduced a code change causing connection leaks in "
        "checkout-service"
    )
    # Premises, asserted rather than assumed. ``all()`` over an empty dict is
    # vacuously true, so the COUNT is asserted first; and a root the anchors
    # alone already fail to cover would be released for the wrong reason.
    from faultmaven.core.investigation.causal_graph import (
        ROOT_NOVELTY_MIN_FRACTION,
        _content_tokens,
    )

    assert len(case.hypotheses) == 1
    assert all(h.root_node_id for h in case.hypotheses.values())
    st = _content_tokens(root.statement)
    anchors = _content_tokens(case.problem_verification.symptom_statement)
    assert len(st - anchors) / len(st) >= ROOT_NOVELTY_MIN_FRACTION, (
        "fixture no longer exercises the sibling arm: the anchors alone leave "
        "this root non-novel, so it would be held by the anchors clause"
    )
    assert root_restates_case_frame(root, case) is False  # released


def test_656_disjunction_root_blocked_when_disjuncts_are_anchored():
    """REGRESSION PIN — the #656 TP in the shape a LIVE case actually has.

    Every pre-existing #656 fixture builds its siblings UNATTACHED, but
    ``templates.py`` mandates ``root_node_ref`` on every hypothesis, so in a real
    case the disjuncts carry roots of their own. A fix that keyed on attachment
    (excluding attached-elsewhere siblings from the frame) would pass every
    shipped pin and gut the guard here — fm#1140 attempt 2, in a shape no
    fixture covered. The disjunction must stay blocked."""
    case = _case(
        "Intermittent 502 errors under load",
        chains=[
            (
                "Transient network congestion between ingress and the backend pods",
                [
                    "Transient network congestion between the ingress controller "
                    "and the backend pods causes intermittent 502 errors whenever "
                    "request volume rises under load"
                ],
            ),
            (
                "Resource contention on the backend host from co-tenant workloads",
                [
                    "Resource contention on the backend host - CPU and memory "
                    "pressure from co-tenant workloads - causes intermittent 502 "
                    "errors under load"
                ],
            ),
        ],
    )
    root = _root(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors"
    )
    _assert_sibling_held_premises(case, root, attached=2, unattached=0)
    assert root_restates_case_frame(root, case) is True


def test_656_disjunction_root_blocked_with_one_disjunct_refused():
    """REGRESSION PIN — the mixed shape fm#1140 attempt 2 died on: one disjunct
    attaches, the second's ``root_node_ref`` is refused by the fm#1091
    one-cause-one-chain guard and it stands unattached. Still a disjunction,
    still blocked."""
    case = _case(
        "Intermittent 502 errors under load",
        hyp_statements=[
            "Resource contention on the backend host - CPU and memory pressure "
            "from co-tenant workloads - causes intermittent 502 errors under load"
        ],
        chains=[
            (
                "Transient network congestion between ingress and the backend pods",
                [
                    "Transient network congestion between the ingress controller "
                    "and the backend pods causes intermittent 502 errors whenever "
                    "request volume rises under load"
                ],
            )
        ],
    )
    root = _root(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors"
    )
    _assert_sibling_held_premises(case, root, attached=1, unattached=1)
    assert root_restates_case_frame(root, case) is True


def test_three_way_disjunction_root_stays_blocked():
    """The attribution test gets STRONGER as a disjunction widens — three
    disjuncts leave each single sibling further short of the union."""
    case = _case(
        "Intermittent 502 errors under load",
        hyp_statements=[
            "Transient network congestion on the ingress path",
            "Resource contention on the backend host",
            "TLS handshake failure at the upstream connection",
        ],
    )
    root = _root(
        "Transient network congestion, resource contention, or a TLS handshake "
        "failure causing intermittent 502 errors"
    )
    _assert_sibling_held_premises(case, root, attached=0, unattached=3)
    assert root_restates_case_frame(root, case) is True


def test_attribution_is_set_equality_not_a_second_novelty_threshold():
    """BOUND on the attribution test's SHAPE. Asking "does one sibling leave the
    root under the novelty bar" instead of "does one sibling account for
    everything the frame accounts for" releases a lopsided disjunction: here the
    dominant disjunct alone leaves 3/13 = 0.231 novel, under the 0.30 bar, while
    the union leaves nothing. Set equality holds it; a second threshold would
    not. fm#1140 records seven wrong implementations that passed the whole
    suite — this is the one that looks right."""
    from faultmaven.core.investigation.causal_graph import (
        _FRAME_OWNER_JACCARD,
        ROOT_NOVELTY_MIN_FRACTION,
        _content_tokens,
        _mutual_mirror,
    )

    dominant = (
        "Connection pool exhaustion in checkout-service under peak load causing "
        "checkout timeouts, because the HikariCP maximum pool size is smaller "
        "than the concurrent request fan-out from the storefront"
    )
    case = _case(
        "Checkout timeouts under load",
        hyp_statements=[dominant, "A stale DNS entry"],
    )
    root = _root(
        "Connection pool exhaustion in checkout-service under peak load or a "
        "stale DNS entry causing checkout timeouts"
    )
    st = _content_tokens(root.statement)
    anchors = _content_tokens(case.problem_verification.symptom_statement)
    dom = _content_tokens(dominant)
    # The dominant disjunct must stay IN the frame — if it mirrors the root it
    # is excluded as a presumptive owner and the fixture tests nothing.
    assert not _mutual_mirror(st, dom, _FRAME_OWNER_JACCARD)
    solo = len(st - (anchors | dom)) / len(st)
    assert solo < ROOT_NOVELTY_MIN_FRACTION, (
        f"fixture no longer discriminates: the dominant disjunct leaves "
        f"{solo:.3f} novel, so a fraction-based attribution test would not "
        f"release it either"
    )
    assert root_restates_case_frame(root, case) is True


def test_symptom_restatement_is_not_excused_by_a_covering_sibling():
    """BOUND on the attribution test's ORDER. The problem anchors are a claim
    the case makes on their own: a root they alone cover is the symptom dressed
    as a cause, and a sibling that happens to cover it too must not excuse it.
    Without the unconditional anchors arm, ANY standing hypothesis would release
    every verbatim-symptom root — once the anchors already cover everything, a
    sibling's residue trivially equals the union's."""
    case = _case(
        "Kafka consumer group lag keeps increasing",
        hyp_statements=[
            "A slow downstream consumer is falling behind the produce rate on "
            "the order-processing topic, so Kafka consumer group lag keeps "
            "increasing"
        ],
    )
    root = _root("Kafka consumer group lag increasing")
    # Premises. The pin is about the ORDER of the two arms, so it means nothing
    # unless (a) a sibling is actually standing and (b) the anchors alone
    # already account for the root — which is what makes the sibling able to
    # excuse it if the anchors arm is dropped.
    from faultmaven.core.investigation.causal_graph import (
        ROOT_NOVELTY_MIN_FRACTION,
        _content_tokens,
    )

    assert len(case.hypotheses) == 1
    st = _content_tokens(root.statement)
    anchors = _content_tokens(case.problem_verification.symptom_statement)
    assert (
        len(st - anchors) / len(st) < ROOT_NOVELTY_MIN_FRACTION
    ), "fixture no longer exercises the anchors arm"
    (sibling,) = case.hypotheses.values()
    sib = _content_tokens(sibling.statement)
    assert not (st - (anchors | sib)), (
        "fixture no longer discriminates: with the anchors arm removed the "
        "attribution loop must be able to attribute the hold to this sibling"
    )
    assert root_restates_case_frame(root, case) is True


def test_attribution_only_releases_never_holds():
    """SAFETY INVARIANT. The attribution test is a conjunct ADDED to the novelty
    test, so it can only release a root the plain frame held — never hold a new
    one. That is what keeps the FP pin at 0 and the dilution bound at <= 2% by
    construction rather than by measurement, and what bounds the blast radius of
    any future edit to it. Swept over the whole shipped corpus under the
    harshest realistic frame."""
    from faultmaven.core.investigation.causal_graph import (
        ROOT_NOVELTY_MIN_FRACTION,
        _content_tokens,
        _node_restates,
    )

    pack = json.loads(PACK_JSON.read_text(encoding="utf-8"))
    checked = held = plain_held = 0
    for rb in pack["runbooks"]:
        anchors = _content_tokens(rb["title"])
        causes = [
            c
            for c in rb.get("causes", [])
            if not c.get("is_fallback_cause") and c.get("cause_statement")
        ]
        for i, cause in enumerate(causes):
            st = _content_tokens(cause["cause_statement"])
            if not st:
                continue
            siblings = [
                (None, _content_tokens(o["cause_statement"]))
                for j, o in enumerate(causes)
                if j != i
            ]
            frame = set(anchors)
            for _rid, tokens in siblings:
                frame |= tokens
            plain = bool(frame) and (
                len(st - frame) / len(st) < ROOT_NOVELTY_MIN_FRACTION
            )
            checked += 1
            plain_held += plain
            if _node_restates(st, "cn_under_test", anchors, siblings):
                held += 1
                assert plain, (
                    "attribution HELD a root the plain frame test released: "
                    f"{cause['cause_statement'][:80]}"
                )
    # DENOMINATORS. The implication above is vacuously true over an empty set,
    # so the sweep must be shown to have run AND the predicate to have actually
    # fired. Without these, a harness that loaded no causes — or a guard that
    # never held anything — would pin nothing and stay green.
    assert checked > 400, f"sweep did not run: {checked} causes"
    assert plain_held > 0, "the plain frame test never held anything to compare"
    assert held > 0, "the guard never fired: the implication is vacuous"
    # The corpus's unattached same-runbook frames are the shape attribution was
    # NOT built for, so it releases nothing here (measured 0). That is worth
    # recording rather than hiding: this sweep proves the SAFETY direction only,
    # and a mutation to the release condition is invisible to it. The release
    # direction is pinned on real cases instead (the population pins above).
    released = plain_held - held
    assert released == 0, (
        f"attribution released {released} corpus roots; this sweep is the "
        "safety-direction pin and its release count is a documented 0 — a "
        "change here needs the release pins re-measured, not this assert moved"
    )


def test_a_roots_own_hypothesis_cannot_attribute_the_frame_away():
    """BOUND on the attribution test's INPUT. The loop must run over the FRAME's
    elements, not over every standing hypothesis: the root's OWN attached
    hypothesis is excluded from the frame precisely because it is not "other",
    and letting it account for the frame's coverage is the fm#1140 attempt-2
    collapse — "the first attaches (excluded as owner) ... the frame collapses
    to the problem anchors, and the disjunction validates".

    Here the disjunction root carries an owner hypothesis narrating BOTH
    alternatives. Its solo residue is exactly the frame's ({caus}), so a loop
    over the raw hypothesis list releases the #656 TP; a loop over the frame's
    elements does not. The owner deliberately does NOT mutually mirror the root,
    so the pin tests the attribution loop rather than the presumptive-owner
    arm."""
    from faultmaven.core.investigation.causal_graph import (
        _FRAME_OWNER_JACCARD,
        _content_tokens,
        _mutual_mirror,
    )

    root = _root(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors"
    )
    owner_stmt = (
        "Either transient network congestion or resource contention is what "
        "causes the intermittent 502 errors seen under load"
    )
    case = _case(
        "Intermittent 502 errors under load",
        hyp_statements=[
            "Transient network congestion between the ingress controller and "
            "the backend pods causes intermittent 502 errors whenever request "
            "volume rises under load",
            "Resource contention on the backend host - CPU and memory pressure "
            "from co-tenant workloads - causes intermittent 502 errors under load",
        ],
    )
    # Attach the owner to the root under test.
    case.causal_nodes[root.node_id] = root
    owner = Hypothesis(
        statement=owner_stmt,
        category=HypothesisCategory.OTHER,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="posited",
        generated_at_turn=1,
        root_node_id=root.node_id,
    )
    case.hypotheses[owner.hypothesis_id] = owner

    st = _content_tokens(root.statement)
    ot = _content_tokens(owner_stmt)
    assert not _mutual_mirror(st, ot, _FRAME_OWNER_JACCARD), (
        "fixture drifted onto the presumptive-owner arm instead of the "
        "attribution loop"
    )
    anchors = _content_tokens(case.problem_verification.symptom_statement)
    frame = set(anchors)
    for h in case.hypotheses.values():
        if h.root_node_id != root.node_id:
            frame |= _content_tokens(h.statement)
    assert (st - (anchors | ot)) == (st - frame), (
        "fixture no longer discriminates: the owner's solo residue must equal "
        "the frame's, or an owner-leaking attribution loop would not release it"
    )
    assert root_restates_case_frame(root, case) is True


def test_population_pin_multi_element_attached_frame():
    """fm#1122 POPULATION PIN — distilled verbatim from ``case_c26d7905f26d``
    (dev corpus), the multi-element member of the five real holds.

    Deliberately NOT the single-sibling shape. With exactly one frame element
    ``frame == anchors | tokens``, so ``statement_tokens - (anchors | tokens)
    == residue`` is a TAUTOLOGY and the attribution loop releases under set
    equality, subset, and a second-threshold implementation alike — such a pin
    cannot discriminate the rule it is supposed to pin. Here TWO standing
    hypotheses sit on a near-duplicate sibling root, so the residue test has to
    pick the one that actually accounts for the frame, and the subsumption
    condition has to find the other one covered by it.

    On the pre-fix engine this case derived ``cause_assurance=no_root`` with
    zero VALIDATED roots."""
    from faultmaven.core.investigation.causal_graph import (
        _content_tokens,
        _node_restates,
    )

    symptom = (
        "The production PostgreSQL primary is currently rejecting new "
        "connections with a connection-limit error, and the web tier is "
        "failing writes with 500s during ongoing business-hours traffic."
    )
    sib_a = (
        "etl-reporting's direct PostgreSQL pool is exhausting available server "
        "connections"
    )
    sib_b = (
        "Aggregate client pools exceed max_connections because etl-reporting "
        "opens a direct PostgreSQL pool instead of using PgBouncer"
    )
    case = _case(
        symptom,
        chains=[
            (
                "etl-reporting opens a large direct connection pool to the "
                "PostgreSQL primary",
                [sib_a, sib_b],
            )
        ],
    )
    root = _root("etl-reporting opens a direct PostgreSQL pool that bypasses PgBouncer")
    # Its own two hypotheses, as the real graph has them (owner-excluded).
    case.causal_nodes[root.node_id] = root
    for stmt in (sib_a, sib_b):
        h = Hypothesis(
            statement=stmt,
            category=HypothesisCategory.OTHER,
            state=HypothesisState.ACTIVE,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="posited",
            generated_at_turn=1,
            root_node_id=root.node_id,
        )
        case.hypotheses[h.hypothesis_id] = h

    # PREMISES. The frame must really carry TWO elements, or this is the
    # single-element tautology again and pins nothing.
    st = _content_tokens(root.statement)
    anchors = _content_tokens(symptom)
    elements = [
        _content_tokens(h.statement)
        for h in case.hypotheses.values()
        if h.root_node_id != root.node_id
    ]
    assert len(elements) == 2, f"not a multi-element frame: {len(elements)}"
    frame = set(anchors)
    for e in elements:
        frame |= e
    residue = st - frame
    solo = [st - (anchors | e) for e in elements]
    assert sum(r == residue for r in solo) == 1, (
        "fixture no longer discriminates: exactly one element must account for "
        f"the frame's coverage, got {sum(r == residue for r in solo)}"
    )
    assert any(r != residue for r in solo), "the second element is inert"
    assert (
        _node_restates(st, root.node_id, anchors, [(None, e) for e in elements])
        is False
    )
    assert root_restates_case_frame(root, case) is False  # released


def test_656_disjunction_blocked_when_the_anchor_pre_names_a_disjunct():
    """REGRESSION PIN — the #656 boundary crossed with a CONTAMINATED anchor.

    The attribution test subtracts the anchors before asking whether one
    sibling accounts for the root. When the problem statement itself pre-names
    a disjunct (the #661 contaminated-anchor class — 6 of the 7 roots still
    held in the dev corpus), that subtraction DELETES the disjunct, and the
    remaining fragment is covered by the other disjunct alone: a distributed
    aggregation presents as attributable-to-one and the #656 root releases.

    Caught by review, reproduced on both trees: ``main`` HELD, the first cut of
    this fix RELEASED. Held now by the two conditions read off the root's FULL
    token set — no rival may contribute root content the attributing claim
    lacks, and the attributing claim must out-cover the problem statement."""
    from faultmaven.core.investigation.causal_graph import (
        ROOT_NOVELTY_MIN_FRACTION,
        _content_tokens,
    )

    case = _case(
        "Intermittent 502 errors under load, possibly from transient network "
        "congestion",
        hyp_statements=[
            "Transient network congestion",
            "Resource contention on the backend",
        ],
    )
    root = _root(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors"
    )
    # PREMISE: the anchors arm must NOT be what holds it, or the pin tests the
    # wrong arm and the regression walks straight back in.
    st = _content_tokens(root.statement)
    anchors = _content_tokens(case.problem_verification.symptom_statement)
    assert len(st - anchors) / len(st) >= ROOT_NOVELTY_MIN_FRACTION, (
        "fixture drifted into the anchors arm; it no longer exercises "
        "attribution under a contaminated anchor"
    )
    assert root_restates_case_frame(root, case) is True


def test_656_disjunction_blocked_when_the_anchor_pre_names_the_only_rival():
    """The same boundary with NO rival standing for the pre-named disjunct, so
    the subsumption condition is vacuous and only the principal-source
    condition can hold it. Without that condition this releases."""
    from faultmaven.core.investigation.causal_graph import (
        ROOT_NOVELTY_MIN_FRACTION,
        _content_tokens,
    )

    case = _case(
        "Intermittent 502 errors under load, possibly from transient network "
        "congestion",
        hyp_statements=["Resource contention on the backend"],
    )
    root = _root(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors"
    )
    st = _content_tokens(root.statement)
    anchors = _content_tokens(case.problem_verification.symptom_statement)
    assert len(case.hypotheses) == 1, "the subsumption arm must be vacuous here"
    assert len(st - anchors) / len(st) >= ROOT_NOVELTY_MIN_FRACTION
    assert len(st & anchors) > len(
        st & _content_tokens("Resource contention on the backend")
    ), "fixture no longer exercises the principal-source condition"
    assert root_restates_case_frame(root, case) is True


def test_656_disjunction_blocked_under_contaminated_anchor_with_verbose_rivals():
    """The contaminated anchor at realistic sibling length — the shape where the
    principal-source condition alone is not enough and subsumption carries it."""
    case = _case(
        "Intermittent 502 errors under load, possibly from transient network "
        "congestion",
        hyp_statements=[
            "Transient network congestion between the ingress controller and "
            "the backend pods causes intermittent 502 errors whenever request "
            "volume rises under load",
            "Resource contention on the backend host - CPU and memory pressure "
            "from co-tenant workloads - causes intermittent 502 errors under load",
        ],
    )
    root = _root(
        "Transient network congestion or resource contention causing "
        "intermittent 502 errors"
    )
    assert root_restates_case_frame(root, case) is True


def test_known_limit_lopsided_disjunction_escapes():
    """KNOWN LIMIT, pinned as CURRENT behaviour on BOTH trees (documented in
    §7.1 alongside filler padding, and NOT introduced by the attribution test).

    A disjunction with one dominant disjunct escapes through the PRE-EXISTING
    presumptive-owner arm: the dominant sibling mutually mirrors the root at
    0.647 against ``_FRAME_OWNER_JACCARD`` 0.6, so it is excluded from the frame
    as the root's own not-yet-attached hypothesis, and what remains ("a config
    bug") leaves the root 0.643 novel. Attribution is never consulted. Pinned
    with that mechanism asserted, so the pin cannot silently start passing for a
    different reason."""
    from faultmaven.core.investigation.causal_graph import (
        _FRAME_OWNER_JACCARD,
        ROOT_NOVELTY_MIN_FRACTION,
        _content_tokens,
        _mutual_mirror,
        _node_restates,
    )

    case = _case(
        "Intermittent 502 errors under load",
        hyp_statements=[
            "Transient network congestion between the ingress controller and "
            "the backend pods causes intermittent 502 errors under load",
            "A config bug",
        ],
    )
    root = _root(
        "Transient network congestion between the ingress controller and the "
        "backend pods, or a config bug, causing intermittent 502 errors"
    )
    st = _content_tokens(root.statement)
    anchors = _content_tokens(case.problem_verification.symptom_statement)
    elements = [_content_tokens(h.statement) for h in case.hypotheses.values()]
    mirrored = [e for e in elements if _mutual_mirror(st, e, _FRAME_OWNER_JACCARD)]
    assert len(mirrored) == 1, (
        "fixture no longer escapes through the presumptive-owner arm: "
        f"{len(mirrored)} of {len(elements)} siblings mirror the root"
    )
    frame = set(anchors)
    for e in elements:
        if e not in mirrored:
            frame |= e
    assert len(st - frame) / len(st) >= ROOT_NOVELTY_MIN_FRACTION, (
        "the surviving frame no longer leaves the root novel, so this is not "
        "the documented escape any more"
    )
    assert _node_restates(st, "cn_x", anchors, [(None, e) for e in elements]) is False
    assert root_restates_case_frame(root, case) is False  # escapes — known limit
