from __future__ import annotations

from typing import TYPE_CHECKING

from faultmaven.core.investigation.cause_assurance import (
    CONFIRMED_RCC_LIKELIHOOD_FLOOR,
    MECHANISTIC_RCC_LIKELIHOOD,
    root_counterfactually_confirmed,
)
from faultmaven.core.investigation.cause_assurance import (
    ENGINE_RCC_AUTHOR as _ENGINE_RCC_AUTHOR,
)
from faultmaven.core.investigation.cause_assurance import (
    evidence_category_map as _evidence_category_map,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    llm_rcc_cause_linked_total,
    llm_rcc_cause_named_total,
    llm_rcc_retracted_disconfirmed_total,
    rcc_precedence_inversion_total,
)
from faultmaven.modules.case.contracts import (
    ConfidenceLevel,
    EvidenceStance,
    HypothesisState,
    RootCauseConclusion,
)

from .clusters import mece_contested_root_ids
from .projection import _STANDING_HYP_STATES, _standing_hypotheses
from .queries import conjuncts_for_chain, is_chain_root_validated, mechanism_for_chain
from .similarity import (
    RESTATEMENT_AMBIGUOUS,
    RESTATEMENT_STRONG,
    _substantive_overlap,
    restatement_score,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, Hypothesis


def _net_refuted(hyp: Hypothesis) -> bool:
    """Decisive-disconfirmation test for M6: refuting evidence at least matches
    supporting evidence (and there is at least one refuting link).

    A *lone* refuting link among a body of supporting evidence is NOT decisive —
    treating it as such tears down a legitimately grounded cause that merely
    attracted one contrary data point during search (a real false-demotion risk).
    A refutation that flips or ties the support balance is. With no links at all
    this is False (absence of evidence is not disconfirmation).
    """
    refuting = sum(
        1 for link in hyp.evidence_links if link.stance == EvidenceStance.REFUTES
    )
    if refuting == 0:
        return False
    supporting = sum(
        1 for link in hyp.evidence_links if link.stance == EvidenceStance.SUPPORTS
    )
    return refuting >= supporting


def _representative_cause_hypothesis(case: Case) -> Hypothesis | None:
    """The hypothesis that best represents the currently-grounded cause.

    When the conclusion names one (``validated_hypothesis_id``) that is
    authoritative. Otherwise the cause was grounded case-wide (high likelihood +
    causal evidence / ``evidence_basis``) WITHOUT naming a hypothesis — the
    common grounding shape, and the one in case_e970a5c24fe1 (``likelihood`` 1.0,
    ``validated_hypothesis_id`` null). The engine's best proxy is then the
    strongest hypothesis by ORIGINAL confidence: ``initial_likelihood`` is stable
    where ``likelihood`` is not — refutation zeroes ``likelihood``, so a
    just-refuted believed-cause would vanish from a max-by-``likelihood`` pick.
    """
    rcc = case.root_cause_conclusion
    vhid = getattr(rcc, "validated_hypothesis_id", None) if rcc else None
    if vhid:
        return case.hypotheses.get(vhid)
    if not case.hypotheses:
        return None
    return max(case.hypotheses.values(), key=lambda h: h.initial_likelihood)


def _hypothesis_disconfirmed(hyp: Hypothesis) -> bool:
    """Hypothesis-side disconfirmation: REFUTED, or net-refuted (refuting
    evidence at least matches support, ``_net_refuted``). The single definition
    shared by the M6 trigger and the RCC retraction below so the two cannot
    drift; the M6 trigger layers a node-side counterfactual clause on top."""
    return hyp.state == HypothesisState.REFUTED or _net_refuted(hyp)


def retract_disconfirmed_rcc(case: Case) -> bool:
    """Clear a ``RootCauseConclusion`` whose NAMED cause has been disconfirmed, at
    the SOURCE — so a disproven cause is asserted by NO consumer (terminal gate,
    report, copilot UI, KB-runbook conversion), not just one guarded reader.
    Returns True if it retracted one.

    Link-based ONLY — it acts solely on the RCC's explicit ``validated_hypothesis_id``
    cause link. It deliberately does NOT infer the cause from a likelihood proxy:
    ``_representative_cause_hypothesis``'s ``max(initial_likelihood)`` fallback would
    refuse a valid RCC whenever an unrelated early-refuted alternative dominated
    (a NO-COLLAPSE stall), since ``initial_likelihood`` never decays. This
    complements the M6 retraction (``demote_disconfirmed_cause_via_evidence``,
    gated on ``cause_state=IDENTIFIED``) by covering the gap where an RCC's
    hypothesis is refuted but no chain root ever validated — so cause_state never
    reached IDENTIFIED and M6's gate never fired. A free-text RCC with no
    ``validated_hypothesis_id`` (no reliable cause link) is left untouched — a
    documented residual, not a guess.
    """
    rcc = case.root_cause_conclusion
    vhid = getattr(rcc, "validated_hypothesis_id", None) if rcc else None
    if not vhid:
        return False
    hyp = case.hypotheses.get(vhid)
    if hyp is not None and _hypothesis_disconfirmed(hyp):
        # §7.6 / INV-34: an LLM conclusion linked to its cause (link_llm_rcc_to_cause)
        # is retracted here just like an engine one when that cause is disconfirmed.
        if getattr(rcc, "determined_by", None) != _ENGINE_RCC_AUTHOR:
            llm_rcc_retracted_disconfirmed_total.inc()
        case.root_cause_conclusion = None
        return True
    return False


def link_llm_rcc_to_cause(case: Case) -> bool:
    """§7.6 / INV-34 + §7.7 / INV-35 — attribute an LLM-authored
    ``RootCauseConclusion`` to the STANDING hypothesis it names, so the link-based
    retraction lifecycle (``retract_disconfirmed_rcc``, the M6
    ``_representative_cause_hypothesis`` pick) can reach it.

    An LLM conclusion arrives with ``validated_hypothesis_id`` unset, so without a
    link a disconfirmed conclusion lingers and M6's max-``initial_likelihood`` proxy
    can wipe a re-grounded one that names a DIFFERENT live cause. Two tiers, both
    guarded by the SAME trust discipline — a STANDING hypothesis (never a refuted or
    retired one) with substantive shared-token overlap — so neither can attribute a
    conclusion to a refuted, retired, or textually-unrelated cause. That guard is
    load-bearing: a wrong link would let ``retract_disconfirmed_rcc`` wipe a valid,
    just-authored conclusion on an unrelated refutation (a NO-COLLAPSE breach).

      * **Tier 1 — authoritative (INV-35).** When the LLM named its cause's root
        node (``names_root_node_id``), pick the SINGLE standing hypothesis rooted
        there — exact identity, no lexical ambiguity resolution — and confirm the
        conclusion text substantively overlaps it (a coherence rail against a
        stale or mis-copied id). Counts ``llm_rcc_cause_named_total``.
      * **Tier 2 — lexical fallback (INV-34).** For an id-less conclusion (older
        turns, a same-turn root the LLM has no ``cn_`` id for yet, a non-compliant
        model), scan standing hypotheses by ``restatement_score`` (orphan-chain T1
        discipline): link only when EXACTLY ONE clears ``RESTATEMENT_STRONG`` (so it
        is the only one ``>= AMBIGUOUS``) with substantive overlap — "when unsure,
        don't link". Counts ``llm_rcc_cause_linked_total``.

    Giving the conclusion a cause link is NOT authorship — the engine never re-words
    the LLM's ``root_cause`` text (``determined_by`` stays the LLM's; the mirror
    synthesis may REPLACE the whole conclusion when a validated root outranks it,
    §7.7, but it never edits the LLM's prose in place). It only
    records which standing hypothesis the LLM's stated cause corresponds to. An
    unattributable conclusion stays the documented residual it has always been (no
    regression). A conclusion already linked to a PRESENT hypothesis is left stable
    (membership not liveness — a REFUTED linked hypothesis retains the link so
    ``retract_disconfirmed_rcc`` acts this recompute); a stale/dangling link is
    cleared before re-resolving. Returns True if it wrote a link.
    """
    rcc = case.root_cause_conclusion
    if rcc is None or getattr(rcc, "determined_by", None) == _ENGINE_RCC_AUTHOR:
        return False
    current = getattr(rcc, "validated_hypothesis_id", None)
    if current and current in case.hypotheses:
        return False
    # A dangling link (points at a hypothesis no longer present) is cleared before
    # we re-resolve: left set, it would make ``_representative_cause_hypothesis``
    # return None (the ``if vhid`` branch short-circuits the max-likelihood
    # fallback) and silently disable the M6 demotion for this case. A failed
    # re-resolve below then correctly leaves it None (proxy fallback restored).
    if current:
        rcc.validated_hypothesis_id = None
    statement = getattr(rcc, "root_cause", None) or ""
    if not statement:
        return False
    standing = list(_standing_hypotheses(case))

    # Tier 1 — authoritative: the LLM named its cause's root node. Restrict to a
    # SINGLE standing hypothesis rooted there (never a refuted/retired one — that
    # would let retract_disconfirmed_rcc wipe a just-authored valid conclusion) and
    # require substantive overlap (a stale or mis-copied id must not mis-attribute).
    named = getattr(rcc, "names_root_node_id", None)
    if named:
        named_matches = [
            h for h in standing if getattr(h, "root_node_id", None) == named
        ]
        if len(named_matches) == 1 and _substantive_overlap(
            statement, named_matches[0].statement
        ):
            rcc.validated_hypothesis_id = named_matches[0].hypothesis_id
            llm_rcc_cause_named_total.inc()
            return True

    # Tier 2 — lexical fallback: the SOLE hypothesis >= AMBIGUOUS must clear STRONG
    # with a substantive overlap (mirrors resolve_orphan_chains' T1 gate; a second
    # contender >= AMBIGUOUS means "don't guess").
    scored = [
        (s, h)
        for h in standing
        if (s := restatement_score(statement, h.statement)) >= RESTATEMENT_AMBIGUOUS
    ]
    if (
        len(scored) == 1
        and scored[0][0] >= RESTATEMENT_STRONG
        and _substantive_overlap(statement, scored[0][1].statement)
    ):
        rcc.validated_hypothesis_id = scored[0][1].hypothesis_id
        llm_rcc_cause_linked_total.inc()
        return True
    return False


def _chain_outranks_llm_conclusion() -> bool:
    """Whether a standing validated, uncontested chain root outranks an
    LLM-authored ``RootCauseConclusion`` (§7.7 precedence; kill switch
    ``FAULTMAVEN_CHAIN_AUTHORED_CONCLUSION``, default on).

    The ONE precedence consult. Reading configuration is the single impurity in
    this module's mirror path; it is deliberately kept to one call site so the
    switch cannot be honored in one lane and missed in another.
    """
    from faultmaven.config.settings import get_settings

    return bool(get_settings().features.chain_authored_conclusion)


def _conclusion_provider_label() -> str:
    """CHAT provider label for the conclusion-precedence counter.

    Delegates to the resolution-metric helper rather than re-deriving the label:
    the two counters are read as a ratio, so a second resolution rule that drifted
    would silently divide one provider's numerator by another's denominator.
    Imported lazily — this module is the graph leaf and must not take a module-
    level dependency on the terminal layer.
    """
    from faultmaven.core.investigation.terminal_transitions import (
        _resolve_resolution_provider,
    )

    return _resolve_resolution_provider()


def synthesize_rcc_from_validated_root(case: Case) -> bool:
    """§9.3 — mirror the validated chain into a ``RootCauseConclusion`` so the
    disposition / report layer reads cause text rendered from what the chain
    actually proves.

    The validated root IS the cause, so a derived RCC (root statement + the chain
    as mechanism, at the confidence the M2 grade supports — CONFIDENT for a
    mechanistic root, VERIFIED only for a counterfactually confirmed one) is a
    faithful mirror, not an assertion. Because it can assert no more than the
    chain proves, it **outranks** an LLM-authored conclusion (§7.7): when a
    standing validated root exists, the mirror is minted over one, and the LLM's
    own conclusion is surfaced only as the fallback for the no-root case (below).
    With the precedence switched off, an LLM-authored conclusion is instead left
    untouched and the mirror is minted only into an empty or engine-authored slot.

    Every refusal sits AHEAD of the precedence. While identification is
    MECE-contested the engine asserts nothing at all, and with no standing
    validated root this is a no-op — whatever conclusion the case carries, LLM
    text included, stands byte-identical. An engine mirror is also refreshed when
    it has gone stale — its named hypothesis is no longer a standing-validated
    root (the grounding chain handed off to another root via an INCONCLUSIVE
    drift, NOT a refutation, so M6 never cleared it) — or when its confidence no
    longer agrees with the root's M2 grade (confirmation arrived, so the mirror
    upgrades to VERIFIED; or a pre-cap persisted mirror over-claims VERIFIED on a
    mechanistic root, so it corrects down). Returns True if it wrote one.

    Replacement is one-way by design: a mirror that took over from an LLM
    conclusion and whose root later demotes is retracted like any other mirror
    (``retract_stale_engine_rcc``) and the case is then left with NO conclusion.
    The replaced text is not kept anywhere to restore — retaining it would be a
    second conclusion namespace, the thing single authority retires — and
    re-surfacing it would assert a cause no validated root backs, whether or not
    it named the same cause the demoted root did.
    """
    rcc = case.root_cause_conclusion
    llm_authored = (
        rcc is not None and getattr(rcc, "determined_by", None) != _ENGINE_RCC_AUTHOR
    )
    if llm_authored and not _chain_outranks_llm_conclusion():
        # Precedence off: an LLM-authored conclusion is never overwritten.
        # Checked before any graph work — the scans below would be pure waste.
        return False

    # §7.1.2 defense-in-depth: while identification is MECE-contested the
    # engine asserts NO conclusion — a mirror naming one of several
    # simultaneously-validated exclusive causes is an arbitrary pick. The
    # per-turn recompute already gates its call on the same predicate (and
    # retract_stale_engine_rcc clears a standing contested mirror), so this
    # refusal protects direct/non-recompute callers; no IDENTIFIED-with-null-
    # conclusion split is possible because cause_state is held by the
    # identical predicate.
    if mece_contested_root_ids(case):
        return False

    cat_by_id = _evidence_category_map(case)  # one snapshot for every check below

    def _hyp_confirmed(h: "Hypothesis") -> bool:
        r = case.causal_nodes.get(h.root_node_id or "")
        return r is not None and root_counterfactually_confirmed(r, cat_by_id)

    validated_hyps = [
        h
        for h in _standing_hypotheses(case)
        if is_chain_root_validated(h, case.causal_nodes)
    ]
    # ONE confirmation scan feeds the faithfulness check, the root selection,
    # and the mint band below — separate scans of the same predicate would let
    # an asymmetric edit split the named root from the minted confidence.
    confirmed_hyps = [h for h in validated_hyps if _hyp_confirmed(h)]

    # The faithfulness short-circuit and the "keep the named root" selection tie
    # below both read the STANDING MIRROR. An LLM-authored conclusion is not one:
    # its cause link (§7.6) says which hypothesis its prose corresponds to, never
    # that its text renders that root — so treating it as a prior would let a
    # linked, correctly-graded LLM conclusion short-circuit the very replacement
    # the precedence exists to perform.
    prior = None
    if rcc is not None and not llm_authored:
        prior = case.hypotheses.get(getattr(rcc, "validated_hypothesis_id", None) or "")
        if prior is not None and prior in validated_hyps:
            # The mirror still names a grounding root — but it must also (a)
            # claim the confidence its M2 grade supports ("verified" ⇔
            # counterfactually confirmed; expected level derived from the SAME
            # mint constants below, so a constant/band change can never split
            # the check from the mint into re-mint churn), and (b) name the
            # confirmed root when one exists — a mirror asserting an
            # unconfirmed sibling while another root carries the gone⇒gone
            # confirmation is not faithful. Disagreement → fall through, re-mint.
            prior_confirmed = prior in confirmed_hyps
            expected_level = ConfidenceLevel.from_score(
                CONFIRMED_RCC_LIKELIHOOD_FLOOR
                if prior_confirmed
                else MECHANISTIC_RCC_LIKELIHOOD
            )
            if (
                rcc.confidence_level == expected_level
                and (prior_confirmed or not confirmed_hyps)
                and list(rcc.contributing_factors or [])
                == conjuncts_for_chain(case, prior)
            ):
                return False  # faithful mirror: root, grade AND conjuncts agree
        # else: stale/misgraded engine mirror — refresh from the current
        # validated root.
    # No restatement check here BY DESIGN: the §7.1 guard is an ENTRY bar on
    # validation (empirical lane + exclusion lane), so no restating root can
    # freshly validate — and a root that stands VALIDATED (incl. grandfathered
    # pre-guard ones) must be mirrorable, or cause_state=IDENTIFIED would
    # split from a permanently-absent conclusion. The mirror follows the
    # validation verdict; it never re-adjudicates it.
    #
    # Root selection order: a counterfactually CONFIRMED root first (the M2 top
    # grade must be what the conclusion asserts), then the prior mirror's own
    # root (a level-only correction must not silently swap the named cause),
    # then the first standing validated chain.
    # Within the confirmed set, the prior mirror's own root wins: this function
    # now also re-mints for reasons unrelated to the cause (a conjunct that
    # validated this turn), and taking confirmed_hyps[0] blindly would swap the
    # published cause on such a refresh — the swap the "keep the named root"
    # rule below exists to prevent, one tier up.
    hyp = None
    if confirmed_hyps:
        hyp = prior if prior in confirmed_hyps else confirmed_hyps[0]
    if hyp is None and prior is not None and prior in validated_hyps:
        hyp = prior
    if hyp is None:
        hyp = next(iter(validated_hyps), None)
    if hyp is None:
        return False
    root = case.causal_nodes[hyp.root_node_id]
    mechanism = mechanism_for_chain(case, hyp)
    # M2 confidence grades: the GRADE, not the LLM's likelihood, rules the
    # mirror in both directions. A validated root — EMPIRICAL or DEDUCTIVE
    # alike — is mechanistic, so the mirror reads CONFIDENT at the fixed
    # grade-band value (the LLM's own root_cause_likelihood can neither push a
    # mechanistic cause into "verified" nor drag the engine's validation
    # verdict below its band). Only a counterfactually CONFIRMED root
    # (causal_absence SUPPORTS link, gone⇒gone) reads VERIFIED, floored at 0.9.
    # confidence_level must agree with likelihood
    # (RootCauseConclusion.confidence_consistency), so derive it from the final
    # score rather than hardcoding.
    if hyp in confirmed_hyps:
        likelihood = max(
            case.progress.root_cause_likelihood or 0.0,
            CONFIRMED_RCC_LIKELIHOOD_FLOOR,
        )
    else:
        likelihood = MECHANISTIC_RCC_LIKELIHOOD
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=root.statement[:1000],
        mechanism=mechanism,
        confidence_level=ConfidenceLevel.from_score(likelihood),
        likelihood=likelihood,
        validated_hypothesis_id=hyp.hypothesis_id,
        evidence_basis=[
            link.evidence_id
            for link in root.evidence_links
            if link.stance == EvidenceStance.SUPPORTS
        ],
        # The cause's co-necessary conjuncts (#1096). Derived from the graph like
        # every other field here — the mirror renders one chain, so without this
        # a cause the investigation established as a conjunction would reach the
        # report as its first conjunct alone. NOT the LLM's own factor prose:
        # single authority is unchanged, this is the graph speaking.
        contributing_factors=conjuncts_for_chain(case, hyp),
        determined_by=_ENGINE_RCC_AUTHOR,
    )
    if llm_authored:
        # The mirror REPLACED an LLM-authored conclusion — the one event this
        # counter measures. Deliberately not incremented on a first mint into an
        # empty conclusion, nor on a mirror refreshing a mirror: those say nothing
        # about how often the chain outranks the model's prose.
        rcc_precedence_inversion_total.labels(
            provider=_conclusion_provider_label()
        ).inc()
    return True


