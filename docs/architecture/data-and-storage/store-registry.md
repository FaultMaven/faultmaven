# Store Registry

Every store whose contents outlive a request or cross a process has one row
here. A row records what the store holds, its owner kinds, how its tenant is
known, its writer, where its write sits relative to the turn's commit, how each
owner's data ends, and what bounds whatever that misses. This is
[Principle 13.1](../core-architecture/architectural-design-principles.md#13-one-owner-per-fact).
The relational rows are [Principle 3](../core-architecture/architectural-design-principles.md#3-database-per-module-boundaries)'s
tables, enumerated by the [ER diagram](er-diagram.md). No other document lists
stores; each links here.

Every cell is a fact read in the code, cited by path and line in the
[notes](#notes), or it says `not built` or `pending #N`. Line numbers are those
of the commit that last changed this file. One exception: the trace SDK's own
local buffer, which FaultMaven's code does not show, is cited to the document
and the issue that state it.

**Tenant retirement is deferred** ([ruling on #2616](https://github.com/FaultMaven/faultmaven/issues/2616#issuecomment-6716225805)).
Retiring a tenant keeps its cases, evidence and knowledge items today
(`faultmaven/cli/personal_tenant.py:59-60`). Each row's retirement cell says
how a retirement would reach the store, by its own tenant stamp or through the
owning row, and `not built`.

**In scope:** every store a FaultMaven process writes whose contents outlive the
request that wrote them, or that another process reads. A Redis key family is
one row; an in-process cache that holds tenant data is one row.

**Out of scope**, each with its reason, in [Not stores](#not-stores): process-local
caches that hold no tenant data, work-in-flight registries, and components that
are composed but never written. The Slack agent's stores belong to that
repository: its two SQLite files and the `faultmaven_slack` PostgreSQL database
(`faultmaven/cli/wipe_deployment.py:116`; [Deployment Wipe](../../operations/deployment-wipe.md)).

## Columns

| Column | Values |
|---|---|
| Content class | customer content · derived data · operational metadata |
| Owner kinds | case · conversion · document · user · enterprise · process |
| Tenant | stamped on the row · derived through `<join>` · none |
| Writer | the module that writes the store; no other module does |
| Place relative to the turn's commit | inside · gated on it · outside, with the reason · not written by turns |
| Delete | the end of life for each owner kind |
| Tenant retirement | how a retirement would reach the store, and `not built` |
| Wipe | what `fm-wipe-deployment` does to the store |
| Retention bound | the time bound on what no delete reaches; `none` where there is none |

Redis families name the key as written to Redis. On a standalone deployment
the Redis rows live in the API process's in-process FakeRedis and end with it;
`fm-wipe-deployment` reports that keyspace as nothing durable to wipe
(`faultmaven/cli/wipe_deployment.py:966-974`).

## What each store holds

| Store | Backend | Content class | Owner kinds | Tenant | Writer | Place relative to the turn's commit |
|---|---|---|---|---|---|---|
| **Relational tables** | SQLite (standalone) or PostgreSQL, one database | per table: customer content, derived data, operational metadata | case · conversion · document · user · enterprise | stamped on every tenant-scoped row (`enterprise_id`, the RLS key) | the module that owns the table | inside: a turn's rows commit once; other writes commit in their own request |
| **Usage ledger** (`llm_usage_daily`, `llm_turn_spend`) | the same database | operational metadata: token counts and estimated cost | enterprise (daily rows) · case (turn rows) | stamped on the row | `infrastructure/llm/usage_ledger.py` | outside: its own transaction, awaited at the end of `process_turn`; it records spend that happened, and fails open |
| **Case evidence vectors** (`case_{case_id}`) | ChromaDB, the evidence instance | customer content: chunks of evidence text, with embeddings | case | derived through the case row (the collection is named by case id; chunks carry `case_id`, no `enterprise_id`) | `core/preprocessing/vector_storage.py`, through the agent's `vectorize_file` tool | outside: does not depend on the turn's commit; chunk ids are keyed by evidence id, and a lost `vectorized` flag is retried by a later turn |
| **KB vectors** (`faultmaven_kb`) | ChromaDB, the KB instance | customer content: runbook chunks, with embeddings | document | stamped on chunk metadata (`enterprise_id`, refused on write when absent since #1168); a chunk written earlier carries it once an operator runs the `kb_vector_enterprise_backfill` job (#1777) | the knowledge module's `KnowledgeService`, through `KnowledgeVectorStore`, the collection's only writer | not written by turns |
| **Evidence objects** | `data/evidence/` (filesystem) or S3 | customer content: uploaded file bytes, and a `.meta.json` sidecar per object (the filesystem backend adds a `.meta`) | case | stamped in the key: `{enterprise_id}/{case_id}/{date}/{uuid}_{filename}` | the evidence module's `FileStorageService` | outside: stored at the turn's intake, before its commit; the sidecar is marked linked only after the commit |
| **Redis `session:`, `sessions:user:`, `client_index:`** (auth sessions) | Redis | operational metadata: the session record and its indexes | user | derived through the user row | the auth module's `RedisSessionStore` | not written by turns |
| **Redis `session:case_seq:`** (case number counters) | Redis | operational metadata: a counter | user | derived through the user row | the case module's `CaseService`, through the session store | not written by turns |
| **Redis `session:session:…:current_case_id`** (session pointers) | Redis | operational metadata: a case id | case | derived through the case row | the case module's `CaseService`, through the session store | not written by turns |
| **Redis `idempotency:`** (replayed POST answers) and **`idempotency:case:{case_id}`** (their index by case) | Redis | customer content: the cached response's status, headers and body. The index, a sorted set keyed by case id, holds the entry keys of the requests that acted on the case, each scored by its expiry; each write drops expired members, and it lapses 1 h after its newest entry | user · case | none: the caller's identity is hashed into the key; the index names the case | `api/middleware/idempotency.py`, the index from the case a route declares, in the entry's transaction | outside: cached after the route answers |
| **Redis `sso:state:`, `sso:login:`** | Redis | operational metadata: sign-in state and its completion code | user | none | the auth module's `SSOEphemeralStore` | not written by turns |
| **Redis `oauth:code:`, `oauth:claim:`** | Redis, when `OAUTH_ENABLED` | operational metadata: authorization codes and their single-use claims | user | none on the key | the auth module's `RedisOAuthCodeRepository` | not written by turns |
| **Redis `password_reset:`** | Redis | operational metadata: a reset token's id | user | none | the auth module's `UserService` | not written by turns |
| **Redis `redaction:registry:{case_id}`** (PII redaction registry) | Redis | customer content: the case's original PII values and their placeholders | case | derived through the case row | `infrastructure/security/case_redaction.py`'s `CaseRedactionContext` | outside: written during the turn; does not depend on its commit |
| **Redis `redaction:pseudonym_key_fingerprint`** | Redis | operational metadata: a fingerprint of the pseudonym key, not the key | process | none | `infrastructure/security/pseudonym_key.py` | not written by turns |
| **Redis token revocations** (`revoked:token:jti:`, `revoked:token:user:`, under the configured prefix) | Redis, cloud only; standalone writes the `token_revocations` table | operational metadata: revoked token ids and per-user watermarks | user | none on the key | the auth module's `RedisTokenRevocationStore` | not written by turns |
| **Redis `<preset>:rl:`** (rate-limit windows) | Redis; the per-replica in-process stand-in when Redis is lost | operational metadata: request timestamps per client key | user | none | `infrastructure/protection/rate_limiter.py` | not written by turns |
| **Redis `<preset>:dedup:`** (duplicate-request claims) | Redis | operational metadata: a request hash's claim | user | none | `api/middleware/deduplication.py` | not written by turns |
| **Redis `case-inflight:`, `case-deleting:`** (case lease, delete tombstone) | Redis | operational metadata, no content | case | stamped in the key | `infrastructure/concurrency/inflight.py` | outside: a lease held around the turn's work, not data the turn commits |
| **Redis `turn-inflight:`** (keyed-turn claim) | Redis | operational metadata, no content | case | stamped in the key | the case module's `turn_idempotency.py` | outside: taken before the turn, released once it is answered |
| **Redis `kb-inflight:`** (keyed KB request claim) | Redis | operational metadata, no content | user | stamped in the key | the knowledge module's `request_idempotency.py` | not written by turns |
| **Redis `job:`** | Redis | no writer | — | — | none: `JobService` is composed, and nothing creates a job | not written |
| **Redis `lock:report_generation:case:`** | Redis | no writer | — | — | none: `ReportLockManager` is never constructed | not written |
| **Redis `auth:`** (fallback user store) | Redis | customer content: account records | user | none | `infrastructure/auth/user_store.py`'s `RedisUserStore`, composed only when the database user store is not | not written by turns |
| **Redis `faultmaven:llm_config:version`** | Redis | operational metadata: a counter | process | none | `config/llm_config_overrides.py`, on a cloud configuration write | not written by turns |
| **Trace store** (Opik) | the Opik backend, when `OPIK_ENABLED` (off by default) | customer content: prompts and responses, and the SDK's exception text | case · conversion | stamped on trace and span metadata (`enterprise_id`, from the request's tenant context) | `LLMRouter` (`infrastructure/llm/router.py`); the preprocessing classifier writes a metadata-only span | outside: the SDK sends spans in its own batches while the call runs |
| **Trace SDK's local buffer** | the Opik SDK's SQLite, in a temporary directory on each pod | customer content | case · conversion | as the trace it holds | the Opik SDK | outside |
| **LLM response cache** | in-process dict, per replica, at most 1,000 entries | customer content: model responses | case | none: the case id is hashed into the key | `LLMRouter`, through `LLMResponseCache` | outside: stored when the call returns |
| **`cached_content_tokens`** | in-process `lru_cache`, per replica, at most 4,096 entries | customer content: the evidence text is the key; its token set is the value | case | none: keyed by the text alone | `core/investigation/cause_assurance.py` | outside: filled as the turn reads evidence |
| **Metrics** | the in-process Prometheus registry, scraped | operational metadata: no label carries a case, user or enterprise id | process | none | each module's metric definitions | outside: counters move as work happens |
| **Logs** | stderr, collected by the deployment's log pipeline | operational metadata, by policy: avoid raw user content | process | none; records carry `user_id` and `correlation_id` | every module | outside |
| **Secret files under the data root** (`data/.jwt_secret`, `data/.redaction_pseudonym_key`) | local files, standalone | operational metadata: deployment secrets | process | none | `config/settings.py` (JWT secret), `infrastructure/security/pseudonym_key.py` | not written by turns |

## How each store's data ends

| Store | Delete, per owner kind | Tenant retirement | Wipe | Retention bound |
|---|---|---|---|---|
| **Relational tables** | case: the row goes after its erasers, with its `CASCADE` children; document: an authored one's row is deleted, a built-in one is unpublished; conversion: a case delete removes the case's conversion jobs and drafts, and a document delete discards its drafts with their text kept; user: `DELETE /auth/users/{username}`, refused while the account created cases, and deactivation is a soft delete; enterprise: `not built` | by `enterprise_id` (`ON DELETE CASCADE` from `enterprises`); a retirement soft-deletes the enterprise row and keeps its rows: `not built` | `DROP DATABASE`, an operator step. `--verify` asserts the tenant, case and identity tables empty and the five migration-seeded tables (`enterprises`, `teams`, `permissions`, `role_permissions`, `roles`) non-empty, and reports `knowledge_items` without asserting it | none, except the usage ledger and `token_revocations` (standalone): a revocation stops revoking at its `expires_at`, and each write that adds a revocation deletes every elapsed row |
| **Usage ledger** | case: turn rows go with the case (`ON DELETE CASCADE`); daily rows name no case and stay | by the row's `enterprise_id`: `not built` | `DROP DATABASE` | daily rows 400 days, turn rows 90 days (`LLM_USAGE_DAILY_RETENTION_DAYS`, `LLM_USAGE_TURN_RETENTION_DAYS`), pruned by the `llm_usage_retention` job where it runs: the job runner, or the in-process scheduler under `RUN_SCHEDULER` (default off) |
| **Case evidence vectors** | case: the collection is deleted before the row; a closed case keeps it | through the case row's `enterprise_id`: `not built` | cleared | none built. The orphan sweep reclaims a collection whose case row is gone: in-process every 6 h under `RUN_SCHEDULER` (refused under multi-tenancy), or the `case_cleanup` job |
| **KB vectors** | document: an authored one's vectors are deleted best effort after its row; a leftover chunk is never served (every read is row-checked) and the authored-orphan sweep removes it where an operator arms it; a built-in one's vectors go at the next pack-loading bootstrap | by the chunk's `enterprise_id` stamp: `not built` | cleared | none |
| **Evidence objects** | case: every object under the case's prefix, sidecars included, before the row | by the key's `{enterprise_id}/` prefix: `not built` | cleared | none built in `infrastructure/storage/`; a bucket lifecycle policy is the operator's. An unlinked orphan older than `ORPHAN_FILE_TTL_HOURS` is swept where `ORPHAN_CLEANUP_ENABLED` (default off) |
| **Redis `session:`, `sessions:user:`, `client_index:`** | user: `DELETE /sessions/{session_id}` deletes one session | through the user row: `not built` | cleared | `SESSION_TIMEOUT_MINUTES` (default 30) past the last write; an index lives as long as its newest session |
| **Redis `session:case_seq:`** | none | through the user row: `not built` | cleared, under `session:` (the wipe's `case_seq:` prefix matches none of these keys) | 48 h |
| **Redis `session:session:…:current_case_id`** | case: erased by the case delete | through the case row: `not built` | cleared, under `session:` | 24 h |
| **Redis `idempotency:`** | case: every entry whose request acted on the case, as its route declared, not every case it mentions, through `idempotency:case:{case_id}`, erased by the case delete; one cached after the eraser read the index, or under an index Redis lost, is a [residual](../../operations/case-deletion.md#residuals); user: none | no stamp to reach it by: `not built` | cleared, the indexes with it | 1 h; an index, 1 h past its newest entry |
| **Redis `sso:state:`, `sso:login:`** | consumed once | none needed: bounded by its TTL | cleared | 600 s and 60 s |
| **Redis `oauth:code:`, `oauth:claim:`** | claimed once | none needed: bounded by its TTL | `oauth:code:` cleared; `oauth:claim:` left, reported under no known prefix | `OAUTH_CODE_EXPIRY_SECONDS` (default 600); a claim inherits its code's remaining TTL |
| **Redis `password_reset:`** | none | none needed: bounded by its TTL | cleared | 1 h |
| **Redis `redaction:registry:{case_id}`** | case: erased by the case delete | through the case row: `not built` | cleared | 7 days, re-armed on every write |
| **Redis `redaction:pseudonym_key_fingerprint`** | none; deleted by hand when the key is rotated | not tenant data | cleared | none |
| **Redis token revocations** | none | through the user row: `not built` | cleared (the configured prefix) | a token id: the token's remaining life; a user watermark: the longest token lifetime plus the mint-basis carry |
| **Redis `<preset>:rl:`** | none | none needed: bounded by its TTL | cleared, for all three namespaces (`faultmaven_dev`, `faultmaven_prod`, `faultmaven_staging`) | the limit's window plus 60 s |
| **Redis `<preset>:dedup:`** | none | none needed: bounded by its TTL | cleared, for all three namespaces | the endpoint's TTL (default 30 s) |
| **Redis `case-inflight:`, `case-deleting:`** | the tombstone is removed when its delete is refused or fails | by the key's enterprise id: `not built`; bounded by its TTL | left, reported under no known prefix | 30 s after the holder's last renewal (every 10 s), plus a 5 s key margin for the lease |
| **Redis `turn-inflight:`** | released once the turn is answered | by the key's enterprise id: `not built`; bounded by its TTL | left, reported under no known prefix | the turn's response bound plus 30 s |
| **Redis `kb-inflight:`** | released once the request is answered | by the key's enterprise id: `not built`; bounded by its TTL | left, reported under no known prefix | 60 s, renewed every 20 s while the request runs |
| **Redis `job:`** | no writer | — | left, reported under no known prefix | 24 h, were one written |
| **Redis `lock:report_generation:case:`** | no writer | — | left, reported under no known prefix | 300 s, were one written |
| **Redis `auth:`** | user: the account's keys | no stamp to reach it by: `not built` | left, reported under no known prefix | none |
| **Redis `faultmaven:llm_config:version`** | none | not tenant data | left, reported under no known prefix | none |
| **Trace store** | case and conversion: purged by their tags before the case delete and the draft and document deletes, with the residuals in [Case deletion](../../operations/case-deletion.md#residuals) | by the `enterprise_id` tag: `not built`; covered by the retention bound once it is built (#2616) | not covered by `fm-wipe-deployment`: pending #2616 | none built. Ruled: content off by default, and on only with a verified retention bound: pending #2616 |
| **Trace SDK's local buffer** | replayed to the backend when a delete runs on that pod, and only then purgeable there | as the trace store | not covered: pending #2616 | none: a parked trace otherwise replays only after the SDK's ping interval, set to 999999 s (about 11.6 days): #2573 |
| **LLM response cache** | none, a stated exclusion ([ruling on #2624](https://github.com/FaultMaven/faultmaven/issues/2624#issuecomment-6719598372)): entries outlive a case delete, at most 1,000 per replica, evicted oldest first, or until the process ends. No request asks for a deleted case id again, and an eraser would need a cross-replica broadcast for memory nothing can read | no stamp to reach it by; ends with the process | ends with the process | none: eviction is by count only |
| **`cached_content_tokens`** | none, a stated exclusion ([ruling on #2624](https://github.com/FaultMaven/faultmaven/issues/2624#issuecomment-6719598372)): entries outlive a case delete, at most 4,096 per replica, least recently used, or until the process ends. No request reads a key back as text, and an eraser would need a cross-replica broadcast for memory nothing can read | no stamp to reach it by; ends with the process | ends with the process | none: eviction is by count only |
| **Metrics** | none | not tenant data | ends with the process | the process's lifetime; the scraper's retention is the operator's |
| **Logs** | none | no stamp to reach it by: `not built` | not covered | the operator's log pipeline |
| **Secret files under the data root** | none | not tenant data | not covered | none |

## Notes

Citations only. Paths are relative to `faultmaven/` unless they start with
`docs/`.

### Relational tables

- Tenant: `infrastructure/persistence/models.py:26-27`. Owners per table:
  [Principle 3](../core-architecture/architectural-design-principles.md#3-database-per-module-boundaries)
  and the [ER diagram](er-diagram.md).
- Place: `core/investigation/milestone_engine/engine.py:410-415` (#1882).
- Delete, case: `modules/case/domain/services/case_service.py:945`, `:1042`;
  `container/providers/services.py:61-127` (the erasers, in order);
  `modules/knowledge/domain/services/conversion_service/case_erasure.py:1-9`;
  `infrastructure/persistence/models.py:31-33`, `:39-41`;
  [Case deletion](../../operations/case-deletion.md).
- Delete, document and its drafts: `modules/knowledge/domain/services/knowledge_service.py:599-640`.
- Delete, user: `modules/auth/api/auth.py:982-990`;
  `modules/auth/domain/services/user_service.py:791-797`.
- Tenant retirement: `cli/personal_tenant.py:59-60`;
  `docs/operations/sso-org-provisioning.md:535-537`.
- Wipe: [Deployment Wipe](../../operations/deployment-wipe.md#the-database-drop-and-recreate-never-delete);
  `cli/wipe_deployment.py:128-129`, `:198-219`, `:420-457`.
- Retention, `token_revocations`: `modules/auth/infrastructure/stores/token_revocation_store.py:268-277`,
  `:319-334`; `cleanup_expired` (`:532-536`) has no caller in `faultmaven/`.

### Usage ledger

- Writer and its one transaction per turn: `infrastructure/llm/usage_ledger.py:10-23`, `:296-318`.
- Place: `core/investigation/milestone_engine/engine.py:410-417`, `:529-537`; fails
  open: `infrastructure/llm/usage_ledger.py:32-38`.
- Delete, case: `infrastructure/persistence/models.py:699-701`, `:712-721`.
- Retention: `jobs/llm_usage_retention.py:1-18`; `config/settings.py:2386-2410`;
  `bootstrap/lifespan.py:296-316`; `config/settings.py:172-173`.
  [The usage ledger](../../operations/monitoring/llm-cost-observability.md#the-usage-ledger)
  describes it.

### Case evidence vectors

- Writer: `core/preprocessing/vector_storage.py:295` (chunk ids), `:341`;
  `modules/agent/tools/vectorize_file_tool.py:275-289`.
- Tenant: `infrastructure/persistence/case_vector_store.py:37`, `:64`;
  `core/preprocessing/vector_storage.py:279-281`.
- Place: `core/investigation/milestone_engine/engine.py:410-417`;
  `core/investigation/milestone_engine/vectorization.py:181-197`.
- Delete: `infrastructure/persistence/case_vector_store.py:1-8`, `:284-287`.
- Retention: `infrastructure/tasks/case_cleanup.py:129-162`;
  `bootstrap/lifespan.py:296-350`; `jobs/case_cleanup.py:1-19`.

### KB vectors

- Tenant and writer: `infrastructure/knowledge/knowledge_vector_store.py:1480-1488`;
  `modules/knowledge/domain/services/knowledge_service.py:1145`; the backfill:
  `jobs/kb_vector_enterprise_backfill.py:1-8` (a dry run unless `--no-dry-run`).
- Delete: `modules/knowledge/domain/services/knowledge_service.py:599-640`, `:747-751`;
  `bootstrap/kb_orphan_sweep.py:1-18`; `bootstrap/kb_init.py:713`.

### Evidence objects

- Backend: `config/settings.py:104-108`; `infrastructure/storage/factory.py:75-80` (no other backend).
- Key and tenant: `modules/evidence/domain/services/file_storage_service.py:540-555`.
- Sidecars: `modules/evidence/domain/services/file_storage_service.py:28-33`;
  `infrastructure/storage/filesystem.py:167`.
- Place: `modules/agent/domain/services/investigation_service/attachments.py:666-678`.
- Delete: `modules/evidence/domain/services/file_storage_service.py:558-582`.
- Retention: no lifecycle or expiry code in `infrastructure/storage/`;
  `modules/agent/jobs/storage_cleanup.py:1-6`; `config/settings.py:3454-3466`.

### Redis families

- The wipe's prefixes: `cli/wipe_deployment.py:226-284`; a key under none of them is
  reported and never fails `--verify`: `cli/wipe_deployment.py:992-1006`.
- Auth sessions: `modules/auth/infrastructure/stores/redis_session_store.py:44-51`,
  `:93-96`, `:136`, `:185`; `container/providers/infrastructure.py:454-467`;
  `config/settings.py:1396-1398`; `modules/auth/domain/services/auth_session_service.py:351-366`, `:544-551`;
  `modules/auth/api/session.py:670-716`.
- Case number counters: `modules/case/domain/services/case_service.py:244-248`;
  the store's prefix: `modules/auth/infrastructure/stores/redis_session_store.py:205-212`.
- Session pointers: `modules/case/domain/services/case_erasure.py:14-31`;
  `modules/case/domain/services/case_service.py:323-327`, `:783-787`.
- `idempotency:`: `api/middleware/idempotency.py:185-186`, `:772-781`, `:823-845`, `:599-603`.
  Delete, case: a route declares the case its request acts on
  (`api/middleware/idempotency.py:144-176`, read at `:400-411`): the case router
  by its `{case_id}` path (`modules/case/api/routes/router.py:27`), the create
  route by the new case (`modules/case/api/routes/cases.py:416`), the report
  routes by `case_id` and by the report's case (`modules/report/api/routes.py:331`,
  `:915`). Case search answers many cases and is `never_replayed` (`main.py:214-225`).
  The entry joins that case's index in its own MULTI/EXEC, with the prune
  (`api/middleware/idempotency.py:838-845`). `IdempotencyReplayEraser`
  (`:904-951`) reads the index's members scored after now, then unlinks and
  unindexes each batch in one MULTI/EXEC, so a member cached after the read stays
  indexed for a later delete and a failed batch leaves the rest indexed for the
  retry. A stale member is skipped: its entry expired by the index's own record,
  and its key, which names no case, may since hold another case's entry. That
  line is approximate. A score is taken before its write commits (`:841`,
  executed at `:845`), so it falls short of the entry's expiry by the write's
  commit delay: the eraser skips an entry of the case in that last interval,
  which outlives the delete by at most the delay. Clocks out of step between
  replicas move the line by their skew, either way: a writer clock behind the
  eraser's lengthens that interval by the skew, and one ahead lets a case's
  delete unlink another case's entry whose key was reused within the skew of its
  expiry. It is
  bound in `container/providers/services.py:121-126`, and reported
  `not_configured` under `SKIP_SERVICE_CHECKS`, where the middleware is not
  mounted (`bootstrap/middleware.py:104-119`). The index's bounds: a member can
  outlive its entry until the next write to the index prunes it or the index
  lapses; a `WRONGTYPE` inside the transaction does not roll back the `SETEX`,
  reachable only if something wrote another type under an index key, and nothing
  does; an eviction or a failover can lose an index while its entries survive.
- SSO: `modules/auth/infrastructure/stores/sso_ephemeral_store.py:19-47`;
  `modules/auth/domain/services/sso_login_service.py:117`, `:121`.
- OAuth: `modules/auth/infrastructure/repositories/oauth_code_repository.py:87-103`,
  `:117-119`, `:178-182`; `container/providers/services.py:1051-1066`, `:1505-1512`;
  `config/settings.py:1864-1867`.
- `password_reset:`: `modules/auth/domain/services/user_service.py:123`, `:460-462`;
  `modules/auth/domain/services/jwt_token_generator.py:67`.
- PII redaction registry: `infrastructure/security/case_redaction.py:104-105`, `:130`,
  `:214-220`, `:362-374`; place: `core/investigation/milestone_engine/engine.py:410-417`.
- Pseudonym key fingerprint: `infrastructure/security/pseudonym_key.py:233`, `:283`;
  rotation by hand: `:86-92`.
- Token revocations: `modules/auth/infrastructure/stores/token_revocation_store.py:157-196`;
  `config/settings.py:1723`; `container/providers/services.py:1090-1100`;
  `modules/auth/domain/services/auth_service.py:642-703`.
- Rate limits: `infrastructure/protection/rate_limiter.py:135-137`, `:222`, `:842-848`;
  `api/middleware/rate_limiting.py:161`; `config/protection.py:75-82`.
- Deduplication: `api/middleware/deduplication.py:100`, `:409-414`; `config/protection.py:588`, `:751`.
- Case lease and tombstone: `infrastructure/concurrency/inflight.py:55-62`, `:76-93`,
  `:124-129`; [Case deletion](../../operations/case-deletion.md#work-in-flight-holds-a-delete-off-2096).
- `turn-inflight:`: `modules/case/api/turn_idempotency.py:91-108`.
- `kb-inflight:`: `modules/knowledge/api/request_idempotency.py:66-67`, `:155-160`, `:196-202`.
- `job:`: `infrastructure/jobs/job_service.py:51-52`; `bootstrap/composition.py:591`;
  no caller of `create_job` in `faultmaven/`.
- `lock:report_generation:case:`: `infrastructure/concurrency/report_lock_manager.py:51`;
  `container/providers/services.py:1659-1664`; nothing sets `container.lock_manager`.
- `auth:`: `infrastructure/auth/user_store.py:74-78`, `:534-537`;
  `container/providers/infrastructure.py:548-553`, `:604-627`.
- `faultmaven:llm_config:version`: `config/llm_config_overrides.py:28-34`, `:256`.

### Trace store

- Content class: the [ruling on #2616](https://github.com/FaultMaven/faultmaven/issues/2616#issuecomment-6716225805).
- Writer and content: `infrastructure/llm/router.py` (`_opik_track_llm`,
  `_update_opik_span`); metadata-only span: `modules/preprocessing/classifier.py`
  (`_opik_track_classifier`). The one module that imports the SDK is
  `infrastructure/observability/opik_sdk.py` (#2616 part (c)): it sets the SDK's
  process policy and configures it, and the writers, the eraser and
  `bootstrap/middleware.py` reach the SDK through it.
- Tenant: `infrastructure/llm/router.py:243-252`.
- Gate: the record `opik_sdk.configure` writes from `OPIK_ENABLED` and the backend
  URL, and the SDK's span writers (`opik_sdk.tracing_is_effective`).
- Delete: `infrastructure/observability/trace_erasure.py:92-116`;
  `container/providers/services.py:34-58`, `:103-107`;
  [Case deletion](../../operations/case-deletion.md#llm-traces).
- Wipe: no trace store in `cli/wipe_deployment.py`.
- Retention: no retention call in `faultmaven/`; ruled direction: #2616 (Q0, Q1).

### Trace SDK's local buffer

- `docs/operations/case-deletion.md` §Residuals and #2573. FaultMaven sets the ping
  interval (`opik_sdk.PING_INTERVAL_S`), and `GET /admin/config/status` reports the
  serving worker's buffer under `trace_backend` (`server_connection`,
  `parked_messages`); each worker of each replica has its own.

### LLM response cache

- `infrastructure/llm/cache.py:39-44`, `:74-88`, `:145-166`; constructed per router:
  `infrastructure/llm/router.py:384`, read and written at `:691`, `:822`.
- No case eraser, as ruled ([ruling on #2624](https://github.com/FaultMaven/faultmaven/issues/2624#issuecomment-6719598372)): `container/providers/services.py:61-127`.

### `cached_content_tokens`

- `core/investigation/cause_assurance.py:403-409`. No case eraser, as ruled
  ([ruling on #2624](https://github.com/FaultMaven/faultmaven/issues/2624#issuecomment-6719598372)).

### Metrics

- No metric's label names carry a case, user or enterprise id (every `Counter`,
  `Gauge`, `Histogram` and `Summary` in `faultmaven/`, read for its label names);
  OAuth metrics label the OAuth client, not a tenant:
  `modules/auth/infrastructure/metrics/oauth_metrics.py:81`.

### Logs

- `infrastructure/logging/config.py:146-176`, `:345-357`; `user_id`: `:60-64`, `:417-418`.
- Policy: [Logging Policy, Redaction](../../operations/monitoring/logging-policy.md#redaction).

### Secret files under the data root

- `config/settings.py:1423`, `:1545-1556`; `config/settings.py:2293-2297`;
  `infrastructure/security/pseudonym_key.py:159`.

## Not stores

Each of these was considered and is not a row, for the reason given.

| Candidate | Reason |
|---|---|
| Relational tables' files and the ChromaDB directories under `data/` (`faultmaven.db`, `chroma-kb/`, `chroma-evidence/`) | The relational tables, KB vectors and case evidence vectors rows (`config/settings.py:1099`, `:1207-1208`) |
| `data/evidence/` | The evidence objects row (`config/settings.py:3414`) |
| `data/knowledge/` | An import directory an operator fills by hand; FaultMaven reads it on a scan and writes nothing there (`utils/runbook_id.py:282`; [Data and Storage Management](../../operations/data-storage-management.md)) |
| The KB pack (`resources/knowledge/pack/` or `KB_PACK_DIR`) | Shipped input, read at startup, never written by the runtime |
| The in-process FakeRedis keyspace (`infrastructure/redis_client.py:34`) | The Redis rows' backend on a standalone deployment, not a store of its own |
| The pseudonym key cache (`infrastructure/security/pseudonym_key.py:82`) | Holds the deployment's key, keyed by its source; no tenant data |
| The KB term index (`CorpusTermStats`, `infrastructure/knowledge/knowledge_vector_store.py:411-424`) | Built over the global tier only, which every tenant reads |
| `_get_tiktoken_encoder` (`utils/token_estimation.py:49`), the DOCX style cache (`modules/knowledge/domain/services/document_parser.py:313`, one parse), `abort_on_cancel_connection_class` (`infrastructure/persistence/pg_connection.py:66`) | Hold no tenant data |
| Log-throttle sets keyed by ids: `_log_undated` (`core/investigation/cause_assurance.py:711`), `_PROFILE_DEGRADE_WARNED` (`modules/auth/api/auth.py:1357`), `_session_not_found_log_tracker` (`modules/auth/api/session.py:63`) | Hold identifiers to throttle a log line, no content |
| Work in flight: `inflight._TRACKED` and `_LEASES` (`infrastructure/concurrency/inflight.py:117-119`), `EvidenceVectorizer._inflight_vectorize` (`core/investigation/milestone_engine/vectorization.py:21`) | Live only while the work runs; drained at shutdown (#2505) |
| The OAuth endpoints' rate limiter (`modules/auth/api/rate_limiting.py:58-63`) | Process-local windows keyed by client IP and endpoint; no tenant data |
| `InMemoryTurnLedger` (`infrastructure/protection/tenant_turn_cap.py:315`) | Never composed: the composition wires `SqlTurnLedger` (`container/providers/services.py:403-426`), a relational table |
| `InMemoryOAuthCodeRepository` (`container/providers/services.py:1062-1066`) | Selected only when no Redis client is passed; the composition passes one (`:1509-1512`) |
| `SimpleStorageBackend` (`modules/case/domain/services/case_data_ingestion_service.py:1711`) | Composed into the data service (`container/providers/services.py:512`), which stores into it (`case_data_ingestion_service.py:343`, `:532`), but nothing outside the service resolves it: only `app.state.data_service` holds it (`bootstrap/composition.py:579`), and its dependency (`api/v1/dependencies.py:204`) is used by no route |
| `GlobalConfidenceService` (`infrastructure/observability/confidence_service.py:58`) | Never constructed: nothing sets `container.confidence_service` (`_container_impl.py:836-843`). It writes no file: a model reaches it as bytes (`:593-602`) |
| `IntelligentCache` (`infrastructure/caching/intelligent_cache.py:314`) | Never constructed: nothing sets `container.intelligent_cache` (`_container_impl.py:383-391`) |
| APScheduler job stores (`infrastructure/tasks/job_runners.py:89`, `infrastructure/tasks/case_cleanup.py:165`) | In-memory schedule definitions; no tenant data |
| `faultmaven-kb-index.lock` in the temporary directory (`infrastructure/index_slot.py:83`) | A host-wide lock file; holds no data |
| The Slack agent's `credentials.db`, `cases.db` and the `faultmaven_slack` database | The Slack agent repository's stores ([Deployment Wipe](../../operations/deployment-wipe.md#the-surfaces)) |
