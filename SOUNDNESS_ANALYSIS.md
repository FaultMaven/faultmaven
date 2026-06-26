# Soundness Review: Runbook Matcher Hypothesis Attachment (4b-1)

## Critical Scenario Analysis

### The Risk: Working Conclusion Drives M5 on Matcher Likelihood Alone

**Key finding**: The matcher creates an ACTIVE hypothesis with `likelihood=belief` (runbook match score, 0.5–1.0). This hypothesis is STANDING (per `_standing_hypotheses`, which includes ACTIVE). The root node of that chain starts as CANDIDATE (no evidence). Then:

1. `generate_working_conclusion()` picks the hypothesis with max likelihood (line 95 of working_conclusion_generator.py)
2. It returns `WorkingConclusion(statement=..., likelihood=best_hypothesis.likelihood, ...)`
3. `_cause_identified()` in terminal_transitions.py checks: `working_conclusion.likelihood >= 0.6` (line 70)
4. `_solution_cause_validated()` (the M5 gate) delegates to `_cause_identified()`
5. If the matcher belief >= 0.6, M5 PASSES and a solution can be registered

**Problem**: The root is CANDIDATE (no rung evidence yet), but M5 gates on working_conclusion likelihood, which is a RUNBOOK-MATCH SCORE, not an evidence-derived probability. This allows a solution registration (and potentially resolution) on a runbook prior alone.

### Evidence Chain Validation

`cause_state=IDENTIFIED` CORRECTLY requires:
- `any_chain_root_validated()` checks a STANDING hypothesis's root node
- Root must be VALIDATED (line 144 of causal_graph.py)
- VALIDATED requires: rung evidence, net-supporting (supports > refutes), M7 AND-gate satisfied (line 315 of causal_graph.py)
- A CANDIDATE root CANNOT VALIDATE without rung evidence (line 269 of causal_graph.py: "no bearing evidence yet")

So cause_state CORRECTLY REJECTS a matcher hypothesis until evidence grounds it.

### The Gate Hierarchy Problem

`_cause_identified()` has a fallback chain (lines 53–71 of terminal_transitions.py):
1. cause_state == IDENTIFIED? (requires rung evidence)
2. root_cause_conclusion set? (LLM-authored, or engine-synthesized from validated root)
3. working_conclusion.likelihood >= 0.6? (FALLBACK)

The fallback is INTENTIONAL (per the docstring and the M5 comment): cause_state is a SOFT signal; the LLM may validate a root without updating cause_state (under-reporting), so the fallback lets a case resolve. But the fallback uses ANY active hypothesis with likelihood >= 0.6, including a MATCHER hypothesis with no evidence.

### Scenario: Wrong Conclusion

**Case**: Host reboot imminent, suspected cause is "old kernel".
- LLM emits hypothesis A: "kernel too old" (ACTIVE, likelihood 0.4, no evidence yet)
- Matcher finds runbook: "reboot imminent → old kernel" (belief 0.75)
- Matcher creates hypothesis B: "old kernel" (ACTIVE, likelihood 0.75, root=CANDIDATE, no evidence)
- cause_state recomputes: count_active_hypotheses >= 2, so cause_state = CANDIDATES
- generate_working_conclusion() picks hypothesis B (likelihood 0.75 > 0.4)
- M5 gate calls _cause_identified() → working_conclusion.likelihood >= 0.6 → TRUE
- Solution REGISTERED on runbook match score
- Kernel is NOT actually old (the reboot was an update), but FM concludes it is

The root is CANDIDATE (no evidence yet), but the case can RESOLVE on the working_conclusion fallback.

---

## Axis Analysis

### 1. cause_state inflation: SAFE

**Finding**: Adding a matcher hypothesis DOES flip cause_state UNKNOWN→CANDIDATES (if LLM emitted ≥1 ACTIVE already). But cause_state is a SOFT signal with a fallback (terminal_transitions._cause_identified). The cause_state inflation is NOT a wrong conclusion vector — it's correctly NOT used to gate M5.

**Gate**: M5 does NOT read cause_state directly; it reads _cause_identified(), which uses the working_conclusion fallback. See milestone_engine.py line 220.

### 2. working_conclusion / likelihood: RISK

**Finding**: generate_working_conclusion() ranks hypotheses by likelihood and returns the top one. A matcher hypothesis with belief 0.8 (runbook match score, not evidence) can become the working_conclusion with likelihood 0.8. If no other hypothesis has higher likelihood, the working_conclusion IS the matcher's belief score.

