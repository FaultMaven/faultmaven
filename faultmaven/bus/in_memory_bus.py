from typing import Callable, Awaitable, Dict, List


class InMemoryBus:
    def __init__(self) -> None:
        self._subs: Dict[str, List[Callable[[dict], Awaitable[None]]]] = {}

    async def publish(self, topic: str, message: dict) -> None:
        for handler in self._subs.get(topic, []):
            await handler(message)

    async def subscribe(self, topic: str, handler: Callable[[dict], Awaitable[None]]) -> None:
        self._subs.setdefault(topic, []).append(handler)


