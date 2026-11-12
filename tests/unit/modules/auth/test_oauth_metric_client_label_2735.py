"""OAuth metrics never label a request-supplied client id (#2735).

The authorize path's invalid-client branch and the unauthenticated refresh
grant passed the raw request ``client_id`` to the recorder, which labelled the
OAuth counters with it: every distinct id a caller sent was a new series, held
in process memory until restart. The recorder now folds every client id through
``client_label``: a configured client keeps its id, anything else is
``unknown``.

The series tests need ``prometheus_client`` (``requirements/cloud.txt``), so
they run in the cloud slice and skip elsewhere; the ``client_label`` tests run
everywhere.
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.config.settings import AuthSettings
from faultmaven.models.exceptions import InvalidGrantError, InvalidRequestError
from faultmaven.modules.auth.contracts import OAuthAuthorizationDTO
from faultmaven.modules.auth.domain.services.jwt_token_generator import RefreshClaim
from faultmaven.modules.auth.domain.services.oauth_service import OAuthServiceImpl
from faultmaven.modules.auth.infrastructure.metrics.oauth_metrics import (
    OAuthMetricsRecorder,
)
from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (
    InMemoryOAuthCodeRepository,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

N = 25
COPILOT = "faultmaven-copilot"
SLACK_AGENT = "faultmaven-slack-agent"
REDIRECT = "chrome-extension://abc123/callback.html"
_CLIENT_LABELS = ("client_id", "attempted_client_id")


def _service(allowed=(COPILOT,), generator=None, users=None):
    return OAuthServiceImpl(
        code_repository=InMemoryOAuthCodeRepository(),
        user_repository=users,
        token_generator=generator,
        settings=AuthSettings(
            oauth_allowed_clients=list(allowed),
            oauth_redirect_uri_patterns=[
                r"^chrome-extension://[a-z0-9]+/callback\.html$"
            ],
        ),
    )


def _authorize(client_id):
    return OAuthAuthorizationDTO(
        client_id=client_id,
        redirect_uri=REDIRECT,
        state="state",
        code_challenge="c" * 43,
    )


def _refresh_service(allowed=(COPILOT,), valid=True, claims=()):
    generator = AsyncMock()
    generator.validate_refresh_token = AsyncMock(
        return_value=(
            {"sub": "user_1", "type": "refresh", **dict(claims)} if valid else None
        )
    )
    generator.generate_token_pair = AsyncMock(return_value=("access", "rotated"))
    generator.claim_refresh_token = AsyncMock(return_value=RefreshClaim.CLAIMED)
    users = AsyncMock()
    users.get = AsyncMock(
        return_value=SimpleNamespace(
            user_id="user_1", username="u", is_active=True, enterprise_id=None
        )
    )
    return _service(allowed, generator=generator, users=users)


def _ids():
    tag = uuid.uuid4().hex[:12]
    return [f"attacker-{tag}-{i}" for i in range(N)]


def _oauth_series():
    """Every (sample name, labels) of every ``oauth_*`` family on the registry."""
    from prometheus_client import REGISTRY

    return {
        (sample.name, tuple(sorted(sample.labels.items())))
        for family in REGISTRY.collect()
        if family.name.startswith("oauth_")
        for sample in family.samples
    }


def _assert_bounded(before, after_first, after_all, raw_ids):
    """No growth past the first refusal; new series are ``unknown``; no raw id."""
    assert after_all == after_first
    for _, labels in after_all - before:
        for name, value in labels:
            if name in _CLIENT_LABELS:
                assert value == "unknown", labels
    leaked = {
        value for _, labels in after_all for _, value in labels if value in raw_ids
    }
    assert leaked == set()


def _value(name, labels):
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels) or 0.0


class _UnhashableStr(str):
    """A ``str`` whose hashing raises: ``in`` on a set would raise TypeError."""

    __hash__ = None


class TestClientLabel:
    """The fold itself, without ``prometheus_client``."""

    def test_a_configured_client_keeps_its_id(self):
        assert OAuthMetricsRecorder([COPILOT]).client_label(COPILOT) == COPILOT

    @pytest.mark.parametrize(
        "value",
        [
            "someone-else",
            "",
            None,
            42,
            [COPILOT],
            {"x": 1},
            b"bytes",
            pytest.param(_UnhashableStr(COPILOT), id="unhashable-str-subclass"),
        ],
    )
    def test_anything_else_is_unknown(self, value):
        assert OAuthMetricsRecorder([COPILOT]).client_label(value) == "unknown"

    def test_no_configured_clients_folds_everything(self):
        assert OAuthMetricsRecorder().client_label(COPILOT) == "unknown"

    def test_a_bare_str_of_clients_is_refused(self):
        with pytest.raises(TypeError):
            OAuthMetricsRecorder(COPILOT)


#: Each recorder method that writes a client label, called on every branch that
#: writes one.
_RECORDS = {
    "record_authorization_request": lambda r, cid: (
        r.record_authorization_request(cid, False, error_code="INVALID_CLIENT"),
        r.record_authorization_request(cid, True),
    ),
    "record_token_exchange": lambda r, cid: r.record_token_exchange(
        "authorization_code", cid, 0.01, True
    ),
    "record_token_refresh": lambda r, cid: (
        r.record_token_refresh(cid, False, error_code="INVALID_REFRESH_TOKEN"),
        r.record_token_refresh(cid, True),
    ),
    "record_pkce_failure": lambda r, cid: r.record_pkce_failure(cid),
    "record_code_replay_attempt": lambda r, cid: r.record_code_replay_attempt(cid),
    "record_redirect_uri_mismatch": lambda r, cid: r.record_redirect_uri_mismatch(cid),
    "record_code_expired": lambda r, cid: r.record_code_expired(cid),
}


@pytest.mark.parametrize("method", sorted(_RECORDS))
def test_no_recorder_method_labels_a_raw_id(method):
    """Every writer folds, not only the ones the service reaches with raw ids."""
    pytest.importorskip("prometheus_client")
    recorder = OAuthMetricsRecorder([COPILOT])
    raw_ids = _ids()

    before = _oauth_series()
    for i, client_id in enumerate(raw_ids):
        _RECORDS[method](recorder, client_id)
        if i == 0:
            after_first = _oauth_series()

    _assert_bounded(before, after_first, _oauth_series(), set(raw_ids))


class TestAuthorizeSeries:
    async def test_distinct_invalid_ids_add_no_series(self):
        pytest.importorskip("prometheus_client")
        service = _service()
        raw_ids = _ids()

        before = _oauth_series()
        for i, client_id in enumerate(raw_ids):
            with pytest.raises(InvalidRequestError):
                await service.create_authorization_code("user_1", _authorize(client_id))
            if i == 0:
                after_first = _oauth_series()

        _assert_bounded(before, after_first, _oauth_series(), set(raw_ids))

    async def test_an_allowed_client_keeps_its_label(self):
        pytest.importorskip("prometheus_client")
        labels = {"client_id": COPILOT, "status": "success"}
        before = _value("oauth_authorization_requests_total", labels)

        await _service().create_authorization_code("user_1", _authorize(COPILOT))

        assert _value("oauth_authorization_requests_total", labels) == before + 1


class TestRefreshSeries:
    async def test_distinct_ids_on_a_refused_refresh_add_no_series(self):
        pytest.importorskip("prometheus_client")
        service = _refresh_service(valid=False)
        raw_ids = _ids()

        before = _oauth_series()
        for i, client_id in enumerate(raw_ids):
            with pytest.raises(InvalidGrantError):
                await service.refresh_access_token("garbage", client_id=client_id)
            if i == 0:
                after_first = _oauth_series()

        _assert_bounded(before, after_first, _oauth_series(), set(raw_ids))

    async def test_distinct_ids_on_a_successful_refresh_add_no_series(self):
        pytest.importorskip("prometheus_client")
        service = _refresh_service()
        # Each token is bound to the id it is presented under (#2750).
        service.token_generator.validate_refresh_token.side_effect = lambda token: {
            "sub": "user_1",
            "type": "refresh",
            "client_id": token,
        }
        raw_ids = _ids()

        before = _oauth_series()
        for i, client_id in enumerate(raw_ids):
            await service.refresh_access_token(client_id, client_id=client_id)
            if i == 0:
                after_first = _oauth_series()

        _assert_bounded(before, after_first, _oauth_series(), set(raw_ids))

    async def test_distinct_ids_refused_by_a_bound_token_add_no_series(self):
        """A client mismatch (#2750) labels the presented id through the fold."""
        pytest.importorskip("prometheus_client")
        service = _refresh_service(claims={"client_id": COPILOT})
        raw_ids = _ids()
        labels = {"client_id": "unknown", "error_code": "CLIENT_MISMATCH"}
        before_mismatch = _value("oauth_refresh_errors_total", labels)

        before = _oauth_series()
        for i, client_id in enumerate(raw_ids):
            with pytest.raises(InvalidGrantError):
                await service.refresh_access_token("bound", client_id=client_id)
            if i == 0:
                after_first = _oauth_series()

        _assert_bounded(before, after_first, _oauth_series(), set(raw_ids))
        assert _value("oauth_refresh_errors_total", labels) == before_mismatch + N

    @pytest.mark.parametrize(
        "allowed, client_id",
        [((COPILOT,), COPILOT), ((COPILOT, SLACK_AGENT), SLACK_AGENT)],
        ids=["default-client", "client-added-to-allowlist"],
    )
    async def test_an_allowed_client_keeps_its_label(self, allowed, client_id):
        pytest.importorskip("prometheus_client")
        labels = {"client_id": client_id, "status": "success"}
        before = _value("oauth_tokens_refreshed_total", labels)

        await _refresh_service(allowed).refresh_access_token("valid", client_id)

        assert _value("oauth_tokens_refreshed_total", labels) == before + 1


def _scratch_app(service, *overrides):
    """The OAuth router on a scratch app, with *overrides* answering nothing."""
    from fastapi import FastAPI

    from faultmaven.api.exception_handlers import oauth_protocol_error_handler
    from faultmaven.models.exceptions import OAuthProtocolError
    from faultmaven.modules.auth.api.oauth import get_oauth_service, router

    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(OAuthProtocolError, oauth_protocol_error_handler)
    app.dependency_overrides[get_oauth_service] = lambda: service
    for dependency, answer in overrides:
        app.dependency_overrides[dependency] = answer
    return app


def _drive(client, send, raw_ids):
    """Send each id; return the series before, after the first, and after all."""
    before = _oauth_series()
    for i, client_id in enumerate(raw_ids):
        send(client, client_id)
        if i == 0:
            after_first = _oauth_series()
    return before, after_first, _oauth_series()


def test_the_authorize_route_adds_no_series_for_unknown_clients():
    pytest.importorskip("prometheus_client")
    from fastapi.testclient import TestClient

    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.auth.api.rate_limiting import (
        require_oauth_rate_limit_authorize,
    )
    from faultmaven.modules.auth.domain.models.auth import DevUser

    user = DevUser(
        user_id="user_1",
        username="u",
        email="u@example.com",
        display_name="U",
        created_at=datetime.now(timezone.utc),
    )
    app = _scratch_app(
        _service(),
        (require_authentication, lambda: user),
        (require_oauth_rate_limit_authorize, lambda: None),
    )
    raw_ids = _ids()
    refused = {"attempted_client_id": "unknown"}
    before_refused = _value("oauth_invalid_client_attempts_total", refused)

    def send(client, client_id):
        response = client.get(
            "/auth/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "state": "state",
                "code_challenge": "c" * 43,
            },
        )
        assert response.status_code == 400

    client = TestClient(app)  # a scratch app: no lifespan to run
    _assert_bounded(*_drive(client, send, raw_ids), set(raw_ids))
    assert _value("oauth_invalid_client_attempts_total", refused) == (
        before_refused + N
    )


def test_the_token_route_labels_an_unknown_client_unknown():
    """``POST /token`` is unauthenticated: the id arrives straight off the wire."""
    pytest.importorskip("prometheus_client")
    from fastapi.testclient import TestClient

    from faultmaven.modules.auth.api.rate_limiting import (
        require_oauth_rate_limit_token,
    )

    app = _scratch_app(
        _refresh_service(valid=False), (require_oauth_rate_limit_token, lambda: None)
    )
    raw_ids = _ids()
    unknown = {"client_id": "unknown", "error_code": "INVALID_REFRESH_TOKEN"}
    before_unknown = _value("oauth_refresh_errors_total", unknown)

    def send(client, client_id):
        response = client.post(
            "/auth/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": "garbage",
                "client_id": client_id,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"

    client = TestClient(app)  # a scratch app: no lifespan to run
    _assert_bounded(*_drive(client, send, raw_ids), set(raw_ids))
    assert _value("oauth_refresh_errors_total", unknown) == before_unknown + N
