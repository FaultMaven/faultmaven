# Design — read-time assurance-grade labeling on user-facing cause surfaces (§3.5)

Completion of the §3.5 item from `REVIEW-rcc-dual-authoring-design-review.md`: the
frontend audit found the assurance grade is **not** juxtaposed with the root-cause
conclusion (RCC) on any user-facing surface — and, upstream of the frontends, the
API does not even carry the grade on the surface where a finished RCC is read. This
design closes that gap so the #572 / INV-28 read-time-labeling decision holds
everywhere a cause is shown, not only inside the generated report prose.

## Problem

The engine authors a free-text RCC (`root_cause_conclusion.root_cause`) that may
carry its own self-claimed `confidence_level` (up to `VERIFIED`). The engine
separately grades the *actual* assurance behind that cause —
`CauseAssuranceGrade` = `no_root | mechanistic | confirmed` (the M2 ladder,
`grade_cause_assurance`). #572 chose read-time labeling over apply-site clamping:
wherever the RCC text is shown, the grade must be shown beside it, so a
confidently-worded conclusion is never presented at a certainty the graph does not
support. The overclaim seam is `conclusion_overclaims(rcc, grade)` — RCC claims
`VERIFIED` while grade `< CONFIRMED`.

Today the grade reaches the user on exactly one surface: the resolution/closure
**report** body, as an `_assurance_note` italic qualifier. Every structured and
narration surface drops it:

- `CaseUIResponse_Resolved.root_cause` (`RootCauseSummary`) carries no grade field.
- `TurnResponse` carries the grade only nested in `progress_transparency`, which is
  gated to stalled investigations (≥5 investigative turns, no milestone) and is
  absent on a resolution turn.
- Copilot renders `root_cause.description` bare (case header + resolution banner).
- Slack forwards the turn's `agent_response` narration plus a `State:` footer, and
  never fetches structured cause data at all.

## Change

Expose the grade on the two payloads a user-facing cause surface actually reads,
recomputed from the causal graph (never the persisted per-turn field).

1. **`RootCauseSummary`** (RESOLVED UI payload) gains:
   - `cause_assurance: str` — the grade value (`no_root | mechanistic | confirmed`).
   - `cause_overclaim: bool` — `conclusion_overclaims(rcc, grade)`.
   Populated in `case_ui_adapter._transform_resolved`.

2. **`TurnResponse`** (per-turn API) gains the same two optional fields,
   populated whenever the turn's case has an RCC (`root_cause_conclusion` set),
   so a resolution turn reliably carries the grade for narration-only clients
   (Slack). Built in `investigation_service` at the `TurnResponse` construction.

Both read the grade via `grade_cause_assurance(case)` and the overclaim via
`conclusion_overclaims(case.root_cause_conclusion, grade)` — the same recompute the
report's `_assurance_note` uses, and for the same reason: terminal cases never
recompute, so the persisted `progress.cause_assurance` can read a stale `no_root`
default and would falsely discredit a validated conclusion.

**Rejected alternative:** adding the grade to `WorkingConclusionSummary` (the
INVESTIGATING leading-hypothesis surface) — it is a single hypothesis carrying its
own likelihood axis, so a cause-assurance grade beside it conflates two different
signals; the grade already flows to that phase via `progress_transparency` and the
new `TurnResponse` field.

## Per-surface rendering

- **Copilot** — render the grade beside the RCC text on the terminal "Root Cause"
  row (`CaseDetails`) and the `ResolutionActionsCard` banner: a short grade label
  (`mechanistic`/`confirmed`/`no_root`) plus a warning affordance when
  `cause_overclaim` is set. `confirmed` needs no qualifier.
- **Slack** — when the turn response carries `cause_assurance`, append a plain-text
  grade line to the context footer beside the existing `State:` line. This is the
  first read-time label on the narration-only surface (a #668 surface: the cause
  claim lives in free-text `agent_response`; the footer grade is the honest
  qualifier the surface otherwise lacks).
- **Dashboard** — no display change: its structured tabs render a milestone boolean,
  not the RCC text, and its only cause-text surface (ReportTab) already renders the
  report's `_assurance_note`. Types regenerate for contract parity only.

## Testing

Mechanical, LLM-agnostic (per the testing invariant): construct cases with a known
graph shape (no validated root → `no_root`; validated root, no counterfactual →
`mechanistic`; counterfactually confirmed → `confirmed`; and a `VERIFIED`-claiming
RCC over an unconfirmed root → `cause_overclaim=True`), run the adapter /
turn-response build, and assert the emitted field values. No model-graded judge.
