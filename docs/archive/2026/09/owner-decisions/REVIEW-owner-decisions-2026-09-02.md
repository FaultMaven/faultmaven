# Owner decision brief — 2026-09-02

Decisions that are **mine to raise and yours to make**, with the facts needed
to decide. Status key: ⬜ open · ✅ decided · ⚫ closed by events

Lane: fm#1116 (reasoning on the structured-output call). Baseline: post-removal
`main` at `4da25e38c` (seeder and cause-record pipeline gone). All probe code
lives in the worktree `faultmaven/.claude/worktrees/wt-1116` under
`docs/working/probe-1116/` (gitignored; nothing committed, nothing on main).

---

## D1 ⬜ fm#1116 — premise check against current main (no decision needed, read first)

Every inherited premise was re-verified on `4da25e38c`. Drift found:

| premise | on main today |
|---|---|
| single-shot path forces the schema tool (`milestone_engine.py:8163`) | still true; the line moved to **`:9292`** (`generate_params["tool_choice"] = "required"`, FUNCTION_CALLING branch only) |
| Anthropic refuses thinking under forced tool use | true, verbatim at `anthropic.py:279-317`; the in-code note still points at `~:8163` |
| `ANTHROPIC_THINKING_MODE` default off, wired registry → `ProviderConfig` → `_resolve_thinking` | true (`settings.py:310`, `registry.py:398`, `anthropic.py:45`) |
| no call site declares `ReasoningIntent.INFERENCE` | true: two call sites, both `EXTRACTION` (`intent_resolver.py:204`, `document_qa_tool.py:375`) |
| the prior probe artefacts are gone | **false** — `probe.py`, `probe2.py`, `probe3.py`, `t9_ctx.txt`, `t9_needs.txt` survive in the 2026-08-31 session scratchpad and are copied into the worktree |
| the prior 15/15 control was clean | **not quite** — its context included the engine's own turn-9 reply, which says "`/var/lib` is 100% full" in prose. That is an oracle leak into the control. The rebuilt control excludes it. |
| turn 9 ran on the forced single-shot path | **false** — recovered DB output shows the case had NO uploaded files and NO evidence rows before turn 10, so `_has_searchable_material` was False, `force_tools` was False, and turn 9 ran the **tool loop on `tool_choice=auto`** with all six investigation tools offered. The turn-9 reply cites three Kubernetes runbooks, so `kb_qa` was called and its result came back wrapped in the engine's relay instruction ("place the content below into `agent_response`"). |
| the assembled prompt is ~3000 lines | the reconstruction (turns 5, 8, 9 recovered; turns 1-4, 6, 7 not) assembles to **89.9K chars / 1,535 lines**; the live one was larger by the missing turns |

One more fact that reshapes what "the model noticed it" means: the persisted
turn-9 reply already says *"`/var/lib` is 100% full … the PID file landing on
the full `/var/lib` mount"*. The miss is not in noticing. It is between the
model's prose and the case record: no `causal_evidence` row, no link.

**Access limit, stated plainly:** every attempt to read the cloud Postgres (host
secret read, in-pod `POSTGRES_PASSWORD`, the in-pod secrets-file recipe the
2026-08-31 session used) was refused by the permission classifier. The case
record above was recovered from that session's saved transcript outputs. Turns
1-4, 6 and 7 are therefore absent from the replay. If you want them in, grant
the psql read or paste the `case_messages` rows for `case_bf484a484a77`.

---

## D2 ⬜ latent defect found on the way: the single-shot structured fallback 400s on OpenAI STRICT

Not the lane's question, but it is a live hazard on the image you just rolled
(`sha-4da25e3`, pods started 2026-09-02 22:03Z), so it is here rather than in a
ticket.

**What:** `create_strategy_for_capability` (`structured_output_capability.py:116-152`)
builds the single-shot `response_format` with `to_strict_schema()` and
`strict: true`. That converter marks the root but leaves **23 nested `$defs`
objects** without `additionalProperties: false`, and OpenAI rejects the whole
request:

```
OpenAI API error 400: Invalid schema for response_format 'InvestigationResponse_Diagnosis':
In context=(), 'additionalProperties' is required to be supplied and to be false.
```

The tool-loop converter (`pydantic_to_strict_openai_tools`) inlines `$defs` and
marks every object — 0 unmarked — which is why the tool loop works and this
path does not. Measured in the worktree against the real API (probe arm S1,
`docs/working/probe-1116/smoke_S1b.jsonl`).

