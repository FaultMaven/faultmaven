from typing import Tuple


class DiagnoserSkill:
    name = "diagnoser"

    async def can_handle(self, turn) -> Tuple[bool, float]:
        # Default diagnoser relevance
        return True, 0.6

    async def estimate_cost(self, turn):
        return {"time_ms": 600, "tokens": 800, "api_calls": 2}

    async def execute(self, turn, budget) -> dict:
        # Placeholder hypothesis score
        hypothesis_top = 0.65
        return {
            "success": True,
            "confidence_delta": {"hypothesis_top": hypothesis_top, "evidence_count_norm": 0.3},
            "evidence": [],
            "next_action": "investigate" if hypothesis_top < 0.7 else "solve",
            "response": "Generated hypotheses and a minimal test plan.",
            "cost": {"time_ms": 600, "tokens": 800, "api_calls": 2},
        }


