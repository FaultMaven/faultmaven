"""Workflow Progression Prompts (v3.0)

Agent-initiated confirmations when ready to progress to next phase.

PURPOSE: Agent detects it's time to move forward → seeks user buy-in to continue
vs. User-initiated status changes (user wants to stop → requests documentation)

These are NOT administrative status changes - they're about PROGRESSING through
the troubleshooting workflow with user agreement.

Design Reference:
- Based on Phase 1 routing confirmation pattern (phase1_routing_prompts.py)
- User can type natural language or choose explicitly
- 3-attempt clarification with graceful fallback
"""

from typing import List, Dict, Optional, Any
from faultmaven.models.investigation import InvestigationState


# =============================================================================
# Scenario 1: Start Systematic Investigation (CONSULTING → INVESTIGATING)
# =============================================================================

def get_start_investigation_prompt(
    problem_summary: str,
    complexity_indicators: List[str],
    estimated_time_range: str = "45-65 minutes",
) -> str:
    """Get confirmation to start systematic investigation (Phase 0 → Phase 1)

    PURPOSE: Agent is ready to start structured investigation → seeks user buy-in

    Args:
        problem_summary: Brief summary of the problem discussed
        complexity_indicators: Why systematic approach needed (not just Q&A)
        estimated_time_range: Time estimate for full investigation

    Returns:
        Confirmation prompt explaining systematic investigation process
    """
    indicator_map = {
        "multi_turn_conversation": "We've discussed this across multiple turns without resolution",
        "multiple_symptoms": "Multiple symptoms/components are affected",
        "unclear_scope": "The problem scope is still unclear or expanding",
        "user_expressed_need": "You've indicated you need deeper troubleshooting",
        "complexity_detected": "This appears to be a complex issue requiring structured approach",
    }

    why_investigate = "\n".join(
        f"• {indicator_map.get(ind, ind)}"
        for ind in complexity_indicators
    )

    return f"""# 🔍 Ready to Start Systematic Investigation

## Where We Are

**Problem**: {problem_summary}

**Why I'm Suggesting a Structured Approach**:
{why_investigate}

## What Happens Next

I'd like to lead a **systematic investigation** to find the root cause. This means:

**Phase 1: Scope Assessment** (5-10 min)
→ Map exactly who/what is affected, severity, blast radius

**Phase 2: Timeline Analysis** (5-10 min)
→ When did it start? What changed around that time?

**Phase 3: Theory Generation** (10 min)
→ Generate possible root causes based on evidence

**Phase 4: Hypothesis Testing** (15-20 min)
→ Systematically test theories to validate root cause (≥70% confidence)

**Phase 5: Solution** (10-15 min)
→ Apply validated fix, verify it works

**Phase 6: Documentation** (5 min)
→ Generate knowledge base entry, runbook

**Total Time**: {estimated_time_range}
**End Result**: Validated root cause (≥70% confidence) + verified solution

## Your Decision

I'm ready to start the systematic investigation process.

**Type one of:**
- **"start investigation"** or **"let's do it"** → I'll begin structured troubleshooting
- **"not yet"** or **"keep consulting"** → I'll continue answering questions as they come up
- **"tell me more"** → I can explain the investigation process in detail

Or just tell me what you're thinking in your own words.

**Ready to start the systematic investigation?**
"""


def get_start_investigation_clarification(attempt: int = 1) -> str:
    """Get clarification when response to investigation start is ambiguous

    Args:
        attempt: Which clarification attempt (1, 2, or 3)

    Returns:
        Clarification prompt (escalates with attempts)
    """
    if attempt == 1:
        # Attempt 1: Polite clarification
        return """I'm not sure if you'd like to start the systematic investigation or continue with Q&A.

Could you clarify?

**Choose one:**
- **"start investigation"** - Begin 7-phase structured troubleshooting (45-65 min)
- **"keep consulting"** - Continue answering questions as they come up

Or let me know what you're thinking. For example:
- "Let's figure out the root cause" → start investigation
- "Just need some quick answers" → keep consulting
"""

    elif attempt == 2:
        # Attempt 2: More explicit
        return """I need a clear answer to proceed with the investigation.

Please type one of these:
- Type **"start investigation"** if you want structured troubleshooting
- Type **"keep consulting"** if you want to continue Q&A mode

Just copy and paste one of those phrases so I know how to help you best.
"""

    else:
        # Attempt 3: Give up gracefully
        return """I haven't received a clear decision after multiple attempts.

I'll **continue in consulting mode** and keep answering your questions as they come up.

If you'd like to start a systematic investigation later, just say:
"Start the investigation"

What would you like to know next?
"""