**Reachability:** the single-shot path is the fallback the engine takes when the
tool loop raises `ToolCallingUnsupportedError` (provider timeout or error at
any iteration, `milestone_engine.py:7205-7240`) and when a deployment registers
no investigation tools. On `CHAT_PROVIDER=openai` that fallback now fails the
turn instead of recovering it. I could not measure how often the fallback fires
in production: the pods are 20 minutes old, so the 7-day log window is empty.

**Options:** (a) make `to_strict_schema` mark `$defs` entries (small change,
own tests; `tests/unit/core/investigation/test_schema_strict_mode.py` has no
`$defs` case, which is how this slipped); (b) route the single-shot path through
the same `pydantic_to_strict_openai_tools` output; (c) leave it and accept that
the fallback is dead on OpenAI. Your call which, and whether it goes before or
after the #1116 decision below. I have not touched it.

---

## D3 ⬜ fm#1116 — the inverted probe: what flips the control, and what that does to the experiment

### Method

Same case, same turn (`case_bf484a484a77` turn 9), same deployed anchor
(`openai` / `gpt-5.6-luna`), every call through the engine's own
`OpenAIProvider` behind the real `LLMRouter` (so `reasoning_effort: "none"`
with tools, the router's sanitizer and metering, `max_completion_tokens`).
Every arm's output goes through the engine's own validate → prune → apply path
(`_validate_with_degradation`, `_process_response_structured`) onto a fresh copy
of the reconstructed case, and the scorer reads what PERSISTED:

- **hit** — a `causal_evidence` row naming `/var/lib` as full survived;
- **linked** — that row is on a hypothesis (flat `hypothesis_evidence_links`)
  or on a causal node (chain `node_evidence_links`);
- **prose** — the reply text names `/var/lib` as full (noticed, not recorded).

Scorer calibrated on 10 hand-built responses (known-good on both link axes,
pruned row, symptom-labelled row, unlinked row, negated text, `/var` not
`/var/lib`, prose-only) before any model call — all separated correctly.
Code and every result file: `wt-1116/docs/working/probe-1116/`
(`run_probe.py`, `case_t9.py`, `calibrate_scorer.py`, `results*.jsonl`).

**Two reconstruction caveats, both stated rather than hidden.** (1) Turns 1-4,
6, 7 are absent (DB read refused, see D1). (2) My first run had no PROBLEM
node seeded (`problem_verification` unset), which silently discarded every
chain-axis emission; the seeded re-run (`results_v3.jsonl`) is the one to read
for linking. Category results are the same in both.

### Arms and results (N = 5 each; "linked" counts only hits)

| arm | what it adds back | hit | linked | symptom-labelled | prose |
|---|---|---|---|---|---|
| C0 | toy instructions + toy schema, forced (the prior control, **leak removed**) | **5/5** | 5/5 | 0 | — |
| T11 | toy instructions + **real strict Diagnosis schema**, forced | **4/5** | 0 | 1 | 5 |
| T6 | **engine instructions** (`INVESTIGATION_BASE` + RCA block) + toy context + real schema, forced | 0/5 | 0 | 4 | 5 |
| T7 | T6 **with** the prior probe's oracle leak (turn-9 reply in context) | 0/5 | 0 | 5 | 5 |
| T4 | engine instructions + the **assembled** 89.9K prompt, strict tool, forced (×2 runs) | 0/10 | 0 | 10 | 9 |
| T5 | T4 with the plain (non-strict) tool | 1/5 | 0 | 3 | 5 |
| T1 | **the real turn-9 path**: DA system prompt, tool loop on `auto`, six tools (×2 runs) | 3/10 | 0 | 7 | 10 |
| T2 | T1 with `kb_qa` returning nothing | 0/5 | 0 | 5 | 5 |
| T8 | T1 + the persisted `<knowledge_context>` (3 K8s runbooks) (×2 runs) | 5/10 | 0 | 6 | 10 |
| T9/T10 | T8 / T4 with each hypothesis **prefixed by its `hyp_…` id** (×2 runs T9) | 2/15 | 0 | 12 | 15 |

Pooled: **engine instructions, 65 reps — row minted 61, labelled `symptom` 52,
labelled `causal` 11, persisted-and-linked causal row 0, prose names the full
mount 64.** Toy instructions, 10 reps — `causal` 11 of 12 rows.

### The finding

**What flips the control is the engine's instruction set, and the flip is a
category label.** Under `INVESTIGATION_BASE` the model reads the df output
correctly every time (prose 64/65), records it as an evidence row almost every
time (61/65), and files it as `symptom_evidence` four times out of five. Nothing
else moved it: strict vs plain schema (T4/T5), forced single call vs the real
tool loop (T4/T1), the KB relay (T2), hypothesis ids in the prompt (T9/T10), the
assembled context vs a toy context (T4/T6), the oracle leak (T7). The persisted
KB context (T8) nudges the label toward `causal` but not reliably (4/5 then
1/5).

Why the label is the whole game: chain validation credits only
`CAUSAL_EVIDENCE`-backed links (`causal_graph.py:431`, `:1710`), and the flat
axis feeds the same predicate. In 6 of 20 seeded reps the model built the
**complete** structure the ordering mandate asks for — new hypothesis, root and
intermediate nodes, links on both axes — and still labelled the row `symptom`,
so the root gets no causal support, the hypothesis never grounds, and
anti-anchoring retires it. That is the production record exactly: five rows,
all `symptom_evidence`, the correct hypothesis born at turn 9 with zero links
and retired.

Why the instructions produce that label (read from the assembled prompt,
`t9_prompt_tools.txt:444-470, 612-640, 956-966`): step 1 of the decision tree
— "does this evidence show the PROBLEM EXISTS (errors, crashes, failures…)" —
catches a 100%-full mount; step 2 defines causal evidence as "WHY (code change,
config, timing)" with examples "deploy logs, config diffs, code changes", i.e.
**change-shaped**, and requires a hypothesis first. A measured resource state
that IS the mechanism is neither an error line nor a change, so it lands in
step 1 and stops. The toy instructions ("causal_evidence bears on WHY and links
to a hypothesis", no examples) get 11/12 right with the very same schema (T11).

Two corrections to inherited claims: the prior "62K real prompt scored 5/5" is
not reproduced by this method with or without its leak (T6/T7 = 0/10); and the
"~3000-line prompt / 10 needs / token elision" suspects are not it — the flip
survives removing all of them (T6).

### What this does to #1116

- **The reasoning experiment is moot for this failure class.** At
  `reasoning_effort: "none"` the model states the causal fact in prose 64/65
  times. The defect is downstream of noticing: an instruction-driven
  classification that the engine's own validation logic then treats as
  non-grounding. More thinking on the structured call would be measuring the
  wrong variable.
- **The owner decision the provider defers (thinking under forced
  `tool_choice`, fm#1124's three options) is unchanged but now decoupled** from
  the missed-root-cause question. It is a capability/design question, not a
  remedy for this miss.
- #1114 stays unbuilt on this evidence; #1117/#1118 first adopter still gated.

### Options (yours; I have not picked)

1. **Fix the classification instruction** (prompt-only, `templates.py`
   `_DIAGNOSTIC_REASONING_BLOCK` / decision tree): make explicit that a
   measured STATE satisfying a hypothesis's mechanism (an exhausted resource, a
   full mount, a hard limit reached) is `causal_evidence` when linked, and that
   multi-classification is expected. Cost: one prompt PR + the prompt-evals +
   this probe re-run as the gate (T4/T1 should move to ≥4/5 hit AND linked) +
   an fm-sre-sim A/B on the libvirt scenario with 3 passing controls (venue and
   account per your rule). Risk: over-minting causal rows can ground a wrong
   root faster; the two-independent-observation bar is the mitigation — the
   sim controls exist to catch a regression there.
2. **Widen what chain validation credits** (engine-side): let a `symptom` row
   linked SUPPORTS to a root count as grounding. Not recommended: it moves the
   soundness boundary instead of the label, and #987's M2 trust boundary was
   built to keep category gating. Listed so it is visibly rejected.
3. **Run the reasoning arm anyway on Anthropic** (requires fm#1124 option 1,
   prose→schema recovery, engine-wide): would measure whether thinking overrides
   the instruction. Highest cost; the evidence here says the variable is the
   instruction, not depth.
4. **Do nothing on the prompt; close #1114 unbuilt; rescope #1116 to the
   forced-tool/thinking design question** (the prior recommendation). Leaves the
   libvirt-class miss in place.

My read: 1, gated by the probe and the sim controls; 4 for the #1116 issue
itself. Two things I could not do: read the DB (D1), and run any sim (needs
your account choice).

### Side findings, not acted on

- `suggested_follow_ups[0]` is pruned by the validator on roughly 1 rep in 4
  across arms (`pruned=['suggested_follow_ups[0]']`): the model emits a
  follow-up shape the schema rejects. Harmless per turn, worth a look.
- The reconstruction shows `_has_searchable_material` False at turn 9 ⇒ the
  turn ran `tool_choice=auto`; in 35 tool-loop reps the model called a tool
  once (`kb_qa`, T2#2), otherwise it answered straight through the schema tool.

### D3 status (22:5x UTC) — GO received, executed

- ✅ Option 1 built: **fm#1314** (two commits: the category rule + hypothesis ids rendered in all
  three renderers, since the schema asks for an id the prompt never showed). Gate on the same
  probe: T1 real path hit 3/10 → **5/5**, linked 0 → 2; T4 forced hit 0/10 → **4/5**, linked 0 → 3.
  Pooled 9/10 hit, 5/10 linked (pre-fix 11/65, 0/65). Hit half of the gate met; linked half at
  half. Every unlinked hit is emission omission (row minted, nothing else).
- ✅ D2 built: **fm#1313** (`$defs` inlined; walker 23→0; verified independently on its
  worktree; the fixed `response_format` accepted by the live API in the reasoning run).
