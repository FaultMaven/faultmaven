"""Draft-slot conflict checks: whether a runbook id or file path is
already held by a live draft in this tenant, before any new draft
write."""

import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import or_, select

from faultmaven.config.tenant_context import (
    writable_enterprise_id,
)
from faultmaven.exceptions import (
    ConflictError,
)
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
)
from faultmaven.modules.knowledge.domain.case_authoring import (
    case_disambiguated_runbook_id,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    ConversionError,
    FailureModeAnalysis,
    generate_runbook_id,
)

logger = logging.getLogger(__name__)


async def _find_live_draft_owning(
    db_session_factory,
    enterprise_id: str,
    runbook_ids: Sequence[Optional[str]],
    file_path: str = None,
) -> Optional[Tuple[str, str, str]]:
    """``(runbook_id, file_path, draft_id)`` of a live draft already holding
    one of these ids OR this file, in this tenant. ``None`` if the slot is
    free.

    Two keys, one query, because a draft occupies two slots and losing
    either one loses a runbook:

    - its ``runbook_id``, which migration 046 makes unique per tenant; and
    - its ``file_path``, which is NOT in that index and does not follow
      from the id. ``draft_filename`` runs the id back through ``_slug``,
      so ``"foo--bar"`` and ``"foo-bar"`` resolve to one ``foo-bar.md``.
      A legacy row holding the double-hyphen form — which the mint could
      produce before #1243 — is invisible to an id lookup, and the write would
      clobber its file while the INSERT sailed past the index. Measured on
      the dev database: 0 rows of that shape today, so this is a guard
      against a state I could not reproduce rather than one I observed —
      which is exactly the case for keying on the resolved slot rather than
      on the id that was supposed to imply it.

    Ordered, and deliberately not ``LIMIT 1`` on an unordered scan: the
    message names a specific row, and naming a different row on each call
    makes it unactionable.
    """
    if not db_session_factory:
        return None
    # ``is not None``, NOT truthiness: an empty ``runbook_id`` is a real
    # (legacy) value that collides with every other empty one — the very
    # shape #1230 reported — and dropping it here let the raw
    # ``IntegrityError`` escape past every caller written to catch the
    # typed refusal.
    ids = [rid for rid in runbook_ids if rid is not None]
    if not ids and file_path is None:
        return None
    conditions = []
    if ids:
        conditions.append(ConversionDraftModel.runbook_id.in_(ids))
    if file_path is not None:
        conditions.append(ConversionDraftModel.file_path == file_path)
    async with db_session_factory() as probe:
        result = await probe.execute(
            select(
                ConversionDraftModel.runbook_id,
                ConversionDraftModel.file_path,
                ConversionDraftModel.id,
            )
            .where(ConversionDraftModel.enterprise_id == enterprise_id)
            .where(or_(*conditions))
            .where(ConversionDraftModel.status != "discarded")
            .order_by(ConversionDraftModel.created_at, ConversionDraftModel.id)
            .limit(1)
        )
        return result.first()


def _duplicate_draft_conflict(taken: Tuple[str, str, str]) -> ConflictError:
    """The 409 for a slot already held. One wording, both call sites.

    Names the draft and the runbook id, never the draft's file (#836). The
    slot is enterprise-wide, so the holder can be a colleague's draft, and its
    ``conversion_drafts.file_path`` is a server path that names their
    directory. The message also reaches ``/convert``'s persisted warnings, so a
    path here would be stored and served again.
    """
    runbook_id, _file_path, draft_id = taken
    return ConflictError(
        f"A runbook draft with id '{runbook_id}' already exists in this "
        f"enterprise (draft {draft_id}). Discard it before "
        "creating another with the same service and title — verifying it "
        "does not release the id.",
        resource_type="conversion_draft",
        resource_id=draft_id,
        conflict_reason="duplicate_runbook_id",
    )


