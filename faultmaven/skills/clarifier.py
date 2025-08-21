from dataclasses import dataclass
from typing import Tuple


@dataclass
class Cost:
    time_ms: int = 100
    tokens: int = 200
    api_calls: int = 0


class ClarifierSkill:
    name = "clarifier"

    async def can_handle(self, turn) -> Tuple[bool, float]:
        clarity_score = self._calculate_clarity(turn.get("query", ""))
        needs = clarity_score < 0.35
        return needs, max(0.0, 1.0 - clarity_score)

    async def estimate_cost(self, turn) -> Cost:
        return Cost()

    async def execute(self, turn, budget) -> dict:
        questions = self._generate_questions(turn.get("query", ""), max_questions=2)
        return {
            "success": True,
            "confidence_delta": {"clarity": 0.0},
            "evidence": [],
            "next_action": "clarify",
            "response": questions,
            "cost": Cost().__dict__,
        }

    def _calculate_clarity(self, query: str) -> float:
        query = (query or "").strip()
        if not query:
            return 0.0
        score = 0.0
        if len(query.split()) > 5:
            score += 0.2
        for token in ("error", "issue", "problem", "fail", "exception"):
            if token in query.lower():
                score += 0.1
                break
        if any(x in query.lower() for x in ("since", "after", "at ")):
            score += 0.1
        return min(1.0, score)

    def _generate_questions(self, query: str, max_questions: int = 2) -> str:
        base = [
            "When did the issue start?",
            "Which operations/services are affected?",
            "How many users are impacted?",
        ]
        return "\n".join(base[:max_questions])


