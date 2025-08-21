from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ConfirmationRequest:
    action_id: str
    action_type: str
    description: str
    rationale: str
    risks: List[str]
    rollback_procedure: Optional[str]


@dataclass
class PolicyDecision:
    allowed: bool
    risk_level: str
    reason: str
    confirmation_required: bool
    confirmation_payload: Optional[ConfirmationRequest]


class PolicyEngine:
    def check_action(self, action: str, context: dict | None = None) -> PolicyDecision:
        text = (action or "").lower()
        if "rm -rf" in text or "drop database" in text:
            return PolicyDecision(False, "critical", "Denied by policy", False, None)
        if text.startswith("restart") or "config" in text:
            payload = ConfirmationRequest(
                action_id="confirm-1",
                action_type="require_confirmation",
                description=action,
                rationale=(context or {}).get("rationale", "Troubleshooting step"),
                risks=["service interruption"],
                rollback_procedure="Revert config or restart previous version",
            )
            return PolicyDecision(False, "high", "Confirmation required", True, payload)
        return PolicyDecision(True, "low", "Allowed", False, None)


