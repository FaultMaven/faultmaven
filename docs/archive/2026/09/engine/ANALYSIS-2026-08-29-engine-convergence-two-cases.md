# Investigation-engine convergence: what two libvirt cases actually show

**Date:** 2026-08-29
**Status:** Analysis only. No code changed. No issue filed.
**Cases:** `case_c68b75b853ee` (natural run), `case_bf484a484a77` (human-hinted run)
**Corpus:** 35 cases in the onprem `faultmaven` database at time of query.

## Provenance

An agent drafted a report titled *"Engine converges slowly on path-based write
failures: hypothesis-testing bias over cheap-state gathering"*, proposing that the
engine's evidence-acquisition **ordering** is a systemic reasoning flaw. That
report was never filed as a GitHub issue and its author **never read either
case's stored data** — its turn-by-turn table is a reconstruction.

This document is the data check. It treats the report as a trigger for
investigation, not as a finding.

## Method

Read-only queries against the onprem primary (`faultmaven-postgresql-primary-0`)
over `cases`, `case_messages`, `hypotheses`, `hypothesis_evidence`, `evidence`,
`evidence_needs`, `uploaded_files`. Engine code read at the two deployed image
SHAs. Queries are in the appendix.

## The report's trace does not match the record

`case_c68b75b853ee` is **26 turns, not 7**. It is still `investigating`. The
report describes an early slice as if it were the whole case, and draws its
central counterfactual ("breadth-first reaches root cause in 2 turns") from it.
That counterfactual is contradicted below.

## Confounds between the two cases

| | `c68b75b853ee` | `bf484a484a77` |
|---|---|---|
| surface | `slack` | `copilot` |
| window | 2026-08-25 07:26–09:09 | 2026-08-25 20:17–21:13 |
| image | `sha-df46819` | `sha-434ec7f` |
| human hint | none | direction at turn 5, `df -h` at turn 9 |

**Build drift is immaterial.** `git diff df46819..434ec7f` touches only ChromaDB
auth, dependabot, CI and docs. `core/investigation/`, the prompt templates and
`hypothesis_manager.py` are byte-identical. The one live channel — KB seeding via
the Chroma token fix — behaved identically: both cases produced exactly 2
`opportunistic` hypotheses at turn 2 and systematic ones from turn 4.

**Surface is uncontrolled but not obviously load-bearing.** The engine never
reads `case.source` (no references in `core/investigation/`), and both cases
produced `causal_evidence` rows with no source file, so the evidence path is not
gated on the upload channel.

The hint is therefore the live variable, as intended.

## Finding 1 — the human intervention did not rescue the run

At turn 5 the user asked *"did you rule out a problem with disk space?"*. At
turn 9 they pasted a full `df -h` / `df -hi` containing the root cause verbatim:

```
/dev/sdd4        98G   94G     0 100% /var/lib
/dev/sdd3        74G  962M   69G   2% /var
```

The engine read it — it formed the correct hypothesis that same turn — and then
produced **no causal evidence from it at all**. The hypothesis sat at likelihood
0.18 with zero evidence links, aged into stagnation, and was retired with
`"Anti-anchoring: retired a stalled hypothesis to diversify the differential"`.
The user re-pasted `df -h` at turn 15; the hypothesis was recreated at 0.80 and
the case is still `no_root` at turn 16.

| | natural (26t) | hinted (16t) |
|---|---|---|
| causal-evidence rows by turn 16 | 2 | **0** |
| total causal-evidence rows | 5 | **0** |
| distinct facts recorded | several | **1**, recorded 5× |
| correct hypothesis | turn 19 → anti-anchoring retired | turn 9 → anti-anchoring retired |

**This refutes the report's counterfactual.** The breadth-first intervention was
performed by hand and the run was still ungrounded at turn 16 — worse-grounded
than the unhinted run. Acquisition ordering was bypassed and nothing improved.

## Finding 2 — the narrow-query defect is real, in the *natural* case

`c68b75b853ee` turn 4 recorded a `causal_evidence` row:

> "Filesystem capacity and inode availability do not indicate exhaustion:
> /var 2% used with 1% inode use; /run 1% used with 1% inode use."

That is the report's Area 3 (hypothesis-shaped narrow query) and Area 6
(misleading negative) — and it is worse than the report described. The false
clearance was written into the **durable record as causal evidence** and stood
for 17 turns, until turn 21 recorded the contradicting truth. Both rows coexist
today. `superseded` exists on `evidence_needs` only; **`evidence` has no
retraction path**.

## Finding 3 — corpus prevalence separates systemic from edge case

This is the part that governs whether anything should change.

### Systemic — anti-anchoring retires hypotheses that were never tested

Hypothesis deaths across all 35 cases:

| state | n | of which anti-anchoring |
|---|---|---|
| retired | 54 | **35** |
| active | 39 | — |
| validated | 18 | — |
| refuted | **9** | — |
| captured | 1 | — |

Anti-anchoring is the single largest cause of hypothesis death in the corpus —
roughly 4× refutation by evidence. Splitting those 35 by whether the hypothesis
was ever linked to evidence:

| grounding at retirement | n |
|---|---|
| **never linked (never tested)** | **27 (77%)** |
| 1 evidence link | 5 |
| 2 evidence links | 2 |
| 3 evidence links | 1 |

Baseline for non-anti-anchoring retirements: 9 linked vs 10 never-linked (53%).
So anti-anchoring is disproportionately retiring **untested** candidates.

