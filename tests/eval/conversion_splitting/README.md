# Conversion splitting — labelled documents

Ground truth for one question: **how many runbooks should this document become?**

The conversion pipeline splits a source document into one runbook per *failure
mode* (`ANALYSIS_SYSTEM_PROMPT` → `_convert_all_failure_modes`). What counts as
a failure mode decides the answer, and getting it wrong is not visible in any
unit test — the criterion lives in a prompt, and the output is an LLM's.

These three documents pin the distinction that criterion has to make. None of
them is a FaultMaven runbook, so none is stopped by the already-a-runbook gate
(#1375): they exercise **generation**, which is where the defect actually was.

| document | expected runbooks | why |
|---|---|---|
| `vendor-guide-one-symptom.md` | **1** | One observable symptom (NGINX 502 Bad Gateway) with five causes in prose — dead upstream, slow upstream, oversized headers, all-upstreams-down, stale DNS. One runbook with five `### Cause` sections. |
| `postmortem-one-incident.md` | **1** | One incident (connection-pool exhaustion) with several contributing factors. |
| `multi-failure-guide.md` | **4** | Four genuinely distinct symptoms — `OOMKilled`, `ImagePullBackOff`, `Pending`, `CrashLoopBackOff`. An operator seeing one is not seeing the others. |

The third is the **control**, and it is the one that matters most: without it,
a criterion that simply always answered "1" would score two out of three and
look like a fix. Any change to `ANALYSIS_SYSTEM_PROMPT` must keep it at 4.

## What it caught

Before #1375's second commit, `ANALYSIS_SYSTEM_PROMPT` read:

> Failure modes must be distinct -- different symptoms **OR different resolutions**.

Causes of one failure have different resolutions by definition, so that `OR`
licensed one runbook per cause. Measured against the shipped pack and these
documents (`gemini-3.7-flash`):

| document / runbook | causes | modes before | modes after |
|---|---|---|---|
| `vendor-guide-one-symptom.md` | 5 | 4 | **1** |
| `multi-failure-guide.md` (control) | — | 4 | **4** |
| `es-cluster-yellow-red.md` | 8 | 8 | **1** |
| `mysql-replication-broken.md` | 7 | 7 | **1** |
| `redis-high-latency.md` | 7 | 7 | **1** |
| `pg-vacuum-bloat.md` | 6 | 6 | **1** |
| `cassandra-read-write-timeout.md` | 5 | 5 | **1** |
| `pg-connection-pool-exhaustion.md` | 5 | 5 | **1** |
| `rds-storage-full.md` | 4 | 4 | **1** |

End to end on the vendor guide, the single failure mode generated a runbook
carrying **Causes A–E plus Cause Z**, passing `RunbookValidator` — so the
collapse preserved every cause rather than discarding four of them. The old
path did discard: 4 modes became 3 drafts, the fourth dropped by the coarse
`(service, symptom_class)` collapse (#1376).

## Does the criterion survive a provider change?

It is enforced by PROSE. `_analyze_document` sends
`response_format={"type": "json_object"}`, which constrains the envelope and not
the content, so nothing structural stops a weaker model splitting by cause. The
role follows `CHAT_PROVIDER` when `KNOWLEDGE_PROVIDER` is unset, so whichever
provider a deployment runs is the one that actually decides.

Measured over these documents plus three shipped pack runbooks, one provider per
process (`get_settings()` is cached and reads the provider from the environment):

| provider | model | structured output | score |
|---|---|---|---|
| gemini | `gemini-3.7-flash` | STRICT | **6/6** |
| openai | `gpt-5.6-luna` | STRICT | **6/6** |
| fireworks | `deepseek-v4-flash` | **BEST_EFFORT** | **6/6** |
| anthropic | `claude-sonnet-4-5` | — | **not scorable** — #1380 |
| groq | `llama-3.3-70b-versatile` | — | **not scorable** — #1381 |

The Fireworks row is the informative one: a BEST_EFFORT provider, with no schema
enforcement of any kind, still merged the five-cause vendor guide into one
failure mode and still split the four-symptom control into four. The criterion
travels on prose alone rather than riding on a provider's schema mode.

The two unscorable rows are **not** criterion failures — neither provider could
be reached at all. Anthropic ignores `response_format` and returns its (correct)
JSON inside a markdown fence, which the bare `json.loads` at
`conversion_service.py:1099` cannot read; Groq's shipped default model id is
decommissioned and 404s. Both predate this corpus and are filed separately.

## Running it

There is no driver in `tests/` — the measurement calls a live LLM, so it is not
a CI test.

Drive `ConversionService._analyze_document` over `documents/` and compare the
mode counts to the table above. Note it needs the real settings and router (the
call resolves `settings.llm.get_knowledge_model()` and passes
`**_knowledge_route_kwargs()`), so construct the service rather than calling the
prompt directly:

```python
svc = ConversionService.__new__(ConversionService)
svc._settings = get_settings()
svc._llm_router = LLMRouter()
analysis = await svc._analyze_document(path.read_text(), path.name)
```

Do **not** copy the illustrative snippet in
`docs/architecture/knowledge-and-ai/document-to-runbook-conversion.md` §5.1: it
shows an `_analyze_and_split` that has never existed in the codebase, and a bare
`route(...)` that omits the knowledge-provider routing.
