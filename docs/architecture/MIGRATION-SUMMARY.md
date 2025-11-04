# Migration Summary: OODA → Milestone-Based Investigation

## 🎯 The Big Picture

You're moving from a **rigid phase-based system** to a **flexible milestone-based system** that allows the agent to complete investigations as fast as the user can provide data.

---

## 📊 Architecture Comparison

### OLD System (OODA Framework)

```
┌─────────────────────────────────────────────────────┐
│  Phase 0: INTAKE (Consultant Mode)                  │
│  ↓ (must complete before advancing)                 │
│  Phase 1: BLAST_RADIUS (Begin OODA)                 │
│  ↓ (must complete before advancing)                 │
│  Phase 2: TIMELINE                                  │
│  ↓ (must complete before advancing)                 │
│  Phase 3: HYPOTHESIS (Required)                     │
│  ↓ (must complete before advancing)                 │
│  Phase 4: VALIDATION (Full OODA)                    │
│  ↓ (must complete before advancing)                 │
│  Phase 5: SOLUTION                                  │
│  ↓ (must complete before advancing)                 │
│  Phase 6: DOCUMENT                                  │
└─────────────────────────────────────────────────────┘

Problem: Can't skip phases, even if user provides all data at once
```

### NEW System (Milestone-Based)

```
┌─────────────────────────────────────────────────────┐
│  Status: CONSULTING                                  │
│  ↓ (user confirms investigation)                    │
│  Status: INVESTIGATING                               │
│  │                                                   │
│  │  ✅ symptom_verified                             │
│  │  ✅ scope_assessed                               │
│  │  ✅ timeline_established                         │
│  │  ✅ changes_identified                           │
│  │  ✅ root_cause_identified ← Can complete ANY     │
│  │  ✅ solution_proposed        milestone when      │
│  │  ✅ solution_applied         data is available   │
│  │  ✅ solution_verified                            │
│  │                                                   │
│  ↓ (solution_verified = true)                       │
│  Status: RESOLVED                                    │
└─────────────────────────────────────────────────────┘

Benefit: Complete ALL milestones in ONE turn if user provides complete data
```

---

## 🔑 Key Changes

### 1. Progress Tracking

**BEFORE**: Sequential phases
```python
case.current_phase = 3  # Hypothesis phase
case.turns_in_current_phase = 5
```

**AFTER**: Independent milestone flags
```python
case.progress.symptom_verified = True
case.progress.root_cause_identified = True
case.progress.solution_proposed = True
# Complete any combination based on data available!
```

### 2. Workflow Control

**BEFORE**: Phase gates
```python
if case.current_phase == 3:
    # Can ONLY generate hypotheses
    generate_hypotheses()
    # Must wait until next phase to test them
```

**AFTER**: Data-driven
```python
if has_diagnostic_data() and not case.progress.root_cause_identified:
    # Complete root cause immediately
    identify_root_cause()
    case.progress.root_cause_identified = True
```

### 3. One-Turn Resolution

**BEFORE**: ❌ Impossible
```python
# User provides complete error log with stack trace
turn_1: Phase 0 → Phase 1 (start blast radius)
turn_2: Phase 1 → Phase 2 (start timeline)
turn_3: Phase 2 → Phase 3 (start hypothesis)
turn_4: Phase 3 → Phase 4 (start validation)
turn_5: Phase 4 → Phase 5 (start solution)
# Minimum 5 turns even with perfect data
```

**AFTER**: ✅ Fully Supported
```python
# User provides complete error log with stack trace
turn_1:
  ✅ symptom_verified = True
  ✅ timeline_established = True
  ✅ root_cause_identified = True
  ✅ solution_proposed = True
  status = RESOLVED
# DONE IN ONE TURN! 🎉
```

### 4. Hypothesis Generation

**BEFORE**: Required Phase 3 step
```python
# Must enter Phase 3 and generate hypotheses
# Even if root cause is obvious from logs
```

**AFTER**: Optional exploration
```python
# If root cause is clear from evidence:
case.progress.root_cause_identified = True
# No hypotheses needed!

# If root cause unclear:
generate_hypotheses()  # For systematic exploration
```

---

## 🗑️ What Gets Deleted

### Core OODA Engine (5 files)
```
❌ faultmaven/core/investigation/ooda_engine.py
❌ faultmaven/core/investigation/phases.py
❌ faultmaven/core/investigation/phase_loopback.py
❌ faultmaven/core/investigation/iteration_strategy.py
❌ faultmaven/core/investigation/ooda_step_extraction.py
```

**Why?** These manage phase progression and OODA loops - **not needed in milestone-based approach**

### OODA State Models
```python
# From models/investigation.py
❌ InvestigationPhase (enum)
❌ OODAStep (enum)
❌ OODAIteration (model)
❌ PhaseTransition (model)
❌ OODAEngineState (model)
❌ InvestigationLifecycle.current_phase
❌ InvestigationLifecycle.phase_history
```

**Why?** Replaced by simple milestone booleans in `InvestigationProgress`

---

## ✏️ What Gets Refactored

### Updated (Keep but Modify)

1. **`investigation_coordinator.py`**
   - Remove: Phase transition logic
   - Add: Milestone completion detection
   - Keep: Overall investigation orchestration

2. **`hypothesis_manager.py`**
   - Remove: "Phase 3" requirement
   - Add: Optional hypothesis generation
   - Keep: Hypothesis tracking and validation

