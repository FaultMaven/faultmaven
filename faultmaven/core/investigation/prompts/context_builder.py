"""Investigation Prompt Context Builder

This module handles gathering and truncating investigation context for LLM prompts,
ensuring we stay within token limits while preserving high-priority information.

Priority:
1. System Prompt & Response Schema (Fixed)
2. Case Definition & Core Identity
3. Recent Conversation History (Last N turns)
4. Knowledge Base Search Results
5. Detailed Evidence Summaries
6. Older Conversation History (Truncated)
"""

import logging
from typing import List, Dict, Any, Optional
from faultmaven.modules.case.contracts import Case, CaseStatus, InvestigationStage

logger = logging.getLogger(__name__)

class TokenBudget:
    """Simple character-based token approximation (1 token ~= 4 chars)"""
    def __init__(self, limit_tokens: int = 8000):
        self.limit_chars = limit_tokens * 4
        self.used_chars = 0

    def has_budget(self, text: str) -> bool:
        return self.used_chars + len(text) <= self.limit_chars

    def use(self, text: str) -> str:
        if self.has_budget(text):
            self.used_chars += len(text)
            return text
        else:
            # Truncate if partially fits
            remaining = self.limit_chars - self.used_chars
            if remaining > 100:
                truncated = text[:remaining-50] + "\n[... Content truncated due to context limit ...]"
                self.used_chars = self.limit_chars
                return truncated
            return ""

def build_investigation_context(
    case: Case,
    user_message: str,
    kb_results: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = 8000
) -> Dict[str, str]:
    """
    Gather and format context elements within token budget.
    """
    budget = TokenBudget(max_tokens)
    
    # 1. Identity & Status
    identity = f"CASE_ID: {case.case_id}\nSTATUS: {case.status.value.upper()}\n"
    if case.status == CaseStatus.INVESTIGATING and case.current_stage:
        identity += f"CURRENT_STAGE: {case.current_stage.value.upper()}\n"
    
    # 2. Case Core Context
    core_context = f"TITLE: {case.title}\nDESCRIPTION: {case.description}\n"
    if case.problem_verification:
        pv = case.problem_verification
        core_context += f"SYMPTOM_STATEMENT: {pv.symptom_statement}\n"
        if pv.severity: core_context += f"SEVERITY: {pv.severity}\n"
        if pv.temporal_state: core_context += f"TEMPORAL_STATE: {pv.temporal_state.value}\n"

    # 3. Milestone Status
    milestones_str = "MILESTONES COMPLETED:\n"
    if case.status == CaseStatus.INVESTIGATING:
        p = case.progress
        for milestone, completed in p.dict().items():
            if isinstance(completed, bool) and completed:
                milestones_str += f"- {milestone}\n"
    else:
        milestones_str = ""

    # 4. Evidence Summary
    evidence_str = "EVIDENCE COLLECTED:\n"
    if case.evidence:
        for i, ev in enumerate(case.evidence[-10:]): # Last 10 evidence items
            evidence_str += f"- [{ev.category}] {ev.summary} (ID: {ev.evidence_id})\n"
    else:
        evidence_str = "No formal evidence collected yet.\n"

    # 5. Hypothesis Summary
    hypothesis_str = "WORKING HYPOTHESES:\n"
    active_h = [h for h in case.hypotheses.values() if h.status != "retired"]
    if active_h:
        for h in active_h:
            hypothesis_str += f"- {h.statement} (Confidence: {h.likelihood*100:.0f}%, Status: {h.status.value})\n"
    else:
        hypothesis_str = ""

    # 6. Conversation History (Prioritized)
    # We take the last 5 turns in full, and older turns truncated
    recent_history = ""
    history_turns = case.turn_history[-10:] # Take up to 10
    for turn in history_turns:
        recent_history += f"TURN {turn.turn_number} ({turn.outcome.value}):\n"
        recent_history += f"USER: {turn.user_message_summary}\n"
        recent_history += f"AGENT: {turn.agent_response_summary}\n\n"

    # 7. Knowledge Base Results
    kb_str = "KNOWLEDGE BASE SEARCH RESULTS:\n"
    if kb_results:
        for i, res in enumerate(kb_results[:3]): # Top 3
            kb_str += f"MATCH {i+1} ({res.get('type')}): {res.get('summary')}\n"
            kb_str += f"SOLUTION: {res.get('solution')}\n\n"
    else:
        kb_str = ""

    # 8. System Feedback (Validation errors from previous turn)
    feedback_str = ""
    if case.turn_history:
        last_turn = case.turn_history[-1]
        if last_turn.system_feedback:
            feedback_str = f"IMPORTANT - SYSTEM FEEDBACK FROM PREVIOUS TURN:\n{last_turn.system_feedback}\n\n"

    # Assembly with budget check
    ctx = {
        "identity": budget.use(identity),
        "core_context": budget.use(core_context),
        "milestones": budget.use(milestones_str),
        "evidence": budget.use(evidence_str),
        "hypotheses": budget.use(hypothesis_str),
        "kb_results": budget.use(kb_str),
        "system_feedback": feedback_str, # Priotitize feedback
        "conversation_history": budget.use(recent_history),
        "user_message": user_message # User message always included
    }
    
    return ctx
