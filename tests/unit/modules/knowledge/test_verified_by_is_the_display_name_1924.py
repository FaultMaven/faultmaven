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

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.middleware.auth import get_auth_service
from faultmaven.api.v1.auth_dependencies import get_current_user_optional
from faultmaven.modules.knowledge.api import conversion_routes as cr
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)

pytestmark = [pytest.mark.unit, pytest.mark.api]

USER_ID = "3f9a1c2e-0000-4000-8000-000000000001"
ENTERPRISE_ID = "ent-1924"
#: The JWT's ``username`` claim: an SSO username is the email's local part.
USERNAME = "alice.smith"
#: The name on the account row. Quotes and a backslash, so the YAML written
#: must escape them to read back as this one value.
ROW_NAME = 'Alice "Ace" O\'Neil \\ Ops'
DRAFT_PATH = "data/knowledge/team/rb.md"

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
            "email": "alice.smith@example.com",
            "roles": ["user"],
            "auth_mode": "local",
        }


class _AccountReader:
    """The user repository's ``get_many_in_enterprise``, over one row."""

    def __init__(self):
        self.calls: list = []

    async def get_many_in_enterprise(self, enterprise_id, user_ids):
        self.calls.append((enterprise_id, list(user_ids)))
        row = SimpleNamespace(
            user_id=USER_ID, enterprise_id=ENTERPRISE_ID, display_name=ROW_NAME
        )
        return [row] if enterprise_id == ENTERPRISE_ID and USER_ID in user_ids else []


def _session_factory():
    job = MagicMock()
    job.id = "conv_x"
    job.user_id = USER_ID
    job.scope = "team"
    job.enterprise_id = ENTERPRISE_ID

    draft = MagicMock()
    draft.id = "draft_1924"
    draft.runbook_id = "rb-1924"
    draft.title = "Disk full"
    draft.status = "draft"
    draft.file_path = DRAFT_PATH
    draft.validation_passed = True

    async def _execute(stmt):
        result = MagicMock()
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


def _service(account_reader) -> ConversionService:
    knowledge = AsyncMock()
    knowledge.ingest_runbook.return_value = 3
    return ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=_session_factory(),
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
def draft_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / DRAFT_PATH
    path.parent.mkdir(parents=True)
    path.write_text(DRAFT, encoding="utf-8")
    return path


def _verified_by(path) -> object:
    return frontmatter.load(str(path)).metadata["verified_by"]


async def test_the_jwt_principal_carries_the_username_as_its_display_name():
    """The premise, measured: a fix reading the principal would publish the
    username. Without this the route tests below could pass for a reason that
    no longer holds."""
    user = await get_current_user_optional(
        request=MagicMock(), token="t", auth_service=_AuthService()
    )
    assert user.display_name == USERNAME


class TestTheRouteWritesTheRowsName:
    def test_verify(self, draft_file):
        reader = _AccountReader()

        response = _client(_service(reader)).post(
            "/knowledge/conversions/conv_x/drafts/draft_1924/verify"
        )

        assert response.status_code == 200, response.text
        assert _verified_by(draft_file) == ROW_NAME
        assert reader.calls == [(ENTERPRISE_ID, [USER_ID])]
        assert USERNAME not in draft_file.read_text(encoding="utf-8")

    def test_verify_batch(self, draft_file):
        response = _client(_service(_AccountReader())).post(
            "/knowledge/drafts/verify-batch",
            json={"draft_ids": [{"conversion_id": "conv_x", "draft_id": "draft_1924"}]},
        )

        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["status"] == "verified"
        assert _verified_by(draft_file) == ROW_NAME


def test_the_fallback_writer_escapes_the_name(draft_file, monkeypatch):
    """When the frontmatter library fails, the text fallback writes the name
    into a double-quoted YAML scalar. Unescaped, a ``"`` in the name ended the
    scalar early and the frontmatter no longer parsed as written (#1924)."""

    def _broken_load(*_args, **_kwargs):
        raise RuntimeError("frontmatter unavailable")

    monkeypatch.setattr(frontmatter, "load", _broken_load)

    response = _client(_service(_AccountReader())).post(
        "/knowledge/conversions/conv_x/drafts/draft_1924/verify"
    )

    assert response.status_code == 200, response.text
    text = draft_file.read_text(encoding="utf-8")
    block = text.split("---\n")[1]
    meta = yaml.safe_load(block)
    assert meta["verified_by"] == ROW_NAME
    assert meta["status"] == "verified"


def test_no_account_read_leaves_the_field_empty_not_the_username(draft_file):
    response = _client(_service(None)).post(
        "/knowledge/conversions/conv_x/drafts/draft_1924/verify"
    )

    assert response.status_code == 200, response.text
    assert _verified_by(draft_file) == ""
    assert USERNAME not in draft_file.read_text(encoding="utf-8")


def test_an_account_outside_the_jobs_enterprise_is_not_named(draft_file):
    class _Elsewhere(_AccountReader):
        async def get_many_in_enterprise(self, enterprise_id, user_ids):
            return []

    response = _client(_service(_Elsewhere())).post(
        "/knowledge/conversions/conv_x/drafts/draft_1924/verify"
    )

    assert response.status_code == 200, response.text
    assert _verified_by(draft_file) == ""