def parse_start_investigation_response(user_response: str) -> tuple[str, bool]:
    """Parse user response to investigation start suggestion

    Args:
        user_response: User's response text

    Returns:
        Tuple of (decision, is_ambiguous)
        - decision: "start", "decline", "more_info", or "ambiguous"
        - is_ambiguous: True if response unclear
    """
    response_lower = user_response.lower().strip()

    # Start investigation keywords
    start_keywords = [
        "start investigation", "start", "let's do it", "yes", "ok", "sure",
        "investigate", "systematic", "let's figure", "find root cause",
        "troubleshoot", "go ahead", "proceed", "do it"
    ]

    # Decline/continue consulting keywords
    decline_keywords = [
        "not yet", "keep consulting", "no", "continue", "just questions",
        "stay in q&a", "not now", "maybe later", "decline"
    ]

    # More info keywords
    more_info_keywords = [
        "tell me more", "explain", "what does", "how long", "what happens",
        "more details", "clarify", "what's involved"
    ]

    # Check for start indicators
    if any(kw in response_lower for kw in start_keywords):
        # But check if they're actually asking questions
        question_indicators = ["what", "how", "why", "when", "?"]
        if any(q in response_lower for q in question_indicators):
            # Asking questions, not confirming
            return "ambiguous", True
        return "start", False

    # Check for decline indicators
    if any(kw in response_lower for kw in decline_keywords):
        return "decline", False

    # Check for more info request
    if any(kw in response_lower for kw in more_info_keywords):
        return "more_info", False

    # Couldn't determine clear preference
    return "ambiguous", True


# =============================================================================
# Scenario 2: Mark Investigation Complete (INVESTIGATING → RESOLVED)
# =============================================================================

def get_mark_complete_prompt(
    root_cause: str,
    solution_summary: str,
    verification_details: str,
    confidence_level: float,
) -> str:
    """Get confirmation to mark investigation as complete (Phase 5 → RESOLVED)

    PURPOSE: Agent verified solution successfully → seeks user buy-in to mark as RESOLVED
    This triggers INVESTIGATING → RESOLVED status change and advances to Phase 6 (Documentation)

    Args:
        root_cause: Identified root cause
        solution_summary: Solution that was applied
        verification_details: How solution was verified
        confidence_level: Confidence in root cause (0.0-1.0)

    Returns:
        Confirmation prompt explaining completion
    """
    return f"""# ✅ Investigation Complete - Ready to Mark as Resolved

## What We Accomplished

**Root Cause Identified**:
{root_cause}

**Solution Applied**:
{solution_summary}

**Verification**:
{verification_details}

**Confidence Level**: {confidence_level:.0%} (validated root cause)

## What Happens Next

I'm ready to **mark this investigation as RESOLVED** and proceed to documentation.

**If you confirm:**
1. ✅ Case status changes to RESOLVED
2. ✅ System advances to Phase 6 (Documentation)
3. ✅ Final artifacts generated (case report, runbook, KB entry)
4. ✅ Investigation closed successfully

**Note**: This is a successful resolution - we found root cause, applied fix, and verified it works.

## Your Decision

**Type one of:**
- **"mark as complete"** or **"we're done"** → Close investigation successfully
- **"not yet"** or **"more verification"** → Continue monitoring/verification
- **"I have questions"** → Ask before we close

Or just let me know what you're thinking.

**Should we mark this investigation as complete?**
"""


