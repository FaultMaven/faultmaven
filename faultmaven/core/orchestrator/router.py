from typing import List
import random


class Router:
    def __init__(self, epsilon: float = 0.05, max_skills: int = 2):
        self.epsilon = epsilon
        self.max_skills = max_skills

    async def select(self, turn: dict, skills: List[object], budget: dict) -> List[object]:
        available = []
        scored = []
        for s in skills:
            can, score = await s.can_handle(turn)
            if can:
                available.append(s)
                scored.append((s, score))

        if not available:
            return []

        if random.random() < self.epsilon:
            random.shuffle(available)
            return available[: self.max_skills]

        scored.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in scored[: self.max_skills]]




