from typing import Dict, List


class ConfidenceAggregator:
    def __init__(self) -> None:
        self.weights = {
            "retrieval_score": 0.3,
            "hypothesis_top": 0.3,
            "validation_score": 0.2,
            "pattern_boost": 0.1,
            "evidence_count_norm": 0.1,
        }

    def score(self, features: Dict[str, float]) -> float:
        val = 0.0
        for k, w in self.weights.items():
            val += w * float(features.get(k, 0.0))
        return max(0.0, min(1.0, val))

    def get_band(self, score: float, history: List[float]) -> str:
        # Simple hysteresis: need two consecutive below 0.5 to drop out of gray/high
        if score >= 0.8:
            return "high"
        if score >= 0.5:
            return "gray"
        # Check if we have a trend of low
        if len(history) >= 2 and all(h < 0.5 for h in history[-2:]):
            return "low"
        return "gray"




