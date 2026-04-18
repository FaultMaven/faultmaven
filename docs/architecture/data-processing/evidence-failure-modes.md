# Evidence Creation Failure Modes and Recovery

**Version:** 1.3
**Date:** 2026-04-18
**Status:** Design Specification — partial implementation; remainder deferred post-MVP
**Context:** Failure analysis and recovery strategies for single-phase evidence creation

---

## Current Implementation Status (validated 2026-04-18)

| Area | Status | Notes |
| --- | --- | --- |
| Scenario 1 — File upload fails | Implicit (no code change needed) | Storage failures raise before any evidence object exists. No orphan state. |
| Scenario 2 — LLM call timeout | **Partial** | Tier 0+1 is zero-LLM (classification + extraction), so the legacy "LLM timeout at ingest" path in this doc no longer applies. A separate timeout exists for Tier 1 extractors (2 s) with a TEXT-preview fallback — not the LLM-timeout failure mode this doc was originally written for. Turn-time LLM calls in `milestone_engine.py` can still time out; async-retry is not yet wired. |
| Scenario 3 — LLM returns invalid category | **Done** | `EvidenceToAdd.validate_category` in `core/investigation/schemas.py:306` falls back to `CONTEXTUAL_EVIDENCE` with a warning log. |
| Scenario 4 — DB insert fails after LLM / storage | Deferred | No idempotency key, no orphan cleanup. Orphan files can accumulate in storage if DB writes fail after `store_file` succeeds. |
| Content-hash deduplication — hash consistency | **Done** | `PreprocessingService.classify_and_extract` computes `SHA-256(UTF-8 text)` uniformly for file uploads and pasted content. Both paths produce the same hash for the same content. |
| Content-hash deduplication — repository lookup | **Deferred** | `PreprocessingResult.content_hash` is computed but no repository consults it. `find_by_content_hash()` is not implemented. Re-uploading the same file today produces a second Evidence row. |
| Storage cleanup (TTL / reference counting) | Deferred | Neither approach below is implemented. |
| Retry infrastructure (async retry job) | Deferred | No `retry_evidence_analysis` / `retry_evidence_creation` tasks exist. |
| `file_references` table | Deferred | Not created. |

The rest of the document describes the **target** failure-handling design. Treat scenarios marked "Deferred" as design commitments, not current behavior; scenarios marked "Done"/"Partial" have code references above.

---

## The Problem

Single-phase evidence creation has a dependency chain:

```
File upload → Storage (local/S3) → Preprocessing (Tier 0+1, zero LLM) → DB insert → (later) LLM turn processing
```

Each step can fail, leaving the system in an inconsistent state. This document defines explicit error recovery strategies for each failure point.

> **Note:** The 2026-02 version of this document placed LLM analysis *between* storage and DB insert. That was for an earlier architecture where an LLM classifier ran at ingest. As of v4.1 (Unified Ingestion Pipeline), classification is purely rule-based and runs before storage — no LLM calls happen during evidence creation. LLM calls happen at turn processing time (`milestone_engine.process_turn`). This doc has been partially rewritten; scenarios below retain the original framing for reference and will be refactored when implementation begins.

---

## Failure Scenarios

### Scenario 1: File Upload Fails

**Failure Point:** File upload to S3 fails (network, auth, quota, etc.)

**State:**
- ❌ No file in S3
- ❌ No evidence record in DB
- ❌ No LLM tokens spent

**User Experience:**
- Error message: "Failed to upload file. Please try again."

**Recovery:**
- **None needed** - Clean failure before any state change
- User retries from client
- No orphaned resources

**Implementation:**
```python
async def process_turn_with_attachment(case_id, user_message, file):
    # Step 1: Upload file
    try:
        content_ref = await storage_service.upload(file)
    except StorageError as e:
        logger.error(f"File upload failed: {e}")
        raise UserFacingError("Failed to upload file. Please try again.")

    # Step 2: Continue to LLM...
```

---

### Scenario 2: LLM Call Timeout

**Failure Point:** LLM call times out or fails after file uploaded to S3

**State:**
- ✅ File in S3
- ❌ No evidence record in DB
- ❌ LLM tokens spent (partial)

**Problem:**
- **Orphaned file** in S3 (storage cost, clutter)
- User doesn't know what happened

