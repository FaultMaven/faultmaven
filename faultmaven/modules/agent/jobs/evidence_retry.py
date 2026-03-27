"""Evidence Creation Retry Infrastructure

This module implements async retry logic for evidence creation failures,
preserving expensive LLM work and ensuring data consistency.

Design Reference:
- docs/architecture/data-processing/EVIDENCE-CREATION-FAILURE-MODES.md
- Scenario 2: LLM Call Timeout (Option B: Async Retry with Background Job)
- Scenario 4: Database Insert Fails After LLM

Key Features:
- Exponential backoff with configurable delays
- Idempotency via content_hash deduplication
- Preservation of LLM analysis results
- Automatic degradation after max retries
- Comprehensive metrics and alerting
"""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from faultmaven.container import container
from faultmaven.modules.case.contracts import (
    Case,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Retry Configuration
# =============================================================================

DEFAULT_LLM_MAX_RETRIES = 3
DEFAULT_LLM_BASE_DELAY = 60  # 1 minute

DEFAULT_DB_MAX_RETRIES = 5
DEFAULT_DB_BASE_DELAY = 10  # 10 seconds


# =============================================================================
# Task 7.2: Async Retry for LLM Analysis
# =============================================================================


async def retry_evidence_analysis(
    case_id: str,
    content_ref: str,
    content_hash: str,
    user_message: str,
    retry_count: int = 0,
    max_retries: int = DEFAULT_LLM_MAX_RETRIES,
    base_delay: int = DEFAULT_LLM_BASE_DELAY,
) -> Dict[str, Any]:
    """
    Background job to retry failed LLM analysis with exponential backoff.

    This function handles LLM timeouts and transient failures during evidence
    analysis. The file remains in storage during retries, and if all retries
    fail, a REJECTED evidence record is created to maintain audit trail.

    Retry Schedule (default):
    - Retry 1: 1 minute delay
    - Retry 2: 2 minutes delay
    - Retry 3: 4 minutes delay
    - After max retries: Create REJECTED evidence

    Args:
        case_id: Case identifier
        content_ref: Storage reference to the uploaded file
        content_hash: SHA-256 hash for deduplication
        user_message: Original user message context
        retry_count: Current retry attempt (0-indexed)
        max_retries: Maximum retry attempts before giving up
        base_delay: Base delay in seconds for exponential backoff

    Returns:
        Result dictionary with status and details

    Metrics:
        - evidence.llm_retry_attempts
        - evidence.llm_retry_successes
        - evidence.llm_retry_permanent_failures
    """
    logger.info(
        f"Retry {retry_count + 1}/{max_retries} for LLM analysis of {content_ref} "
        f"in case {case_id}"
    )

    # Increment retry attempt metric
    try:
        metrics_collector = container.get_metrics_collector()
        await metrics_collector.increment("evidence.llm_retry_attempts")
    except Exception as e:
        logger.warning(f"Failed to record metric: {e}")

    # Check if max retries reached
    if retry_count >= max_retries:
        logger.error(
            f"Max retries ({max_retries}) reached for {content_ref}. "
            f"Creating REJECTED evidence as fallback."
        )

        # Create REJECTED evidence to maintain audit trail
        try:
            await _create_rejected_evidence(
                case_id=case_id,
                content_ref=content_ref,
                content_hash=content_hash,
                reason=f"LLM analysis failed after {max_retries} retry attempts",
                metadata={
                    "retry_count": retry_count,
                    "failure_type": "llm_timeout",
                    "user_message_preview": user_message[:200],
                },
            )

            # Record permanent failure metric
            try:
                await metrics_collector.increment(
                    "evidence.llm_retry_permanent_failures"
                )
            except Exception:
                pass

            return {
                "status": "failed_max_retries",
                "evidence_created": "rejected",
                "content_ref": content_ref,
                "retry_count": retry_count,
            }

        except Exception as e:
            logger.exception(f"Failed to create REJECTED evidence: {e}")
            return {
                "status": "error",
                "error": str(e),
                "content_ref": content_ref,
            }

    # Attempt LLM analysis retry
    try:
        # Get required services from container
        case_service = container.get_case_service()
        llm_provider = container.get_llm_provider()

        # Retrieve case
        case = await case_service.get_case(case_id)
        if not case:
            logger.error(f"Case {case_id} not found for retry")
            return {"status": "error", "error": "Case not found"}

        # Retry LLM analysis with longer timeout for retries
        logger.info(f"Attempting LLM analysis (timeout: 60s)")

        # TODO: Replace with actual LLM analysis call when Phase 4 is complete
        # For now, this is a placeholder that would call the evidence classification
        # logic from the core investigation module
        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        engine = MilestoneEngine(llm_provider=llm_provider)

        # The actual LLM call would happen here as part of evidence processing
        # This would extract classification, category, source_type, etc.
        llm_result = await _analyze_evidence_with_llm(
            engine=engine,
            case=case,
            content_ref=content_ref,
            user_message=user_message,
            timeout=60,  # Longer timeout for retries
        )

        # Create evidence record from LLM result
        evidence = await _create_evidence_from_llm_result(
            case_id=case_id,
            content_ref=content_ref,
            content_hash=content_hash,
            llm_result=llm_result,
            case=case,
        )

        logger.info(
            f"LLM retry successful for {content_ref}. "
            f"Created evidence: {evidence.evidence_id}"
        )

        # Record success metric
        try:
            await metrics_collector.increment("evidence.llm_retry_successes")
        except Exception:
            pass

        return {
            "status": "success",
            "evidence_id": evidence.evidence_id,
            "content_ref": content_ref,
            "retry_count": retry_count + 1,
        }

    except asyncio.TimeoutError as e:
        logger.warning(f"LLM timeout on retry {retry_count + 1} for {content_ref}: {e}")

        # Schedule next retry with exponential backoff
        delay_seconds = (2**retry_count) * base_delay
        logger.info(f"Scheduling retry {retry_count + 2} with {delay_seconds}s delay")

        # Queue next retry via job service
        await _schedule_retry(
            job_func="retry_evidence_analysis",
            delay_seconds=delay_seconds,
            case_id=case_id,
            content_ref=content_ref,
            content_hash=content_hash,
            user_message=user_message,
            retry_count=retry_count + 1,
            max_retries=max_retries,
            base_delay=base_delay,
        )

        return {
            "status": "retry_scheduled",
            "next_retry": retry_count + 2,
            "delay_seconds": delay_seconds,
            "content_ref": content_ref,
        }

    except Exception as e:
        logger.exception(f"Unexpected error during LLM retry: {e}")

        # For unexpected errors, still schedule retry (might be transient)
        if retry_count < max_retries - 1:
            delay_seconds = (2**retry_count) * base_delay
            await _schedule_retry(
                job_func="retry_evidence_analysis",
                delay_seconds=delay_seconds,
                case_id=case_id,
                content_ref=content_ref,
                content_hash=content_hash,
                user_message=user_message,
                retry_count=retry_count + 1,
                max_retries=max_retries,
                base_delay=base_delay,
            )

            return {
                "status": "retry_scheduled",
                "next_retry": retry_count + 2,
                "delay_seconds": delay_seconds,
                "error": str(e),
            }
        else:
            # Last retry failed - create REJECTED evidence
            await _create_rejected_evidence(
                case_id=case_id,
                content_ref=content_ref,
                content_hash=content_hash,
                reason=f"LLM analysis error: {str(e)}",
            )

            return {
                "status": "failed_max_retries",
                "evidence_created": "rejected",
                "error": str(e),
            }


# =============================================================================
# Task 7.3: Async Retry for DB Insert
# =============================================================================


async def retry_evidence_creation(
    case_id: str,
    llm_result: Dict[str, Any],
    content_ref: str,
    content_hash: str,
    retry_count: int = 0,
    max_retries: int = DEFAULT_DB_MAX_RETRIES,
    base_delay: int = DEFAULT_DB_BASE_DELAY,
) -> Dict[str, Any]:
    """
    Retry database insert with exponential backoff, preserving LLM work.

    This function handles database failures that occur after successful (and
    expensive) LLM analysis. The LLM result is serialized and preserved across
    retries to avoid re-analyzing the same content.

    Idempotency is ensured via content_hash - if evidence already exists with
    the same hash, the retry is considered successful (duplicate insert race).

    Retry Schedule (default):
    - Retry 1: 10 seconds delay
    - Retry 2: 20 seconds delay
    - Retry 3: 40 seconds delay
    - Retry 4: 80 seconds delay
    - Retry 5: 160 seconds delay
    - After max retries: Critical alert, manual intervention required

    Args:
        case_id: Case identifier
        llm_result: Serialized LLM analysis output (category, source_type, etc.)
        content_ref: Storage reference to the file
        content_hash: SHA-256 hash for idempotency
        retry_count: Current retry attempt (0-indexed)
        max_retries: Maximum retry attempts before alerting
        base_delay: Base delay in seconds for exponential backoff

    Returns:
        Result dictionary with status and details

    Metrics:
        - evidence.db_retry_attempts
        - evidence.db_retry_successes
        - evidence.db_retry_permanent_failures

    Alerts:
        - Critical alert on permanent failure (requires manual intervention)
    """
    logger.info(
        f"Retry {retry_count + 1}/{max_retries} for DB insert of {content_ref} "
        f"in case {case_id}"
    )

    # Increment retry attempt metric
    try:
        metrics_collector = container.get_metrics_collector()
        await metrics_collector.increment("evidence.db_retry_attempts")
    except Exception as e:
        logger.warning(f"Failed to record metric: {e}")

    # Check if max retries reached
    if retry_count >= max_retries:
        logger.critical(
            f"Max retries ({max_retries}) reached for evidence creation. "
            f"Manual intervention required. "
            f"Case: {case_id}, File: {content_ref}, Hash: {content_hash}"
        )

        # Trigger critical alert
        try:
            alerting_service = container.get_alerting_service()
            await alerting_service.critical(
                title="Evidence creation failed permanently",
                message=f"Database insert failed after {max_retries} retries",
                details={
                    "case_id": case_id,
                    "content_ref": content_ref,
                    "content_hash": content_hash,
                    "retry_count": retry_count,
                    "llm_result": llm_result,
                },
                severity="critical",
                team="platform",
            )
        except Exception as e:
            logger.error(f"Failed to send critical alert: {e}")

        # Record permanent failure metric
        try:
            await metrics_collector.increment("evidence.db_retry_permanent_failures")
        except Exception:
            pass

        return {
            "status": "failed_max_retries",
            "alert": "critical_alert_sent",
            "case_id": case_id,
            "content_ref": content_ref,
            "retry_count": retry_count,
        }

    # Attempt database insert retry
    try:
        # Get required services
        case_service = container.get_case_service()

        # Retrieve case
        case = await case_service.get_case(case_id)
        if not case:
            logger.error(f"Case {case_id} not found for retry")
            return {"status": "error", "error": "Case not found"}

        # Check for existing evidence (idempotency via content_hash)
        existing_evidence = await _find_evidence_by_hash(
            case_id=case_id, content_hash=content_hash
        )

        if existing_evidence:
            logger.info(
                f"Evidence already exists for hash {content_hash}: "
                f"{existing_evidence.evidence_id}. Retry is idempotent success."
            )

            # Record success metric (idempotent duplicate)
            try:
                await metrics_collector.increment("evidence.db_retry_successes")
            except Exception:
                pass

            return {
                "status": "success_idempotent",
                "evidence_id": existing_evidence.evidence_id,
                "content_ref": content_ref,
                "reason": "Evidence already exists (duplicate insert race)",
            }

        # Create evidence from preserved LLM result
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            case_id=case_id,
            content_ref=content_ref,
            content_hash=content_hash,
            category=EvidenceCategory(llm_result["category"]),
            source_type=EvidenceSourceType(llm_result["source_type"]),
            summary=llm_result["summary"],
            primary_purpose=llm_result.get("primary_purpose", ""),
            collected_at=datetime.now(UTC),
            collected_by=case.created_by,
            collected_at_turn=case.current_turn,
            # Additional fields from LLM result
            **{
                k: v
                for k, v in llm_result.items()
                if k not in ["category", "source_type", "summary", "primary_purpose"]
            },
        )

        # Persist to database
        case_repository = container.get_case_repository()
        await case_repository.add_evidence(case_id=case_id, evidence=evidence)

        logger.info(
            f"DB retry successful for {content_ref}. "
            f"Created evidence: {evidence.evidence_id}"
        )

        # Record success metric
        try:
            await metrics_collector.increment("evidence.db_retry_successes")
        except Exception:
            pass

        return {
            "status": "success",
            "evidence_id": evidence.evidence_id,
            "content_ref": content_ref,
            "retry_count": retry_count + 1,
        }

    except Exception as e:
        logger.warning(f"DB insert failed on retry {retry_count + 1}: {e}")

        # Schedule next retry with exponential backoff
        delay_seconds = (2**retry_count) * base_delay
        logger.info(f"Scheduling retry {retry_count + 2} with {delay_seconds}s delay")

        # Queue next retry
        await _schedule_retry(
            job_func="retry_evidence_creation",
            delay_seconds=delay_seconds,
            case_id=case_id,
            llm_result=llm_result,
            content_ref=content_ref,
            content_hash=content_hash,
            retry_count=retry_count + 1,
            max_retries=max_retries,
            base_delay=base_delay,
        )

        return {
            "status": "retry_scheduled",
            "next_retry": retry_count + 2,
            "delay_seconds": delay_seconds,
            "error": str(e),
        }


