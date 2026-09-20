"""Every completed request line names the principal the request was bound to.

``LoggingMiddleware`` derived ``user_id`` from a **session id** it dug out of a
header, query parameter or body. A bearer-authenticated request carries none, so
on the live deployment every case read, list and search logged ``user_id: null``
and named no enterprise at all — "who read case X" needed correlation-id joins
against the sign-in lines, and the isolation boundary (the enterprise) appeared
nowhere.

The answer already existed one layer down: ``bind_request_enterprise_context``
verifies the token and publishes a ``RequestPrincipal`` on ``request.state``. It
cannot hand it over in a contextvar — Starlette runs a ``BaseHTTPMiddleware``'s
downstream in a separate task — but ``request.state`` is backed by the ASGI
scope, one dict shared by both tasks.

What is pinned here is the whole contract: the published principal is the ONLY
source of an actor, and a request it names nobody for is recorded as naming
nobody.

There used to be a second source. When the principal named no user, the line
fell back to the owner of a session id read off the ``X-Session-ID`` header —
which a caller writes — so an unauthenticated request carrying somebody else's
session id was recorded against that somebody, and an incident responder
reading the line was handed a name to act on that the caller had chosen
(fm#1461). The caller's value is still on the line, because correlating by it
is genuinely useful; it is called ``claimed_session_id``, and nothing resolves
it to an account.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from faultmaven.api.middleware.logging import LoggingMiddleware
from faultmaven.api.middleware.principal import (
    RequestPrincipal,
    publish_request_principal,
)

USER = "user_01HQXJ"
ENTERPRISE = "11111111-1111-1111-1111-111111111111"
ORGANIZATION = "22222222-2222-2222-2222-222222222222"
SESSION_USER = "user-from-the-session-store"
SESSION_ID = "sess-abc-123"


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


def _line(caplog, needle):
    """The one request record whose message starts with ``needle``."""
    matches = [
        record
        for record in caplog.records
        if record.name.startswith("faultmaven") and needle in record.getMessage()
    ]
    assert matches, f"no record carried {needle!r}"
    return matches[0]


def _app(principal=None, session_user_id=None, boom=False):
    """An app whose route optionally publishes a principal, as the binder does.

    The principal is published from a route DEPENDENCY rather than inside the
    handler on purpose: that is where the real binder runs, and it is the task
    boundary the middleware has to read across.
    """
    dependencies = []
    if principal is not None:

        async def bind(request: Request):
            publish_request_principal(request, principal)

        dependencies.append(Depends(bind))

    app = FastAPI(dependencies=dependencies)
    app.add_middleware(LoggingMiddleware)

    if session_user_id is not None:
        session_service = MagicMock()
        session_service.get_session = AsyncMock(
            return_value=MagicMock(user_id=session_user_id)
        )
        app.state.session_service = session_service

    @app.get("/api/v1/cases/{case_id}")
    async def read_case(case_id: str):
        if boom:
            raise RuntimeError("kaboom")
        return {"id": case_id}

    return app


def _get(app, headers=None):
    with (
        patch("faultmaven.api.middleware.logging.request_counter") as counter,
        patch("faultmaven.api.middleware.logging.request_duration") as duration,
        patch("faultmaven.api.middleware.logging.sla_tracker"),
    ):
        counter.labels.return_value = MagicMock()
        duration.labels.return_value = MagicMock()
        client = TestClient(app, raise_server_exceptions=False)
        return client.get("/api/v1/cases/case-42", headers=headers or {})


@pytest.mark.unit
class TestTheCompletedLineNamesTheBoundPrincipal:
    def test_it_carries_the_user_enterprise_and_organization(self, caplog):
        app = _app(
            principal=RequestPrincipal(
                user_id=USER,
                enterprise_id=ENTERPRISE,
                organization_id=ORGANIZATION,
            )
        )

        with caplog.at_level("DEBUG"):
            response = _get(app)

        assert response.status_code == 200
        record = _line(caplog, "Request completed")
        assert _field(record, "user_id") == USER
        assert _field(record, "enterprise_id") == ENTERPRISE
        assert _field(record, "organization_id") == ORGANIZATION

    def test_the_human_readable_message_names_both(self, caplog):
        """An operator reading the console renderer, not Loki, gets it too."""
        app = _app(principal=RequestPrincipal(user_id=USER, enterprise_id=ENTERPRISE))

        with caplog.at_level("DEBUG"):
            _get(app)

        message = _line(caplog, "Request completed").getMessage()
        assert f"[user: {USER}]" in message
        assert f"[enterprise: {ENTERPRISE}]" in message

    def test_the_failure_line_names_it_too(self, caplog):
        """An unhandled exception is raised by the route, so the binder has run.

        A 500 is exactly when attribution matters, and it is a separate call
        site — the two drifted apart trivially before this assertion existed.
        """
        app = _app(
            principal=RequestPrincipal(
                user_id=USER,
                enterprise_id=ENTERPRISE,
                organization_id=ORGANIZATION,
            ),
            boom=True,
        )

        with caplog.at_level("DEBUG"):
            response = _get(app)

        assert response.status_code == 500
        record = _line(caplog, "Request failed")
        assert _field(record, "user_id") == USER
        assert _field(record, "enterprise_id") == ENTERPRISE
        assert _field(record, "organization_id") == ORGANIZATION
        assert f"[enterprise: {ENTERPRISE}]" in record.getMessage()

    def test_an_account_in_no_organization_reports_no_organization(self, caplog):
        """``None``, not an invented value — the ordinary state of a personal
        account (ADR-017 D5), and the field must say so rather than go missing."""
        app = _app(principal=RequestPrincipal(user_id=USER, enterprise_id=ENTERPRISE))

        with caplog.at_level("DEBUG"):
            _get(app)

        record = _line(caplog, "Request completed")
        assert _field(record, "organization_id") is None
        assert _field(record, "enterprise_id") == ENTERPRISE


@pytest.mark.unit
@pytest.mark.security
class TestTheActorIsNeverTheCallersToChoose:
    """The heart of fm#1461. A session id is an assertion, not a credential."""

    def test_without_a_principal_the_line_names_nobody(self, caplog):
        """No binder ran — an unmatched route, or a middleware answering above
        the router. Nothing was verified, so nothing is claimed: not even
        ``anonymous``, which would assert that somebody looked."""
        app = _app(principal=None, session_user_id=SESSION_USER)

        with caplog.at_level("DEBUG"):
            _get(app, headers={"X-Session-ID": SESSION_ID})

        record = _line(caplog, "Request completed")
        assert _field(record, "user_id") is None
        assert _field(record, "enterprise_id") is None
        assert "[user:" not in record.getMessage()

    def test_a_principal_naming_no_user_is_recorded_as_anonymous(self, caplog):
        """The single-tenant arm binds an enterprise without reading the token,
        and the unauthenticated arm verifies nobody. Both are ``anonymous``:
        somebody looked, and there was no subject. The enterprise it DID bind
        is a fact and is still reported."""
        app = _app(
            principal=RequestPrincipal(user_id=None, enterprise_id=ENTERPRISE),
            session_user_id=SESSION_USER,
        )

        with caplog.at_level("DEBUG"):
            _get(app, headers={"X-Session-ID": SESSION_ID})

        record = _line(caplog, "Request completed")
        assert _field(record, "user_id") is None
        assert _field(record, "enterprise_id") == ENTERPRISE
        assert "[user: anonymous]" in record.getMessage()
        assert SESSION_USER not in record.getMessage()

    def test_the_failure_line_does_not_name_a_session_owner_either(self, caplog):
        """A 500 is exactly the line an incident review reads."""
        app = _app(
            principal=RequestPrincipal(user_id=None, enterprise_id=ENTERPRISE),
            session_user_id=SESSION_USER,
            boom=True,
        )

        with caplog.at_level("DEBUG"):
            _get(app, headers={"X-Session-ID": SESSION_ID})

        record = _line(caplog, "Request failed")
        assert _field(record, "user_id") is None
        assert SESSION_USER not in record.getMessage()

    def test_the_session_store_is_never_consulted_at_all(self, caplog):
        """Not "the answer is discarded" — the question is never asked.

        A lookup whose result is dropped is one edit away from being used
        again, and it spends a store round-trip on every request to produce a
        value nothing may read.
        """
        app = _app(principal=None, session_user_id=SESSION_USER)

        with caplog.at_level("DEBUG"):
            _get(app, headers={"X-Session-ID": SESSION_ID})

        app.state.session_service.get_session.assert_not_called()

    def test_the_verified_principal_is_what_the_line_says(self, caplog):
        """The other direction: a verified subject IS named, on every line."""
        app = _app(
            principal=RequestPrincipal(user_id=USER, enterprise_id=ENTERPRISE),
            session_user_id=SESSION_USER,
        )

        with caplog.at_level("DEBUG"):
            _get(app, headers={"X-Session-ID": SESSION_ID})

        record = _line(caplog, "Request completed")
        assert _field(record, "user_id") == USER


