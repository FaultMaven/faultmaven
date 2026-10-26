"""Every anonymous session minted is counted under exactly one (bearer, origin).

#1460 step 2. ``POST /api/v1/sessions`` mints an anonymous identity when no
bearer resolves to a user, and the server cannot require one until the mints
that send no ``Authorization`` header at all read zero. Those have to be told
apart from mints whose header resolves to nobody, and from mints no browser
panel made.

Driven through the booted application, so the middleware stack and the real
``get_current_user_optional`` decide which branch the route takes. The shim's
counter is a no-op unless ``ENABLE_METRICS`` is set at import, so a real
``prometheus_client.Counter`` on a private registry stands in for it; the route
reads it as ``session_metrics.<name>`` and sees the swap. ``prometheus_client``
ships with the ``cloud`` extra, so these run in the Test Cloud job.

Each mint's warning line is asserted beside the counter: step 3 counts that
line in the log store to catch mints no scrape saw, so a renamed or deleted
line would make that count read zero.
"""

import logging

import pytest

from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthService,
    RevocationStateUnknownError,
)
from faultmaven.modules.auth.infrastructure.metrics import session_metrics

prometheus_client = pytest.importorskip("prometheus_client")

pytestmark = pytest.mark.integration

NAME = "faultmaven_auth_session_anonymous_mints_total"
LOGGER = "faultmaven.modules.auth.api.session"
LINE = "Minted anonymous session ("
SERIES = [
    (bearer, origin)
    for bearer in session_metrics.BEARER_KINDS
    for origin in session_metrics.ORIGIN_KINDS
]


@pytest.fixture
def registry(monkeypatch):
    registry = prometheus_client.CollectorRegistry()
    counter = prometheus_client.Counter(
        NAME, "test double", ["bearer", "origin"], registry=registry
    )
    for bearer, origin in SERIES:
        counter.labels(bearer=bearer, origin=origin)
    monkeypatch.setattr(session_metrics, "session_anonymous_mints_total", counter)
    return registry


@pytest.fixture
def admin(booted_app_client):
    """A real access token from ``dev-login``, and the user it names."""
    login = booted_app_client.post("/api/v1/auth/dev-login", json={"username": "admin"})
    assert login.status_code == 200, login.text
    body = login.json()
    return body["access_token"], body["user"]["user_id"]


def _read(registry):
    return {
        (bearer, origin): registry.get_sample_value(
            NAME, {"bearer": bearer, "origin": origin}
        )
        for bearer, origin in SERIES
    }


def _mint(client, registry, caplog, headers):
    """Mint once: the response, every series that moved and by how much, and
    the minted-session warning lines the mint wrote."""
    caplog.set_level(logging.WARNING, logger=LOGGER)
    caplog.clear()
    before = _read(registry)
    response = client.post("/api/v1/sessions", json={}, headers=headers)
    after = _read(registry)
    moved = {s: after[s] - before[s] for s in SERIES if after[s] != before[s]}
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.getMessage().startswith(LINE)
    ]
    return response, moved, lines


@pytest.mark.parametrize(
    "headers, series",
    [
        ({}, ("absent", "none")),
        ({"Origin": "chrome-extension://abc"}, ("absent", "chrome-extension")),
        ({"Origin": "CHROME-EXTENSION://abc"}, ("absent", "chrome-extension")),
        (
            {"Authorization": "", "Origin": "moz-extension://x"},
            ("absent", "moz-extension"),
        ),
        ({"Origin": "safari-web-extension://x"}, ("absent", "safari-web-extension")),
        ({"Origin": "null"}, ("absent", "none")),
        (
            {"Authorization": "Bearer not-a-jwt", "Origin": "https://app.example"},
            ("unresolved", "web"),
        ),
        ({"Authorization": "Basic dXNlcjpwYXNz", "Origin": ""}, ("unresolved", "none")),
    ],
    ids=[
        "bare",
        "chrome",
        "chrome-mixed-case",
        "blank-header-moz",
        "safari",
        "opaque-origin",
        "bad-bearer-web",
        "basic",
    ],
)
def test_an_anonymous_mint_moves_exactly_one_series(
    booted_app_client, registry, caplog, headers, series
):
    response, moved, lines = _mint(booted_app_client, registry, caplog, headers)

    assert response.status_code == 201, response.text
    # The anonymous arm's own shape: the branch under test was the one taken.
    assert response.json()["user_id"].startswith("user_")
    assert moved == {series: 1.0}
    bearer, origin = series
    assert len(lines) == 1, lines
    assert f"bearer={bearer}, origin={origin}" in lines[0]


def test_an_authenticated_mint_moves_nothing(
    booted_app_client, registry, caplog, admin
):
    token, user_id = admin

    response, moved, lines = _mint(
        booted_app_client,
        registry,
        caplog,
        {"Authorization": f"Bearer {token}", "Origin": "chrome-extension://abc"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["user_id"] == user_id
    assert moved == {}
    assert lines == []


def test_an_unreadable_revocation_store_mints_and_counts_nothing(
    booted_app_client, registry, caplog, admin, monkeypatch
):
    """A 503 is not an anonymous mint: the store's failure must not fail open."""
    token, _ = admin

    async def _store_down(self, claims):
        raise RevocationStateUnknownError("OperationalError")

    monkeypatch.setattr(AuthService, "_is_revoked", _store_down)

    response, moved, lines = _mint(
        booted_app_client, registry, caplog, {"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 503, response.text
    assert response.headers["x-error-code"] == "REVOCATION_STATE_UNKNOWN"
    assert moved == {}
    assert lines == []


def test_a_failed_mint_counts_nothing(booted_app_client, registry, caplog, monkeypatch):
    """Counted after the store write, so a mint that fails is not a mint."""

    class _StoreDown:
        async def create_session(self, user_id, client_id=None, metadata=None):
            raise ConnectionError("session store unavailable")

    async def _service():
        return _StoreDown()

    monkeypatch.setitem(
        booted_app_client.app.dependency_overrides, get_session_service, _service
    )

    response, moved, lines = _mint(booted_app_client, registry, caplog, {})

    assert response.status_code == 500, response.text
    assert moved == {}
    assert lines == []
