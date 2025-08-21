from dataclasses import dataclass
from datetime import datetime
from typing import List


@dataclass
class Evidence:
    source: str
    content: str
    score: float
    timestamp: datetime
    metadata: dict


class KBAdapter:
    async def search(self, query: str, context: List[str]) -> List[Evidence]:
        return []


class PatternAdapter:
    async def match(self, query: str, context: List[str]) -> List[Evidence]:
        return []


class PlaybookAdapter:
    async def lookup(self, query: str, context: List[str]) -> List[Evidence]:
        return []


class RetrievalService:
    def __init__(self, kb: KBAdapter, pattern: PatternAdapter, playbook: PlaybookAdapter):
        self.kb = kb
        self.pattern = pattern
        self.playbook = playbook

    async def search(self, query: str, context: List[str], max_results: int = 8) -> List[Evidence]:
        results: List[Evidence] = []
        results += await self.kb.search(query, context)
        results += await self.playbook.lookup(query, context)
        results += await self.pattern.match(query, context)
        # Normalize and sort
        for ev in results:
            ev.score = max(0.0, min(1.0, ev.score))
        results.sort(key=lambda e: e.score, reverse=True)
        return results[:max_results]


