from typing import List
import numpy as np
from faultmaven.infrastructure.model_cache import model_cache


class LoopStatus:
    NONE = "none"
    WARNING = "warning"
    RECOVERY_NEEDED = "recovery"


class LoopGuard:
    def __init__(self) -> None:
        self.similarity_threshold = 0.85
        self.min_confidence_slope = 0.02
        self.debounce_turns = 2
        self._warn_count = 0

    def check(self, history: List[dict]) -> str:
        if len(history) < 3:
            return LoopStatus.NONE
        # Embedding similarity using cached model if available
        model = model_cache.get_bge_m3_model()
        similarity = 0.0
        if model:
            queries = [h.get("user_query") or h.get("query") or "" for h in history[-3:]]
            embeddings = model.encode(queries, normalize_embeddings=True)
            v1 = np.array(embeddings[0])
            v3 = np.array(embeddings[-1])
            similarity = float(np.dot(v1, v3))
        confidences = [h.get("confidence", 0.0) for h in history[-3:]]
        slope = confidences[-1] - confidences[0]
        is_loop = similarity >= self.similarity_threshold and slope <= self.min_confidence_slope
        if is_loop:
            self._warn_count += 1
            return LoopStatus.RECOVERY_NEEDED if self._warn_count >= self.debounce_turns else LoopStatus.WARNING
        self._warn_count = max(0, self._warn_count - 1)
        return LoopStatus.NONE