def get_mark_complete_clarification(attempt: int = 1) -> str:
    """Get clarification when completion confirmation is ambiguous"""
    if attempt == 1:
        return """I'm not sure if you're ready to mark this as complete or want more verification.

Could you clarify?

**Choose one:**
- **"mark as complete"** - Close investigation, generate documentation
- **"more verification"** - Continue monitoring before closing

Or let me know your concerns. For example:
- "Yes, we're done" → mark as complete
- "Want to monitor for 24 hours first" → more verification
"""

    elif attempt == 2:
        return """I need a clear answer about completion.

Please type one of these:
- Type **"mark as complete"** if investigation is done
- Type **"more verification"** if you want to keep monitoring

Just copy and paste one of those phrases.
"""

    else:
        return """I haven't received a clear decision after multiple attempts.

I'll **keep the investigation open** so you can continue verification.

When you're ready to close, just say:
"Mark this as complete"

What would you like to do next?
"""


def parse_mark_complete_response(user_response: str) -> tuple[str, bool]:
    """Parse user response to completion suggestion"""
    response_lower = user_response.lower().strip()

    # Complete keywords
    complete_keywords = [
        "mark as complete", "complete", "we're done", "yes", "close it",
        "done", "finish", "resolved", "fixed", "good to close"
    ]

    # More verification keywords
    more_keywords = [
        "not yet", "more verification", "keep monitoring", "wait",
        "not ready", "need more time", "continue", "not complete"
    ]

    # Questions keywords
    question_keywords = [
        "i have questions", "question", "clarify", "explain",
        "what about", "concerns"
    ]

    if any(kw in response_lower for kw in complete_keywords):
        # Check if questioning
        if "?" in response_lower:
            return "ambiguous", True
        return "complete", False

    if any(kw in response_lower for kw in more_keywords):
        return "more_verification", False

    if any(kw in response_lower for kw in question_keywords):
        return "questions", False

    return "ambiguous", True


# =============================================================================
# Scenario 3: Suggest Escalation/Closure (INVESTIGATING → CLOSED)
# =============================================================================

def get_suggest_escalation_prompt(
    limitation_type: str,
    limitation_explanation: str,
    findings_summary: str,
    confidence_level: float,
    next_steps_recommendations: List[str],
) -> str:
    """Get confirmation to close investigation due to limitations (Any Phase → Terminal)

    PURPOSE: Agent hit limits/blocked → seeks user buy-in to escalate or close

    Args:
        limitation_type: Type of limitation (expertise, evidence, systemic, etc.)
        limitation_explanation: Detailed explanation of limitation
        findings_summary: What we discovered so far
        confidence_level: Current confidence (likely <70%)
        next_steps_recommendations: Suggested next actions

    Returns:
        Confirmation prompt explaining situation and options
    """
    return f"""# 🚧 Investigation Limitations Reached

## Current Situation

**Limitation**: {limitation_explanation}
**Type**: {limitation_type}

**What We've Discovered So Far**:
{findings_summary}

**Current Confidence**: {confidence_level:.0%} (below validation threshold)

## Why I'm Suggesting This

I've reached the limits of what I can investigate with the available information and expertise.

Continuing would mean:
- Working with low confidence (<70%)
- Unable to validate root cause
- Risk of incorrect conclusions

## Your Options

### Option 1: **Close and Escalate** (Recommended)
- Type: **"close and escalate"** or **"escalate"**
- Action: Document findings, hand off to specialist
- Best for: Need expert help to proceed

**Recommended Next Steps**:
{chr(10).join(f"{i+1}. {step}" for i, step in enumerate(next_steps_recommendations))}

### Option 2: **Keep Trying** (Low Confidence)
- Type: **"keep trying"** or **"continue"**
- Action: Continue investigation with degraded confidence
- Best for: Want to exhaust all options before escalating

### Option 3: **I Have Questions**
- Type: **"questions"** or **"wait"**
- Action: Discuss before deciding
- Best for: Need clarification about limitations or options

## Your Decision

**Type one of:**
- **"close and escalate"** → Document findings and escalate
- **"keep trying"** → Continue with degraded confidence
- **"questions"** → Ask before deciding

Or just tell me what you'd prefer.

**What would you like to do?**
"""


