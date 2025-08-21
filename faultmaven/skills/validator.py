from typing import Tuple


class ValidatorSkill:
    name = "validator"

    async def can_handle(self, turn) -> Tuple[bool, float]:
        q = (turn.get("query", "") or "").lower()
        has_assumption = any(x in q for x in ("database is down", "network issue"))
        has_risky = any(x in q for x in ("restart", "delete", "config"))
        return (has_assumption or has_risky), 0.9 if has_risky else 0.6

    async def estimate_cost(self, turn):
        return {"time_ms": 150, "tokens": 200, "api_calls": 0}

    async def execute(self, turn, budget) -> dict:
        q = (turn.get("query", "") or "").lower()
        risky_action = None
        if "restart" in q:
            risky_action = "restart_service"
        elif "delete" in q:
            risky_action = "delete_resource"
        elif "config" in q:
            risky_action = "update_config"

        result = {
            "success": True,
            "confidence_delta": {"validation_score": 0.1},
            "evidence": [],
            "next_action": "clarify",
            "response": "Please confirm: Can you reach the database host:port from the app node?",
            "cost": {"time_ms": 150, "tokens": 200, "api_calls": 0},
        }
        if risky_action:
            result["proposed_action"] = risky_action
        return result