**User Experience:**
- Error message: "Analysis timed out. Your file was saved and will be analyzed shortly."
- Or: "Analysis failed. Please try again."

**Recovery Strategy:**

#### Option A: Cleanup on Failure (Immediate)
```python
async def process_turn_with_attachment(case_id, user_message, file):
    content_ref = None
    try:
        # Step 1: Upload file
        content_ref = await storage_service.upload(file)

        # Step 2: LLM analysis
        result = await llm_service.analyze(
            case, user_message, content_ref,
            timeout=30  # 30 second timeout
        )

        # Step 3: Create evidence
        await create_evidence(case, result)

    except LLMTimeout as e:
        logger.error(f"LLM timeout: {e}")
        # Cleanup: Delete uploaded file
        if content_ref:
            await storage_service.delete(content_ref)
        raise UserFacingError("Analysis timed out. Please try again with your file.")

    except LLMError as e:
        logger.error(f"LLM error: {e}")
        # Cleanup: Delete uploaded file
        if content_ref:
            await storage_service.delete(content_ref)
        raise UserFacingError("Analysis failed. Please try again.")
```

**Pros:**
- ✅ No orphaned files
- ✅ Clean state (nothing persisted on failure)
- ✅ Simple retry (user just re-uploads)

**Cons:**
- ❌ User must re-upload file (poor UX for large files)
- ❌ Wasted upload bandwidth

#### Option B: Async Retry with Background Job (Better UX)
```python
async def process_turn_with_attachment(case_id, user_message, file):
    # Step 1: Upload file
    content_ref = await storage_service.upload(file)
    content_hash = compute_hash(file)

    # Step 2: LLM analysis with retry
    try:
        result = await llm_service.analyze(
            case, user_message, content_ref,
            timeout=30
        )
    except LLMTimeout:
        # Queue for async retry (don't fail user's request)
        logger.warning(f"LLM timeout, queueing retry for {content_ref}")
        await job_queue.enqueue(
            "retry_evidence_analysis",
            case_id=case_id,
            content_ref=content_ref,
            content_hash=content_hash,
            user_message=user_message,
            retry_count=0,
            max_retries=3
        )
        # Return partial response to user
        return {
            "status": "analyzing",
            "message": "Your file is being analyzed. Check back shortly."
        }

    # Step 3: Create evidence
    await create_evidence(case, result)
```

**Background Job:**
```python
async def retry_evidence_analysis(case_id, content_ref, content_hash, user_message, retry_count, max_retries):
    """Background job to retry failed LLM analysis"""

    if retry_count >= max_retries:
        logger.error(f"Max retries reached for {content_ref}, creating REJECTED evidence")
        # Create REJECTED evidence as fallback
        await create_rejected_evidence(
            case_id=case_id,
            content_ref=content_ref,
            content_hash=content_hash,
            reason="Analysis failed after multiple retries"
        )
        # Optionally notify user
        return

    try:
        case = await case_service.get(case_id)
        result = await llm_service.analyze(case, user_message, content_ref, timeout=60)
        await create_evidence(case, result)
        logger.info(f"Retry successful for {content_ref}")

    except LLMError as e:
        logger.warning(f"Retry {retry_count + 1} failed for {content_ref}: {e}")
        # Exponential backoff
        delay = 2 ** retry_count * 60  # 1min, 2min, 4min
        await job_queue.enqueue_delayed(
            "retry_evidence_analysis",
            delay_seconds=delay,
            case_id=case_id,
            content_ref=content_ref,
            content_hash=content_hash,
            user_message=user_message,
            retry_count=retry_count + 1,
            max_retries=max_retries
        )
```

