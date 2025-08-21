from typing import Tuple


class RetrievalSkill:
    name = "retrieval"

    def __init__(self, retrieval_service) -> None:
        self._svc = retrieval_service

    async def can_handle(self, turn) -> Tuple[bool, float]:
        return True, 0.7

    async def estimate_cost(self, turn):
        return {"time_ms": 300, "tokens": 500, "api_calls": 1}

    async def execute(self, turn, budget) -> dict:
        results = await self._svc.search(
            query=turn.get("query", ""),
            context=turn.get("validated_facts", []),
            max_results=8,
        )
        boost = 0.2 if results and getattr(results[0], "score", 0.0) > 0.8 else 0.0
        return {
            "success": bool(results),
            "confidence_delta": {"retrieval_score": getattr(results[0], "score", 0.0) if results else 0.0, "pattern_boost": boost},
            "evidence": [e.__dict__ for e in results],
            "next_action": "solve" if boost >= 0.2 else "investigate",
            "response": "\n".join([getattr(e, "content", "") for e in results[:3]]),
            "cost": {"time_ms": 300, "tokens": 500, "api_calls": 1},
        }


