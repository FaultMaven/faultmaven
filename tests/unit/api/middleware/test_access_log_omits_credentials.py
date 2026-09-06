"""A query string must never reach a log record.

The access log stamped ``query_params=str(request.query_params)`` on every
request at INFO. The SSO callback takes the identity provider's authorization
code and the CSRF state as query parameters
(``modules/auth/api/sso.py``), so every sign-in wrote a live credential into a
record that ships to Loki with 14-day retention.

This is the class ``exception_handlers.py`` already documents as uncatchable
downstream: "there is no redaction processor in the structlog chain to catch
that downstream". It was fixed once at one call site (fm#1156) and recurred
here, which is why the assertion below is on the CHANNEL — no value from the
query string, whatever it is called — rather than on a list of secret names.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.logging import LoggingMiddleware


def register_500_handler(app: FastAPI) -> None:
    """Attach main.py's 500 handler to a throwaway app.

    Imported rather than re-implemented so the test breaks if the real handler
    starts logging the URL again.
    """
    from faultmaven.main import app as real_app

    for exc_type, handler in real_app.exception_handlers.items():
        if exc_type in (500, Exception):
            app.add_exception_handler(exc_type, handler)


SECRET = "authcode-Ei9kQ2xhdWRlLXNlY3JldA"
STATE = "csrf-7f3a91c4"


@pytest.fixture()
def client():
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/api/v1/auth/sso/callback")
    async def callback(code: str = "", state: str = ""):
        return {"ok": True}

    with (
        patch("faultmaven.api.middleware.logging.request_counter") as counter,
        patch("faultmaven.api.middleware.logging.request_duration") as duration,
        patch("faultmaven.api.middleware.logging.sla_tracker"),
    ):
        counter.labels.return_value = MagicMock()
        duration.labels.return_value = MagicMock()
        yield TestClient(app, raise_server_exceptions=False)


def _field(record, name):
    """Read a structured field off a record, wherever this stack put it.

    Fields passed to a structlog logger arrive as keys of the event dict in
    ``record.msg``; fields passed to a plain stdlib logger via ``extra`` arrive
    as record attributes. Checking only one of the two silently finds nothing
    and turns an assertion into a tautology.
    """
    if isinstance(record.msg, dict) and name in record.msg:
        return record.msg[name]
    return record.__dict__.get(name)


def _emitted(caplog) -> str:
    """Everything FaultMaven handed to logging, message and extras.

    Scoped to our own loggers on purpose. The HTTP *client* library the test
    harness drives also logs the request URL, query string included — a real
    leak, but a different one, fixed by the redaction filter in
    ``infrastructure/logging/config.py`` and pinned by
    ``tests/unit/infrastructure/logging/test_url_query_redaction.py``. Asserting
    over every logger here would make this test fail for that reason and point
    at the wrong file.
    """
    parts = []
    for record in caplog.records:
        if not record.name.startswith("faultmaven"):
            continue
        parts.append(record.getMessage())
        parts.append(repr(record.__dict__))
    return " ".join(parts)


@pytest.mark.unit
@pytest.mark.security
class TestAccessLogOmitsQueryValues:
    def test_an_sso_authorization_code_never_reaches_a_log_record(self, client, caplog):
        with caplog.at_level("DEBUG"):
            response = client.get(
                f"/api/v1/auth/sso/callback?code={SECRET}&state={STATE}"
            )

        assert response.status_code == 200
        emitted = _emitted(caplog)
        assert SECRET not in emitted, "the authorization code reached a log record"
        assert STATE not in emitted, "the CSRF state reached a log record"

    def test_parameter_names_are_still_recorded(self, client, caplog):
        """Names are what debugging needs; values are what leaks.

        Without this, deleting the field entirely would also pass the test
        above — and the next person would have no way to see which parameters
        a failing request carried.

        Asserted on the field's value, not on the substring "code" appearing
        somewhere in the output. Every completed-request record carries
        ``status_code``, so a substring check for "code" is satisfied by an
        unrelated field and pins nothing.
        """
        with caplog.at_level("DEBUG"):
            client.get(f"/api/v1/auth/sso/callback?code={SECRET}&state={STATE}")

        recorded = [
            names
            for record in caplog.records
            for names in [_field(record, "query_param_names")]
            if names is not None
        ]
        assert recorded, "no record carried query_param_names at all"
        assert all(names == ["code", "state"] for names in recorded), recorded


@pytest.mark.unit
@pytest.mark.security
class TestErrorPathsOmitQueryValues:
    """The failure path is where this class of leak survives longest.

    The access log was fixed first, but the 500 handler logged ``request.url``
    — the whole URL, query string included — so any unhandled exception on the
    SSO callback still wrote the authorization code, at ERROR. The redaction
    filter cannot reach it: that filter covers foreign stdlib loggers that
    print URLs, and this is a first-party structlog event whose ``msg`` is an
    event dict.
    """

    def test_an_unhandled_exception_does_not_log_the_query_string(self, caplog):
        app = FastAPI()

        @app.get("/api/v1/auth/sso/callback")
        async def callback(code: str = "", state: str = ""):
            raise RuntimeError("boom")

        register_500_handler(app)

        with caplog.at_level("DEBUG"):
            TestClient(app, raise_server_exceptions=False).get(
                f"/api/v1/auth/sso/callback?code={SECRET}&state={STATE}"
            )

        emitted = " ".join(
            record.getMessage() + repr(record.__dict__)
            for record in caplog.records
            if record.name.startswith("faultmaven")
        )
        assert SECRET not in emitted, "the authorization code reached an ERROR record"
        assert STATE not in emitted, "the CSRF state reached an ERROR record"
        assert "/api/v1/auth/sso/callback" in emitted, "the path must survive"
