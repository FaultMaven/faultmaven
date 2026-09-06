# Architect review — is the root-cause dual-authoring seam a design gap or an implementation deviation?

**Role.** You are the solutions architect for FaultMaven's investigation engine — the design authority for the milestone-based investigation framework, the 2D hypothesis methodology, and the root-cause conclusion model. You are being asked to conduct a **deep architectural review**, grounded in the original design intent. This is a review-and-design task: **do not change code.**

## Context — the campaign that just closed
Issue **#656** ("Investigation derails after premature 'verified' cause") drove a 15-PR systemic correction plan (invariants **INV-26 through INV-39**) that fixed a real production derail: the engine minted a false "verified 0.9" root cause from a symptom-restating disjunction, latched into an irreversible frame, discarded the user's mid-diagnosis analyses, and bricked into a pending-transition dead-loop. The plan's acceptance scenario (replay the incident: turn 6 no verified-0.9, turns 10-11 analyses visible, turns 12-13 reach the LLM) now passes, and **#656 is closed as completed**.

The consistent design move throughout the campaign was to **relocate trust from LLM self-claims to engine-derived truth** — INV-23 (strip self-labeled causal links at ingest), INV-29 (≥2 independent causal supports), INV-30 (counterfactual bearing), INV-34 (RCC link/retract), INV-35 (prompt/engine realignment: the LLM decides the cause, the engine certifies grounding).

## The question you are being asked
Two open follow-ups — **#668** and **#673** — are argued to *challenge the original design*, not merely to be loose ends. For each, and for any additional manifestations you find, decide:

> **Is this a real design gap, or an implementation deviation from the intended design?**

### The seam both issues touch
The root-cause conclusion (and the user-facing resolution claim) is **dual-authored**:
- the **LLM** writes a free-text `RootCauseConclusion` (`root_cause` / `mechanism`) *and* narrates user-facing prose, in a namespace **disconnected from the causal graph**;
- the **engine** independently mints a chain-derived mirror and runs a **reconciliation layer** (INV-34 / methodology §7.6: `link_llm_rcc_to_cause`, `retract_disconfirmed_rcc`, MECE read-suppress) to keep the two authorities coherent.

- **#673** proposes **retiring dual-authoring** — derive the conclusion from the validated chain so the reconciliation layer becomes unnecessary. It frames that layer as the standing cost of the LLM/graph disconnect (gated today on "reliable chain-grounding").
- **#668** is that seam leaking in production: the LLM narrated **"Case resolved."** with a full resolution summary while every engine truth surface correctly said the case was still INVESTIGATING (no `proposed_transition`, no `causal_absence` row, `cause_state` not IDENTIFIED). The engine was right; the narration lied — the INV-15 class ("agent claims an action it did not take"), on the highest-stakes claim.

## What to assess
1. **Design intent.** Was dual-authoring an intentional division of labor you designed (LLM proposes/expresses, engine certifies/guards), or an accretion the implementation grew that was never the intended single-authority model?
2. **Trajectory.** Given the campaign's own direction (progressively moving authority LLM→engine), does the design *imply* the RCC should complete that move — i.e. is **#673 the intended endpoint**, making the current dual-authoring a deviation? Or is LLM free-text authorship a **load-bearing feature** (expressiveness, mechanism narration) that should stay, with #668 handled as a bounded narration-constraint problem?
3. **Reachability of the fix.** Is #668 a symptom the reconciliation layer **structurally cannot** fix (because prose is authored outside every truth surface), or is it a fixable prompt-/render-time constraint *within* the current design?
4. **Additional manifestations (hunt, don't wait to be told).** Find other places where LLM prose or LLM self-claims can diverge from engine-derived truth on user-facing or decision-driving surfaces — e.g. the working-conclusion proxy, resolution/closure **summary generation**, `symptom_verified` (LLM-set), the terminal **report** prose. For each: real divergence risk, or already guarded?
5. **Scope judgment on the closure.** Was closing #656 correct given these, or does **#668 in particular** belong inside #656's guarantee scope — a *user-visible* false "resolved" claim vs. the engine-state guarantees?

## Where to look
- **Design docs:** `docs/architecture/investigation-engine/` — `two-dimensional-hypothesis-methodology.md` (§7.6/§7.7), `investigation-invariants.md` (the full INV matrix, esp. INV-23/29/30/34/35), `insufficient-evidence-handling.md`, `investigation-flow-redesign.md`.
- **Code:** `faultmaven/core/investigation/` — the RCC model (`RootCauseConclusion`, `case.root_cause_conclusion`), `cause_assurance.py`, `causal_graph.py` (link/retract/mirror), `milestone_engine._recompute_assessment_state`, the prose/render paths (`_prose_with_gate_notice`, resolution/closure summary generation), `terminal_transitions.py`; the working-conclusion generator.
- **Issues:** #656 (full body + the systemic-review comment carrying DF-1..DF-8 + the acceptance/close comment), **#668**, **#673**, and the adjacent **#675** (stale readers of the decommissioned `root_cause_identified` signal).
- **Ground truth:** the two soundness guarantees — **NO INCORRECT CONCLUSION**, **NO COLLAPSE UNDER PRESSURE** — the documented design principles, and the **LLM-agnostic testing invariant** (mechanical engine-state assertions decide correctness, not a model-tuned judge).

## Deliverable
A **design-review document** (present-tense current-state framing; one "rejected alternative" line where relevant) giving:
- **(a) a per-gap verdict** — *real design gap* / *implementation deviation* / *acceptable-by-design trade-off* — with the reasoning, for #668, #673, and any additional seam manifestations found;
- **(b) a systemic assessment** of the dual-authoring seam: is the intended design single-authority, and if so where has the implementation diverged;
- **(c) a recommendation** — keep #656 closed / reopen it / open a **new design initiative** (e.g. single-authority RCC derived from the chain), with sequencing and the explicit guardrail against re-breaching either guarantee;
- **(d)** if a redesign is warranted, specify it **at the design level** so it can be planned as its own campaign — do not implement.

## Constraints
Review + design only; change no code. Flag uncertainty rather than asserting. **A "the design is sound, the gaps are bounded and out-of-scope, no redesign needed" verdict is a valid and useful outcome** — do not manufacture a redesign to justify the review.
