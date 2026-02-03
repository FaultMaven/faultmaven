"""Case Replayer Service.

This module provides the CaseReplayer service, responsible for:
1. Restoring case state at specific turns (Time Travel).
2. Computing semantic diffs between turns (Debugging).

This service is Read-Only and does not modify existing cases.
"""

from typing import Any, Dict, List, Optional, Union

from faultmaven.modules.case.domain.models import Case
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository


class CaseReplayer:
    """
    Service for replaying and debugging investigation cases.

    Provides read-only access to historical states via checkpoints.
    """

    def __init__(self, case_repo: CaseRepository):
        self.repo = case_repo

    async def restore_at_turn(self, case_id: str, turn_number: int) -> Case:
        """
        Reconstruct the full state of a case at a specific turn.

        Args:
            case_id: The ID of the case.
            turn_number: The turn number to restore.

        Returns:
            The reconstructed Case object.

        Raises:
            ValueError: If the checkpoint is not found.
        """
        checkpoints = await self.repo.get_checkpoints(case_id)

        # Find the specific checkpoint
        target_checkpoint = next(
            (cp for cp in checkpoints if cp.turn_number == turn_number), None
        )

        if not target_checkpoint:
            raise ValueError(
                f"Checkpoint for case {case_id} turn {turn_number} not found. "
                f"Available turns: {sorted([cp.turn_number for cp in checkpoints])}"
            )

        # Reconstruct the case from the snapshot
        # Pydantic's model_validate handles the deserialization
        return Case.model_validate(target_checkpoint.case_snapshot)

    async def diff_turns(
        self, case_id: str, turn_a: int, turn_b: int
    ) -> Dict[str, Any]:
        """
        Compute the semantic difference between two turns of a case.

        Args:
            case_id: The ID of the case.
            turn_a: The starting turn number (from).
            turn_b: The ending turn number (to).

        Returns:
            A dictionary representing the diff (added, removed, modified).
        """
        state_a_obj = await self.restore_at_turn(case_id, turn_a)
        state_b_obj = await self.restore_at_turn(case_id, turn_b)

        # Convert to dicts for comparison using model_dump
        state_a = state_a_obj.model_dump()
        state_b = state_b_obj.model_dump()

        return self._compute_recursive_diff(state_a, state_b)

    def _compute_recursive_diff(self, obj_a: Any, obj_b: Any) -> Any:
        """
        Recursively compute the difference between two objects (dicts, lists, or primitives).

        Returns:
            None if identical.
            Dict with 'old' and 'new' if different values.
            Dict with 'added', 'removed', 'modified' keys if dicts differ.
            List with items present in one but not other if lists.
        """
        if obj_a == obj_b:
            return None

        # Compare Dictionaries
        if isinstance(obj_a, dict) and isinstance(obj_b, dict):
            diff = {}
            all_keys = set(obj_a.keys()) | set(obj_b.keys())

            added = {}
            removed = {}
            modified = {}

            for key in all_keys:
                if key not in obj_a:
                    added[key] = obj_b[key]
                elif key not in obj_b:
                    removed[key] = obj_a[key]
                else:
                    # Key exists in both, check for modification
                    nested_diff = self._compute_recursive_diff(obj_a[key], obj_b[key])
                    if nested_diff is not None:
                        modified[key] = nested_diff

            if added:
                diff["added"] = added
            if removed:
                diff["removed"] = removed
            if modified:
                diff["modified"] = modified

            return diff if diff else None

        # Compare Lists (Naive comparison by value, not index-stable for complex lists)
        if isinstance(obj_a, list) and isinstance(obj_b, list):
            # If lists are simple primitives, we can show set difference
            try:
                set_a = set(
                    map(str, obj_a)
                )  # Convert to string to make hashable if needed
                set_b = set(map(str, obj_b))

                # Check actual values if hash calculation worked
                # This is a simplification; for strict diffs we might want index-based comparison
                # But for debugging, knowing WHAT changed is often enough.
                if obj_a != obj_b:
                    return {
                        "old_value": obj_a,
                        "new_value": obj_b,
                        "count_diff": len(obj_b) - len(obj_a),
                    }

            except TypeError:
                # Unhashable items (like list of dicts)
                pass

            return {"old_value": obj_a, "new_value": obj_b}

        # Primitive Values changed
        return {"old": obj_a, "new": obj_b}
