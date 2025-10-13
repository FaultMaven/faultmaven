# Evidence Integration Plan

**Date:** 2025-10-12
**Status:** In Progress
**Goal:** Integrate evidence consumption into all 7 phase handlers

---

## Overview

Evidence requests are currently **generated** by phase handlers but **not consumed**. This integration adds evidence consumption logic so that:

1. Phase handlers check for new evidence since last turn
2. Evidence is incorporated into phase-specific analysis
3. Phase progression depends on evidence completeness
4. Investigation state is updated with evidence findings

---

## Integration Strategy

### Phase-by-Phase Approach

Each phase handler needs:
1. **Evidence retrieval logic** - Get new evidence since last turn
2. **Evidence consumption logic** - Incorporate findings into analysis
3. **Completeness checking** - Determine if enough evidence collected
4. **State updates** - Update investigation state with evidence insights

### Common Utilities (Create First)

Before modifying handlers, create shared utilities:

**File:** `faultmaven/services/evidence/consumption.py` (NEW)

```python
"""Evidence Consumption Utilities

Shared logic for phase handlers to consume evidence provided by users.
"""

from typing import List, Optional
from faultmaven.models.evidence import EvidenceProvided, EvidenceRequest, EvidenceStatus
from faultmaven.models.investigation import InvestigationState

def get_new_evidence_since_turn(
    investigation_state: InvestigationState,
    since_turn: int
) -> List[EvidenceProvided]:
    """
    Get evidence provided since specified turn.

    Args:
        investigation_state: Current investigation state
        since_turn: Turn number to get evidence after

    Returns:
        List of EvidenceProvided records with turn_number > since_turn
    """
    return [
        evidence for evidence in investigation_state.evidence.evidence_provided
        if evidence.turn_number > since_turn
    ]

def get_evidence_for_requests(
    investigation_state: InvestigationState,
    request_ids: List[str]
) -> List[EvidenceProvided]:
    """
    Get all evidence that addresses specified request IDs.

    Args:
        investigation_state: Current investigation state
        request_ids: List of request IDs to match

    Returns:
        List of EvidenceProvided that addressed any of the request IDs
    """
    evidence_list = []
    for evidence in investigation_state.evidence.evidence_provided:
        # Check if any addresses_requests match our request_ids
        if any(req_id in request_ids for req_id in evidence.addresses_requests):
            evidence_list.append(evidence)
    return evidence_list

def check_requests_complete(
    investigation_state: InvestigationState,
    request_ids: List[str],
    completeness_threshold: float = 0.8
) -> bool:
    """
    Check if specified evidence requests are complete.

    Args:
        investigation_state: Current investigation state
        request_ids: Request IDs to check
        completeness_threshold: Minimum completeness score (default 0.8)

    Returns:
        True if all requests are COMPLETE or have completeness ≥ threshold
    """
    for req_id in request_ids:
        request = next(
            (req for req in investigation_state.evidence.evidence_requests if req.request_id == req_id),
            None
        )
        if not request:
            continue

        if request.status == EvidenceStatus.COMPLETE:
            continue

        if request.completeness >= completeness_threshold:
            continue

        # Request not complete
        return False

    return True

def summarize_evidence_findings(
    evidence_list: List[EvidenceProvided]
) -> str:
    """
    Generate summary of evidence findings for LLM context.

    Args:
        evidence_list: Evidence records to summarize

    Returns:
        Formatted string summarizing evidence findings
    """
    if not evidence_list:
        return "No new evidence provided."

    summary_lines = []
    for evidence in evidence_list:
        form_str = "User input" if evidence.form.value == "user_input" else "Document upload"
        summary_lines.append(
            f"- {form_str}: {evidence.content[:200]}... "
            f"(Evidence type: {evidence.evidence_type.value})"
        )

    return "\\n".join(summary_lines)
```

---

## Phase-Specific Integration

### Phase 0: Intake Handler

