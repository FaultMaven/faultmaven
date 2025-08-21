from typing import List


class SkillRegistry:
    def __init__(self, skills: List[object]):
        self._skills = list(skills)

    def all(self) -> List[object]:
        return self._skills