async def _refuse_modes_whose_id_is_taken(
    db_session_factory,
    failure_modes: List[FailureModeAnalysis],
    enterprise_id: Optional[str],
) -> Tuple[List[FailureModeAnalysis], List[ConversionError]]:
    """Drop the modes whose minted id a LIVE draft already holds, in ONE query.

    The cost pre-filter described at the call site. ``refuse_if_draft_slot_taken``
    asks the same question one id at a time, from inside the per-mode
    coroutine — i.e. after that mode's generation has already been paid for.
    Both remain: this one saves the call, that one is the authoritative
    pre-write guard and closes the race this query cannot.

    Degrades per mode, exactly like the committed-duplicate branch in
    ``_convert_single_failure_mode``: a taken id is a fact about ONE failure
    mode, and a document analysed into five of them should still yield the
    other four. The wording is ``_duplicate_draft_conflict``'s, so a
    duplicate refused here and one refused at the write site read the same.

    Returns ``(survivors, errors)``. Inert with no database (nothing is
    written, so nothing can be taken) — the same early return
    ``refuse_if_draft_slot_taken`` and ``_persist_job`` make, and for the
    same reason: ``writable_enterprise_id`` raises on an unscoped context, which
    must not become a failure on a path that writes nothing.
    """
    if not db_session_factory or not failure_modes:
        return list(failure_modes), []

    minted = {fm.id: generate_runbook_id(fm) for fm in failure_modes}
    taken = await _find_live_drafts_owning(
        db_session_factory, writable_enterprise_id(enterprise_id), list(minted.values())
    )
    if not taken:
        return list(failure_modes), []

    survivors: List[FailureModeAnalysis] = []
    errors: List[ConversionError] = []
    for fm in failure_modes:
        row = taken.get(minted[fm.id])
        if row is None:
            survivors.append(fm)
            continue
        logger.warning(
            "conversion_draft_id_taken",
            extra={"failure_mode_id": fm.id, "runbook_id": minted[fm.id]},
        )
        errors.append(
            ConversionError(
                failure_mode_id=fm.id,
                error=str(_duplicate_draft_conflict(row)),
                retryable=False,
            )
        )
    return survivors, errors


async def _find_live_drafts_owning(
    db_session_factory, enterprise_id: str, runbook_ids: Sequence[Optional[str]]
) -> Dict[str, Tuple[str, str, str]]:
    """``{runbook_id: (runbook_id, file_path, draft_id)}`` for every live
    draft in this tenant holding one of these ids.

    The batch form of ``_find_live_draft_owning``. That one answers "is this
    ONE slot free" and stops at the first row, which is right for a
    pre-write guard and wrong for a pre-flight over a whole batch: five
    taken ids would need five queries, or one query that names only one of
    them.

    Ordered, and the FIRST row per id wins, so the draft this names is the
    same one ``_find_live_draft_owning`` would name for that id — the two
    refusals must not point at different rows for the same collision.
    """
    ids = [rid for rid in runbook_ids if rid is not None]
    if not ids:
        return {}
    async with db_session_factory() as probe:
        result = await probe.execute(
            select(
                ConversionDraftModel.runbook_id,
                ConversionDraftModel.file_path,
                ConversionDraftModel.id,
            )
            .where(ConversionDraftModel.enterprise_id == enterprise_id)
            .where(ConversionDraftModel.runbook_id.in_(ids))
            .where(ConversionDraftModel.status != "discarded")
            .order_by(ConversionDraftModel.created_at, ConversionDraftModel.id)
        )
        found: Dict[str, Tuple[str, str, str]] = {}
        for row in result.all():
            found.setdefault(row[0], (row[0], row[1], row[2]))
        return found


async def refuse_if_draft_slot_taken(
    db_session_factory, enterprise_id: Optional[str], runbook_id: str, draft_path: str
) -> None:
    """Refuse BEFORE writing, on every path that mints a NEW draft file.

    The draft file is named after ``runbook_id``, so a duplicate resolves
    to the SAME path. Writing first and letting migration 046 reject the
    INSERT leaves the EXISTING draft's row pointing at the new author's
    content — a worse state than the duplicate rows the index removes,
    because the surviving row then lies about its own file.

    Both new-draft write paths call this: the LLM conversion
    (``_generate_runbook_draft``) and the manual template create. The edit
    path (``update_draft``) does NOT and must not — it rewrites the file
    its own row already owns, so the row it would "conflict" with is
    itself.

    This is the ordinary case; the index stays the backstop for the genuine
    cross-replica race, which is what an index is for.

    Takes the RAW ``enterprise_id`` and resolves it here, after the
    factory check — unlike ``_raise_if_runbook_id_taken``, whose only caller
    has already resolved it. ``writable_enterprise_id`` raises on an unscoped
    context, and evaluating it at the call site would make that a failure on
    a path with no database, which writes nothing and has no index to
    honour. Mirrors ``_persist_job``'s own early return.
    """
    if not db_session_factory:
        return
    taken = await _find_live_draft_owning(
        db_session_factory,
        writable_enterprise_id(enterprise_id),
        [runbook_id],
        file_path=draft_path,
    )
    if taken:
        raise _duplicate_draft_conflict(taken)