**Current:** Detects problems, offers investigation
**Integration:** Minimal (no evidence requests in Phase 0)

**Changes:** None needed (Phase 0 is pre-evidence collection)

---

### Phase 1: Blast Radius Handler

**Current:** Generates scope/impact evidence requests
**Integration:** Consume scope/impact evidence to refine AnomalyFrame

**Changes to `blast_radius_handler.py`:**

```python
# Add import
from faultmaven.services.evidence.consumption import (
    get_new_evidence_since_turn,
    get_evidence_for_requests,
    summarize_evidence_findings
)

# In process() method, after generating evidence requests:
async def process(self, investigation_state: InvestigationState, user_message: str) -> AgentResponse:
    # ... existing code ...

    # NEW: Check for new evidence since last OODA iteration
    last_turn = investigation_state.ooda_engine.iterations[-1].turn_number if investigation_state.ooda_engine.iterations else 0
    new_evidence = get_new_evidence_since_turn(investigation_state, last_turn)

    if new_evidence:
        # Incorporate evidence into blast radius analysis
        evidence_summary = summarize_evidence_findings(new_evidence)

        # Add to LLM prompt context
        additional_context = f"\\n\\n## New Evidence Provided:\\n{evidence_summary}\\n\\nIncorporate this evidence into your scope/impact assessment."

        # Update anomaly frame with evidence findings
        for evidence in new_evidence:
            if evidence.evidence_type.value == "supportive":
                # Evidence confirms suspected scope
                investigation_state.anomaly_frame.confidence_score *= 1.1
            elif evidence.evidence_type.value == "refuting":
                # Evidence contradicts suspected scope - adjust
                investigation_state.anomaly_frame.confidence_score *= 0.9

    # ... rest of existing code ...
```

**Estimated Effort:** 1 hour

---

### Phase 2: Timeline Handler

**Current:** Generates temporal evidence requests
**Integration:** Consume timeline evidence to populate TimelineEvent objects

**Changes to `timeline_handler.py`:**

```python
# Add import
from faultmaven.services.evidence.consumption import (
    get_new_evidence_since_turn,
    summarize_evidence_findings
)
from faultmaven.models.investigation import TimelineEvent

# In process() method:
async def process(self, investigation_state: InvestigationState, user_message: str) -> AgentResponse:
    # ... existing code ...

    # NEW: Check for timeline evidence
    last_turn = investigation_state.ooda_engine.iterations[-1].turn_number if investigation_state.ooda_engine.iterations else 0
    new_evidence = get_new_evidence_since_turn(investigation_state, last_turn)

    if new_evidence:
        evidence_summary = summarize_evidence_findings(new_evidence)

        # Extract timeline events from evidence
        for evidence in new_evidence:
            # Parse temporal information from evidence content
            # (can use LLM to extract timestamps/events)
            if "started at" in evidence.content.lower() or "began" in evidence.content.lower():
                # Create timeline event
                event = TimelineEvent(
                    timestamp=None,  # LLM can extract
                    event_type="user_reported",
                    description=evidence.content[:200],
                    source="evidence_provided"
                )
                # Add to timeline (if timeline attribute exists)
                # investigation_state.timeline.add_event(event)

        # Add evidence to LLM context
        additional_context = f"\\n\\n## New Timeline Evidence:\\n{evidence_summary}\\n\\nUpdate timeline correlation based on this evidence."

    # ... rest of existing code ...
```

**Estimated Effort:** 1.5 hours

---

### Phase 3: Hypothesis Handler

**Current:** Generates hypotheses, creates validation evidence requests
**Integration:** Consume evidence to refine/update hypotheses

**Changes to `hypothesis_handler.py`:**

