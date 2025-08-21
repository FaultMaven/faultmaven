"""Pre-Processing Gateway (Modular Monolith)

Fast, in-process checks before orchestration: clarity scoring, reality filter,
and lightweight assumption extraction.
"""

from dataclasses import dataclass
from typing import List
import re


@dataclass
class GatewayResult:
    needs_clarification: bool
    is_absurd: bool
    assumptions: List[str]
    clarity_score: float
    is_greeting: bool = False
    is_definition_question: bool = False
    is_performance_issue: bool = False
    is_general_question: bool = False
    is_risky_action: bool = False
    is_best_practices: bool = False


class PreProcessingGateway:
    def __init__(self, clarity_threshold: float = 0.35) -> None:
        self.clarity_threshold = clarity_threshold
        self.reality_patterns = [
            re.compile(r"black hole.*server", re.I),
            re.compile(r"time travel.*backup", re.I),
            re.compile(r"quantum.*debug", re.I),
        ]
        self.greeting_pattern = re.compile(r"^(hi|hello|hey|yo|howdy|sup|good\s*(morning|afternoon|evening))\b", re.I)
        self.definition_pattern = re.compile(r"^(what\s+is|what's)\b", re.I)
        self.performance_pattern = re.compile(
            r"\b(server|service|site|api|app|application)\b[\s\S]*\b(slow|latency|lag|sluggish|takes\s+(too\s+)?long)\b",
            re.I,
        )
        self.general_question_pattern = re.compile(r"^(what|how|why|which|where|when)\b", re.I)
        # Safety-sensitive patterns
        # Destructive operations only (draining traffic is operational, not destructive)
        self.risky_action_patterns = [
            re.compile(r"delete\s+production\s+data", re.I),
            re.compile(r"(drop|delete|truncate)\s+(table|database|index).*(prod|production)?", re.I),
            re.compile(r"wipe\s+(disk|volume|data)", re.I),
        ]
        # Best-practices style questions
        self.best_practices_patterns = [
            re.compile(r"rollback\s+procedure\s+for\s+(a\s+)?bad\s+deploy", re.I),
            re.compile(r"disaster\s+recovery\s+drills", re.I),
            re.compile(r"backup\s+strategy\s+.*high[- ]write\s+database", re.I),
            re.compile(r"safest\s+way\s+to\s+drain\s+traffic", re.I),
        ]

    def process(self, query: str) -> GatewayResult:
        query = (query or "").strip()
        clarity = self._quick_clarity_score(query)
        is_absurd = self._check_reality(query)
        is_greeting = bool(self.greeting_pattern.search(query)) if query else False
        is_definition_question = bool(self.definition_pattern.search(query)) if query else False
        is_performance_issue = bool(self.performance_pattern.search(query)) if query else False
        is_general_question = bool(self.general_question_pattern.search(query)) if query else False
        is_risky_action = any(p.search(query) for p in self.risky_action_patterns) if query else False
        is_best_practices = any(p.search(query) for p in self.best_practices_patterns) if query else False
        assumptions = self._extract_obvious_assumptions(query)
        return GatewayResult(
            needs_clarification=(
                clarity < self.clarity_threshold
                and not is_absurd
                and not is_greeting
                and not is_definition_question
                and not is_performance_issue
                and not (is_general_question and len(query) >= 15)
                and not is_risky_action
                and not is_best_practices
            ),
            is_absurd=is_absurd,
            assumptions=assumptions[:1],
            clarity_score=clarity,
            is_greeting=is_greeting,
            is_definition_question=is_definition_question,
            is_performance_issue=is_performance_issue,
            is_general_question=is_general_question,
            is_risky_action=is_risky_action,
            is_best_practices=is_best_practices,
        )

    def _quick_clarity_score(self, query: str) -> float:
        if not query:
            return 0.0
        score = 0.0
        words = query.lower().split()
        if len(words) > 5:
            score += 0.2
        if re.search(r"\b(error|issue|problem|fail|exception)\b", query, re.I):
            score += 0.2
        if re.search(r"\b(\d{3}|0x[0-9a-fA-F]+|line\s*\d+)\b", query):
            score += 0.2
        if re.search(r"\b(since|after|at|\d{1,2}:\d{2}|yesterday|today|this morning)\b", query, re.I):
            score += 0.1
        if re.search(r"\b(when|where|which|how|what|why)\b", query, re.I):
            score += 0.1
        return min(1.0, score)

    def _check_reality(self, query: str) -> bool:
        return any(p.search(query) for p in self.reality_patterns)

    def _extract_obvious_assumptions(self, query: str) -> List[str]:
        assumptions: List[str] = []
        # Naive examples
        if "database is down" in query.lower():
            assumptions.append("Database is down")
        if re.search(r"network (issue|problem)", query, re.I):
            assumptions.append("Network is root cause")
        return assumptions