This is working as designed — `advance_stagnation_if_ignored`
(`hypothesis_manager.py:549`) was added deliberately (#713) so that ignored
hypotheses cannot linger as permanent unrefuted siblings. The design gap is that
it feeds ignored hypotheses into the *same* machinery as tested ones, so the
record cannot distinguish **"tested and abandoned"** from **"never grounded and
discarded"**.

### Systemic — the deadlock path is unreachable

Of 26 cases with ≥3 hypotheses, **21 (81%) have zero `REFUTED` hypotheses**. The
`HYPOTHESIS DEADLOCK` block (`templates.py:1986`) keys on refutation, so in four
out of five substantive cases it can never fire.

### EDGE CASE — do not generalise: causal-evidence extraction

Cases with ≥5 turns:

| state | cases | zero causal evidence | avg causal rows | avg turns |
|---|---|---|---|---|
| investigating | 17 | **1** | 5.5 | 15.2 |
| resolved | 6 | 0 | 4.2 | 12.3 |
| closed | 4 | 0 | 3.3 | 12.0 |

The single zero-causal case is `bf484a484a77` itself — **1 of 27**. Extraction
works in the other 26. Its total failure there is an outlier that deserves its
own bug report; it is **not** grounds for redesigning the extraction path.

### Common but moderate — evidence recycling

Duplicate extract prefixes within a case appear in ~7 cases, typically 2–4×
(worst: 4). Real, and visible in `bf484a484a77` (the same `Aug 21 06:13:38`
journal line four times), but secondary.

### Correlational only — anti-anchoring vs outcome

| state | cases | avg anti-anchoring retirements | avg turns |
|---|---|---|---|
| investigating | 17 | 1.6 | 15.2 |
| resolved | 6 | 0.7 | 12.3 |
| closed | 4 | 0.8 | 12.0 |

Unresolved cases show ~2× the anti-anchoring rate — but they also run longer
(more turns = more chances to fire), and causality plausibly runs the other way
(hard cases stall more *and* trip the detector more). Three **resolved** cases
contain never-linked anti-anchoring retirements and resolved anyway.

**This is not evidence that anti-anchoring causes non-resolution.** The
mechanism is survivable and fires in successful cases.

## Verdict on the report's six areas

| # | Area | Verdict |
|---|---|---|
| 1 | Evidence-acquisition cost model | Absent in code; **not the bottleneck** (Finding 1) |
| 2 | Observation vs hypothesis phase | Confirmed natural case; **refuted** hinted case |
| 3 | Query broadening | **Confirmed** — turn-4 `/var` check |
| 4 | Contradiction escalation | Confirmed absent, and unreachable in 81% of cases |
| 5 | Domain invariants | Untested; still plausible |
| 6 | Misleading negatives | **Confirmed, and worse** — false clearance is durable |

The report ranks acquisition ordering first. The data puts it third at best, and
Finding 1 shows fixing it would have changed neither case.

## Recommendation — sequenced for regression safety

The engine's methods work in most cases. Nothing below changes agent behaviour.

1. **Make the distinction observable first (zero behavioural risk).** Record, at
   retirement, whether a hypothesis had ever been linked to evidence — without
   changing *whether* it is retired. This is a data change only, and it makes
   every later decision measurable. It also addresses a soundness concern
   independent of any tuning: `report_generation_service.py:910` buckets
   hypotheses into validated / refuted / inconclusive / **other**, and `retired`
   falls into the unlabelled "Other" bucket under the heading "Hypotheses
   Considered", rendered with a confidence percentage and nothing to mark it as
   never grounded. A reader sees a candidate that was considered and dispatched;
   the record cannot say it was never tested.
2. **Build #1142 (case telemetry, attribute a stall to a side).** Already in the
   backlog, not built. The only reason this analysis required a database
   spelunk is that it does not exist.
3. **Then, and only then, evaluate anti-anchoring** with a simulator scenario
   family and an A/B on the grounding distinction. Do not tune thresholds off
   these two cases.
4. **File `bf484a484a77`'s zero-causal-extraction as its own bug.** Scoped as an
   outlier, not an engine redesign.
5. **Do not act on acquisition ordering.** Refuted by Finding 1.
6. **Open question — evidence retraction.** The turn-4 false clearance is a
   soundness issue (a durable causal row asserting something untrue). Measure
   prevalence before designing anything.

## Appendix — queries

See `scratchpad/case_dump.sql` for the per-case dump. Corpus queries:

```sql
-- hypothesis death causes
SELECT state, count(*) AS n,
       count(*) FILTER (WHERE retirement_reason LIKE 'Anti-anchoring%') AS anti_anchor
FROM hypotheses GROUP BY state ORDER BY n DESC;

-- anti-anchoring: grounded or never tested?
SELECT CASE WHEN coalesce(l.n,0)=0 THEN 'NEVER LINKED' ELSE 'linked ('||l.n||')' END AS grounding,
       count(*) AS hypotheses
FROM hypotheses h
LEFT JOIN (SELECT hypothesis_id, count(*) AS n FROM hypothesis_evidence GROUP BY hypothesis_id) l
  ON l.hypothesis_id = h.hypothesis_id
WHERE h.retirement_reason LIKE 'Anti-anchoring%'
GROUP BY grounding ORDER BY hypotheses DESC;

-- causal-evidence coverage by outcome
SELECT c.state, count(*) AS cases,
       count(*) FILTER (WHERE ev.causal = 0) AS zero_causal,
       round(avg(ev.causal),1) AS avg_causal, round(avg(c.current_turn),1) AS avg_turns
FROM cases c
JOIN (SELECT case_id, count(*) FILTER (WHERE category='causal_evidence') AS causal
      FROM evidence GROUP BY case_id) ev ON ev.case_id=c.case_id
WHERE c.current_turn >= 5 GROUP BY c.state;
```
