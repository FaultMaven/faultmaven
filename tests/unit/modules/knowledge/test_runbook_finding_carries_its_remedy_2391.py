"""A refused runbook's every security finding says what to do (#2391).

Invariant: every error ``find_security_hazards`` returns ends with its remedy
sentence, and its warnings carry none. The legacy-pattern credential and the
three destructive commands carried none; the copy is the owner's ruling (a),
written here as literals so the tests pin it. Each route shows the finding:
the upload and edit 422 ``detail``, verify-batch's per-draft ``error``.

Mutation: revert the product change: every test but ``TestUnchanged`` fails.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.infrastructure.persistence.models import ConversionDraftModel
from faultmaven.modules.knowledge.api import conversion_routes as cr
from faultmaven.modules.knowledge.api import routes
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    enforce_runbook_quality,
    enforce_runbook_security,
    find_security_hazards,
)
from tests.unit.modules.knowledge.test_runbook_body_refused_not_rewritten_2012 import (  # noqa: E501
    _AWS,
    _body_with,
    _client,
    _conversion,
    _fragments,
    _knowledge,
    _template,
    session_factory,  # noqa: F401 - fixture
)

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base, pytest.mark.security]

CREDENTIAL_REMEDY = "Replace it with a placeholder, and rotate the credential."
DESTRUCTIVE_REMEDY = "Remove it, or replace it with a safe form."
_CRED = "Security: Potential hardcoded {} (hardcoded credential). " + CREDENTIAL_REMEDY
_RM = "recursive force-remove of a root/top-level/home path (rm -rf)"

#: ``(snippet, its one error)``. No shape pattern matches the legacy values.
KINDS = {
    "password": ('password = "hunter2-prod"', _CRED.format("password")),
    "api_key": ('api_key = "k3y-for-staging"', _CRED.format("API key")),
    "secret": ('secret = "s3cret-sauce"', _CRED.format("secret")),
    "token": ('token = "t0ken-of-trust"', _CRED.format("token")),
    "rm_rf": ("rm -rf /", f"Security: Dangerous command: {_RM}. {DESTRUCTIVE_REMEDY}"),
    "fork_bomb": (
        ":(){ :|:& };:",
        f"Security: Dangerous: fork bomb. {DESTRUCTIVE_REMEDY}",
    ),
    "dd": (
        "dd if=/dev/zero of=/dev/sda bs=1M",
        "Security: Potentially destructive: dd writing to a block device. "
        + DESTRUCTIVE_REMEDY,
    ),
}
_SHAPE = f"export AWS_ACCESS_KEY_ID={_AWS}"
#: Either remedy, or the shape finding's own ("… such as <X>, and rotate …").
_ENDS_WITH_A_REMEDY = re.compile(
    r"(?:Replace it with a placeholder(?: such as \S+)?, and rotate the "
    r"credential\.|Remove it, or replace it with a safe form\.)$"
)
ROUTED = pytest.mark.parametrize("kind", ["password", "fork_bomb"])


def _leaked(snippet: str, text: str) -> list[str]:
    value = re.search(r'"([^"]+)"', snippet)
    return [f for f in _fragments(value.group(1), 6) if f in text] if value else []


def test_every_error_over_every_hazard_kind_ends_with_its_remedy():
    body, _ = _body_with("\n".join([s for s, _ in KINDS.values()] + [_SHAPE]))

    errors, warnings = find_security_hazards(body)

    assert len(errors) == len(KINDS) + 1, errors  # one per kind, and the shape
    assert [e for e in errors if not _ENDS_WITH_A_REMEDY.search(e)] == []
    assert warnings == []


@pytest.mark.parametrize("kind", list(KINDS))
def test_each_kind_alone_is_one_error_with_the_approved_sentence(kind):
    snippet, expected = KINDS[kind]

    assert find_security_hazards(_body_with(snippet)[0]) == ([expected], [])


@ROUTED
def test_an_upload_says_it(kind):
    snippet, expected = KINDS[kind]

    async def upload_document(content, **_):
        enforce_runbook_quality(content)  # the service's gate
        return {"document_id": "kb_x"}

    response = _client(SimpleNamespace(upload_document=upload_document)).post(
        "/api/v1/knowledge/documents",
        data={"title": "T", "document_type": "runbook", "scope": "personal"},
        files={"file": ("rb.md", _body_with(snippet)[0].encode(), "text/markdown")},
    )

    assert response.status_code == 422, response.text
    assert expected in response.json()["detail"]
    assert _leaked(snippet, response.text) == []


@ROUTED
def test_an_edit_says_it(monkeypatch, kind):
    snippet, expected = KINDS[kind]

    async def update_document_metadata(document_id, **updates):
        enforce_runbook_security(updates["content"])  # the service's check
        return {"document_id": document_id}

    service = SimpleNamespace(
        get_document=AsyncMock(
            return_value={"document_id": "kb_x", "owner_id": "user-1"}
        ),
        update_document_metadata=update_document_metadata,
    )
    monkeypatch.setattr(routes, "_authorize_document_write", AsyncMock())

    response = _client(service).put(
        "/api/v1/knowledge/documents/kb_x", json={"content": _body_with(snippet)[0]}
    )

    assert response.status_code == 422, response.text
    assert expected in response.json()["detail"]
    assert _leaked(snippet, response.text) == []


@ROUTED
async def test_verify_batch_says_it(session_factory, tmp_path, monkeypatch, kind):
    """A draft whose ``validation_passed`` predates the check is refused by the
    security check at publish; the batch reports that finding."""
    snippet, expected = KINDS[kind]
    monkeypatch.chdir(tmp_path)
    conversion = _conversion(session_factory, _knowledge(session_factory))
    created = await conversion.create_runbook_from_template(**_template())
    ref = {
        "conversion_id": created["conversion_id"],
        "draft_id": created["draft"].draft_id,
    }
    async with session_factory() as session:
        row = await session.get(ConversionDraftModel, ref["draft_id"])
        row.content, row.validation_passed = _body_with(snippet)[0], True
        await session.commit()
    app = FastAPI()
    app.include_router(cr.router, prefix="/api/v1")
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    user = SimpleNamespace(user_id="u1", is_platform_admin=lambda: False)
    app.dependency_overrides[cr._require_auth] = lambda: user
    app.dependency_overrides[cr._get_conversion_service] = lambda: conversion

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        response = await c.post(
            "/api/v1/knowledge/drafts/verify-batch", json={"draft_ids": [ref]}
        )

    assert response.status_code == 200, response.text
    (item,) = response.json()["results"]
    assert item["status"] == "failed" and expected in item["error"], item
    assert _leaked(snippet, response.text) == []


class TestUnchanged:
    def test_a_placeholder_is_a_warning_with_no_remedy(self):
        errors, warnings = find_security_hazards(
            _body_with('password = "<PASSWORD>"')[0]
        )

        assert errors == [] and warnings
        remedies = (CREDENTIAL_REMEDY, DESTRUCTIVE_REMEDY)
        assert [w for w in warnings if any(r in w for r in remedies)] == []

    def test_the_shape_error_is_byte_identical(self):
        body, line = _body_with(_SHAPE)

        errors, _ = find_security_hazards(body)

        assert errors == [
            f"Security: hardcoded credential: an AWS access key ID on line {line} "
            "(AWS_ACCESS_KEY). Replace it with a placeholder such as "
            "<AWS_ACCESS_KEY_ID>, and rotate the credential."
        ]

    def test_three_catastrophic_rms_are_one_error(self):
        errors, _ = find_security_hazards(
            _body_with("rm -rf /\nrm -rf /etc\nrm -rf ~")[0]
        )

        assert len(errors) == 1 and "(rm -rf)" in errors[0], errors
