# Evidence File Storage

How raw evidence blobs (uploaded logs, configs, metric dumps) are written,
read, and reclaimed — and how the storage backend is selected.

Related: [overview.md](./overview.md) ·
[architectural-design-principles.md](../core-architecture/architectural-design-principles.md)
(Principle 1: provider selection is explicit; business logic depends on
interfaces, never on vendors).

## The seam

Evidence blobs cross exactly one boundary between domain logic and storage
vendor:

```text
InvestigationService / agent tools / Tier 2 analysis
        │  store_file() · retrieve_file() · mark_linked()
        ▼
FileStorageService                    ← evidence domain service
        │  validation · key naming · orphan sidecars
        │
        │  store_file(key, data) · retrieve_file(key) · list_keys(prefix)
        ▼
IFileStorageBackend                   ← infrastructure port
        ├── FilesystemStorageBackend  (STORAGE_BACKEND=filesystem)
        └── S3StorageBackend          (STORAGE_BACKEND=s3)
```

**`FileStorageService` owns the domain half** and is backend-agnostic: file
validation (size, MIME, filename safety), storage-key generation, and the
orphan-tracking sidecar protocol. It holds no filesystem paths and performs no
file I/O of its own.

**`IFileStorageBackend` owns the vendor half**: bytes in, bytes out, keyed by
an opaque string. It is the only place that knows about local paths or S3
buckets.

Consumers depend on `FileStorageService`. Nothing outside
`infrastructure/storage/` constructs a backend directly.

## Backend selection

`get_storage_backend()` (`infrastructure/storage/factory.py`) resolves
`STORAGE_BACKEND` to a singleton backend instance and is the single
construction point. `FileStorageService` accepts a backend by injection and
falls back to the factory when none is supplied, so every construction site —
the DI container, the request-scoped service factory, and the on-demand sites
in `read_file_tool`, `agent_orchestration_service`, and the `storage_cleanup`
job — honours the configured backend without each having to plumb one through.

| Setting | Backend | Deployment |
|---------|---------|------------|
| `STORAGE_BACKEND=filesystem` (default) | `FilesystemStorageBackend` rooted at `EVIDENCE_STORAGE_ROOT` | Standalone / self-hosted, single replica |
| `STORAGE_BACKEND=s3` | `S3StorageBackend` against `S3_BUCKET_NAME` | Cloud, or any multi-replica deployment |

Filesystem storage is single-node: two API replicas sharing one filesystem
backend need a shared mount (RWX volume), which makes the volume a single point
of failure for all evidence I/O. Cloud deployments should use an object-storage
backend instead. A cloud deployment configured with the filesystem backend logs
a coherence warning at startup.

## Key layout

`FileStorageService` generates keys as:

```text
{organization_id}/{case_id}/{YYYY-MM-DD}/{uuid12}_{sanitized_filename}
```

The key is stored on `UploadedFile.storage_ref` and is opaque to everything
above the service — it is a backend key, not a path. Organization and case
components are sanitized so a hostile identifier cannot escape its prefix, and
`retrieve_file` rejects traversal sequences and absolute keys before the
backend sees them.

## Orphan tracking

A file is written before the `Evidence` row that references it exists, so a
failure in between leaves an unreferenced blob. Each stored file therefore gets
a companion **sidecar object** at key `{key}.meta.json`:

```json
{
  "case_id": "case_abc",
  "organization_id": "org_xyz",
  "uploaded_at": "2026-04-18T10:00:00+00:00",
  "linked": false,
  "schema_version": 1
}
```

`mark_linked()` flips `linked` to `true` once the referencing `Evidence` row
exists. The `storage_cleanup` job sweeps sidecars via
`IFileStorageBackend.list_keys()` and deletes only files that are both
`linked=false` and older than `ORPHAN_FILE_TTL_HOURS`. A file with no sidecar
is *never* deleted, and neither is one whose sidecar cannot be read — unknown
state is not a licence to delete.

The suffix is **reserved**, in two independent layers. Without them an object
whose own key ends in `.meta.json` is enumerated as some other object's
sidecar, its user-controlled content parsed as orphan metadata, and the object
deleted as that phantom's companion.

1. **At key generation**, a filename whose sanitized form ends in the suffix is
   mangled to `.meta_json`. This runs *after* length truncation, because
   truncation rebuilds the name as `{name}.{ext}` and can otherwise
   reconstitute the suffix it just removed.
2. **At sweep time**, a stripped base is only treated as a candidate if that
   base names a genuinely stored object. Layer 1 is generation-time only and
   so cannot protect keys written before it existed; this is what covers them.

Because sidecars are ordinary backend objects rather than local files, orphan
cleanup works identically on S3 and on the filesystem.

## Async discipline

`aiofiles` makes the filesystem backend genuinely async. **boto3 is
synchronous**, so every S3 call in `S3StorageBackend` runs under
`asyncio.to_thread`. A blocking S3 round-trip on the event loop would stall
unrelated requests including `/health`, which in Kubernetes escalates to a
liveness kill — the same failure mode that motivated the embedding-call
boundary in the knowledge pipeline.

## Rejected alternative

Teaching `FileStorageService` to branch on `STORAGE_BACKEND` internally
(`if s3: ... else: ...`) was rejected: it puts vendor knowledge in a domain
service and forecloses adding a backend without editing evidence code.