```python
# Add import
from faultmaven.services.evidence.consumption import (
    get_new_evidence_since_turn,
    get_evidence_for_requests,
    summarize_evidence_findings
)

# In process() method:
async def process(self, investigation_state: InvestigationState, user_message: str) -> AgentResponse:
    # ... existing code ...

    # NEW: Check evidence that refutes/supports existing hypotheses
    last_turn = investigation_state.ooda_engine.iterations[-1].turn_number if investigation_state.ooda_engine.iterations else 0
    new_evidence = get_new_evidence_since_turn(investigation_state, last_turn)

    if new_evidence:
        evidence_summary = summarize_evidence_findings(new_evidence)

        # Check if evidence refutes any hypotheses
        for evidence in new_evidence:
            if evidence.evidence_type.value == "refuting":
                # Find hypothesis this evidence refutes
                for hypothesis in investigation_state.hypothesis_manager.active_hypotheses:
                    if hypothesis.request_id in evidence.addresses_requests:
                        # Mark hypothesis confidence lower
                        hypothesis.confidence *= 0.7

        # Add to LLM context
        additional_context = f"\\n\\n## Evidence Update:\\n{evidence_summary}\\n\\nAdjust hypothesis confidence based on this evidence."

    # ... rest of existing code ...
```

**Estimated Effort:** 1.5 hours

---

### Phase 4: Validation Handler

**Current:** Tests hypotheses with validation evidence
**Integration:** Consume validation evidence to mark hypotheses validated/refuted

**Changes to `validation_handler.py`:**

```python
# Add import
from faultmaven.services.evidence.consumption import (
    get_new_evidence_since_turn,
    get_evidence_for_requests,
    check_requests_complete
)

# In process() method:
async def process(self, investigation_state: InvestigationState, user_message: str) -> AgentResponse:
    # ... existing code ...

    # NEW: Check validation evidence completeness
    last_turn = investigation_state.ooda_engine.iterations[-1].turn_number if investigation_state.ooda_engine.iterations else 0
    new_evidence = get_new_evidence_since_turn(investigation_state, last_turn)

    if new_evidence:
        # Check which hypotheses have sufficient validation evidence
        for hypothesis in investigation_state.hypothesis_manager.active_hypotheses:
            # Get evidence for this hypothesis
            hypothesis_evidence = get_evidence_for_requests(
                investigation_state,
                [hypothesis.validation_request_id]  # Assuming hypothesis has validation_request_id
            )

            if hypothesis_evidence:
                # Count supportive vs refuting evidence
                supportive_count = sum(1 for e in hypothesis_evidence if e.evidence_type.value == "supportive")
                refuting_count = sum(1 for e in hypothesis_evidence if e.evidence_type.value == "refuting")

                if supportive_count > refuting_count:
                    hypothesis.status = "validated"
                    hypothesis.confidence = 0.9
                elif refuting_count > supportive_count:
                    hypothesis.status = "refuted"
                    hypothesis.confidence = 0.1

    # Check if ready to progress to Solution phase
    all_validated = all(h.status in ["validated", "refuted"] for h in investigation_state.hypothesis_manager.active_hypotheses)
    if all_validated:
        # Progress to Phase 5
        pass

    # ... rest of existing code ...
```

**Estimated Effort:** 2 hours (most complex integration)

---

### Phase 5: Solution Handler

**Current:** Proposes solutions based on validated hypotheses
**Integration:** Consume solution verification evidence

**Changes to `solution_handler.py`:**

```python
# Add import
from faultmaven.services.evidence.consumption import (
    get_new_evidence_since_turn,
    summarize_evidence_findings
)

# In process() method:
async def process(self, investigation_state: InvestigationState, user_message: str) -> AgentResponse:
    # ... existing code ...

    # NEW: Check for solution verification evidence
    last_turn = investigation_state.ooda_engine.iterations[-1].turn_number if investigation_state.ooda_engine.iterations else 0
    new_evidence = get_new_evidence_since_turn(investigation_state, last_turn)

    if new_evidence:
        evidence_summary = summarize_evidence_findings(new_evidence)

        # Check if user reports solution worked
        for evidence in new_evidence:
            if "works" in evidence.content.lower() or "fixed" in evidence.content.lower():
                # Solution confirmed successful
                investigation_state.lifecycle.case_status = "resolved"
            elif "didn't work" in evidence.content.lower() or "failed" in evidence.content.lower():
                # Solution didn't work - back to hypothesis
                investigation_state.lifecycle.case_status = "active"

        # Add to LLM context
        additional_context = f"\\n\\n## Solution Feedback:\\n{evidence_summary}\\n\\nAdjust solution based on verification results."

    # ... rest of existing code ...
```

