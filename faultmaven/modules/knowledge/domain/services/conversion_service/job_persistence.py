"""The conversion job + drafts database write: one transaction, plus
IntegrityError classification after the session has been returned to
the pool."""

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
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionDraft,
    ConversionStatus,
    DraftStatus,
    SourceFileInfo,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.draft_slots import (
    _raise_if_runbook_id_taken,
)


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
) -> None:
    """Persist conversion job and drafts to database."""
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
            status=status,
            source_file=source_file,
            analysis=analysis,
            drafts=drafts,
            created_at=created_at,
            source_type=source_type,
            case_id=case_id,
            warnings=warnings,
        )
    except IntegrityError:
        # Classified AFTER the session block has exited, never inside it:
        # the classifier opens a SECOND session, and holding both at once
        # deadlocks a deployment pooled at one connection.
        await _raise_if_runbook_id_taken(
            db_session_factory, enterprise_id, [d.runbook_id for d in drafts]
        )
        raise

    # Team publish target: record it as a share row on the conversion_job
    # (source of truth, ADR-013 §D4). On verify, it is transferred to the
    # promoted knowledge_item. Outside the session block — the share repo is
    # sessionless. Inert (no-op) when no share repo is wired.
    if scope == "team" and team_id and share_repo:
        await share_repo.share(
            resource_type="conversion_job",
            resource_id=conversion_id,
            scope_type="team",
            scope_id=team_id,
            enterprise_id=enterprise_id,
            created_by=user_id,
        )


async def _persist_job_rows(
    db_session_factory,
    *,
    enterprise_id: str,
    conversion_id: str,
    user_id: str,
    scope: str,
    status: ConversionStatus,
    source_file: SourceFileInfo,
    analysis: AnalysisResult,
    drafts: List[ConversionDraft],
    created_at: datetime,
    source_type: str,
    case_id: Optional[str],
    warnings: Optional[List[str]] = None,
) -> None:
    """The upload + job + drafts write, as ONE transaction and one session.

    Split out of ``_persist_job`` only so its caller can classify an
    ``IntegrityError`` after this session has been returned to the pool.
    """
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
            storage_ref=source_file.retained_path or None,
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
                file_path=draft.file_path,
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

        await session.commit()
