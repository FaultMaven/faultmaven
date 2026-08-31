"""Storage Cleanup Job — TTL-Based Orphan File Removal

Implements per evidence-failure-modes.md. Deletes files that the DATABASE does
not reference AND whose sidecar metadata shows `linked=False` AND whose
`uploaded_at` is older than `orphan_file_ttl_hours`. Files without sidecars are
skipped (unknown state is not a license to delete).

## The authority, and the cache

`uploaded_files.storage_ref` is the authoritative record of whether a stored
object is referenced. The sidecar (below) is a *cache* of that state, written
once at upload and never revisited.

Before this job cross-checked the database (issue #1232) it decided purely from
that cache, and the cache is wrong in one direction that matters: `mark_linked`
is best-effort in `InvestigationService._preprocess_attachment`, so a transient
storage failure at exactly that step leaves `linked: false` beside a file the
case genuinely references. At TTL the sweep deleted it — irreversible loss of
user-uploaded evidence, detectable only by a user reporting a missing upload.

So the sweep now asks the authority first: **anything named by an
``uploaded_files`` row is skipped, whatever the sidecar says.** That makes the
whole class impossible rather than rarer, and demotes the sidecar from a
decision to a hint.

A stale `linked: false` is then not merely tolerated but self-healing: the
object is protected exactly while its `uploaded_files` row lives, and
`uploaded_files.case_id` is `ON DELETE CASCADE` (enforced on SQLite too — the
engine sets `PRAGMA foreign_keys=ON`), so deleting the case leaves an ordinary
unreferenced orphan this sweep reclaims on schedule. Nothing leaks. That is a
promise `docs/operations/evidence-job-scheduling.md` makes to operators, so
`TestDriftIsSelfHealing` pins all three of its legs.

The cross-check is deliberately **additive** — it only ever protects. The
inverse rewrite ("the DB is the authority, so delete what it does not
reference") sounds tidier and is a data-loss change wearing a data-safety
label: the cache is *also* stale in the other direction (a `linked: true`
sidecar outlives the row it described), so on a measured production corpus of
850 candidates, 160 objects had no row and every one of them said
`linked: true`. Deleting on "no row" alone would have destroyed all 160 on the
first armed run. Both signals must agree before anything is deleted.

## Sidecar pairing

Every file stored via `FileStorageService.store_file()` gets a companion
`{filename}.meta.json` sidecar with:

    {
        "case_id": "case_abc",
        "organization_id": "org_xyz",
        "uploaded_at": "2026-04-18T10:00:00+00:00",
        "linked": false,
        "schema_version": 1
    }

`FileStorageService.mark_linked()` flips `linked=true` once an Evidence row
is created referencing the file (called from
`InvestigationService._preprocess_attachment`).

## Fail-closed postures

Three refusals. All delete nothing — for a delete-deciding sweep "I could not
ask" and "nothing is referenced" are indistinguishable at the point of
decision. They differ in how loudly they say so, and the status is stated on
each one rather than summarised separately, because a summary is what drifts:

1. **No authority reachable** — no DI container, no case repository, or the
   query raised. ``status="failed"`` (exit 1). A dry run is refused too: its
   classification would be a fiction someone might act on.
2. **Reference set disjoint from the candidates** — the authority answered,
   but not one candidate is in its answer, so every one of them would be
   deleted. ``status="failed"`` (exit 1), overridable per run with
   ``--allow-disjoint-reference-set``, and **never applied to a dry run**
   (which deletes nothing and is how you diagnose this).

   The test is the OVERLAP, not emptiness. Guarding on "the reference set is
   empty" leaves the worse half open: a *non-empty* set that shares nothing
   with the candidates passes such a guard and then deletes all of them. That
   is reachable rather than theoretical — `knowledge_service` and
   `conversion_service` write filesystem paths into `storage_ref`, and a path
   can never equal a backend key, so a conversion-heavy deployment has exactly
   that shape. A changed `STORAGE_BACKEND` or key prefix does too. RLS is the
   third route: `uploaded_files` is tenanted and fail-closed (migration 018),
   so a session with no org bound sees ZERO rows.
3. The pre-existing `orphan_cleanup_enabled` gate (below).
   ``status="skipped"`` (exit 0), unchanged.

The split is deliberate. The third is a *configured* refusal — the deployment
asked for it, it is expected every night on the shipped defaults, and it exits
0 as it always has. The first two are the job wanting to run and being unable
to decide safely, which is the "a CronJob that refuses at boot looks like a
CronJob with nothing to do" failure that hid this bug in the first place. The
runner maps `completed`/`skipped` to exit 0, so reporting those two as
`skipped` would make a sweep that cannot reach the database look like a clean
night, for as long as it lasted. A fail-closed nobody hears about is half a
fail-closed.

## Safety protocol

This job ships with `orphan_cleanup_enabled=False` and `dry_run=True` by
default. Per the M1 canary protocol: run dry-run for ≥48 hours, eyeball
logs, fix any unexpected entries in the `mark_linked` path, then flip
`dry_run=False`. A run reporting `found=0` has watched nothing — the
selection branch never executed — so seed one known orphan and confirm a
dry run reports it before flipping (docs/operations/evidence-job-scheduling.md).

Two things #1232 adds to that protocol. The "unexpected entries in the
`mark_linked` path" step no longer needs eyeballing — `skipped_db_referenced`
counts them and a WARNING names each file. And `referenced_by_db` should be a
plausible fraction of `scanned`: a 0 there across a live corpus means the run
is seeing an RLS-scoped view rather than the truth, which a total-zero answer
would have refused outright but a partial one cannot detect.

## Usage

    python -m faultmaven.jobs.run storage_cleanup
    python -m faultmaven.jobs.run storage_cleanup --dry-run
    python -m faultmaven.jobs.run storage_cleanup --no-dry-run
    python -m faultmaven.jobs.run storage_cleanup --ttl-hours 72

Under `TENANT_PROVIDER=multi` every one of those is refused outright: this job
is `cross_tenant` (below), so it runs only on the audited maintenance path,
connected as the dedicated BYPASSRLS role:

    python -m faultmaven.jobs.run storage_cleanup --cross-tenant-maintenance

Omitting a flag defers to settings (`ORPHAN_CLEANUP_DRY_RUN`,
`ORPHAN_FILE_TTL_HOURS`), which is not the same as passing their current
values — the plain invocation above is what the deployed CronJob runs.
`--no-dry-run` asks for deletion but cannot grant it: with
`orphan_cleanup_enabled=False` the run is still refused (`status="skipped"`).
`--ttl-hours` is bounded by the same range as the setting.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Set

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Declared rather than duck-typed. The call below reaches the repository
    # off the DI container, which is invisible to import-linter — so without
    # this the agent module would carry a hard dependency on a Case-module
    # method while the contract check still reported 13/13 kept. Contract 12
    # forbids case.INFRASTRUCTURE; the contract module is the sanctioned
    # surface, which is exactly what this names.
    from faultmaven.modules.case.contracts import ICaseRepository

from faultmaven.infrastructure.observability.evidence_metrics import (
    EVIDENCE_ORPHAN_FILES_DELETED_TOTAL,
    EVIDENCE_ORPHAN_FILES_FOUND_TOTAL,
    EVIDENCE_ORPHAN_FILES_RESCUED_TOTAL,
)
from faultmaven.jobs.reference_set import (
    REASON_REFERENCE_SET_DISJOINT,
    assess_reference_set,
)
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)

logger = logging.getLogger(__name__)


def _inc(counter: Any) -> None:
    """Increment a Prometheus counter, never letting it break the sweep.

    Three copies of ``try: c.inc() except Exception: pass`` used to sit inline.
    A metric is a side-channel on a deletion decision that has already been
    made; a broken or absent registry must not change what the sweep does.
    """
    try:
        counter.inc()
    except Exception:  # pragma: no cover - metrics must never break a sweep
        pass


JOB_DESCRIPTION = (
    "Delete stored files that no uploaded_files row references, whose sidecar "
    "shows linked=False, and whose uploaded_at is older than the TTL "
    "(PLAN-evidence-failure-modes M1)."
)
# The sweep asks the DATABASE whether an object is still referenced before
# deleting it (issue #1232), and `uploaded_files` is not partitioned by anything
# the storage backend exposes: a candidate key is just a key, so "referenced" is
# only decidable against the storage_ref set of ALL organizations. An RLS-scoped
# partial view would report every OTHER tenant's live files as unreferenced —
# the same hazard case_cleanup carries, with irreversible loss at the end of it.
# Under the multi-tenant provider the runner therefore refuses this job except on
# the audited maintenance path (--cross-tenant-maintenance + the dedicated
# BYPASSRLS role, probe-verified); see faultmaven.jobs.run and
# docs/operations/evidence-job-scheduling.md.
JOB_TENANT_SCOPE = "cross_tenant"

# Refusal reasons, carried as the `reason` key on the refusing result. The
# status differs per reason and is stated with each one in the module
# docstring: the configured refusal is `skipped` (exit 0), the two
# authority refusals are `failed` (exit 1).
REASON_CLEANUP_DISABLED = "orphan_cleanup_disabled"
REASON_AUTHORITY_UNAVAILABLE = "reference_authority_unavailable"
# REASON_REFERENCE_SET_DISJOINT is owned by faultmaven.jobs.reference_set and
# imported above; both sweeps must report the same reason for the same shape.


def ttl_hours_bounds() -> tuple[int, int]:
    """The (min, max) that ``orphan_file_ttl_hours`` itself enforces.

    Read off the pydantic field rather than restated here, so a caller-side
    override (the CLI's ``--ttl-hours``, or a direct ``run()`` call) cannot
    drift from the bound the environment variable has.
    """
    from faultmaven.config.settings import EvidenceStorageSettings

    field = EvidenceStorageSettings.model_fields["orphan_file_ttl_hours"]
    # Read None-safely: pydantic may carry the bounds as separate Ge/Le
    # objects or fold them into one Interval whose unused ends are None, and
    # a None end must not erase a bound another constraint already supplied.
    low = high = None
    for constraint in field.metadata:
        if getattr(constraint, "ge", None) is not None:
            low = constraint.ge
        if getattr(constraint, "le", None) is not None:
            high = constraint.le

    if low is None or high is None:
        raise RuntimeError(
            "orphan_file_ttl_hours no longer declares ge/le bounds; refusing "
            "to validate a TTL override against bounds that do not exist."
        )
    return int(low), int(high)


def validate_ttl_hours(value: int) -> int:
    """Return ``value`` if the settings field would accept it, else raise.

    ``run()`` takes ``ttl_hours`` as a plain kwarg, so without this every
    caller-side override is a path around a field constraint that exists for a
    reason: ``ttl_hours=0`` makes every unlinked file older than "now", which
    includes uploads still in flight.

    Raises:
        ValueError: If the value is outside the setting's own range.
    """
    low, high = ttl_hours_bounds()
    if not low <= value <= high:
        raise ValueError(
            f"ttl_hours must be between {low} and {high} (got {value}); this "
            "is the same range ORPHAN_FILE_TTL_HOURS is bounded to — a "
            "shorter TTL would sweep uploads that are still in flight."
        )
    return value


async def load_referenced_storage_refs(container: Any) -> Set[str]:
    """Ask the authority which stored objects are still referenced.

    Mirrors ``case_cleanup``'s reference-set load: reach the repository off the
    DI container the runner injects, and let a missing one be an error rather
    than an empty answer. Returns every non-null ``uploaded_files.storage_ref``
    in ONE query — ``storage_ref`` carries no index, so a per-candidate lookup
    would be a full table scan each.

    Raises:
        RuntimeError: If no container or no case repository is available. The
            caller must treat this as "cannot decide", never as "nothing is
            referenced" — that distinction is the whole point of #1232.
    """
    case_repository: "ICaseRepository | None" = (
        getattr(container, "case_repository", None) if container else None
    )
    if case_repository is None:
        raise RuntimeError(
            "No case repository available: the orphan sweep cannot ask the "
            "database which stored objects are still referenced."
        )
    refs = await case_repository.list_all_storage_refs()
    return set(refs or ())


async def cleanup_orphaned_files(
    storage: FileStorageService,
    ttl_hours: int,
    dry_run: bool,
    referenced_refs: Set[str],
    allow_disjoint_reference_set: bool = False,
) -> dict[str, Any]:
    """Sweep stored files and delete orphans.

    Enumerates sidecars through the storage backend rather than walking a
    local directory, so the sweep works whichever backend STORAGE_BACKEND
    selects.

    Deletion requires BOTH signals to agree: the database must not reference
    the object, and its sidecar must say `linked: false` past the TTL. See the
    module docstring for why neither alone is safe.

    Returns a stats dict with counts for observability / CLI output.
    Emits three Prometheus counters per run:
      - ``faultmaven_evidence_orphan_files_found_total`` (+= found count)
      - ``faultmaven_evidence_orphan_files_deleted_total`` (+= deleted count;
        not incremented when dry_run is True)
      - ``faultmaven_evidence_orphan_files_rescued_total`` (+= the count of
        files the DB cross-check saved from a sweep that would otherwise have
        selected them — a non-zero value means sidecar drift is live)

    Args:
        storage: FileStorageService owning the sidecar protocol.
        ttl_hours: Delete only files whose sidecar `uploaded_at` is older
            than this many hours. Younger files are always safe.
        dry_run: When True, log `would delete` without deleting.
        referenced_refs: Every ``uploaded_files.storage_ref`` in the database
            (see ``load_referenced_storage_refs``). Required, not optional: a
            default would let a caller silently restore the pre-#1232
            sidecar-only decision. An EMPTY set while candidates exist refuses
            the run rather than deleting everything.

    Returns:
        Dict with keys: ``status``, ``storage_backend``, ``ttl_hours``,
        ``dry_run``, ``scanned``, ``skipped_no_sidecar``,
        ``skipped_linked``, ``skipped_within_ttl``, ``skipped_db_referenced``,
        ``referenced_by_db``, ``unreferenced_by_db``, ``stray_sidecars``,
        ``found``, ``deleted``, ``errors``, plus the three set sizes
        ``referenced_refs_count`` / ``reference_overlap`` (both ints, not
        the sets themselves). A refused run additionally carries ``reason``.
    """
    result: dict[str, Any] = {
        "status": "completed",
        "storage_backend": storage.backend.get_storage_type().value,
        "ttl_hours": ttl_hours,
        "dry_run": dry_run,
        "scanned": 0,
        "skipped_no_sidecar": 0,
        "stray_sidecars": 0,
        "skipped_linked": 0,
        "skipped_within_ttl": 0,
        # Files the DB cross-check saved: the sidecar said orphan, the
        # database said otherwise, and the database won. Non-zero means
        # #1232's failure is live and would have destroyed data.
        "skipped_db_referenced": 0,
        # Standing classification of the whole candidate corpus against the
        # authority, independent of any deletion decision. This is the
        # referenced-fraction report: a one-off probe answers it once, these
        # answer it every night.
        "referenced_by_db": 0,
        "unreferenced_by_db": 0,
        "found": 0,
        "deleted": 0,
        "errors": 0,
        # Sizes of the two sets and how they intersect. The overlap is the
        # quantity the safety guard actually keys on, so it belongs in the
        # run summary rather than only in a log line.
        "referenced_refs_count": len(referenced_refs),
        "reference_overlap": 0,
    }

    cutoff = datetime.now(UTC) - timedelta(hours=ttl_hours)

    candidates, strays = await storage.survey_sidecars()

    # Sidecars whose file is already gone are never swept (a phantom base is
    # how a hostile key gets another object deleted). Report the count so an
    # unbounded set of objects no job will ever touch is visible rather than
    # silent.
    result["stray_sidecars"] = len(strays)
    if strays:
        logger.warning(
            "%d sidecar(s) have no corresponding file and were left in place; "
            "first few: %s",
            len(strays),
            strays[:5],
        )

    # Fail-closed guard on how the reference set lines up with the candidates.
    # OVERLAP is the test, not emptiness: a non-empty reference set that is
    # disjoint from the candidates scores every one of them "unreferenced" and
    # deletes the lot, which is the same irreversible loss. That shape is
    # reachable — `knowledge_service` and `conversion_service` write filesystem
    # paths into `storage_ref`, and a path can never equal a backend key. See
    # faultmaven/jobs/reference_set.py for the full reasoning.
    verdict = assess_reference_set(
        candidates=candidates,
        referenced=referenced_refs,
        dry_run=dry_run,
        acknowledged=allow_disjoint_reference_set,
        authority="uploaded_files.storage_ref",
        candidate_noun="stored object",
    )
    result["reference_overlap"] = verdict.overlap_count
    if verdict.disjoint:
        # ERROR even when the run continues: a dry run reaching here is the
        # diagnostic, and it should be as loud as the refusal it replaces.
        logger.error("Storage cleanup: %s", verdict.message)
    if verdict.refuse:
        # "failed", not "skipped": the runner maps skipped to exit 0, and a
        # sweep that silently exits 0 forever is how nobody finds out.
        result["status"] = "failed"
        result["reason"] = verdict.reason
        # `scanned` is still 0 here — the guard sits before the loop — so it
        # is left alone rather than re-zeroed, which only implied otherwise.
        return result

    for storage_key in candidates:
        result["scanned"] += 1

        # Classify against the authority for every candidate, before any
        # decision branch can `continue` past it — otherwise the standing
        # referenced-fraction report would only cover files that happened to
        # reach the end of the chain.
        is_referenced = storage_key in referenced_refs
        if is_referenced:
            result["referenced_by_db"] += 1
        else:
            result["unreferenced_by_db"] += 1

        try:
            payload = await storage.read_sidecar(storage_key)
        except Exception as e:
            # Corrupt payload, or the backend failed to answer. Either way we
            # do not know this file's state, and unknown state is never a
            # licence to delete.
            logger.warning("Unreadable sidecar for %s — skipping: %s", storage_key, e)
            result["errors"] += 1
            continue

        if payload is None:
            # Listed a moment ago, gone now — deleted concurrently.
            result["skipped_no_sidecar"] += 1
            continue

        if payload.get("linked") is True:
            result["skipped_linked"] += 1
            continue

        uploaded_at_str = payload.get("uploaded_at")
        if not uploaded_at_str:
            logger.warning("Sidecar for %s missing uploaded_at — skipping", storage_key)
            result["errors"] += 1
            continue

        try:
            uploaded_at = datetime.fromisoformat(uploaded_at_str)
        except ValueError:
            logger.warning(
                "Sidecar for %s has unparseable uploaded_at=%r — skipping",
                storage_key,
                uploaded_at_str,
            )
            result["errors"] += 1
            continue

        if uploaded_at > cutoff:
            result["skipped_within_ttl"] += 1
            continue

        # The authority has the last word (#1232). Everything above this line
        # is the sidecar's opinion, and the sidecar is a write-once cache that
        # goes stale in both directions. Reaching here means it says "orphan
        # past TTL"; if the database still references the object, the sidecar
        # is simply wrong and the file is live.
        #
        # Its own bucket, not skipped_linked: this is the drift the sweep would
        # have destroyed, and folding it into a neighbouring counter would make
        # the count lie about why. It is also the operator signal the mark_linked
        # path never had — the WARNING names the file.
        if is_referenced:
            result["skipped_db_referenced"] += 1
            _inc(EVIDENCE_ORPHAN_FILES_RESCUED_TOTAL)
            logger.warning(
                "Sidecar drift: %s is past TTL with linked=False, but an "
                "uploaded_files row still references it — NOT deleting "
                "(uploaded=%s). The sidecar's linked flag was never flipped; "
                "see issue #1232.",
                storage_key,
                uploaded_at_str,
            )
            continue

        # Candidate for deletion: unreferenced by the DB AND unlinked AND
        # past TTL.
        result["found"] += 1
        _inc(EVIDENCE_ORPHAN_FILES_FOUND_TOTAL)

        if dry_run:
            logger.info(
                "[DRY RUN] would delete orphan: %s (uploaded=%s, linked=False)",
                storage_key,
                uploaded_at_str,
            )
            continue

        # Actually delete. delete_file removes the sidecar too, and raises if
        # the backend refuses — so a failed delete is counted as an error
        # rather than reported as reclaimed storage. A False return means the
        # file was already gone, which still counts: the sidecar that kept it
        # findable is now removed.
        try:
            await storage.delete_file(storage_key)
            result["deleted"] += 1
            _inc(EVIDENCE_ORPHAN_FILES_DELETED_TOTAL)
            logger.info(
                "Deleted orphan: %s (uploaded=%s)",
                storage_key,
                uploaded_at_str,
            )
        except Exception as e:
            logger.error("Failed to delete orphan %s: %s", storage_key, e)
            result["errors"] += 1

    logger.info(
        "Storage cleanup %s — scanned=%d, found=%d, deleted=%d, "
        "skipped_db_referenced=%d, skipped_linked=%d, skipped_within_ttl=%d, "
        "skipped_no_sidecar=%d, stray_sidecars=%d, errors=%d | authority: "
        "referenced_refs_count=%d, referenced_by_db=%d, unreferenced_by_db=%d",
        "DRY RUN" if dry_run else "live",
        result["scanned"],
        result["found"],
        result["deleted"],
        result["skipped_db_referenced"],
        result["skipped_linked"],
        result["skipped_within_ttl"],
        result["skipped_no_sidecar"],
        result["stray_sidecars"],
        result["errors"],
        result["referenced_refs_count"],
        result["referenced_by_db"],
        result["unreferenced_by_db"],
    )
    return result


async def run(
    settings: Any = None,
    container: Any = None,
    ttl_hours: int | None = None,
    dry_run: bool | None = None,
    allow_disjoint_reference_set: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """CLI entry point invoked by ``faultmaven.jobs.run``.

    Args:
        settings: ``FaultMavenSettings`` injected by the runner.
        container: DI container injected by the runner.
        ttl_hours: Override for ``evidence_storage.orphan_file_ttl_hours``,
            bounded to the same range as that setting.
        dry_run: Override for ``evidence_storage.orphan_cleanup_dry_run``.
            Caller-supplied overrides win over settings — useful for manual
            testing / one-shot invocations.
        allow_disjoint_reference_set: The operator's explicit assertion that a
            reference set sharing NOTHING with the sweep candidates is expected
            on this deployment. Off by default: that shape is what an
            RLS-scoped session and a changed keyspace also produce, and it is
            not distinguishable from a genuinely empty install. It exists so
            case 3 does not deadlock — an install whose cases were all deleted
            still needs its objects reclaimed. It has no effect on a dry run,
            which proceeds regardless.

    ``None`` means "defer to settings" for both, which is distinct from
    passing the setting's current value: it is what the flagless CronJob
    invocation delivers.

    The gate is:
      1. ``evidence_storage.orphan_cleanup_enabled`` must be True, OR
      2. the *effective* dry-run must be True — the caller's override if it
         gave one, else ``orphan_cleanup_dry_run`` (safe to run anyway). The
         deployed CronJob passes no override and rides the second arm on the
         shipped default.

    Otherwise the job exits with status="skipped" and no side effects. An
    explicit ``dry_run=False`` therefore asks for deletion without granting
    it — it satisfies neither arm while cleanup is disabled.

    A second, independent gate follows it: the reference authority must answer
    (see ``load_referenced_storage_refs``). ``container`` stops being decorative
    here — it is where the case repository comes from — so a run without one is
    refused rather than degraded to the pre-#1232 sidecar-only decision. The
    refusal covers dry runs too: a classification computed without the authority
    is a fiction, and the whole point of the dry run is that someone reads it.
    Unlike the settings gate above, it reports ``status="failed"`` so the runner
    exits non-zero and the CronJob alert fires.

    Raises:
        ValueError: If ``ttl_hours`` is outside the setting's own range.
    """
    if ttl_hours is not None:
        validate_ttl_hours(ttl_hours)

    if settings is None:
        from faultmaven.config.settings import get_settings

        settings = get_settings()

    ev_settings = settings.evidence_storage
    effective_dry_run = (
        ev_settings.orphan_cleanup_dry_run if dry_run is None else dry_run
    )
    effective_ttl_hours = (
        ev_settings.orphan_file_ttl_hours if ttl_hours is None else ttl_hours
    )

    if not ev_settings.orphan_cleanup_enabled and not effective_dry_run:
        logger.info(
            "Storage cleanup skipped: orphan_cleanup_enabled=False and "
            "dry_run=False. Set ORPHAN_CLEANUP_ENABLED=true or run with "
            "--dry-run."
        )
        return {
            "status": "skipped",
            "reason": REASON_CLEANUP_DISABLED,
        }

    # Ask the authority BEFORE constructing the storage service, so a run that
    # cannot decide never enumerates anything it might be tempted to act on.
    try:
        referenced_refs = await load_referenced_storage_refs(container)
    except Exception as e:
        logger.error(
            "Storage cleanup REFUSED: could not read the referenced storage "
            "refs from the database (%s). Nothing was deleted — a sweep that "
            "cannot ask the authority must not fall back to the sidecar, which "
            "is exactly the failure issue #1232 closes.",
            e,
        )
        # "failed", not "skipped": see the module docstring. Exiting 0 here
        # would make an unreachable authority indistinguishable from a clean
        # night, for as long as it lasted.
        return {
            "status": "failed",
            "reason": REASON_AUTHORITY_UNAVAILABLE,
            "error": str(e),
        }

    storage = FileStorageService()
    logger.info(
        "Storage cleanup starting (backend=%s, ttl_hours=%d, dry_run=%s, "
        "enabled=%s, referenced_refs=%d)",
        storage.backend.get_storage_type().value,
        effective_ttl_hours,
        effective_dry_run,
        ev_settings.orphan_cleanup_enabled,
        len(referenced_refs),
    )

    return await cleanup_orphaned_files(
        storage=storage,
        ttl_hours=effective_ttl_hours,
        dry_run=effective_dry_run,
        referenced_refs=referenced_refs,
        allow_disjoint_reference_set=allow_disjoint_reference_set,
    )
