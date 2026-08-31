"""Shared safety verdict for reference-set sweeps (issue #1232).

A *reference-set sweep* enumerates candidates from one store and deletes the
ones a second store — the **authority** — does not reference. Two jobs have
this shape and both delete irreversibly:

- ``storage_cleanup``: stored objects vs ``uploaded_files.storage_ref``.
- ``case_cleanup``: ChromaDB case collections vs the ``cases`` id set.

The failure they share is not "the authority is down" — that raises, and a
raise is easy. It is **the authority answering with something that does not
correspond to the candidates**, because every candidate then scores
"unreferenced" and the sweep deletes the lot. Three ways to reach it, none of
which raise:

1. RLS scopes the session, so a tenanted table answers with a partial or empty
   view (``uploaded_files`` is fail-closed — no org bound means ZERO rows).
2. The two stores stopped sharing a keyspace — ``STORAGE_BACKEND`` or a key
   prefix changed, or rows hold values that were never backend keys at all
   (``knowledge_service`` and ``conversion_service`` both write filesystem
   paths into ``storage_ref``).
3. The authority genuinely references nothing, because everything was deleted.

**Overlap, not emptiness, is the discriminator.** Guarding on "the reference
set is empty" — the first attempt at this — leaves case 2 wide open: a
*non-empty* set that is disjoint from the candidates passes such a guard and
then deletes every candidate. That is the same irreversible loss the guard
exists to prevent, so the test is ``candidates & referenced``.

Cases 1-3 are indistinguishable at this point — all three look like "no
overlap" — so the verdict does not try to tell them apart. It separates
*reporting* from *deleting* instead, which needs no such judgement:

- **A dry run always proceeds.** It deletes nothing, so it cannot lose
  anything, and its counters are precisely how an operator diagnoses which of
  the three they have. Refusing it would also make the mandatory pre-arming
  canary impossible to complete: seeding one orphan on an otherwise-empty
  staging install *is* a disjoint reference set.
- **A live run refuses**, unless the operator passes an explicit
  acknowledgment. Case 3 is legitimate and must not deadlock — an install
  whose cases were all deleted still needs its objects reclaimed — but it is
  not distinguishable from cases 1 and 2 by inspection, so it takes a
  deliberate human assertion rather than a silent default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Iterable, Optional, Set

#: ``reason`` recorded on a run refused by this guard.
REASON_REFERENCE_SET_DISJOINT = "reference_set_disjoint"


@dataclass(frozen=True)
class ReferenceSetVerdict:
    """What a sweep may do, given how its reference set lines up."""

    #: True when no candidate is referenced — the suspect shape.
    disjoint: bool
    #: True when the sweep may go on to delete.
    may_delete: bool
    #: True when the run must stop now, deleting nothing.
    refuse: bool
    #: ``reason`` for a refused run; None otherwise.
    reason: Optional[str]
    #: Operator-facing explanation. Always populated when ``disjoint``.
    message: str
    #: Sizes, for the run summary.
    candidate_count: int
    referenced_count: int
    overlap_count: int


def assess_reference_set(
    *,
    candidates: Collection[str],
    referenced: Iterable[str],
    dry_run: bool,
    acknowledged: bool = False,
    authority: str = "the authority",
    candidate_noun: str = "candidate",
    acknowledge_flag: str = "--allow-disjoint-reference-set",
) -> ReferenceSetVerdict:
    """Decide whether a reference-set sweep may delete.

    Args:
        candidates: Everything the sweep enumerated and could delete.
        referenced: What the authority says is still in use.
        dry_run: True when the caller will not delete regardless.
        acknowledged: The operator's explicit assertion that a disjoint
            reference set is expected here (see the module docstring).
        authority: Name of the authority, for the message only.
        candidate_noun: What a candidate is, for the message only.
        acknowledge_flag: CLI spelling of ``acknowledged``, for the message.

    Returns:
        A ``ReferenceSetVerdict``. ``refuse`` and ``may_delete`` are never both
        True; when there is nothing to sweep, neither is set.
    """
    referenced_set: Set[str] = set(referenced)
    candidate_set: Set[str] = set(candidates)
    overlap = candidate_set & referenced_set

    sizes = {
        "candidate_count": len(candidate_set),
        "referenced_count": len(referenced_set),
        "overlap_count": len(overlap),
    }

    # Nothing enumerated: there is no decision to make and nothing to lose.
    # Deliberately checked first — an empty candidate set is trivially disjoint
    # from everything, and reporting THAT as suspect would refuse every clean
    # deployment.
    if not candidate_set:
        return ReferenceSetVerdict(
            disjoint=False,
            may_delete=not dry_run,
            refuse=False,
            reason=None,
            message="",
            **sizes,
        )

    if overlap:
        return ReferenceSetVerdict(
            disjoint=False,
            may_delete=not dry_run,
            refuse=False,
            reason=None,
            message="",
            **sizes,
        )

    detail = (
        f"{len(candidate_set)} {candidate_noun}(s) enumerated, but NONE is "
        f"referenced by {authority} "
        f"({len(referenced_set)} reference(s) known, 0 overlapping). "
        "Every candidate would therefore be deleted. This is what an "
        "RLS-scoped session, a changed keyspace, or a genuinely empty "
        "deployment all look like from here, and they are not "
        "distinguishable at this point."
    )

    if dry_run:
        return ReferenceSetVerdict(
            disjoint=True,
            may_delete=False,
            refuse=False,
            reason=None,
            message=(
                detail + " Continuing because this is a DRY RUN — nothing "
                "will be deleted, and these counters are how you tell the "
                "three apart. Do NOT arm the sweep until the overlap is "
                "non-zero or you have confirmed the emptiness is real."
            ),
            **sizes,
        )

    if acknowledged:
        return ReferenceSetVerdict(
            disjoint=True,
            may_delete=True,
            refuse=False,
            reason=None,
            message=(
                detail + f" Proceeding anyway: {acknowledge_flag} was passed, "
                "which is an operator assertion that this deployment really "
                "does reference none of them."
            ),
            **sizes,
        )

    return ReferenceSetVerdict(
        disjoint=True,
        may_delete=False,
        refuse=True,
        reason=REASON_REFERENCE_SET_DISJOINT,
        message=(
            detail + " Refusing to delete. Run with --dry-run to see the "
            f"classification, and pass {acknowledge_flag} once you have "
            "confirmed the reference set is trustworthy."
        ),
        **sizes,
    )
