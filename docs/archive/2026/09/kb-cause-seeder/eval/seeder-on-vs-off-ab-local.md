# Recorded run — 2026-09-02 · seeder ON vs OFF A/B (fm#1295 step 2)

The measurement the off-by-default decision rests on. It asks a different
question from the enabling gate: not *is a seed sound* but *does seeding help
the investigation reach the right cause*.

## Setup

- Engine: `faultmaven` at `6d90d94f` (the head of the #1296 branch, whose squash
  merge is `4558959c` on main; engine code identical to main at that point — the
  branch differed only in tests and docstrings), run as a local
  process on port 8091 with `CHAT_PROVIDER=openai`, `OPENAI_MODEL=gpt-5.6-luna`
  (the production chat model), the shipped 1297-chunk KB pack, `AUTH_MODE=local`.
- Arms: `FAULTMAVEN_KB_CAUSE_SEEDER=true` then `=false`, verified from the
  process's own resolved settings before each arm, and from the API log
  (`KB cause seeder: seeded N candidate cause(s)`) and the DB
  (`causal_nodes.metadata.seeded_from_runbook`) after.
- Sim: `fm-sre-simulator` at `825b1d4`, `--mode grounded --ground-truth
  --findings --no-intervention --max-turns 20`; persona and judge on Fireworks
  `deepseek-v4-flash-0731`, judge unchanged across arms; persona LLM timeout
  raised to 300 s after 5 of the first 12 runs died on the persona's own call at
  180 s (the FaultMaven API's slowest turn was 35 s). Those five were rerun; the
  first ended run per (arm, scenario) is what is tabulated.
- Scenarios (one per domain family, all with a matching runbook in the pack):
  `linux-disk-full`, `redis-oom`, `nginx-502-bad-gateway`, `kafka-consumer-lag`,
  `aws-iam-role-assumption-failure`, `grafana-dashboard-slow`.

## Result

| scenario | seeder ON | seeder OFF | seeded runbook (ON) | anti-anchoring retirements ON / OFF |
|---|---|---|---|---|
| aws-iam-role-assumption | resolved @16, judge 0.90 | **resolved @12**, 0.90 | EKS Cluster Auth (off-domain; its root text matched the answer and validated) | 2 / 0 |
| redis-oom | unresolved @20, 0.60, coverage 0 | **resolved @13**, 0.80 | Redis OOM (correct; root stayed candidate) | 5 / 1 |
| linux-disk-full | unresolved @20, 0.90 | **resolved @11**, 0.95 | nothing seeded (grounding gate silent on the sim's phrasing) | 2 / 0 |
| nginx-502 | unresolved @20, 0.60 | unresolved @20, 0.80 | NGINX 502 (correct; root inconclusive) | 2 / 2 |
| kafka-consumer-lag | unresolved @20, **0.95** | unresolved @20, 0.40 | Kafka Consumer Lag (correct runbook; seeded root VALIDATED — see below) | 3 / 3 |
| grafana-dashboard-slow | **resolved @13**, 0.95 | unresolved @20, 0.95 | Grafana Dashboard (correct; root inconclusive) | 1 / 0 |

| totals | ON | OFF |
|---|---|---|
| resolved | 2 / 6 | 3 / 6 |
| root cause identified (judge) | 3 / 6 | 5 / 6 |
| mean judge score | 0.82 | 0.80 |
| mean turns | 18.2 | 16.0 |
| hypotheses / anti-anchoring retirements | 26 / 15 | 16 / 6 |
| seeded rung evidence-needs that surfaced to the user | 4 of 5 seeded cases | — |

## Turn by turn (reconstructed from `case_messages`, `hypotheses`, `causal_nodes`, `evidence_needs`)

- **redis-oom.** Both arms found the oversized `rec:features:user:*` keys from
  the same pasted rollup, on the turn the persona pasted it — ON at turn 5, OFF
  at turn 7 (the OFF persona spent turns 5–6 on config output and a
  "got pulled away" turn). The seeded chains were not the cause found. The three seeded chains
  (maxmemory unset, client output buffers, fragmentation) were retired at turn 9
  as never linked to evidence; none of their 9 rung needs surfaced. The outcome
  gap is after diagnosis: the ON persona executed a rollback at turn 7 and the
  case waited 13 turns for a post-fix reading the persona cannot produce under
  `--no-intervention`; the OFF persona never executed and closed at turn 13.
- **grafana.** Both arms found Prometheus being OOM-killed from the pasted
  journal at turn 4. The correct runbook was seeded; none of its chains
  validated. Outcome gap: the ON persona escalated and closed at turn 13, the
  OFF persona looped on an unrecoverable verification window.
- **kafka.** The judge scored `root_cause_identified=False` for BOTH arms
  (scenario root: a dropped `idx_orders_customer_id` index). Both found the
  mechanical cause
  (poll-interval evictions) at turn 4. ON: the seeded root *"M members < N
  partitions"* validated at turn 6 on a literally-true indicator (3 members, 6
  partitions) surfaced by a seeded rung need at turns 4–5, and became the
  recorded `root_cause_conclusion`; ON never explored upstream. OFF surfaced
  the dropped index at turn 16 as an active hypothesis (likelihood 0.40) but
  did not converge on it. The judge's 0.95 vs 0.40 scored the mechanical
  cause; its own summary says ON "failed to investigate the upstream root
  cause". The one pair the seeder "won" is the fm#1144 failure mode with the
  correct runbook seeded.
- **iam.** The seeded off-domain runbook's root text happened to state the real
  answer and validated — the second of two seeded roots that reached VALIDATED
  in this batch, and the one that was right; resolved @16 vs @12. A correct seed,
  no speed gain.

## The remediation channel with the seeder off

The corrective pattern still reaches the model: in the OFF arm the
remediation-time prefetch fired on the redis case
(`KB pre-fetch (root_cause): 3 matches for case case_b3a35f94895d` in the API
log), exactly as it did in the ON arm, and the KB QA tools were called through
the runs. Pinned by
`test_kb_seed_grounding_1272.py::TestRemediationPrefetchIsNotGatedBySeeding`.

## Reading

In every examined pair the diagnosis came from evidence the user pasted, on
the turn that evidence arrived (which differed between arms only by when the
persona pasted it). No seeded chain was the cause found. Two seeded roots
reached VALIDATED: one stated the right answer (iam), one was wrong and became
the recorded conclusion (kafka). The
ON-vs-OFF outcome totals are otherwise dominated by the treatment-phase
verification loop, which is orthogonal to seeding. Under a symmetric choice the
measurement decides: **off by default** (this PR). Removal of the inert path is
fm#1295 step 4b.

## Reproducing

Run the API locally with the flag set per arm, warm the embedder with one KB
search, then for each scenario
`fm-sre-sim --api-url <local> --scenario <name> --mode grounded --ground-truth
--findings --no-intervention --max-turns 20 --run-id ab-<arm>-<name>-<stamp>`
with `llm.timeout: 300` in the sim config. Compare the findings' `ground_truth`
per run and, per case id (from the run log), `causal_nodes.metadata`
(`seeded_from_runbook`) and `hypotheses.retirement_reason` in the API's DB.