def retract_stale_engine_rcc(case: Case, contested_ids: set | None = None) -> bool:
    """Clear an ENGINE-authored RootCauseConclusion whose named grounding root no
    longer stands validated — the mirror exists only to reflect a validated
    chain, so when the chain's root demotes (e.g. the §7.1 restatement guard on
    a pre-guard persisted case, or an evidence tie) the mirror must not outlive
    it: readiness/report readers key on the RCC's presence and would otherwise
    keep asserting a conclusion nothing grounds. Likewise cleared when the named
    root, though still validated, is MECE-CONTESTED (§7.1.2): a mirror asserting
    ONE of several simultaneously-validated exclusive causes is an arbitrary
    pick, not a reflection — the engine withholds its conclusion pending
    discrimination, and the mirror re-mints automatically when the contest
    resolves. LLM-authored conclusions are NEVER touched here (their retraction
    lifecycle is a separate concern — tracked on #656).

    Note what this means for a mirror that REPLACED an LLM conclusion (§7.7): when
    its root demotes it is cleared like any other mirror and the case is left with
    NO conclusion. The replaced text is not restored — the engine keeps no copy of
    it (a copy would be a second conclusion namespace), and re-surfacing it would
    assert a cause no validated root backs, whether or not it named the same cause
    the demoted root did.

    Returns True if it cleared one. ``contested_ids`` lets the per-turn recompute
    pass its already-computed §7.1.2 set (the once-per-derive snapshot pattern);
    ``None`` means compute here.
    """
    rcc = case.root_cause_conclusion
    if rcc is None or getattr(rcc, "determined_by", None) != _ENGINE_RCC_AUTHOR:
        return False
    prior = case.hypotheses.get(getattr(rcc, "validated_hypothesis_id", None) or "")
    if (
        prior
        and prior.state in _STANDING_HYP_STATES
        and is_chain_root_validated(prior, case.causal_nodes)
    ):
        if contested_ids is None:
            contested_ids = mece_contested_root_ids(case)
        if prior.root_node_id not in contested_ids:
            return False  # still faithfully mirrors a standing UNCONTESTED root
    case.root_cause_conclusion = None
    return True