def get_suggest_escalation_clarification(attempt: int = 1) -> str:
    """Get clarification when escalation response is ambiguous"""
    if attempt == 1:
        return """I'm not sure what you'd like to do about these investigation limitations.

Could you clarify?

**Choose one:**
- **"close and escalate"** - Document findings and get expert help
- **"keep trying"** - Continue investigating despite limitations
- **"questions"** - Ask about limitations or options

Or tell me what you're thinking. For example:
- "Let's get someone who knows this better" → close and escalate
- "We can keep digging" → keep trying
"""

    elif attempt == 2:
        return """I need a clear answer about how to proceed.

Please type one of these:
- Type **"close and escalate"** if you want expert help
- Type **"keep trying"** if you want to continue anyway
- Type **"questions"** if you need clarification

Just copy and paste one of those phrases.
"""

    else:
        return """I haven't received a clear decision after multiple attempts.

I'll **continue the investigation** despite the limitations, but with degraded confidence.

If you'd like to escalate later, just say:
"Close this and escalate"

How should we proceed?
"""


def parse_suggest_escalation_response(user_response: str) -> tuple[str, bool]:
    """Parse user response to escalation suggestion"""
    response_lower = user_response.lower().strip()

    # Escalate keywords
    escalate_keywords = [
        "close and escalate", "escalate", "close", "get help",
        "bring in", "need expert", "hand off", "transfer"
    ]

    # Continue keywords
    continue_keywords = [
        "keep trying", "continue", "keep going", "don't give up",
        "we can figure", "keep investigating", "try anyway"
    ]

    # Questions keywords
    question_keywords = [
        "questions", "wait", "hold on", "clarify", "explain",
        "what do you mean", "tell me more"
    ]

    if any(kw in response_lower for kw in escalate_keywords):
        return "escalate", False

    if any(kw in response_lower for kw in continue_keywords):
        return "continue", False

    if any(kw in response_lower for kw in question_keywords):
        return "questions", False

    return "ambiguous", True


# =============================================================================
# Tracking and State Management
# =============================================================================

def get_workflow_transition_confirmation(
    transition_type: str,
    transition_details: Dict[str, Any],
) -> str:
    """Get confirmation message after workflow transition decided

    Args:
        transition_type: "start_investigation", "mark_complete", or "escalate"
        transition_details: Details about the transition

    Returns:
        Confirmation message explaining what happens next
    """
    if transition_type == "start_investigation":
        return """✅ **Starting Systematic Investigation**

I'll now lead you through structured troubleshooting:

**Next**: Phase 1 - Blast Radius Assessment
→ I'll ask you about who/what is affected, severity, and impact scope.

Let's begin...
"""

    elif transition_type == "mark_complete":
        root_cause = transition_details.get("root_cause", "Root cause identified")
        return f"""✅ **Investigation Marked as Complete**

Generating final documentation:
- ✅ Case report with timeline and findings
- ✅ Runbook for reproducing/fixing similar issues
- ✅ Knowledge base entry

**Root Cause**: {root_cause}

Investigation closed successfully. Documentation will be available shortly.
"""

    elif transition_type == "escalate":
        recommendations = transition_details.get("recommendations", [])
        return f"""✅ **Investigation Closed - Escalation Path Set**

I've documented everything we discovered and the limitations we hit.

**Recommended Next Steps**:
{chr(10).join(f"{i+1}. {step}" for i, step in enumerate(recommendations))}

Investigation documentation generated. Good luck with the escalation!
"""

    elif transition_type == "declined_investigation":
        return """✅ **Continuing in Consulting Mode**

No problem! I'll keep answering questions as they come up.

If you change your mind about systematic investigation, just let me know.

What would you like to know next?
"""

    elif transition_type == "more_verification":
        return """✅ **Continuing Verification**

I'll keep the investigation open for additional verification.

Let me know when you're ready to mark this as complete, or if you need help with verification steps.

What would you like to verify next?
"""

    elif transition_type == "keep_investigating":
        return """✅ **Continuing Investigation**

I'll keep investigating despite the limitations, but with degraded confidence.

Be aware: confidence is below validation threshold (<70%).

How should we proceed?
"""

    else:
        return f"""✅ Workflow transition: {transition_type}"""
