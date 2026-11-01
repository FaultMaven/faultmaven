"""The conversion job + drafts database write: one transaction, plus
IntegrityError classification after the session has been returned to
the pool. The job's team share and a keyed conversion's receipt (#2433)
commit in the same transaction."""

from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from faultmaven.config.tenant_context import (
    get_current_billing_organization_id,
    writable_enterprise_id,
)
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
    UploadedFileModel,
)
from faultmaven.infrastructure.persistence.share_repository import (
    PostgreSQLShareRepository,
)
from faultmaven.modules.knowledge.domain.item_title import sanitize_item_title
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionDraft,
    ConversionStatus,
    DraftStatus,
    SourceFileInfo,
)
from faultmaven.modules.knowledge.domain.request_receipts import (
    KbReceiptWrite,
    add_receipt,
    read_receipt,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.draft_slots import (
    _raise_if_runbook_id_taken,
)
from faultmaven.modules.knowledge.exceptions import KbReceiptExistsError


async def _persist_job(
    db_session_factory,
    share_repo,
    conversion_id: str,
    user_id: str,
    enterprise_id: str,
    scope: str,
    team_id: str,
    status: ConversionStatus,
    source_file: SourceFileInfo,
    analysis: AnalysisResult,
    drafts: List[ConversionDraft],
    created_at: datetime,
    source_type: str = "document",
    case_id: str = None,
    warnings: Optional[List[str]] = None,
    *,
    sanitizer,
    receipt: Optional[KbReceiptWrite] = None,
) -> None:
    """Persist conversion job and drafts to database.

    ``sanitizer`` is required, with no default, because a draft's title is
    disclosed and this is the one place a ``conversion_drafts`` row is written:
    a caller that could leave it out would be a path that stores the title raw
    (#1981). See :func:`_persist_job_rows`.

    ``receipt`` is a keyed ``POST /knowledge/convert``'s (#2433), written with
    the job. Raises :class:`KbReceiptExistsError` when another conversion under
    its key published first: nothing of this one was.
    """
    if not db_session_factory:
        return

    enterprise_id = writable_enterprise_id(enterprise_id)

    try:
        await _persist_job_rows(
            db_session_factory,
            enterprise_id=enterprise_id,
            conversion_id=conversion_id,
            user_id=user_id,
            scope=scope,
            # Team publish target: a share row on the conversion_job (source of
            # truth, ADR-013 §D4), transferred to the promoted knowledge_item on
            # verify. Inert when no share repo is wired.
            share_team=team_id if scope == "team" and share_repo else None,
            status=status,
            source_file=source_file,
            analysis=analysis,
            drafts=drafts,
            created_at=created_at,
            source_type=source_type,
            case_id=case_id,
            warnings=warnings,
            sanitizer=sanitizer,
            receipt=receipt,
        )
    except IntegrityError as refused:
        # Classified AFTER the session block has exited, never inside it:
        # the classifier opens a SECOND session, and holding both at once
        # deadlocks a deployment pooled at one connection.
        if receipt is not None and await read_receipt(db_session_factory, receipt.key):
            raise KbReceiptExistsError(
                receipt.key.route_name, receipt.key.idempotency_key
            ) from refused
        await _raise_if_runbook_id_taken(
            db_session_factory, enterprise_id, [d.runbook_id for d in drafts]
        )
        raise


async def _persist_job_rows(
    db_session_factory,
    *,
    enterprise_id: str,
    conversion_id: str,
    user_id: str,
    scope: str,
    share_team: Optional[str],
    status: ConversionStatus,
    source_file: SourceFileInfo,
    analysis: AnalysisResult,
    drafts: List[ConversionDraft],
    created_at: datetime,
    source_type: str,
    case_id: Optional[str],
    warnings: Optional[List[str]] = None,
    sanitizer,
    receipt: Optional[KbReceiptWrite] = None,
) -> None:
    """The upload + job + drafts write, as ONE transaction and one session,
    with the job's team share (``share_team``) and a keyed conversion's
    receipt in it: a job never stands without its share, and a receipt never
    replays a job that did not commit with everything it names (#2433).

    Split out of ``_persist_job`` only so its caller can classify an
    ``IntegrityError`` after this session has been returned to the pool.

    Every draft's title is redacted here, before the session opens (#1981). A
    draft is NOT author-only: ``GET /knowledge/drafts`` lists every global-scope
    draft to every user, and a case's drafts are listed to its readers
    (``GET /cases/{id}/reports``), so a title the author typed or a model wrote
    is disclosed from the moment this row exists, well before the draft is
    published. It is the one ``ConversionDraftModel`` written by conversion, so
    one call covers the LLM conversions, the manual template and the disk scan.
    The scrub fails closed (``ItemTitleScrubUnavailable``) and runs ahead of the
    first write, so a refusal leaves nothing behind.

    This is the BACKSTOP, not the entry. Each path scrubs the title where it
    arrives as raw text and mints the draft's id and text from the scrubbed
    value (a slug of ``password=hunter2`` is ``...-password-hunter2``, which no
    pattern matches), so by the time a draft reaches here its title is already
    clean and this only has to cut it to the column. It assigns the stored
    value back to the draft, so the object the caller returns and the row are
    one text.
    """
    # Scrubbed to the column, and ASSIGNED BACK to the draft the caller holds
    # (the response, the case-transcript notice and the log line are built from
    # it): the object and the row are one text, never the row alone.
    for draft in drafts:
        draft.title = await sanitize_item_title(
            sanitizer, draft.title, ConversionDraftModel.title
        )

    async with db_session_factory() as session:
        # Billing attribution for every row this writes (ADR-017 D2), read
        # from the request binding exactly as ``CaseService.create_case``
        # and the audit writer read it. ``None`` is the ordinary answer —
        # nobody pays for this account — and it decides nothing about
        # visibility, which is why it comes from a different binding than
        # ``enterprise_id`` and is never a predicate anywhere below.
        organization_id = get_current_billing_organization_id()

        # ``conversion_jobs`` carries a single ``source_file_id`` FK to
        # ``uploaded_files`` (ON DELETE RESTRICT). Create the upload row
        # first; the conversion_jobs row references it. Both tables
        # require enterprise_id NOT NULL.
        source_file_id = f"file_{uuid4().hex[:12]}"
        upload = UploadedFileModel(
            file_id=source_file_id,
            enterprise_id=enterprise_id,
            organization_id=organization_id,
            case_id=None,  # KB-bound, not case-bound
            uploaded_by=user_id,
            filename=source_file.filename,
            size_bytes=source_file.size_bytes,
            content_type=source_file.content_type,
            # ``storage_ref`` is a storage-backend key or NULL. No backend
            # holds a conversion source, so it is NULL — never the path the
            # file happens to sit at on this replica's disk (#836).
            storage_ref=None,
            upload_source="conversion_source",
            uploaded_at_turn=0,
        )
        session.add(upload)
        await session.flush()  # ensure upload row exists before FK ref

        # ``live_case_id`` holds the case only while this case-source job has
        # a live (non-discarded) draft; it is the value the unique index
        # dedups on. Freshly generated drafts are DRAFT (live), a failed
        # conversion persists zero drafts (never blocks regeneration), and
        # document jobs carry no case — all three resolve to NULL here.
        live_case_id = (
            case_id
            if source_type == "case"
            and case_id
            and any(d.status != DraftStatus.DISCARDED for d in drafts)
            else None
        )

        job = ConversionJobModel(
            id=conversion_id,
            user_id=user_id,
            enterprise_id=enterprise_id,
            organization_id=organization_id,
            scope=scope,
            status=status.value,
            source_file_id=source_file_id,
            source_type=source_type,
            case_id=case_id,
            live_case_id=live_case_id,
            failure_modes_detected=len(analysis.failure_modes),
            analysis_result=analysis.model_dump(),
            # ``None`` rather than ``[]`` when there is nothing to say, so a
            # job with no warnings and a pre-048 job are not conflated at
            # the storage layer (migration 048).
            warnings=list(warnings) if warnings else None,
            created_at=created_at,
            completed_at=datetime.now(timezone.utc),
        )
        session.add(job)

        for draft in drafts:
            draft_model = ConversionDraftModel(
                id=draft.draft_id,
                enterprise_id=enterprise_id,
                organization_id=organization_id,
                conversion_id=conversion_id,
                runbook_id=draft.runbook_id,
                title=draft.title,
                # The draft's text, in the same transaction as its row (#1950):
                # there is no draft whose text a replica cannot read.
                content=draft.content,
                status=draft.status.value,
                source_type=source_type,
                validation_passed=draft.validation.passed,
                validation_errors=draft.validation.errors,
                validation_warnings=draft.validation.warnings,
                quality_score=draft.quality_score.overall,
                quality_details=draft.quality_score.model_dump(),
                created_at=created_at,
            )
            session.add(draft_model)

        if receipt is not None:
            # Built here, once the titles are scrubbed and assigned back: the
            # stored body is the body the request returns.
            add_receipt(
                session,
                receipt.key,
                response=receipt.build_response(),
                conversion_id=conversion_id,
            )
        if share_team:
            # Bound to this session, ``share`` never commits (#2396).
            await PostgreSQLShareRepository(session).share(
                resource_type="conversion_job",
                resource_id=conversion_id,
                scope_type="team",
                scope_id=share_team,
                enterprise_id=enterprise_id,
                created_by=user_id,
            )

        await session.commit()