async def claim_case_draft_slot(
    db_session_factory,
    enterprise_id: Optional[str],
    minted_id: str,
    case_id: str,
    path_for: Callable[[str], Path],
) -> str:
    """The id a case-built draft is written under: ``minted_id``, or its
    case-disambiguated form when ``minted_id``'s slot is held. Raises the
    usual 409 when both are held.

    The case path mints from the title the model wrote (#1880), so two
    different cases about one failure mint one id, and the enterprise-wide
    slot may belong to a colleague's personal draft. The re-mint
    (:func:`~faultmaven.modules.knowledge.domain.case_authoring.case_disambiguated_runbook_id`)
    appends the case stem, so a second case gets its own id. A held
    re-minted id is a collision within one case, and is refused as before.
    So is a held case-stem id, which only this case mints.

    The same check as :func:`refuse_if_draft_slot_taken`, on both keys (id and
    file path), so the caller writes the file, forces the frontmatter id and
    persists the row under the one id this returns. Only the case path calls
    it: the document path's ids are fixed before the model call and are
    refused outright.
    """
    if not db_session_factory:
        return minted_id
    scoped = writable_enterprise_id(enterprise_id)
    taken = await _find_live_draft_owning(
        db_session_factory, scoped, [minted_id], file_path=str(path_for(minted_id))
    )
    if not taken:
        return minted_id
    disambiguated = case_disambiguated_runbook_id(minted_id, case_id)
    if disambiguated is None:
        raise _duplicate_draft_conflict(taken)
    retaken = await _find_live_draft_owning(
        db_session_factory,
        scoped,
        [disambiguated],
        file_path=str(path_for(disambiguated)),
    )
    if retaken:
        raise _duplicate_draft_conflict(retaken)
    logger.info(
        "case_draft_id_disambiguated",
        extra={"case_id": case_id, "minted": minted_id, "runbook_id": disambiguated},
    )
    return disambiguated


async def _raise_if_runbook_id_taken(
    db_session_factory, enterprise_id: str, runbook_ids: Sequence[Optional[str]]
) -> None:
    """Translate the 046 unique-index violation into a 409, or return.

    ``uq_conversion_drafts_enterprise_runbook_id`` (migration 046) admits one LIVE
    draft per ``(enterprise_id, runbook_id)``. Two drafts reaching the
    same id is ordinary — ``runbook_id_from_parts`` is deterministic on
    ``(service, title)``, deliberately, because the disk scan reconciles a
    file to its row by that id — so a user converting the same source
    twice lands here, and so do two cases about the same failure that race
    past ``claim_case_draft_slot``. Without this
    the whole commit surfaces as an unhandled ``IntegrityError``, i.e. a
    500 that says nothing.

    This is the BACKSTOP. ``refuse_if_draft_slot_taken`` catches the
    ordinary case before anything is written; what reaches here is a race,
    or a shape the pre-check could not see.

    Classification is by a **confirming re-read**, never by matching the
    exception's message: the same commit also carries
    ``uq_conversion_jobs_live_case_id``. That one is NOT distinguishable
    from a runbook_id duplicate by re-read alone — two replicas converting
    the same case mint their ids from the frontmatter each model wrote
    (#1880). Those ids can coincide across cases too, but there
    ``claim_case_draft_slot`` appends the case stem; within ONE case the stem
    is the same on both replicas, so it cannot separate them, and this
    re-read finds the winner's drafts
    and raises a 409 for what is really the live-case race. ``convert_from_case``
    therefore catches ``ConflictError`` as well as ``IntegrityError`` and
    resolves it with ITS OWN confirming re-read
    (``get_conversion_by_case``), which is the discriminator that actually
    distinguishes the two. Anything it cannot confirm it re-raises.

    Two drafts in ONE job colliding with each other no longer reaches here
    (#1258), and could never have been resolved here: nothing is committed,
    so the re-read finds nothing, this returns, and the caller re-raises the
    bare ``IntegrityError`` — a 500 that says nothing, after the second
    draft's write has already replaced the first one's file (both ids
    resolve to one ``draft_filename``). It is refused where the duplicate is
    produced instead: ``_partition_failure_modes`` mints every id in the
    batch before any conversion runs and degrades each repeat to that
    failure mode's ``ConversionError``, so every draft list reaching
    ``_persist_job`` carries distinct ids and what arrives here is a
    cross-job duplicate or the live-case race.
    """
    taken = await _find_live_draft_owning(
        db_session_factory, enterprise_id, runbook_ids
    )
    if taken:
        raise _duplicate_draft_conflict(taken)
