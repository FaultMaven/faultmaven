# Evidence Creation Failure Modes and Recovery

**Version:** 1.5
**Date:** 2026-05-04
**Status:** **Partially superseded by migration 010** (2026-05-11). Scenarios 1, 2, and 4 (storage, LLM timeout, DB insert) remain accurate; scenario 3 (invalid LLM category) no longer falls back — it now raises a validation error (no `CONTEXTUAL_EVIDENCE` category exists). Dedup is now file-level (`find_uploaded_file_by_content_hash`) rather than evidence-level. See [evidence-driven-investigation-framework.md §5](../investigation-engine/evidence-driven-investigation-framework.md#5-evidence-model) for the current evidence model.
**Context:** Failure analysis and recovery strategies for single-phase evidence creation

---

## Current Implementation Status (validated 2026-04-19)

| Area | Status | Notes |
| --- | --- | --- |
| Scenario 1 — File upload fails | Implicit (no code change needed) | Storage failures raise before any evidence object exists. No orphan state. |
| Scenario 2 — LLM call timeout | **Deferred** | Current turn-submission path (`modules/case/api/routes.py:2234-2269`) already returns specific error codes (`LLM_OVER_CAPACITY`, `RATE_LIMIT_EXCEEDED`, `LLM_TIMEOUT`) with `Retry-After` headers and in-process synchronous retries via `BaseExternalClient`. An async-retry path was designed and discarded (see former `PLAN-async-turn-retry.md`, deleted 2026-04-19) — the additional machinery (forward-only schema migration, 202 polling, cancellation semantics) isn't justified without production evidence that the current error-path UX harms users. Revisit on telemetry signal. A partially-scaffolded `faultmaven/modules/agent/jobs/evidence_retry.py` (with a placeholder LLM call returning hardcoded `symptom_evidence` and an in-memory `asyncio.sleep` "queue") was a leftover from the original Option B recommendation and was **removed on 2026-05-04** to prevent it being mistakenly wired up. The scaffolded `evidence_turn_async_retry_*` Prometheus metrics remain registered as a tripwire if/when the design is revisited. |
| Scenario 3 — LLM returns invalid category | **Done (post-010 — strict)** | `EvidenceToAdd.validate_category` in `core/investigation/schemas.py` raises a Pydantic `ValidationError` on any value outside the four post-010 categories (symptom / causal / mitigation / solution_evidence). The pre-010 `CONTEXTUAL_EVIDENCE` fallback was removed when migration 010 dropped that enum value — fail-loud is the deliberate choice so the milestone engine's self-correction loop (error-handling-and-recovery.md §3.2) can prompt the LLM to retry with a valid category instead of silently miscategorising the row. |
| Scenario 4 — DB insert fails after LLM / storage | **Partial** | Orphan-file cleanup (below) handles the "storage succeeded, evidence didn't persist" case. Idempotency on evidence creation itself is not implemented. |
| Content-hash deduplication — hash consistency | **Done** | `PreprocessingService.classify_and_extract` computes `SHA-256(UTF-8 text)` uniformly for file uploads and pasted content. Both paths produce the same hash for the same content. |
| Content-hash deduplication — repository lookup | **Done** | `ICaseRepository.find_by_content_hash()` live on all bound implementations (`SessionlessCaseRepository`, `SQLiteCaseRepository`, `PostgreSQLHybridCaseRepository`, `InMemoryCaseRepository`). `_preprocess_attachment` short-circuits on match, skipping storage write and evidence creation. See [data-preprocessing-design-specification.md](./data-preprocessing-design-specification.md) §2.4. Emits `faultmaven_evidence_dedup_hits_total`. |
| Storage cleanup — TTL-based orphan sweep | **Done** | `faultmaven.modules.agent.jobs.storage_cleanup` with sidecar-metadata approach: `FileStorageService.store_file()` writes `{filename}.meta.json` with `{case_id, uploaded_at, linked=false}`; `mark_linked()` flips `linked=true` after Evidence creation; sweep deletes files whose sidecar says `linked=false` AND `uploaded_at` past the TTL. Gated on `ORPHAN_CLEANUP_ENABLED` + `ORPHAN_CLEANUP_DRY_RUN` (default dry-run=true). 48h dry-run canary required before enabling real deletes. |
| Storage cleanup — Reference counting | **Rejected** | Approach 2 (below) was considered and rejected: reference counting requires a `file_references` table + maintaining the reference graph on every evidence create/delete, a meaningful surface area for bugs. TTL was chosen for simplicity and because orphan rate is near-zero by design. |
| Monitoring + alerts | **Done** | Six Prometheus metrics defined in `infrastructure/observability/evidence_metrics.py`: `evidence_dedup_hits_total`, `evidence_orphan_files_{found,deleted}_total`, `evidence_turn_async_retry_{enqueued,outcome}_total`, `evidence_turn_async_retry_latency_seconds`. Live emitters: dedup + orphan cleanup. Scaffolded emitters: async retry (will emit if that plan is ever justified). Canonical alert definitions in [docs/operations/monitoring/evidence-metrics.md](../../operations/monitoring/evidence-metrics.md). |
| `file_references` table | **Rejected** | Would be needed only for Approach 2 (reference counting) which was rejected. |

The scenario descriptions below remain largely as originally written, but treat them as historical context — the Status table above is the authoritative current state.

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

> **Current implementation (2026-04-19):** This scenario's "LLM timeout at ingest" framing is obsolete — Tier 0+1 is zero-LLM, so file upload no longer depends on an LLM call. The real remaining failure surface is **LLM calls during turn processing** in `milestone_engine.py`. Current handling:
>
> - `BaseExternalClient.call_external` retries `retryable=True` errors synchronously within the request.
> - On terminal failure, `modules/case/api/routes.py:2234-2269` returns specific error codes (`LLM_OVER_CAPACITY` / `RATE_LIMIT_EXCEEDED` / `LLM_TIMEOUT` / `SERVICE_ERROR`) with appropriate `Retry-After` headers and actionable user-facing messages.
> - Orphan files from failed turns are collected by the TTL-based orphan-cleanup job (see §Storage Cleanup Strategy).
>
> An async-retry path was designed and deferred — see the Status table at the top of this document. The Options A/B below remain as historical design context.

**Failure Point (legacy framing):** LLM call times out or fails after file uploaded to S3

**State:**
- ✅ File in S3
- ❌ No evidence record in DB
- ❌ LLM tokens spent (partial)

**Problem:**

- **Orphaned file** in S3 (storage cost, clutter) — addressed by TTL-based orphan cleanup
- User doesn't know what happened — addressed by specific error codes + `Retry-After` in the turn-submission endpoint

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

**Historical recommendation (now deferred):** Option B was originally recommended for production UX. This recommendation was reconsidered on 2026-04-19 — see the Status table at the top of this document. The async-retry machinery (forward-only schema migration, 202 polling, cancellation semantics) is not justified without production evidence that the current synchronous error-path UX harms users. The synchronous path (specific error codes + `Retry-After` headers + in-process retries via `BaseExternalClient`) is the current behaviour.

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
- Not SYMPTOM/CAUSAL/MITIGATION/SOLUTION (don't want false positives in investigation)
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

**Key: Idempotency via content_hash (application-layer lookup, not a DB constraint)**

Idempotency is enforced by the `evidence_repo.find_by_content_hash(case_id, content_hash)` short-circuit shown in the snippet above (see also the Status table — "Content-hash deduplication — repository lookup" / **Done**). There is **no** `UNIQUE(case_id, content_hash)` SQL constraint on the `evidence` table; deduplication is purely application-layer. A retry can safely re-run because the lookup short-circuits on match before insert.

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

**Problem:** Files can be orphaned in storage if the evidence creation path fails after `store_file` succeeds.

**Implementation: TTL-Based Sidecar Cleanup** (reference counting was considered and rejected — see below).

### Design

Every file stored via `FileStorageService.store_file()` gets a companion `{filename}.meta.json` sidecar with stable schema:

```json
{
    "case_id": "case_abc123def456",
    "organization_id": "org_xyz",
    "uploaded_at": "2026-04-19T10:00:00+00:00",
    "linked": false,
    "schema_version": 1
}
```

`FileStorageService.mark_linked(file_path)` flips `linked=true` once an Evidence row references the file. Called from `InvestigationService._preprocess_attachment` after `store_file` returns.

The sweep (`faultmaven.modules.agent.jobs.storage_cleanup`) walks the storage root, reads each sidecar, and deletes any file where `linked=false` AND `uploaded_at` is older than `ORPHAN_FILE_TTL_HOURS` (default 24). Files without sidecars are skipped (unknown state is not license to delete). Corrupt sidecars count as errors and their files stay.

```python
# Simplified sweep — full implementation in faultmaven/modules/agent/jobs/storage_cleanup.py
async def cleanup_orphaned_files(storage_root, ttl_hours, dry_run):
    cutoff = datetime.now(UTC) - timedelta(hours=ttl_hours)
    for sidecar in Path(storage_root).rglob("*.meta.json"):
        payload = json.loads(sidecar.read_text())
        if payload["linked"] is True:
            continue
        uploaded_at = datetime.fromisoformat(payload["uploaded_at"])
        if uploaded_at > cutoff:
            continue  # within TTL — might still be in-flight
        if dry_run:
            logger.info(f"would delete: {file_path}")
        else:
            file_path.unlink()
            sidecar.unlink()
            EVIDENCE_ORPHAN_FILES_DELETED_TOTAL.inc()
```

### Safety protocol

Ships with `ORPHAN_CLEANUP_ENABLED=False` and `ORPHAN_CLEANUP_DRY_RUN=True`. Mandatory canary before enabling real deletes in production:

1. Run with `ORPHAN_CLEANUP_DRY_RUN=true` for ≥48 hours. Job logs `would delete: {path}` without deleting.
2. Eyeball the dry-run log. If unexpected files appear (anything currently referenced by an Evidence row, or recently uploaded), fix the `mark_linked` path before enabling real deletes.
3. Only after a clean 48-hour dry run, set `ORPHAN_CLEANUP_DRY_RUN=false`.

CLI: `python -m faultmaven.jobs.run storage_cleanup`. Invoke from cron, Kubernetes CronJob, or any external scheduler.

### Why not reference counting?

Considered and rejected. Reference counting would require:

- A `file_references` table tracking per-file state (`pending | referenced | orphaned`).
- Maintaining the reference graph on every evidence create AND delete path.
- Recovery logic if the reference-counting writes fail between file-store and evidence-persist.

That's a meaningful surface area for bugs, and the orphan rate in practice is near zero (evidence is written immediately after file storage; only crashes between the two produce orphans). TTL is a safety net, not a correctness mechanism — which makes the simpler approach the correct one.

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
| **LLM timeout** (current) | In-process, synchronous | Synchronous retries via `BaseExternalClient`; on terminal failure returns `LLM_TIMEOUT` / `LLM_OVER_CAPACITY` / `RATE_LIMIT_EXCEEDED` with `Retry-After` header | Specific error code with actionable message and retry hint |
| **LLM timeout** (deferred async design) | File in S3, no DB record | Async retry (3x) with exponential backoff | "Analyzing... check back shortly." |
| **LLM error** | File in S3, no DB record | Delete file, user retries | "Analysis failed. Try again." |
| **LLM invalid category** | File in S3, LLM done | Pydantic `ValidationError`; engine's self-correction loop reprompts the LLM (no CONTEXTUAL_EVIDENCE fallback post-010) | Transparent — corrected on retry within the same turn |
| **DB insert fails** | File in S3, LLM done | Async retry (5x), preserve LLM result | "Processing... evidence will appear shortly." |
| **Orphaned file** | File in S3, no DB record after 24h | Daily cleanup job deletes file | N/A (background) |

---

## Implementation status

Current state is summarised in the "Current Implementation Status" table at the top of this document. All items other than Scenario 2 async retry are implemented. Scenario 2 async retry was designed (as the former `PLAN-async-turn-retry.md`) and deferred on 2026-04-19 — the current synchronous error-path UX (specific error codes + `Retry-After` headers + in-process retries) is already polished enough that the additional machinery isn't justified without production evidence of user harm.

---

## Related Documentation

- [Evidence Classification Design](./evidence-classification-design.md) — Evidence taxonomy and categories
- [Evidence Flow Architecture](./evidence-flow-architecture.md) — End-to-end evidence pipeline
- [Data Preprocessing Design Specification](./data-preprocessing-design-specification.md) — Four-tier preprocessing model, unified ingestion pipeline, and tier-escalation hardening

---

**Document Version:** 1.5
**Last Updated:** 2026-05-04
**Status:** Design Specification — implemented (dedup, orphan cleanup, monitoring); Scenario 2 async retry deferred. See "Current Implementation Status" table above.