**Estimated Effort:** 1 hour

---

### Phase 6: Document Handler

**Current:** Generates case reports and runbooks
**Integration:** Include evidence summary in documents

**Changes to `document_handler.py`:**

```python
# Add import
from faultmaven.services.evidence.consumption import summarize_evidence_findings

# In _generate_case_report() method:
def _generate_case_report(self, investigation_state: InvestigationState) -> str:
    # ... existing code ...

    # NEW: Add evidence summary section
    evidence_section = "## Evidence Collected\\n\\n"
    if investigation_state.evidence.evidence_provided:
        evidence_section += summarize_evidence_findings(investigation_state.evidence.evidence_provided)
    else:
        evidence_section += "No formal evidence collected."

    # Insert into report
    report += f"\\n{evidence_section}\\n"

    # ... rest of existing code ...
```

**Estimated Effort:** 30 minutes

---

## Implementation Order

### Step 1: Create Consumption Utilities (30 min)
- Create `faultmaven/services/evidence/consumption.py`
- Write utility functions
- Write unit tests for utilities (10 tests)

### Step 2: Integrate Phase 4 Validation (2 hours)
- Most critical for evidence-driven workflow
- Update `validation_handler.py`
- Test validation evidence consumption

### Step 3: Integrate Phase 3 Hypothesis (1.5 hours)
- Hypothesis refinement based on evidence
- Update `hypothesis_handler.py`
- Test hypothesis adjustments

### Step 4: Integrate Phase 1 Blast Radius (1 hour)
- Scope refinement based on evidence
- Update `blast_radius_handler.py`
- Test scope adjustments

### Step 5: Integrate Phase 2 Timeline (1.5 hours)
- Timeline population from evidence
- Update `timeline_handler.py`
- Test timeline extraction

### Step 6: Integrate Phase 5 Solution (1 hour)
- Solution verification from evidence
- Update `solution_handler.py`
- Test solution feedback

### Step 7: Integrate Phase 6 Document (30 min)
- Evidence summary in reports
- Update `document_handler.py`
- Test report generation

---

## Testing Strategy

### Unit Tests (15 new tests)
- `test_consumption_utils.py` (10 tests)
  - Test get_new_evidence_since_turn()
  - Test get_evidence_for_requests()
  - Test check_requests_complete()
  - Test summarize_evidence_findings()

- Update existing handler tests (5 tests)
  - Add evidence consumption scenarios to each handler test
  - Verify state updates when evidence provided
  - Test phase progression with complete evidence

### Integration Tests (5 new tests)
- `test_evidence_workflow_integration.py`
  - Test complete evidence workflow (request → provide → consume)
  - Test phase progression with evidence
  - Test evidence-driven hypothesis validation
  - Test cross-phase evidence flow
  - Test stall detection with evidence

---

## Success Criteria

✅ All 7 phase handlers consume evidence
✅ Evidence findings update investigation state
✅ Phase progression depends on evidence completeness
✅ 20 new tests pass (15 unit + 5 integration)
✅ No regressions in existing 203 tests (117 OODA + 86 evidence)
✅ End-to-end evidence workflow functional

---

## Estimated Total Effort

- **Utilities:** 30 minutes
- **Phase integrations:** 8 hours
- **Testing:** 3 hours
- **Total:** ~11.5 hours (1.5 days)

---

**Next Step:** Create consumption utilities module