@pytest.mark.unit
@pytest.mark.security
class TestTheCallersValueIsKeptUnderACallersName:
    """Recorded, because correlating by it is useful. Named, so nobody reads
    it as established fact — the naming is the load-bearing part of fm#1461."""

    @pytest.mark.parametrize("needle", ["Request started", "Request completed"])
    def test_every_request_line_carries_it_as_claimed(self, caplog, needle):
        app = _app(principal=RequestPrincipal(user_id=USER, enterprise_id=ENTERPRISE))

        with caplog.at_level("DEBUG"):
            _get(app, headers={"X-Session-ID": SESSION_ID})

        record = _line(caplog, needle)
        assert _field(record, "claimed_session_id") == SESSION_ID
        assert _field(record, "session_id") is None, (
            "a caller-asserted value must not appear under the plain name, "
            "which trusted producers elsewhere in the app also write"
        )
        assert f"[claimed session: {SESSION_ID}]" in record.getMessage()

    def test_the_start_line_asserts_no_actor_at_all(self, caplog):
        """It is emitted before the binder runs, so it cannot know one."""
        app = _app(
            principal=RequestPrincipal(user_id=USER, enterprise_id=ENTERPRISE),
            session_user_id=SESSION_USER,
        )

        with caplog.at_level("DEBUG"):
            _get(app, headers={"X-Session-ID": SESSION_ID})

        record = _line(caplog, "Request started")
        assert _field(record, "user_id") is None
        assert "[user:" not in record.getMessage()
