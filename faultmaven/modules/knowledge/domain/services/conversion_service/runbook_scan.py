"""The data/knowledge/ disk scan that discovers untracked runbooks and
reconciles draft rows against what is actually on disk."""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.exceptions import (
    ConflictError,
)
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
    KnowledgeItemModel,
)
from faultmaven.modules.knowledge.domain.global_authoring import (
    is_global_authoring_allowed,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionDraft,
    ConversionStatus,
    DraftStatus,
    SourceAssessment,
    SourceFileInfo,
    generate_conversion_id,
    generate_draft_id,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.errors import (
    ScanAbortedError,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.job_persistence import (
    _persist_job,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    avalidate_and_score,
)
from faultmaven.utils.frontmatter import match_frontmatter
from faultmaven.utils.runbook_id import (
    RunbookPathEscape,
    resolve_runbook_path,
)

logger = logging.getLogger(__name__)


QUALITY_WARNING_THRESHOLD = 50.0


async def _scan_for_runbooks_impl(
    data_dir,
    db_session_factory,
    share_repo,
    user_id: str,
    enterprise_id: Optional[str] = None,
    is_platform_admin: bool = False,
) -> dict:
    import yaml

    # Whether this caller may mint global-scope drafts. Computed once (the
    # policy is per-caller, not per-file); global-inferred files are skipped
    # below when this is False.
    global_authoring_allowed = is_global_authoring_allowed(is_platform_admin)

    discovered = []
    skipped = 0
    errors = []

    from faultmaven.utils.runbook_id import item_id_from_runbook_id

    # Reconcile DB state before scanning disk
    tracked_paths: set[str] = set()
    # item_ids already published into knowledge_items — e.g. by the
    # startup KB bootstrap, which ingests shipped runbooks DIRECTLY
    # (bypassing conversion_drafts entirely, per kb_init's design). Such
    # files have no draft row, so without this set the disk walk below
    # would treat them as "untracked" and manufacture a phantom draft for
    # every already-published runbook.
    ingested_item_ids: set[str] = set()
    if db_session_factory:
        async with db_session_factory() as session:
            ingested_result = await session.execute(select(KnowledgeItemModel.item_id))
            ingested_item_ids = set(ingested_result.scalars().all())

            all_drafts_result = await session.execute(select(ConversionDraftModel))
            all_draft_models = all_drafts_result.scalars().all()

            non_discarded_count = sum(
                1 for d in all_draft_models if d.status != DraftStatus.DISCARDED.value
            )
            pending_discard_ids: list[str] = []
            # Discards for drafts whose runbook is ALREADY published in
            # knowledge_items. Kept separate from pending_discard_ids so
            # they do NOT feed the "would discard ALL active drafts ⇒
            # storage failure" abort guard below — clearing redundant
            # phantom drafts is legitimate cleanup even if it empties the
            # Drafts tab, not a wiped-data-dir signal.
            redundant_discard_ids: list[str] = []

            for draft_model in all_draft_models:
                # Cheap status check FIRST: an already-discarded row is not
                # reconciled at all, so probing its path was two wasted
                # resolve() walks per scan and made a discarded escaping row
                # log a refusal on every scan, forever.
                if draft_model.status == DraftStatus.DISCARDED.value:
                    continue

                # A row whose path is not inside the tree is SKIPPED — not
                # discarded, not counted, not touched (#1213 follow-up).
                #
                # It was treated as "absent" in the first version of this
                # change, and that was wrong in two measurable ways. A
                # deployment whose only active drafts have escaping paths
                # discards all of them, trips the "would discard ALL active
                # drafts" abort guard, and gets a deterministic "Restore
                # from backup" RuntimeError on EVERY scan — so the repair
                # this was supposed to perform never runs. And a knowledge
                # tree assembled with symlinks (``team_x -> /mnt/share``)
                # worked before containment existed; treating it as absent
                # soft-discards every draft under it on the first scan after
                # the upgrade.
                #
                # Refusing to TOUCH such a path is the security posture and
                # it stands. Refusing to touch it while deleting the row
                # that points at it is just data loss. Skip and warn: an
                # operator loses access to those drafts, not the drafts.
                try:
                    file_exists = resolve_runbook_path(
                        draft_model.file_path,
                        source=(
                            "conversion_drafts.file_path "
                            f"(draft_id={draft_model.id})"
                        ),
                        root=data_dir,
                    ).exists()
                except RunbookPathEscape as exc:
                    # NOT counted in ``skipped``: that number is returned to
                    # the client and means "files skipped during the disk
                    # walk". An escaping DB row is a bad ROW, not a walked
                    # file, so folding it in (as round 2 did) made "N files
                    # skipped" stop meaning walk skips. The bad row is
                    # surfaced by this WARNING, which names it for the
                    # operator repair it needs (#1213 follow-up).
                    logger.warning(
                        "skipping draft %s during scan reconciliation "
                        "(not discarded): %s",
                        draft_model.id,
                        exc,
                    )
                    continue

                if not file_exists:
                    draft_model.status = DraftStatus.DISCARDED.value
                    pending_discard_ids.append(draft_model.id)
                    continue

                # If this draft has already been activated (has a
                # knowledge_item_id linking it to a KB entry), it's a
                # verified draft that should not be shown as pending.
                # Do NOT delete drafts based on title matching against
                # ChromaDB — stale data from previous sessions causes
                # false positives that remove un-activated drafts.
                if draft_model.status == "draft" and getattr(
                    draft_model, "knowledge_item_id", None
                ):
                    draft_model.status = DraftStatus.DISCARDED.value
                    pending_discard_ids.append(draft_model.id)
                    logger.info(
                        f"Removed duplicate draft {draft_model.id} "
                        f"(already has knowledge_item_id)"
                    )
                    continue

                # Redundant phantom draft: this runbook is already
                # published in knowledge_items (typically by the startup
                # bootstrap, which never sets knowledge_item_id on a
                # draft because it bypasses the drafts table). Discard so
                # it stops showing as pending in the Drafts tab.
                if (
                    draft_model.status == "draft"
                    and draft_model.runbook_id
                    and item_id_from_runbook_id(draft_model.runbook_id)
                    in ingested_item_ids
                ):
                    draft_model.status = DraftStatus.DISCARDED.value
                    redundant_discard_ids.append(draft_model.id)
                    logger.info(
                        f"Discarded phantom draft {draft_model.id} "
                        f"(runbook '{draft_model.runbook_id}' already "
                        f"published in knowledge_items)"
                    )
                    continue

                if draft_model.status == "verified":
                    # Trust SQLite: if knowledge_item_id is set, the
                    # document was activated.
                    # If status=verified but knowledge_item_id is missing,
                    # this is a legacy half-state row from the pre-atomic
                    # verify_draft path. The current verify_draft only
                    # commits VERIFIED after successful ingestion, so new
                    # rows should never reach this branch. Warn loudly
                    # rather than silently downgrading — a silent revert
                    # is what corrupted live data in the incident this
                    # path was rewritten to prevent.
                    if not getattr(draft_model, "knowledge_item_id", None):
                        logger.warning(
                            f"Draft {draft_model.id} has status=verified "
                            f"but no knowledge_item_id (legacy half-state). "
                            f"Leaving as-is; investigate and clean up via "
                            f"explicit admin action if needed."
                        )

                tracked_paths.add(draft_model.file_path)

            # Guard: abort if the scan would discard every active draft.
            # This signals a storage-layer failure (data directory missing/
            # wiped), not legitimate cleanup. Raising here skips the commit
            # so DB state is fully preserved for manual recovery.
            if (
                non_discarded_count > 0
                and len(pending_discard_ids) >= non_discarded_count
            ):
                preview = pending_discard_ids[:20]
                suffix = "..." if len(pending_discard_ids) > 20 else ""
                raise ScanAbortedError(
                    f"Scan aborted: would discard all {non_discarded_count} active runbook "
                    f"draft(s). Runbook files appear to be missing from the knowledge "
                    f"data directory. DB state is unchanged. "
                    f"Affected draft IDs: {preview}{suffix}. "
                    "Restore the knowledge data directory from backup, then retry "
                    "the scan."
                )

            # Release the live-conversion claim of any case job whose last
            # live draft this sweep discarded — same transaction, same rule
            # as the explicit discard paths: a held unique slot on
            # conversion_jobs.live_case_id with no live draft behind it
            # would block that case's regeneration forever. Flush first so
            # the drained-count sees every status flipped above, even when
            # one job lost several drafts in this sweep.
            discarded_ids = pending_discard_ids + redundant_discard_ids
            if discarded_ids:
                await session.flush()
                drafts_by_id = {d.id: d for d in all_draft_models}
                released_job_ids: set[str] = set()
                for draft_id in discarded_ids:
                    job_id = drafts_by_id[draft_id].conversion_id
                    if job_id in released_job_ids:
                        continue
                    released_job_ids.add(job_id)
                    job = await session.get(ConversionJobModel, job_id)
                    if job is not None:
                        await _release_live_case_key_if_drained(session, job, draft_id)

            await session.commit()

    # Walk all scope directories
    knowledge_dir = data_dir
    if not knowledge_dir.exists():
        return {
            "discovered": 0,
            "skipped": 0,
            "errors": [],
            "drafts": [],
        }

    for md_file in sorted(knowledge_dir.rglob("*.md")):
        # Skip a ``sources`` directory: what it would hold is source uploads,
        # not runbooks. Nothing writes one (sources are not retained,
        # document-to-runbook-conversion.md §9.4); the skip only keeps such a
        # directory, if one is placed in the tree, out of the walk.
        if "sources" in md_file.parts:
            continue

        # The walk starts inside the tree, but ``rglob`` follows symlinks:
        # a link planted at ``data/knowledge/global/innocent.md`` pointing
        # at ``/etc/anything`` is yielded here, and before this check it was
        # read and minted into a draft row — the exact shape every other
        # path in this service refuses. Both halves of the module must agree
        # on whether a file is a runbook, so the walk asks the same guard
        # (#1213 follow-up).
        try:
            resolve_runbook_path(
                md_file,
                source=f"scanned file ({md_file.name})",
                root=knowledge_dir,
            )
        except RunbookPathEscape as exc:
            logger.warning("skipping a scanned file outside the tree: %s", exc)
            skipped += 1
            continue

        file_path_str = str(md_file)

        # Skip if already tracked in drafts DB (in-memory set from
        # reconciliation) or discovered earlier in this scan run.
        # Concurrent scans are serialized by _scan_lock.
        if file_path_str in tracked_paths:
            skipped += 1
            continue

        # Read and validate
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as e:
            # The file's NAME and the exception's CLASS, never the exception's
            # text (#836): an ``OSError`` names the file's server path, and
            # ``errors`` is returned to the client. The detail goes to the log.
            logger.warning("scan cannot read %s: %s", md_file, e)
            errors.append(f"{md_file.name}: cannot read ({type(e).__name__})")
            continue

        if len(content.strip()) < 100:
            errors.append(f"{md_file.name}: too short ({len(content)} chars)")
            continue

        # Extract metadata from frontmatter
        fm_match = match_frontmatter(content)
        metadata = {}
        if fm_match:
            try:
                metadata = yaml.safe_load(fm_match.group(1)) or {}
            except Exception:
                pass

        title = metadata.get("title", md_file.stem.replace("-", " ").title())
        runbook_id = metadata.get("id", md_file.stem)

        # Skip files already published into knowledge_items (e.g. by the
        # startup bootstrap, which ingests directly and never creates a
        # draft). Without this the scan manufactures a phantom draft for
        # every already-published runbook.
        if item_id_from_runbook_id(runbook_id) in ingested_item_ids:
            skipped += 1
            continue

        # Infer scope from directory path
        scope = "global"
        relative = md_file.relative_to(knowledge_dir)
        scope_dir_name = relative.parts[0] if len(relative.parts) > 1 else ""
        if scope_dir_name.startswith("personal_") or scope_dir_name.startswith("user_"):
            scope = "personal"
        elif scope_dir_name.startswith("team_"):
            scope = "team"

        # Global-tier authoring gate: a global-inferred file mints a draft
        # into the platform corpus (verified → readable by every tenant,
        # retrieved for every tenant). A caller who may not author global scope (any
        # tenant session under multi, or a non-admin single-tenant) skips it
        # rather than minting an ungated global draft; personal/team files
        # discovered in the same scan still proceed (#770, R4).
        if scope == "global" and not global_authoring_allowed:
            logger.info(
                "Scan skipped global-scope file %s: caller not permitted to "
                "author global (platform corpus) knowledge",
                md_file.name,
            )
            skipped += 1
            continue

        # Off the event loop, and validating ONCE (#1417). The scan
        # walks every runbook on disk, so this is the site where the
        # duplicated validation pass cost the most in aggregate.
        validation, quality = await avalidate_and_score(content)

        draft_id = generate_draft_id()
        quality_warning = None
        if quality.overall < QUALITY_WARNING_THRESHOLD:
            quality_warning = (
                "Quality score is below 50. Review and edit before verifying."
            )

        draft = ConversionDraft(
            draft_id=draft_id,
            runbook_id=runbook_id,
            title=title if isinstance(title, str) else str(title),
            scope=scope,
            status=DraftStatus.DRAFT,
            validation=validation,
            quality_score=quality,
            file_path=file_path_str,
            content_preview=content[:500],
            content=content,
            quality_warning=quality_warning,
        )

        # Extract metadata from frontmatter for dashboard filters
        from faultmaven.utils.frontmatter import extract_frontmatter_metadata

        fm_meta = extract_frontmatter_metadata(content)
        raw_tags = metadata.get("tags", [])
        # ConversionDraftModel.tags is a TagsArray TypeDecorator that
        # expects a list[str] — the decorator handles cross-dialect
        # serialization (TEXT[] on PG, comma-joined TEXT on SQLite).
        # Don't pre-join; pass the list shape directly.
        if isinstance(raw_tags, list):
            tags_list: Optional[List[str]] = [str(t) for t in raw_tags] or None
        elif raw_tags:
            tags_list = [str(raw_tags)]
        else:
            tags_list = None

        # Persist as a synthetic conversion job.
        #
        # A ``ConflictError`` here means another live draft in this tenant
        # already holds this file's ``runbook_id`` (migration 046) — two
        # on-disk runbooks carrying the same frontmatter ``id``. The scan
        # SKIPS what it cannot take, the way it does for a path it cannot
        # contain: one unmintable file must not abort the walk over the
        # rest, and the operator needs the filename to fix it.
        conversion_id = generate_conversion_id()
        try:
            await _persist_job(
                db_session_factory,
                share_repo,
                conversion_id=conversion_id,
                user_id=user_id,
                enterprise_id=enterprise_id,
                scope=scope,
                team_id=None,
                status=ConversionStatus.COMPLETED,
                source_file=SourceFileInfo(
                    filename=md_file.name,
                    size_bytes=md_file.stat().st_size,
                    content_type="text/markdown",
                ),
                analysis=AnalysisResult(
                    is_actionable=True,
                    failure_modes=[],
                    source_assessment=SourceAssessment(
                        content_type="file_scan",
                        actionability_rating="unknown",
                        missing_information=[],
                    ),
                ),
                drafts=[draft],
                created_at=datetime.now(timezone.utc),
            )
        except ConflictError as exc:
            logger.warning(
                "skipping a scanned runbook whose id is already taken: %s (%s)",
                md_file.name,
                exc,
            )
            errors.append(
                f"{md_file.name}: runbook id {runbook_id!r} is already held "
                f"by another live draft in this enterprise"
            )
            continue

        # Set metadata columns on the draft record
        if db_session_factory:
            async with db_session_factory() as session:
                result = await session.execute(
                    select(ConversionDraftModel).where(
                        ConversionDraftModel.id == draft_id
                    )
                )
                dm = result.scalar_one_or_none()
                if dm:
                    dm.domain = fm_meta.get("domain")
                    dm.service = fm_meta.get("service")
                    dm.severity = fm_meta.get("severity")
                    dm.tags = tags_list
                    dm.document_type = "runbook"
                    await session.commit()

        tracked_paths.add(file_path_str)
        discovered.append(
            {
                "conversion_id": conversion_id,
                "draft_id": draft_id,
                "title": draft.title,
                "runbook_id": runbook_id,
                "scope": scope,
                "validation_passed": validation.passed,
                "quality_score": quality.overall,
            }
        )

    return {
        "discovered": len(discovered),
        "skipped": skipped,
        "errors": errors,
        "drafts": discovered,
    }


async def _release_live_case_key_if_drained(
    session: AsyncSession,
    job: "ConversionJobModel",
    discarded_draft_id: str,
) -> None:
    """Clear ``job.live_case_id`` once the job holds no more live drafts.

    The unique-index slot on ``conversion_jobs.live_case_id`` is the case's
    one live-conversion claim; it must be released in the same transaction
    as the last live draft leaving so a later regeneration can take it. The
    clearing is general (count the OTHER non-discarded drafts of this job) so
    a job carrying more than one live draft keeps the key until the last one
    is gone. No-op for jobs that never held the key (document jobs, failed
    no-draft jobs)."""
    if job.live_case_id is None:
        return
    remaining_live = await session.execute(
        select(func.count())
        .select_from(ConversionDraftModel)
        .where(
            ConversionDraftModel.conversion_id == job.id,
            ConversionDraftModel.id != discarded_draft_id,
            ConversionDraftModel.status != DraftStatus.DISCARDED.value,
        )
    )
    if remaining_live.scalar_one() == 0:
        job.live_case_id = None