3. **`working_conclusion_generator.py`**
   - Remove: Phase-based logic
   - Add: Milestone-based confidence
   - Keep: Working conclusion updates

4. **`strategy_selector.py`**
   - Remove: Phase-based strategy
   - Add: Path selection matrix (temporal × urgency)
   - Keep: Investigation routing

5. **`models/case.py`**
   - Remove: `diagnostic_state.current_phase`
   - Add: `progress: InvestigationProgress`
   - Keep: Evidence, hypotheses, solutions

---

## ✅ What Stays Unchanged

### No Changes Needed
```
✅ faultmaven/models/evidence.py          # Evidence model works as-is
✅ faultmaven/core/knowledge/*            # Knowledge base unchanged
✅ faultmaven/core/processing/*           # Data processing unchanged
✅ faultmaven/infrastructure/*            # LLM, Redis, etc. unchanged
✅ faultmaven/api/*                       # API layer unchanged
```

**Why?** These modules don't depend on phase-based workflow

---

## 📝 Example: Before & After

### Scenario: User Provides Complete Error Log

**BEFORE (OODA Framework)**:
```python
# Turn 1: Phase 0 → Phase 1
user: "API is returning 500 errors. Here's the log file."
agent: "Let me assess the blast radius first..."
case.current_phase = 1

# Turn 2: Phase 1 → Phase 2
agent: "I can see it affects all users. Now let me check the timeline..."
case.current_phase = 2

# Turn 3: Phase 2 → Phase 3
agent: "Started after deployment at 2pm. Let me generate hypotheses..."
case.current_phase = 3

# Turn 4: Phase 3 → Phase 4
agent: "I have 3 hypotheses. Let me test them..."
case.current_phase = 4

# Turn 5: Phase 4 → Phase 5
agent: "Hypothesis validated. Here's the solution..."
case.current_phase = 5

# MINIMUM 5 TURNS
```

**AFTER (Milestone-Based)**:
```python
# Turn 1: COMPLETE INVESTIGATION
user: "API is returning 500 errors. Here's the log file."
agent: """
I've analyzed the log file and can complete the investigation:

✅ Symptom: 500 errors affecting all /api/users requests
✅ Scope: All users, all regions (100% error rate)
✅ Timeline: Started at 2:15 PM after v2.3.1 deployment
✅ Recent Change: Database migration in v2.3.1
✅ Root Cause: Missing index on users.email column causing timeout
✅ Solution: CREATE INDEX idx_users_email ON users(email)

The deployment added email lookups but didn't create the index.
This is causing full table scans and timeouts.

Recommended fix: Run the CREATE INDEX command (1-2 minutes downtime).
"""

case.progress.symptom_verified = True
case.progress.scope_assessed = True
case.progress.timeline_established = True
case.progress.changes_identified = True
case.progress.root_cause_identified = True
case.progress.solution_proposed = True

# ONE TURN! 🎉
```

---

## 🚀 Migration Timeline

### Week 1: Foundation
- Replace OODA models with milestone models
- Delete OODA engine files
- Update `Case` model

### Week 2: Core Logic
- Refactor investigation coordinator
- Make hypotheses optional
- Implement path selection matrix

### Week 3: Prompts
- Create milestone-based prompts
- Implement response processors
- Test conversation flows

### Week 4: Integration
- Full lifecycle testing
- Remove deprecated code
- Documentation updates

---

## 🎯 Success Metrics

### Must Achieve
- ✅ One-turn resolution works
- ✅ Agent completes multiple milestones per turn
- ✅ Hypothesis generation is optional
- ✅ No phase-based constraints

### Quality Targets
- ✅ 80%+ test coverage
- ✅ Token usage reduced
- ✅ Response time maintained or improved

---

## ❓ Decision Points

### Need Your Input On:

1. **Memory Management**: Keep hierarchical memory (hot/warm/cold) or simplify to turn-based?
   - Current: OODA iteration-based compression
   - Proposed: Turn-based compression (simpler)

2. **Engagement Modes**: Map Consultant/Lead Investigator to CONSULTING/INVESTIGATING status?
   - Current: Bidirectional mode switching
   - Proposed: Unidirectional status progression

3. **Branch Strategy**: Create feature branch or work on main?
   - No users = low risk of working on main
   - But feature branch = cleaner rollback

---

## 📚 Reference Documents

- **Full Migration Plan**: [`MIGRATION-PLAN-v2.0.md`](./MIGRATION-PLAN-v2.0.md)
- **Target Architecture**: [`milestone-based-investigation-framework.md`](./milestone-based-investigation-framework.md)
- **Implementation Guide**: [`prompt-implementation-examples.md`](./prompt-implementation-examples.md)

---

## 🤔 Quick Sanity Checks

### "Will this break existing functionality?"
**No** - System has no production users or data to migrate

### "Can we still do multi-turn investigations?"
**Yes** - Milestones can be completed over many turns if data isn't available upfront

### "What if user doesn't provide complete data?"
**Same as before** - Agent requests evidence, user provides incrementally, milestones complete as data arrives

### "Do we lose investigation quality?"
**No** - Same investigation rigor, just without artificial phase barriers

### "Is hypothesis testing gone?"
**No** - It's now OPTIONAL when root cause isn't immediately clear from evidence

---

**Ready to proceed?** Review the full migration plan and let me know if you want to start with Phase 1!