**M5 Gate**: _solution_cause_validated (line 183 of milestone_engine.py) → _cause_identified (line 218 of milestone_engine.py) → terminal_transitions._cause_identified (line 53–71) → working_conclusion.likelihood >= 0.6 (line 70).

**Result**: A matcher hypothesis with belief 0.75 drives working_conclusion.likelihood = 0.75 → M5 PASSES → solution registered on runbook match score alone, with no evidence.

**Severity**: HIGH — this can register solutions (and enable resolution) on runbook priors alone.

**Example scenario**: Matcher returns belief=0.8 (high confidence), but the actual cause is different. Case resolves before real evidence grounded the root. FM has made a WRONG CONCLUSION.

### 3. VALIDATED without evidence: SAFE

**Finding**: Confirmed via derive_node_states (line 310–315 of causal_graph.py): a VALIDATED node requires `causal_supports >= 1`. A matcher node (no evidence links) cannot VALIDATE. So the root of a matcher chain can never VALIDATE without real rung evidence.

**Gate**: cause_state=IDENTIFIED only via any_chain_root_validated (line 768 of milestone_engine.py), which checks is_chain_root_validated (line 785), which checks node_state == VALIDATED (line 144 of causal_graph.py).

**Result**: The chain-based cause_state CORRECTLY gates cause_state=IDENTIFIED. But working_conclusion can bypass this via the fallback.

### 4. Anchoring / hypothesis count: SAFE

**Finding**: The matcher dedups by root_node_id (attach_matched_hypothesis, line 146 of runbook_cause_matcher.py). A re-match resolves to the same root, and if a hypothesis already roots there, no second hypothesis is spawned (idempotent). Hypothesis count is not polluted by repeated matcher runs.

**Anchoring** uses category count (finding-3 / NO-CATEGORY-ANCHORING; not implemented here). Matcher uses category=OTHER, which would not trigger anchoring if implemented. Safe.

### 5. likelihood=belief provenance: RISK

**Finding**: The matcher's belief is a k-of-n match score (indicator evaluation, line 97 of indicator_evaluator.py: `belief >= SURFACE_THRESHOLD`). This is a SEMANTIC CONFIDENCE, not a BAYESIAN POSTERIOR. Using it as a hypothesis likelihood (and feeding it into working_conclusion) conflates TWO PROBABILITY MODELS:

- **Semantic confidence**: "The runbook indicators match the case" (precision of runbook retrieval).
- **Causal likelihood**: "This is the actual root cause" (posterior probability given evidence).

The comment at line 156 of runbook_cause_matcher.py says "belief seeds the prior likelihood (clamped); evidence adjusts it later" — this frames it as a PRIOR. But:
1. A hypothesis likelihood is used directly in working_conclusion (line 95 of working_conclusion_generator.py: `max(..., key=lambda h: h.likelihood)`).
2. The fallback M5 gate reads working_conclusion.likelihood >= 0.6 (line 70 of terminal_transitions.py).
3. If no evidence adjusts the hypothesis (the root stays CANDIDATE), the prior IS the working_conclusion likelihood.

Result: A high-belief runbook match drives a high-likelihood working_conclusion, which gates M5 and solution registration, without evidence.

**Severity**: MEDIUM — the model is consistent (prior + evidence = posterior), but the PRIOR is a runbook match score, not a domain expert prior. A runbook match ≠ ground truth.

### 6. Backward compatibility / M4/M5 soundness: SAFE

**Finding**: The hypothesis attachment is idempotent and guarded. M5 gates are correctly applied via _solution_cause_validated. No solution bypasses M5 (per line 215 of milestone_engine.py comment: "never bypassing [M5]"). M5 gates on _cause_identified, which allows three signals: cause_state, RCC, working_conclusion.

**However**: The working_conclusion fallback allows a solution to register when working_conclusion.likelihood >= 0.6, even if cause_state != IDENTIFIED and no RCC is set. This is INTENTIONAL (per the M5 comment: the LLM may not update cause_state), but it means the fallback uses ANY hypothesis with likelihood >= 0.6, including a matcher hypothesis with no evidence.

---

## Findings

### FINDING 1: Working Conclusion Likelihood Without Evidence
- **File**: faultmaven/core/investigation/working_conclusion_generator.py
- **Line**: 95, 114
- **Summary**: generate_working_conclusion picks the highest-likelihood hypothesis and returns its likelihood in the working_conclusion, even if that hypothesis is a matcher hypothesis with no rung evidence (root node is CANDIDATE).
- **Failure scenario**: Matcher returns belief 0.8, no LLM hypothesis exists. working_conclusion.likelihood = 0.8 → M5 passes → solution registers on runbook match score alone. Root never validates from evidence. FM concludes a cause supported by runbook matching, not case observation.