# =============================================================================
# Helper Functions
# =============================================================================


async def _create_rejected_evidence(
    case_id: str,
    content_ref: str,
    content_hash: str,
    reason: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Evidence:
    """Create REJECTED evidence for failed analysis attempts."""
    case_repository = container.get_case_repository()
    case_service = container.get_case_service()

    case = await case_service.get_case(case_id)

    evidence = Evidence(
        evidence_id=f"ev_{uuid4().hex[:12]}",
        case_id=case_id,
        content_ref=content_ref,
        content_hash=content_hash,
        category=EvidenceCategory.REJECTED,
        source_type=EvidenceSourceType.LOGS,  # Default for unknown
        summary=f"Evidence analysis failed: {reason}",
        primary_purpose=reason,
        collected_at=datetime.now(UTC),
        collected_by=case.created_by,
        collected_at_turn=case.current_turn,
        metadata=metadata,
    )

    await case_repository.add_evidence(case_id=case_id, evidence=evidence)

    logger.info(f"Created REJECTED evidence: {evidence.evidence_id}")

    return evidence


async def _find_evidence_by_hash(case_id: str, content_hash: str) -> Optional[Evidence]:
    """Find evidence by content hash for idempotency check."""
    case_repository = container.get_case_repository()

    # Get all evidence for case
    case = await case_repository.get_case(case_id)
    if not case:
        return None

    # Search for matching hash
    for evidence in case.evidence:
        if evidence.content_hash == content_hash:
            return evidence

    return None


async def _analyze_evidence_with_llm(
    engine: Any,
    case: Case,
    content_ref: str,
    user_message: str,
    timeout: int = 60,
) -> Dict[str, Any]:
    """
    Analyze evidence using LLM with timeout.

    TODO: This is a placeholder for Phase 4 integration.
    When Phase 4 is complete, this should call the actual evidence
    classification logic from milestone_engine.py or a dedicated
    evidence analysis service.

    For now, returns a mock result structure.
    """
    # Placeholder - replace with actual LLM call
    logger.warning("Using placeholder LLM analysis - replace when Phase 4 is complete")

    return {
        "category": "symptom_evidence",
        "source_type": "logs",
        "summary": f"Evidence from {content_ref}",
        "primary_purpose": "Analysis placeholder",
        "likelihood": 0.8,
    }


async def _create_evidence_from_llm_result(
    case_id: str,
    content_ref: str,
    content_hash: str,
    llm_result: Dict[str, Any],
    case: Case,
) -> Evidence:
    """Create evidence record from LLM analysis result."""
    case_repository = container.get_case_repository()

    evidence = Evidence(
        evidence_id=f"ev_{uuid4().hex[:12]}",
        case_id=case_id,
        content_ref=content_ref,
        content_hash=content_hash,
        category=EvidenceCategory(llm_result["category"]),
        source_type=EvidenceSourceType(llm_result["source_type"]),
        summary=llm_result["summary"],
        primary_purpose=llm_result.get("primary_purpose", ""),
        collected_at=datetime.now(UTC),
        collected_by=case.created_by,
        collected_at_turn=case.current_turn,
    )

    await case_repository.add_evidence(case_id=case_id, evidence=evidence)

    return evidence


async def _schedule_retry(
    job_func: str,
    delay_seconds: int,
    **kwargs: Any,
) -> None:
    """
    Schedule a retry job with delay.

    TODO: Integrate with actual job queue infrastructure (Celery, Redis Queue, etc.)
    For now, uses asyncio.sleep as a placeholder.
    """
    logger.info(f"Scheduling {job_func} retry in {delay_seconds}s with args: {kwargs}")

    # Placeholder - replace with actual job queue
    # Options:
    # 1. Celery with Redis broker: apply_async(countdown=delay_seconds)
    # 2. Redis Queue: enqueue_in(timedelta(seconds=delay_seconds))
    # 3. APScheduler: scheduler.add_job(func, 'date', run_date=now + delay)

    # For development/testing, use asyncio task
    async def delayed_retry():
        await asyncio.sleep(delay_seconds)
        if job_func == "retry_evidence_analysis":
            await retry_evidence_analysis(**kwargs)
        elif job_func == "retry_evidence_creation":
            await retry_evidence_creation(**kwargs)

    asyncio.create_task(delayed_retry())
