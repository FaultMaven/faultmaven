"""Overlapping requests each get their own summary (#2293).

``LoggingMiddleware`` builds one ``LoggingCoordinator`` for the app, and the
coordinator kept the request in hand on ``self.context``. Two requests in flight
at once shared it: the first to end summarized the second (its correlation id,
duration and attributes) and cleared it, and the second then had no summary.

Two requests are held inside the route until both have arrived, through the
real middleware, and every summary ``end_request`` produces is recorded.
"""

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from faultmaven.api.middleware.logging import LoggingMiddleware
from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator

pytestmark = pytest.mark.unit


async def test_overlapping_requests_each_summarize_themselves(monkeypatch):
    app = FastAPI()
    both_arrived = asyncio.Event()
    arrived = []

    @app.get("/held")
    async def held():
        arrived.append(True)
        if len(arrived) == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), timeout=10)
        return {}

    app.add_middleware(LoggingMiddleware)
    summaries = []
    end_request = LoggingCoordinator.end_request

    def recording(self):
        summary = end_request(self)
        summaries.append(summary)
        return summary

    monkeypatch.setattr(LoggingCoordinator, "end_request", recording)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first, second = await asyncio.gather(client.get("/held"), client.get("/held"))

    served = {first.headers["X-Correlation-ID"], second.headers["X-Correlation-ID"]}
    assert len(served) == 2
    # One summary per request, each naming its own request: before #2293 the
    # first to end named the second, and the second had none ({}).
    summarized = [summary.get("correlation_id") for summary in summaries]
    assert len(summarized) == 2 and set(summarized) == served, summarized