### FINDING 2: M5 Gate Fallback on working_conclusion
- **File**: faultmaven/core/investigation/terminal_transitions.py
- **Line**: 67–71
- **Summary**: _cause_identified returns True if working_conclusion.likelihood >= 0.6, which is a fallback to handle LLM under-reporting of cause_state. But this fallback does not distinguish evidence-backed hypotheses from prior-only hypotheses. A matcher hypothesis with high belief passes M5 without evidence.
- **Failure scenario**: Matcher belief 0.75 drives working_conclusion.likelihood = 0.75 → _cause_identified returns True → _solution_cause_validated returns True → M5 gate passes → solution registered and case can resolve on a matcher prior.

### FINDING 3: Hypothesis Likelihood Ranks Without Evidence Adjustment
- **File**: faultmaven/core/investigation/working_conclusion_generator.py
- **Line**: 95
- **Summary**: The max() ranking of hypotheses by likelihood uses the initial likelihood (prior) even if the hypothesis has no evidence links yet. A matcher hypothesis with belief 0.9 and zero evidence will rank higher than an LLM hypothesis with likelihood 0.6 and supporting evidence.
- **Failure scenario**: Matcher belief 0.9 (no evidence) vs. LLM hypothesis likelihood 0.6 (with evidence). Matcher wins, drives working_conclusion, M5 gates on the matcher's prior, not the LLM's evidence.

### FINDING 4: No Evidence Requirement in working_conclusion Fallback
- **File**: faultmaven/core/investigation/terminal_transitions.py
- **Line**: 67–71
- **Summary**: The working_conclusion fallback does not check if the hypothesis has rung evidence (no evidence_links filter). It only checks likelihood >= 0.6.
- **Failure scenario**: Matcher hypothesis (root=CANDIDATE, evidence_links=[], likelihood=0.75) passes _cause_identified via working_conclusion, M5 gate passes, solution registered without rung evidence.

### FINDING 5: Belief-to-Likelihood Semantic Mismatch
- **File**: faultmaven/core/investigation/runbook_cause_matcher.py
- **Line**: 156–162
- **Summary**: The matcher's belief (runbook-match semantic confidence, k-of-n indicator score) is used directly as hypothesis likelihood (causal posterior probability). These are different probability models. The belief does not account for domain base rates, case-specific evidence, or the runbook's accuracy in this domain.
- **Failure scenario**: Runbook has high precision (belief 0.85) but low recall in the domain. Matcher hypothesis likelihood 0.85 does not reflect the actual posterior P(cause | case evidence). M5 gates on a miscalibrated prior.

### FINDING 6: M5 Gate Inconsistency with cause_state Strictness
- **File**: faultmaven/core/investigation/milestone_engine.py
- **Line**: 183–220
- **Summary**: cause_state=IDENTIFIED requires rung evidence (via any_chain_root_validated), but _cause_identified falls back to working_conclusion.likelihood >= 0.6 for backward compatibility with LLM under-reporting. The fallback is less strict than cause_state, allowing M5 to pass when cause_state != IDENTIFIED. This is intentional, but it creates a second path to M5 that bypasses the evidence requirement.
- **Failure scenario**: cause_state=UNKNOWN (no evidence yet), but working_conclusion.likelihood=0.75 (matcher prior) → _cause_identified=True → M5 passes. Case can register solution and resolve without cause_state=IDENTIFIED.

---

## Risk Ranking

1. **FINDING 2** (M5 fallback on working_conclusion without evidence): CRITICAL
   - Direct gate to solution registration
   - Affects terminal soundness (wrong conclusion)

2. **FINDING 1** (working_conclusion picks unevidenced hypothesis): HIGH
   - Feeds directly into FINDING 2
   - Ranks matcher priors over LLM evidence

3. **FINDING 4** (no evidence_links check in fallback): HIGH
   - Completes the path: matcher belief → working_conclusion → M5 → solution
   - No structural gate to prevent it

4. **FINDING 3** (ranking by likelihood without evidence adjustment): MEDIUM
   - Affects which hypothesis drives working_conclusion
   - Indirect path via FINDING 1

5. **FINDING 5** (belief-to-likelihood semantic mismatch): MEDIUM
   - Mis-calibrates the prior probability
   - Prior is still a prior (evidence can adjust), but the prior is misaligned

6. **FINDING 6** (M5 inconsistency with cause_state): LOW
   - The fallback is intentional and documented
   - But it creates a second path that bypasses evidence requirement