**Pros:**
- ✅ Better UX (user doesn't re-upload)
- ✅ Automatic retry with exponential backoff
- ✅ File stays in S3 for eventual processing
- ✅ Graceful degradation (REJECTED after max retries)

**Cons:**
- ❌ More complex (background job queue)
- ❌ Temporary inconsistency (file exists, no evidence yet)

**Recommended:** Option B for production (better UX, resilient)

---

### Scenario 3: LLM Returns Invalid Category

**Failure Point:** LLM returns unrecognized/malformed category value

**State:**
- ✅ File in S3
- ✅ LLM tokens spent
- ❌ Can't create evidence (validation fails)

**Problem:**
- Lost LLM work (can't retry without re-spending tokens)
- Invalid data from LLM (schema validation failure)

**User Experience:**
- Should NOT see "LLM returned invalid data" (internal error)
- Should see: "Evidence was analyzed and saved"

**Recovery Strategy: Category Fallback**

```python
# In schemas.py
class EvidenceToAdd(BaseModel):
    category: EvidenceCategory

    @validator('category', pre=True)
    def validate_category(cls, v):
        """Fallback to CONTEXTUAL_EVIDENCE for unrecognized categories"""
        if isinstance(v, str):
            # Try exact match
            try:
                return EvidenceCategory(v)
            except ValueError:
                # Unrecognized category - fallback
                logger.warning(
                    f"LLM returned unrecognized category '{v}', "
                    f"falling back to CONTEXTUAL_EVIDENCE"
                )
                return EvidenceCategory.CONTEXTUAL_EVIDENCE
        return v
```

**Why CONTEXTUAL_EVIDENCE as fallback?**
- Not REJECTED (user uploaded it intentionally)
- Not SYMPTOM/CAUSAL/RESOLUTION (don't want false positives in investigation)
- CONTEXTUAL is neutral ("we have this data, not sure what it means yet")

**Alternative: Log + Manual Review**
```python
@validator('category', pre=True)
def validate_category(cls, v):
    if isinstance(v, str):
        try:
            return EvidenceCategory(v)
        except ValueError:
            # Create alert for manual review
            logger.error(
                f"LLM returned invalid category '{v}'. "
                f"This may indicate prompt drift or schema mismatch.",
                extra={
                    "category_attempted": v,
                    "alert_team": "llm_integration",
                    "severity": "high"
                }
            )
            # Still fallback to CONTEXTUAL_EVIDENCE
            return EvidenceCategory.CONTEXTUAL_EVIDENCE
    return v
```

---

### Scenario 4: Database Insert Fails After LLM

**Failure Point:** DB insert fails after successful LLM analysis

**State:**
- ✅ File in S3
- ✅ LLM tokens spent
- ✅ LLM analysis complete
- ❌ No evidence record in DB

**Problem:**
- **Lost LLM work** (expensive, non-recoverable)
- User thinks submission succeeded (file uploaded)
- Evidence analysis lost

**User Experience:**
- Error message: "Failed to save evidence. Your file was analyzed and will be saved shortly."

**Recovery Strategy: Retry with Idempotency**

```python
async def process_turn_with_attachment(case_id, user_message, file):
    content_ref = None
    content_hash = None
    llm_result = None

    try:
        # Step 1: Upload file
        content_ref = await storage_service.upload(file)
        content_hash = compute_hash(file)

        # Step 2: LLM analysis
        llm_result = await llm_service.analyze(
            case, user_message, content_ref
        )

        # Step 3: Create evidence (with retry)
        await create_evidence_with_retry(
            case=case,
            llm_result=llm_result,
            content_ref=content_ref,
            content_hash=content_hash,
            max_retries=3
        )

    except DBError as e:
        logger.error(f"DB insert failed: {e}")

        # Queue for background retry (preserve LLM work)
        await job_queue.enqueue(
            "retry_evidence_creation",
            case_id=case_id,
            llm_result=llm_result.model_dump(),  # Serialize LLM output
            content_ref=content_ref,
            content_hash=content_hash,
            retry_count=0,
            max_retries=5
        )

        # Return partial success to user
        return {
            "status": "processing",
            "message": "Your file is being processed. Evidence will appear shortly."
        }
```

**Background Retry:**
```python
async def retry_evidence_creation(case_id, llm_result, content_ref, content_hash, retry_count, max_retries):
    """Retry DB insert with exponential backoff"""

    if retry_count >= max_retries:
        logger.critical(
            f"Max retries reached for evidence creation. "
            f"Manual intervention required. "
            f"Case: {case_id}, File: {content_ref}"
        )
        # Alert ops team
        await alerting.critical(
            "Evidence creation failed permanently",
            details={"case_id": case_id, "content_ref": content_ref}
        )
        return

    try:
        case = await case_service.get(case_id)

        # Check if evidence already exists (idempotency via content_hash)
        existing = await evidence_repo.find_by_content_hash(case_id, content_hash)
        if existing:
            logger.info(f"Evidence already exists for {content_hash}, skipping")
            return

        # Retry insert
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            case_id=case_id,
            content_ref=content_ref,
            content_hash=content_hash,
            **llm_result.model_dump()
        )
        await evidence_repo.create(evidence)
        logger.info(f"Retry successful for evidence creation: {evidence.evidence_id}")

    except DBError as e:
        logger.warning(f"Retry {retry_count + 1} failed: {e}")
        # Exponential backoff
        delay = 2 ** retry_count * 10  # 10s, 20s, 40s, 80s, 160s
        await job_queue.enqueue_delayed(
            "retry_evidence_creation",
            delay_seconds=delay,
            case_id=case_id,
            llm_result=llm_result,
            content_ref=content_ref,
            content_hash=content_hash,
            retry_count=retry_count + 1,
            max_retries=max_retries
        )
```

**Key: Idempotency via content_hash**
- UNIQUE constraint on (case_id, content_hash)
- Retry can safely re-run (duplicate will be rejected by DB)
- No need to track "retry state" explicitly

---

## Content Hash Strategy for Deduplication

### Issue: Pasted Content Hash Fragility

**Problem:** For pasted content ("Here are my logs: [200 lines]"), computing SHA-256 of only the extracted data portion is fragile:

```python
# Fragile approach (regex extraction can vary)
extracted_data = extract_external_data(user_message)  # Regex-based
content_hash = sha256(extracted_data)
```

**Why it's fragile:**
- Extraction boundaries can shift slightly on retry
- Different regex patterns might extract slightly different text
- Leading/trailing whitespace handling inconsistencies
- Line break normalization differences

**Example failure:**
```
Turn 1: "Here are logs:\n\n[logs...]" → extracts "[logs...]" → hash_A
Retry:  "Here are logs:\n\n[logs...]" → extracts "\n[logs...]" → hash_B
Result: Different hashes, deduplication fails
```

### Solution: Hash Raw Submission for Pasted Content

**For ALL submission types, hash the ENTIRE raw user message:**

```python
async def compute_content_hash(
    user_message: str,
    submission_type: Literal["user_text", "submitted_data"]
) -> str:
    """
    Compute content hash for deduplication.

    IMPORTANT: Always hash the ENTIRE user message, not extracted portions.
    This ensures consistent hashing even if extraction logic changes.

    Args:
        user_message: Raw user message (entire submission)
        submission_type: Classification type (for logging only)

    Returns:
        SHA-256 hex digest of raw message
    """
    # Hash raw message (entire submission)
    content_bytes = user_message.encode('utf-8')
    hash_digest = hashlib.sha256(content_bytes).hexdigest()

    logger.debug(
        f"Computed content hash for {submission_type}: {hash_digest[:8]}... "
        f"(message length: {len(user_message)} chars)"
    )

    return hash_digest
```

**Why this works:**
- ✅ Consistent: Same input always produces same hash
- ✅ Simple: No extraction logic involved in hashing
- ✅ Complete: Captures entire submission including context
- ✅ Robust: Immune to extraction algorithm changes

**Deduplication behavior:**

| Scenario | Raw Message | Hash | Dedupe? |
|----------|-------------|------|---------|
| Exact resubmission | "Here are logs:\n[logs...]" | hash_A | ✅ Yes |
| Same message, retry | "Here are logs:\n[logs...]" | hash_A | ✅ Yes |
| Slightly different wording | "Here's the logs:\n[logs...]" | hash_B | ❌ No (intentional - different submission) |
| Same logs, different intro | "Logs below:\n[logs...]" | hash_C | ❌ No (different context) |

**Trade-off accepted:**
- User rephrasing intro text creates new hash (not deduplicated)
- **This is correct behavior** - different message = different submission
- Protects against false positives (incorrectly deduplicating genuinely different submissions)

### File Attachments

**For file uploads, hash file content directly:**

```python
async def compute_file_hash(file: UploadFile) -> str:
    """Compute SHA-256 of file content"""
    hasher = hashlib.sha256()
    file.file.seek(0)  # Reset to start

    while chunk := file.file.read(8192):
        hasher.update(chunk)

    file.file.seek(0)  # Reset for actual upload
    return hasher.hexdigest()
```

**Deduplication for file + message:**
- File content_hash (from file itself)
- Message is separate (not hashed with file)
- Duplicate detection: Same file, regardless of accompanying message

---

## Storage Cleanup Strategy

**Problem:** Files can be orphaned in S3 if:
- LLM analysis never completes (timeouts, retries exhausted)
- Evidence record deleted but file remains

**Solution: Garbage Collection**

### Approach 1: TTL-Based Cleanup (Simple)

```python
async def upload_with_ttl(file, ttl_hours=24):
    """Upload file with TTL metadata"""
    content_ref = await s3.upload(
        file,
        metadata={
            "uploaded_at": datetime.now(UTC).isoformat(),
            "ttl_hours": ttl_hours
        }
    )
    return content_ref

# Daily cleanup job
async def cleanup_orphaned_files():
    """Delete files uploaded >24h ago with no evidence record"""
    cutoff = datetime.now(UTC) - timedelta(hours=24)

    # List all files in evidence bucket
    files = await s3.list_objects(prefix="evidence/")

    for file in files:
        uploaded_at = file.metadata.get("uploaded_at")
        if not uploaded_at:
            continue

        if datetime.fromisoformat(uploaded_at) < cutoff:
            # Check if evidence record exists
            content_ref = file.key
            evidence_exists = await evidence_repo.exists_by_content_ref(content_ref)

            if not evidence_exists:
                logger.info(f"Deleting orphaned file: {content_ref}")
                await s3.delete(content_ref)
```

### Approach 2: Reference Counting (Accurate)

```python
# Track file references in separate table
CREATE TABLE file_references (
    content_ref VARCHAR(500) PRIMARY KEY,
    uploaded_at TIMESTAMP NOT NULL,
    case_id VARCHAR(17) NOT NULL,
    status VARCHAR(20) NOT NULL,  -- 'pending', 'referenced', 'orphaned'
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

# On file upload
async def register_file_upload(content_ref, case_id):
    await file_refs.create(
        content_ref=content_ref,
        case_id=case_id,
        status='pending',
        uploaded_at=datetime.now(UTC)
    )

# On evidence creation
async def create_evidence(evidence):
    await evidence_repo.create(evidence)
    # Mark file as referenced
    await file_refs.update(evidence.content_ref, status='referenced')

# Cleanup job
async def cleanup_orphaned_files():
    """Delete files pending >24h with no evidence"""
    cutoff = datetime.now(UTC) - timedelta(hours=24)

    orphaned = await file_refs.find(
        status='pending',
        uploaded_at__lt=cutoff
    )

    for ref in orphaned:
        logger.info(f"Deleting orphaned file: {ref.content_ref}")
        await s3.delete(ref.content_ref)
        await file_refs.update(ref.content_ref, status='orphaned')
```

**Recommended:** Approach 1 (simpler, good enough)
- Approach 2 is more accurate but adds complexity
- 24h TTL is generous enough for retries
- Orphaned files are edge case (LLM usually succeeds)

---

## Complete Failure Handling Flow

```python
async def process_turn_with_attachment(
    case_id: str,
    user_message: str,
    file: UploadFile
) -> TurnResponse:
    """
    Process user turn with file attachment, handling all failure modes.
    """
    content_ref = None
    content_hash = None
    llm_result = None

    try:
        # ==========================================
        # STEP 1: Upload File
        # ==========================================
        try:
            content_hash = await compute_hash(file)

            # Check for duplicate BEFORE uploading
            existing = await evidence_repo.find_by_content_hash(case_id, content_hash)
            if existing:
                logger.info(f"Duplicate file detected: {existing.evidence_id}")
                return TurnResponse(
                    message="This file was already uploaded previously.",
                    evidence_ref=existing.evidence_id,
                    status="duplicate"
                )

            # Upload with TTL metadata
            content_ref = await storage_service.upload(
                file,
                metadata={"ttl_hours": 24, "case_id": case_id}
            )
            logger.info(f"File uploaded: {content_ref}")

        except StorageError as e:
            logger.error(f"File upload failed: {e}")
            raise UserFacingError("Failed to upload file. Please try again.")

        # ==========================================
        # STEP 2: LLM Analysis
        # ==========================================
        try:
            case = await case_service.get(case_id)
            llm_result = await llm_service.analyze(
                case=case,
                user_message=user_message,
                content_ref=content_ref,
                timeout=30
            )
            logger.info(f"LLM analysis complete for {content_ref}")

        except LLMTimeout as e:
            logger.warning(f"LLM timeout: {e}")
            # Queue for async retry (don't delete file yet)
            await job_queue.enqueue(
                "retry_evidence_analysis",
                case_id=case_id,
                content_ref=content_ref,
                content_hash=content_hash,
                user_message=user_message,
                retry_count=0,
                max_retries=3
            )
            return TurnResponse(
                message="Your file is being analyzed. Check back shortly.",
                status="analyzing"
            )

        except LLMError as e:
            logger.error(f"LLM error: {e}")
            # Cleanup: Delete file since LLM can't process it
            await storage_service.delete(content_ref)
            raise UserFacingError("Failed to analyze file. Please try again.")

        # ==========================================
        # STEP 3: Create Evidence Record
        # ==========================================
        try:
            evidence = Evidence(
                evidence_id=f"ev_{uuid4().hex[:12]}",
                case_id=case_id,
                content_ref=content_ref,
                content_hash=content_hash,
                category=llm_result.category,  # Validated with fallback
                data_type=llm_result.data_type,
                summary=llm_result.summary,
                primary_purpose=llm_result.primary_purpose,
                collected_at=datetime.now(UTC),
                collected_by=case.user_id,
                collected_at_turn=case.current_turn + 1,
                # ... other fields
            )

            await evidence_repo.create(evidence)
            logger.info(f"Evidence created: {evidence.evidence_id}")

            return TurnResponse(
                message="Evidence saved successfully.",
                evidence_ref=evidence.evidence_id,
                status="success"
            )

        except DBError as e:
            logger.error(f"DB insert failed: {e}")
            # Queue for retry (preserve LLM work)
            await job_queue.enqueue(
                "retry_evidence_creation",
                case_id=case_id,
                llm_result=llm_result.model_dump(),
                content_ref=content_ref,
                content_hash=content_hash,
                retry_count=0,
                max_retries=5
            )
            return TurnResponse(
                message="Your file is being processed. Evidence will appear shortly.",
                status="processing"
            )

    except Exception as e:
        # Catch-all for unexpected errors
        logger.exception(f"Unexpected error in process_turn_with_attachment: {e}")
        # Cleanup file if created
        if content_ref:
            try:
                await storage_service.delete(content_ref)
            except:
                pass  # Best effort
        raise UserFacingError("An unexpected error occurred. Please try again.")
```

---

## Summary: Failure Mode Matrix

| Failure Point | State | Recovery Strategy | User Experience |
|--------------|-------|-------------------|-----------------|
| **File upload fails** | Clean (nothing persisted) | None needed (user retries) | "Failed to upload. Try again." |
| **LLM timeout** | File in S3, no DB record | Async retry (3x) with exponential backoff | "Analyzing... check back shortly." |
| **LLM error** | File in S3, no DB record | Delete file, user retries | "Analysis failed. Try again." |
| **LLM invalid category** | File in S3, LLM done | Fallback to CONTEXTUAL_EVIDENCE | "Evidence saved successfully." (transparent) |
| **DB insert fails** | File in S3, LLM done | Async retry (5x), preserve LLM result | "Processing... evidence will appear shortly." |
| **Orphaned file** | File in S3, no DB record after 24h | Daily cleanup job deletes file | N/A (background) |

---

## Implementation status

Current state is summarised in the "Current Implementation Status" table at the top of this document. The detailed step-by-step plan for closing the remaining items (orphan cleanup, async LLM timeout recovery, monitoring) lives in a temporary working doc: [`docs/working/PLAN-evidence-failure-modes-implementation.md`](../../working/PLAN-evidence-failure-modes-implementation.md). That plan will be deleted and this design doc will be updated once the milestones land.

---

## Related Documentation

- [Evidence Classification Design](./evidence-classification-design.md) — Evidence taxonomy and categories
- [Evidence Flow Architecture](./evidence-flow-architecture.md) — End-to-end evidence pipeline
- [Data Preprocessing Design Specification](./data-preprocessing-design-specification.md) — Four-tier preprocessing model, unified ingestion pipeline, and tier-escalation hardening

---

**Document Version:** 1.3
**Last Updated:** 2026-04-18
**Status:** Design Specification — partial implementation; see "Current Implementation Status" table above.
