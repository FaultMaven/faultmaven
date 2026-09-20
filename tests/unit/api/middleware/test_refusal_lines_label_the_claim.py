"""A refusal line says which of its identifiers was checked (fm#1461).

The rate-limit WARNING and the deduplication INFO are the two lines an operator
reads to attribute abuse, and each carried two identifiers side by side under
labels that read alike::

    Rate limit exceeded: ..., ip=203.0.113.9, session=whatever-they-sent

``ip=`` is the *resolved* client — the address the limiter actually keyed on,
believed only from a configured trusted proxy. ``session=`` was whatever the
caller put in ``X-Session-ID``, a query parameter or a cookie; neither
middleware authenticates anything, so the value was never checked against
anything at all. Two labels of the same shape, one fact and one assertion —
which is the shape fm#1461 ruled against, here in prose rather than in a
structured field.

Both are still recorded, because correlating a flood by the id it rotates
through is useful. They are called ``claimed_session``, so an operator cannot
mistake one for the other.

These drive the response builders directly. The refusal path is what produces
the line, and the dispatcher's own route to it is covered elsewhere
(``test_rate_limit_client_signalling.py``); what is pinned here is the wording,
which is the whole point.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.models.protection import RateLimitError

CLAIMED_SESSION = "sess-the-caller-typed-this"


def _request(headers=None):
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "root_path": "",
            "path": "/api/v1/cases",
            "raw_path": b"/api/v1/cases",
            "query_string": b"",
            "headers": [
                (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
            ],
            "client": ("203.0.113.9", 1234),
            "app": MagicMock(),
        }
    )


def _messages(caplog):
    return "\n".join(record.getMessage() for record in caplog.records)


@pytest.mark.unit
@pytest.mark.security
class TestARefusalNamesTheClaimAsAClaim:
    def test_the_rate_limit_warning(self, caplog):
        middleware = RateLimitMiddleware(
            app=MagicMock(), settings=get_development_protection_settings()
        )
        error = RateLimitError(
            retry_after=30, limit_type="per_session", current_count=11, limit=10
        )

        with caplog.at_level("DEBUG"):
            middleware._create_rate_limit_response(
                error, _request({"X-Session-ID": CLAIMED_SESSION})
            )

        line = _messages(caplog)
        assert f"claimed_session={CLAIMED_SESSION}" in line
        assert f"session={CLAIMED_SESSION}" not in line.replace("claimed_session=", "")
        assert "ip=" in line, "the resolved client is the fact on this line"

    def test_the_duplicate_request_line(self, caplog):
        middleware = DeduplicationMiddleware(
            app=MagicMock(), settings=get_development_protection_settings()
        )

        with caplog.at_level("DEBUG"):
            middleware._create_duplicate_response(
                _request({"X-Session-ID": CLAIMED_SESSION}),
                original_timestamp=datetime.now(timezone.utc),
                ttl_remaining=17,
            )

        line = _messages(caplog)
        assert f"claimed_session={CLAIMED_SESSION}" in line
        assert f"session={CLAIMED_SESSION}" not in line.replace("claimed_session=", "")