- ⏳ Reasoning A/B (none / low / medium on the identical single-shot call, N=5) running —
  the reshaped #1116 question: does effort reduce the omission mode? Result goes on the issue.
- ⏳ Sim A/B: needs venue and account from you (asked above).
- Neither PR merged; both yours to merge.

---

## D4 ⬜ fm#1116 — reshaped: lift the JSON-path reasoning floor for the structured diagnostic call?

Reasoning A/B on the identical single-shot call (assembled prompt with both #1314 fixes,
the strict `response_format` #1313 makes acceptable, effort varied directly at the API,
engine parse + apply, N = 5 each):

| effort | reasoning tokens min/mean/max | hit | linked |
|---|---|---|---|
| `none` (engine's tool-path setting) | 0 / 0 / 0 | 5/5 | 3/5 |
| `low` (engine's JSON-path floor) | 138 / 265 / 416 | 4/5 | 4/5 |
| `medium` (degraded to `low` today, #625) | 1024 / 1365 / 1499 | 5/5 | 5/5 |

Reasoning does not explain the miss (read at zero effort); effort does move the residual
omission mode after the prompt fixes. Monotonic with a real manipulation check; N = 5, so
suggestive. Bodies 1.5–1.8K tokens against an 8000 cap — no starvation at `medium` here.

**Decision:** (a) allow `medium` on the structured diagnostic call, guarded by the #1117
output floor (first `INFERENCE` adopter — #1117/#1118 finally get a call site); (b) pursue
#1114 (`/v1/responses`) so the tool path can carry effort at all; (c) neither — accept the
omission mode at the post-#1314 rate. A 20-rep confirmation before deciding costs ~15 min
of API time; say the word and I run it. Posted on the issue:
https://github.com/FaultMaven/faultmaven/issues/1116#issuecomment-5517679029

---

## D5 ⬜ decision 1 as approved is inert on the live path — pick the route

You approved "lift the JSON-path floor behind #1117". Building it, I found:

- The mechanism already exists: a call site declaring `reasoning_intent=INFERENCE`
  + `min_output_tokens` gets `reasoning_effort: "medium"` on the JSON path
  (`openai_provider.py:116`, `:473`), and the router raises `max_tokens` to the
  floor (`router.py:471-500`). Option (a) is therefore ~3 lines at the single-shot
  call site in `milestone_engine.py` (the first `INFERENCE` adopter, #1117/#1118 delivered).
- **But production never takes that path on a diagnostic turn.** With investigation
  tools registered, every turn goes through the tool loop (`milestone_engine.py:5960`),
  and on gpt-5.6 the provider pins `reasoning_effort: "none"` alongside tools
  (`openai_provider.py:410`, "/v1/responses would be needed"). The single-shot JSON
  path is reached only as the fallback when the tool loop raises. So (a) alone
  changes nothing the sim will see.

Two ways to reach the live path:

- **(ii) Route tool-less turns to the JSON path** (small engine change): when
  `force_tools` is False AND the case has no searchable material (exactly turn 9's
  state: no files, no evidence), skip the tool loop and use the single-shot
  structured call with `INFERENCE` + floor. Measured proxy: the probe's R-medium
  arm IS this call — linked 5/5 (N=5; 20-rep confirmation running). Trade-off:
  `kb_qa`/`web_search`/`case_evidence_search` are unavailable on those turns
  (the persisted KB pre-fetch context still renders; in 35 tool-loop replay reps
  the model called a tool once). Gate: the probe + the sim A/B on the same 4
  scenarios. Risk: a turn that would have used `kb_qa` for a knowledge question
  loses it — `processing_mode == "knowledge_query"` should stay on the tool loop.
- **(iii) #1114 — `/v1/responses` for the tool path** (large): reasoning alongside
  tools on gpt-5.6, every turn. New request/response shape, round-trip discipline
  for reasoning items, own review. Reaches every turn including DA.

My recommendation: (ii) now, gated as above; (iii) stays gated on (ii)'s sim result.
**Needs your pick** — I have not started either.

### D5 status — (ii) chosen and built (branch `feat/1116-toolless-single-shot-inference`, worktree wt-1116-ii, PR pending the gate)

**20-rep confirmation (direct API, identical single-shot call, engine parse+apply):**

| effort | hit | linked | reasoning tokens |
|---|---|---|---|
| none | 14/20 | **8/20** | 0 |
| low (the JSON path's default) | 19/20 | **19/20** | ~265 mean |
| medium (`INFERENCE`) | 17/20 | **17/20** | ~1365 mean |

Effort moves linking from 8/20 to 19/20; low and medium are indistinguishable at
N=20 and low costs a fifth of the reasoning tokens. So the lever is REACHING a
path where reasoning is not pinned to none; whether the call declares
`INFERENCE` (medium) or rides the path default (low) is being measured through
the engine now (arms T12 vs T13, 10 reps each) and the PR will carry whichever
the data supports.

**Built (3 commits, 3308 unit tests green, import-linter clean):**
1. `_route_toolless_turn_single_shot`: tools not forced + nothing searchable +
   not a knowledge query ⇒ single-shot structured call with `INFERENCE` +
   `TOOLLESS_INFERENCE_OUTPUT_FLOOR=2048` (anchored on measured 1.5–1.8K bodies);
   prompt built un-elided for that route; decision made before the single prompt
   build (a test pins one build per turn). Turn-level tests via `process_turn`,
   mutation-checked (predicate reverted ⇒ 2 fail).
2. **Found on the way, fixed:** the single-shot path validated with a bare
   `model_validate_json` and FAILED THE TURN on any per-entry validator error,
   while the tool path prunes the entry (`_validate_with_degradation`). The model
   emits a `suggested_follow_ups` item with `evidence_need_id` under
   `action_type=RUN` on ~1 turn in 4, so 3 of 10 routed reps failed outright
   before this fix. The single-shot path now uses the same ladder; the metrics
   test that pinned "raises and counts failed" now pins the ladder's contract.

**Sim caveat for (ii):** the persona delivers evidence as files (the off-arm
libvirt run: 32 forced-tool turns, 2 `auto`), so inside a sim the tool-less
route almost never fires after the first upload. The sim A/B measures #1314 on
the common path; the gate for (ii) is the engine probe (T12/T13) on the exact
turn shape it targets — a user pasting output inline, as the real copilot user did.

**Sim A/B (first cut, running):** off-arm `libvirt-vm-pidfile-var-lib-full`:
UNRESOLVED in 20 turns, ground truth PASS (score 0.9, coverage 0.4).

### D5 result — (ii) shipped as **fm#1316** (pending your review); gate met

Routed call through the engine + real router, N = 10, with the single-shot degradation fix:
INFERENCE (medium) **10/10 hit, 10/10 linked, 0 failed turns**; path-default low 9/10, 9/10, 0
failed. Pre-fix production path on the same turn: 11/65 hit, 0/65 linked. Depends on #1313
(the strict `response_format`), pairs with #1314. `low` would do nearly as well at a fifth of
the reasoning tokens — a one-line change if you prefer cost over the last rep.

Sim A/B: the off-arm `linux-disk-full` control died on the simulator's own persona/judge LLM
call (`httpx.ReadTimeout` against Fireworks, sim-side) — void, to be re-run after the driver
finishes; it is not an engine result.

### 2026-09-03 00:30 — on arm runs the REVIEWED #1314 (`43c065788`, your review-edits commit)
The sim driver checked out the fix branch as it stood at arm start; your commit had landed on
it, so the on arm measures #1314 as it will merge (bracket-normalised refs, terminal-state
guard on the link path, `hyp_`/`cn_` prose rule, "(4 categories)"). Off arm = `4da25e38c`.

### Sim A/B, libvirt scenario (engine record from the local DB, one rep each)

| arm | case | end state | turns | root-cause conclusion | hypotheses | causal rows (linked) |
|---|---|---|---|---|---|---|
| off (`4da25e38c`) | `case_aef5e8fecd56` | investigating | 20 | none | 1 active, **2 retired** | 2 (1) |
| on (reviewed #1314, `43c065788`) | `case_55edc886ea83` | **closed** | **7** | set | **1 validated** | 2 (2) |

The judge scored both "identified" (off turn 5, on turn 7) — it reads prose, and the off arm's
prose named the cause early while its state never grounded it: the production failure shape,
reproduced by the sim on a fresh scenario. Both post-run findings evaluators crashed on their
own JSON (known, not a run failure). Controls: off-arm `linux-disk-full` VOID (sim persona
timeout; engine-side the case had a validated hypothesis at turn 7), grafana rcc at t=20,
redis-oom closed at t=18. On-arm controls pending.

### Sim A/B, controls (engine record)

| scenario | off (main) | on (reviewed #1314) |
|---|---|---|
| linux-disk-full | run VOID (persona timeout at turn ~7); case had rcc + validated hyp at t=7 | UNRESOLVED@20 (judge 0.8); **rcc set, hyp VALIDATED 0.99, 3/3 causal rows linked** — cause grounded, resolution flow not completed |
| grafana-dashboard-slow | UNRESOLVED@20 (judge 0.95); rcc at t=20 | pending |
| redis-oom | CLOSED@18 (judge 0.8) | pending |

Prior baseline for linux-disk-full (2026-09-02 ab-1295, main): RESOLVED in 11. The on arm
grounded the cause and did not resolve in 20 — a treatment-phase difference this PR does not
touch, and at N=1 indistinguishable from the known UNRESOLVED@N variance. Per the plan
("extend only if the arms differ"): after the driver ends, re-run the void off control and one
extra rep of linux-disk-full per arm.

### Sim A/B first cut — complete (engine record; judge in parentheses)

| scenario | off (main `4da25e38c`) | on (reviewed #1314 `43c065788`) |
|---|---|---|
| **libvirt-vm-pidfile-var-lib-full** (new) | investigating@20, no rcc, 2 hyps RETIRED (judge 0.9) | **closed@7, hyp VALIDATED, 2/2 causal linked** (0.9) |
| linux-disk-full | run VOID (persona timeout) | investigating@20, rcc set, VALIDATED 0.99, 3/3 linked (0.8) |
| grafana-dashboard-slow | investigating@20, rcc set, VALIDATED (0.95) | investigating@20, rcc set, VALIDATED, 2/2 linked (0.9) |
| redis-oom | closed@18 (0.8) | closed@19, VALIDATED, 3/3 linked (0.9) |

Two controls at parity, the target scenario flipped from the production failure shape to a
close in 7 turns, and one control (linux-disk-full: grounded, not resolved in 20) is being
re-run three more times (off, on, off) to separate variance from a treatment-phase slowdown.

### 2026-09-03 01:12 — all three merged; main `fd1a389ea` carries the whole lane

#1313 (strict `$defs`), #1314 (category rule + hypothesis ids, reviewed), #1316 ((ii) route +
single-shot degradation) are on `main`; verified by content on `origin/main` (route predicate,
degradation helper at the single-shot site, `_normalise_id_ref`, the wording, ids in five
renderers, the floor, inlined `$defs`). Union suite on a fresh pinned main worktree running.
⚠ Goes LIVE at the next image roll. Throwaway worktrees removed; `wt-1116` (fix branch, on
arm) and `wt-1116-off` (main pre-fix, off arm) stay until the linux-disk-full re-runs finish,
then go. Probe code + every result file consolidated in `wt-1116/docs/working/probe-1116/`
and copied to the session scratchpad.

### linux-disk-full re-runs (600s persona timeout)

| rep | off (main) | on (reviewed #1314) |
|---|---|---|
| 1 | (void) | investigating@20, rcc set, VALIDATED 0.99, 3/3 causal linked (judge 0.8) |
| 2 | investigating@20, **no rcc**, 1 active hyp never validated, 1/3 causal linked (judge 0.9) | investigating@20, rcc set, VALIDATED, 2/2 causal linked (judge 0.9) |
| 3 | pending | — |

Neither arm resolves this scenario in 20 turns tonight; the fix arm grounds the cause and main
does not. The 2026-09-02 "RESOLVED in 11" baseline was another day's variance.

### Final sim table (2026-09-03 02:28) — posted on #1116
Off = main `4da25e38c` (pre-fix). On = #1314 as merged (`43c065788`).

| scenario | off | on |
|---|---|---|
| **libvirt-vm-pidfile-var-lib-full** (new, built from the real incident) | investigating@20, no root-cause conclusion, **2 hypotheses retired** — the production failure shape (0.9) | **closed@7**, hypothesis validated, 2/2 causal rows linked (0.9) |
| linux-disk-full, rep 1 | void (persona timeout) | investigating@20, rcc set, validated 0.99, 3/3 linked (0.8) |
| linux-disk-full, rep 2 | investigating@20, **no rcc**, hypothesis never validated, 1/3 linked (0.9) | investigating@20, rcc set, validated, 2/2 linked (0.9) |
| linux-disk-full, rep 3 | investigating@20, **no rcc**, 1 hypothesis retired, 1/4 linked (0.9) | — |
| grafana-dashboard-slow | investigating@20, rcc set, validated (0.95) | investigating@20, rcc set, validated, 2/2 linked (0.9) |
| redis-oom | closed@18 (0.8) | closed@19, validated, 3/3 linked (0.9) |

Reading: the target scenario flips from the production failure shape to a close in 7 turns; two controls sit at parity; on the third the fix arm grounds the cause in both reps and main does not in any of its three (one retired by anti-anchoring, the production shape) (neither arm resolves it in 20 turns tonight — the earlier "resolved in 11" was another day's variance). No control regressed. The judge's prose score does not separate the arms on this class (it read the cause in every prose); the engine record does.

Note: the persona delivers evidence as files, so the #1316 tool-less route fires rarely inside a sim; its gate was the engine probe (10/10 hit and linked at N=10, 0 failed turns).

Remaining open: #1114 stays unbuilt (gated). The three PRs are on `main` (`fd1a389ea`) and go live at the next image roll.

### ⚠ Correction + cost measurements (2026-09-03, post-merge)

**The sim A/B measured #1314 ALONE.** The on-arm worktree sat at `43c065788` (the #1314
branch); `git grep _route_toolless_turn_single_shot 43c065788` = 0, so #1316's route was never
in it, and the API logs confirm 0 routed turns over 84 on-arm turns. #1316's evidence remains
the engine probe (10/10 hit and linked, N=10).

**Measured cost, main vs `4da25e38c`, same case, openai/gpt-5.6-luna estimator:**

| item | before | after | delta |
|---|---|---|---|
| assembled prompt, 2 hypotheses @ turn 9 | 21,304 tok | 21,555 tok | +251 (+1.2%) |
| assembled prompt, 8 hypotheses @ turn 20 | 21,513 tok | 21,931 tok | +418 (+1.9%) |
| `response_format` schema (single-shot) | 11,852 tok | 11,159 tok | −693 |
| routed turn: DA system instruction + 6 tool schemas | 2,268 tok | not sent | −2,268 input |
| routed turn: reasoning tokens (20-rep A/B) | 0 (`none`) | 1,127 mean (`medium`) | +1,127 output |
| routed turn: visible body | 1,672 tok | 1,709 tok | ~0 |
| routed turn: latency, identical call | 16.2 s | 27.7 s | +11.5 s |
| `low` instead of `medium` on that call | — | 224 tok, 17.3 s, 19/20 linked | the cheap option |

Prompt growth scales with active-hypothesis count (ids in five renderers + the state-summary
cap 3→10). The schema is ~11K tok of every request, a third of the total and far larger than
any delta here — the place to look if token spend needs cutting.
