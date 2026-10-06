from __future__ import annotations

from typing import TYPE_CHECKING

from faultmaven.core.investigation.cause_assurance import (
    content_tokens as _content_tokens,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case


def _mutual_mirror(a_tokens: set, b_tokens: set, threshold: float) -> bool:
    """MUTUAL restatement (Jaccard at/above ``threshold``) — both texts are ~the
    same claim. Distinct from one-way containment: a disjunction root contains
    each of its sources but is not mutually theirs. Callers pass their own bar
    (frame ownership vs causal-evidence independence — different comparisons,
    separately calibrated)."""
    if not a_tokens or not b_tokens:
        return False
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens) >= threshold


# ---------------------------------------------------------------------------
# Orphan-chain resolution (the invariant: "every chain explaining D is attached
# to exactly one hypothesis"). The divergence the prompt (step 2) does not fully
# prevent: the LLM emits a real root->D chain but leaves it unlinked, so the
# hypothesis keeps running flat while a parallel orphan chain describes the SAME
# cause (double-representation). This deterministic post-pass runs each turn
# AFTER chain-ingest.
# ---------------------------------------------------------------------------

# A root whose statement restates a hypothesis at/above STRONG, with no other
# hypothesis at/above AMBIGUOUS, is an UNAMBIGUOUS double-representation and is
# re-attached automatically (T1). A weaker or contested match is left for an
# LLM nudge (T2a) — a wrong auto-attach is itself an incorrect conclusion, so
# "when unsure, don't". The scoring + thresholds mirror the sim analyzer's
# ``_restatement_score`` / ``_RESTATEMENT_THRESHOLD``
# (fm-sre-simulator/scripts/analyze_chain_emission.py) so engine and harness
# agree on what "restates" means — keep them reconciled when either moves.
RESTATEMENT_STRONG = 0.6
RESTATEMENT_AMBIGUOUS = 0.4

# §7.1 restatement guard's validation bar: the minimum NOVEL-token fraction a
# ROOT statement must carry beyond the case frame (problem anchors + other
# standing hypotheses) to be ADMITTED to VALIDATED. Deliberately its own knob,
# decoupled from the T1 orphan-reattach threshold (opposite error economics —
# a wrong re-attach is recoverable via the LLM nudge; a wrong validation mints
# a false conclusion). The calibration figures live in ONE executable home:
# test_restatement_guard_calibration.py (methodology prose: §7.1).
ROOT_NOVELTY_MIN_FRACTION = 0.3

# Jaccard at or above which an UNATTACHED hypothesis is treated as a node's
# presumptive OWNER (excluded from that node's frame): both statements are
# mutually ~the same claim, the normal chain-emission shape during the
# attachment lag. Lexical and therefore weak; see the KNOWN LIMIT on
# ``root_restates_case_frame``.
_FRAME_OWNER_JACCARD = 0.6


def restatement_score(a: str, b: str) -> float:
    """How strongly statement ``a`` restates ``b`` (0..1): the max of Jaccard and
    the two containments, so a specific elaboration largely covered by a more
    general statement (or vice versa) still scores high. Fuzzy by nature."""
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    jaccard = inter / len(ta | tb)
    return max(jaccard, inter / len(ta), inter / len(tb))


# A T1 auto-attach needs at least this many shared content tokens. The score
# alone is not enough: a 1–2 token statement fully contained in another yields
# containment 1.0 (a STRONG score) on a single coincidental word, which would
# auto-attach on flimsy evidence. Requiring a substantive overlap keeps the
# deterministic re-root honest ("when unsure, don't") without touching the
# shared thresholds.
_MIN_SHARED_TOKENS_FOR_REATTACH = 2


