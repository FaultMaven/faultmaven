"""A verified runbook names its verifier by their account's display name (#1924).

``verified_by`` is written into the runbook's frontmatter and published with it
to everyone at its scope (team or global). It used to be the verifier's
username, which for an SSO account is the email's local part.

The name must come from the verifier's users ROW. The request's principal is
no source: ``get_current_user_optional`` builds it from the JWT, which carries
no display name, so its ``display_name`` IS the username claim. These tests
therefore drive the real route with the real JWT dependency, so a fix that read
the principal would fail here rather than pass on a hand-built ``DevUser``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.middleware.auth import get_auth_service
from faultmaven.api.v1.auth_dependencies import get_current_user_optional
from faultmaven.infrastructure.persistence.user_repository import User
from faultmaven.modules.knowledge.api import conversion_routes as cr
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.unit, pytest.mark.api]

USER_ID = "3f9a1c2e-0000-4000-8000-000000000001"
ENTERPRISE_ID = "ent-1924"
#: The JWT's ``username`` claim: an SSO username is the email's local part.
USERNAME = "alice.smith"
EMAIL = "alice.smith@example.com"
_NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
#: The name on the account row. Quotes and a backslash, so the YAML written
#: must escape them to read back as this one value.
ROW_NAME = 'Alice "Ace" O\'Neil \\ Ops'

DRAFT = """---
id: rb-1924
title: "Disk full"
status: draft
verified_by: ""
---

# Runbook: Disk full

Body.
"""


class _AuthService:
    """Verifies any token to the claims a local-mode access token carries."""

    async def verify_token_with_revocation_check(self, token, token_type="access"):
        return {
            "sub": USER_ID,
            "username": USERNAME,
            "email": EMAIL,
            "roles": ["user"],
            "auth_mode": "local",
        }


class _AccountReader:
    """The user repository's ``get_many_in_enterprise``, over one full row: the
    repository's own ``User`` model, username and email included, so a writer
    that published either instead of the display name is caught here."""

    def __init__(self):
        self.calls: list = []

    async def get_many_in_enterprise(self, enterprise_id, user_ids):
        self.calls.append((enterprise_id, list(user_ids)))
        row = User(
            user_id=USER_ID,
            enterprise_id=ENTERPRISE_ID,
            username=USERNAME,
            email=EMAIL,
            display_name=ROW_NAME,
            sso_provider="workos",
            sso_provider_id="user_wos_1924",
            created_at=_NOW,
            updated_at=_NOW,
        )
        return [row] if enterprise_id == ENTERPRISE_ID and USER_ID in user_ids else []


def _draft_row() -> MagicMock:
    """The draft row: its text is its ``content`` column (#1950)."""
    draft = MagicMock()
    draft.id = "draft_1924"
    draft.runbook_id = "rb-1924"
    draft.title = "Disk full"
    draft.status = "draft"
    draft.content = DRAFT
    draft.validation_passed = True
    return draft


def _session_factory(draft):
    job = MagicMock()
    job.id = "conv_x"
    job.user_id = USER_ID
    job.scope = "team"
    job.enterprise_id = ENTERPRISE_ID

    async def _execute(stmt):
        result = MagicMock()
        if stmt.is_dml:
            # Verify's compare-and-swap (#1950): it writes the row only if the
            # row still holds the text verify read.
            params = stmt.compile().params
            swapped = draft.status == "draft" and params["content_1"] == draft.content
            if swapped:
                draft.content = params["content"]
                draft.status = params["status"]
            result.rowcount = 1 if swapped else 0
            return result
        table = stmt.get_final_froms()[0].name
        result.scalar_one_or_none.return_value = (
            job if table == "conversion_jobs" else draft
        )
        return result

    session = AsyncMock()
    session.execute = _execute

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, *_):
            return None

    return _Factory()


def _service(account_reader, draft) -> ConversionService:
    knowledge = AsyncMock()
    knowledge.ingest_runbook.return_value = 3
    return ConversionService(
        sanitizer=regex_only_sanitizer(),
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=_session_factory(draft),
        knowledge_service=knowledge,
        account_reader=account_reader,
    )


def _client(service: ConversionService) -> TestClient:
    """The real conversion router behind the real JWT dependency."""
    app = FastAPI()
    app.include_router(cr.router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[cr._get_conversion_service] = lambda: service
    app.dependency_overrides[get_auth_service] = lambda: _AuthService()
    return TestClient(app, headers={"Authorization": "Bearer t"})


@pytest.fixture
def draft():
    return _draft_row()


def _verified_by(text: str) -> object:
    return frontmatter.loads(text).metadata["verified_by"]


def _published(service: ConversionService) -> str:
    """The text verify handed to ingestion: what search will serve."""
    return service._knowledge_service.ingest_runbook.call_args.kwargs["content"]


async def test_the_jwt_principal_carries_the_username_as_its_display_name():
    """The premise, measured: a fix reading the principal would publish the
    username. Without this the route tests below could pass for a reason that
    no longer holds."""
    user = await get_current_user_optional(
        request=MagicMock(), token="t", auth_service=_AuthService()
    )
    assert user.display_name == USERNAME


class TestTheRouteWritesTheRowsName:
    def test_verify(self, draft):
        reader = _AccountReader()
        service = _service(reader, draft)

        response = _client(service).post(
            "/knowledge/conversions/conv_x/drafts/draft_1924/verify"
        )

        assert response.status_code == 200, response.text
        # The row's text and the published text: one and the same, and the
        # name round-trips through YAML as this one value, quotes and all.
        assert draft.content == _published(service)
        assert _verified_by(draft.content) == ROW_NAME
        assert frontmatter.loads(draft.content).metadata["status"] == "verified"
        assert reader.calls == [(ENTERPRISE_ID, [USER_ID])]
        assert USERNAME not in draft.content

    def test_verify_batch(self, draft):
        response = _client(_service(_AccountReader(), draft)).post(
            "/knowledge/drafts/verify-batch",
            json={"draft_ids": [{"conversion_id": "conv_x", "draft_id": "draft_1924"}]},
        )

        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["status"] == "verified"
        assert _verified_by(draft.content) == ROW_NAME


def test_no_account_read_leaves_the_field_empty_not_the_username(draft):
    response = _client(_service(None, draft)).post(
        "/knowledge/conversions/conv_x/drafts/draft_1924/verify"
    )

    assert response.status_code == 200, response.text
    assert _verified_by(draft.content) == ""
    assert USERNAME not in draft.content


def test_an_account_outside_the_jobs_enterprise_is_not_named(draft):
    class _Elsewhere(_AccountReader):
        async def get_many_in_enterprise(self, enterprise_id, user_ids):
            return []

    response = _client(_service(_Elsewhere(), draft)).post(
        "/knowledge/conversions/conv_x/drafts/draft_1924/verify"
    )

    assert response.status_code == 200, response.text
    assert _verified_by(draft.content) == ""
