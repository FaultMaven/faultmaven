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
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.config.settings import AuthSettings
from faultmaven.models.exceptions import InvalidGrantError, InvalidRequestError
from faultmaven.modules.auth.contracts import OAuthAuthorizationDTO
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


def _refresh_service(allowed=(COPILOT,), valid=True):
    generator = AsyncMock()
    generator.validate_refresh_token = AsyncMock(
        return_value={"sub": "user_1", "type": "refresh"} if valid else None
    )
    generator.generate_token_pair = AsyncMock(return_value=("access", "rotated"))
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


class TestClientLabel:
    """The fold itself, without ``prometheus_client``."""

    def test_a_configured_client_keeps_its_id(self):
        assert OAuthMetricsRecorder([COPILOT]).client_label(COPILOT) == COPILOT

    @pytest.mark.parametrize(
        "value", ["someone-else", "", None, 42, [COPILOT], {"x": 1}, b"bytes"]
    )
    def test_anything_else_is_unknown(self, value):
        assert OAuthMetricsRecorder([COPILOT]).client_label(value) == "unknown"

    def test_no_configured_clients_folds_everything(self):
        assert OAuthMetricsRecorder().client_label(COPILOT) == "unknown"


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
        raw_ids = _ids()

        before = _oauth_series()
        for i, client_id in enumerate(raw_ids):
            await service.refresh_access_token("valid", client_id=client_id)
            if i == 0:
                after_first = _oauth_series()

        _assert_bounded(before, after_first, _oauth_series(), set(raw_ids))

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


def test_the_token_route_labels_an_unknown_client_unknown():
    """``POST /token`` is unauthenticated: the id arrives straight off the wire."""
    pytest.importorskip("prometheus_client")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from faultmaven.api.exception_handlers import oauth_protocol_error_handler
    from faultmaven.models.exceptions import OAuthProtocolError
    from faultmaven.modules.auth.api.oauth import get_oauth_service, router
    from faultmaven.modules.auth.api.rate_limiting import (
        require_oauth_rate_limit_token,
    )

    service = _refresh_service(valid=False)
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(OAuthProtocolError, oauth_protocol_error_handler)
    app.dependency_overrides[get_oauth_service] = lambda: service
    app.dependency_overrides[require_oauth_rate_limit_token] = lambda: None
    raw_ids = _ids()[:3]
    unknown = {"client_id": "unknown", "error_code": "INVALID_REFRESH_TOKEN"}
    before_unknown = _value("oauth_refresh_errors_total", unknown)

    client = TestClient(app)  # a scratch app: no lifespan to run
    for client_id in raw_ids:
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

    assert _value("oauth_refresh_errors_total", unknown) == before_unknown + 3
    leaked = {
        value
        for _, labels in _oauth_series()
        for _, value in labels
        if value in raw_ids
    }
    assert leaked == set()