def _substantive_overlap(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` share enough content tokens that a STRONG score
    reflects real overlap, not a single-word containment artifact."""
    return (
        len(_content_tokens(a) & _content_tokens(b)) >= _MIN_SHARED_TOKENS_FOR_REATTACH
    )


# Hypothesis dedup (INV-36) fails OPEN — deduping DROPS an LLM emission for the
# turn, so its bar is deliberately STRICTER than §7.1.2's reversible fold
# (``_ROOT_DISTINCT_JACCARD`` = 0.6): only a near-verbatim restatement collapses.
# A genuinely-distinct short statement that differs by one substantive token
# (e.g. "memory leak in connection pool" vs "... cache pool" → Jaccard 0.6)
# MUST survive; the actual incident duplicate was verbatim-identical (~1.0).
_HYPOTHESIS_DUPLICATE_JACCARD = 0.8

# Standalone negation cues (apostrophes stripped before matching, so "isn't" →
# "isnt"). "not" is a content stopword, so a hypothesis and its negation
# tokenize IDENTICALLY and would mirror at Jaccard 1.0 — dropping a
# disputing/competing hypothesis as a "duplicate" of what it contradicts is a
# NO-COLLAPSE breach. Used ONLY to REFUSE a dedup on asymmetric polarity (fail
# open); it never causes a dedup.
_NEGATION_CUES = frozenset(
    {
        "not",
        "no",
        "never",
        "without",
        "none",
        "nor",
        "neither",
        "cannot",
        "cant",
        "dont",
        "doesnt",
        "didnt",
        "isnt",
        "arent",
        "wasnt",
        "werent",
        "wont",
        "couldnt",
        "shouldnt",
        "wouldnt",
        "non",
        "unable",
    }
)


def _has_negation(text: str) -> bool:
    """True when the raw statement carries a standalone negation cue. Cheap
    polarity probe for the dedup guard — apostrophes are stripped so contractions
    match, then the text is split on non-alphanumerics."""
    cleaned = "".join(
        c.lower() if c.isalnum() else " " for c in (text or "").replace("'", "")
    )
    return any(w in _NEGATION_CUES for w in cleaned.split())


def _numeric_discriminators(text: str) -> set[str]:
    """Maximal digit runs in the raw statement. ``_content_tokens`` drops
    single-digit tokens (len < 2) and stopwords like "version"/"node", so two
    hypotheses distinguished ONLY by a number ("server 1 down" vs "server 2
    down", "version 5" vs "version 6") tokenize identically and would mirror at
    Jaccard 1.0. Preserving the digit runs lets the dedup keep them distinct."""
    out: set[str] = set()
    cur: list[str] = []
    for c in text or "":
        if c.isdigit():
            cur.append(c)
        elif cur:
            out.add("".join(cur))
            cur = []
    if cur:
        out.add("".join(cur))
    return out


def hypothesis_statements_duplicate(a: str, b: str) -> bool:
    """Two hypothesis statements are DUPLICATES for INV-36 dedup: same polarity,
    same numeric discriminators, AND MUTUAL mirrors at
    ``_HYPOTHESIS_DUPLICATE_JACCARD``.

    The bar is stricter than §7.1.2's fold because deduping DROPS an emission
    rather than holding it — it must fail open. Uses the SYMMETRIC mirror, not
    ``restatement_score``'s containment: a more-SPECIFIC elaboration of a standing
    hypothesis scores high on one-way containment but below the mutual-Jaccard
    bar, so it stays a DISTINCT refinement of the differential. Two fail-open
    guards refuse a dedup the token mirror alone would wrongly accept: a **polarity
    guard** (one statement carries a negation cue the other lacks — a dispute is
    never a duplicate of the claim it contradicts) and a **numeric-discriminator
    guard** (the statements differ by a number the similarity tokenizer drops —
    "server 1" vs "server 2")."""
    if _has_negation(a) != _has_negation(b):
        return False
    if _numeric_discriminators(a) != _numeric_discriminators(b):
        return False
    return _mutual_mirror(
        _content_tokens(a), _content_tokens(b), _HYPOTHESIS_DUPLICATE_JACCARD
    )


def find_duplicate_hypothesis(statement: str, case: "Case") -> str | None:
    """Return the id of a standing hypothesis whose statement duplicates
    ``statement`` (``hypothesis_statements_duplicate``), else ``None`` — the
    INV-36 dedup predicate for ``hypotheses_to_add``.

    Same-batch duplicates are caught for free: the apply loop inserts each minted
    hypothesis into ``case.hypotheses`` before the next item is checked, so an
    earlier sibling this turn is already in the scanned set. Terminal
    (``REFUTED``/``RETIRED``) hypotheses are deliberately NOT dedup targets:
    terminal states are immutable (``_apply_hypothesis_updates`` refuses changes
    and instructs "open a NEW hypothesis if that theory is back in play"), so
    deduping against them would DEADLOCK the revival — the re-mint refused here
    and the update refused there, with contradictory guidance. The gate-inflation
    vector is duplicate standing records; a revival minting a fresh
    hypothesis is legitimate diagnostic work, not spurious inflation. The caller
    surfaces the matched id to the LLM so a genuine re-examination updates the
    standing hypothesis rather than cloning it."""
    for hid, hyp in case.hypotheses.items():
        if hyp.state.is_terminal:
            continue
        if hypothesis_statements_duplicate(statement, hyp.statement):
            return hid
    return None
