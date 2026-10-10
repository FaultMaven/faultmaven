"""The runbook taxonomy has one owner, and every reader derives from it (#1886).

``faultmaven.modules.knowledge.taxonomy`` is the one copy in code of the six
closed frontmatter vocabularies the spec defines
(``runbook-content-architecture.md`` §Taxonomy Schema). Before it, each
vocabulary was hand-copied into the validator, the prompts, the ORM CHECKs and
the clients, and the ``conversion_drafts`` severity CHECK lost ``info``.

These tests pin each edge of "one owner":

* the spec table and the enums hold the same values in the same order (the
  table is parsed, so a doc edit and a code edit cannot drift silently);
* the ORM CHECKs admit exactly the enum's values (the migrated database's
  CHECKs are pinned in ``test_runbook_taxonomy_closed_loop.py``);
* the validator refuses what the enum does not hold — exactly, including a
  case variant and a non-string, which used to pass it and then fail the
  column's CHECK at verify;
* the prompts render the enum, not a literal;
* the manual-create request is typed with the enums: an off-vocabulary value
  is a 422, and the schema publishes the allowed values;
* the reranker weights every lifecycle status.

The closed loop itself — every allowed value validated, stored and verified —
is ``tests/integration/modules/knowledge/test_runbook_taxonomy_closed_loop.py``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    _STATUS_WEIGHTS,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.api import conversion_routes as cr
from faultmaven.modules.knowledge.domain.services.conversion_service.prompts import (
    ANALYSIS_SYSTEM_PROMPT,
    CONVERSION_SYSTEM_PROMPT,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookValidator,
)
from faultmaven.modules.knowledge.taxonomy import (
    TAXONOMY_FIELDS,
    KnowledgeScope,
    RunbookDifficulty,
    RunbookDomain,
    RunbookSeverity,
    RunbookStatus,
    SymptomClass,
    member_value,
    render_vocabulary,
    vocabulary,
)
from tests.runbook_samples import valid_runbook
from tests.taxonomy_spec import SPEC, spec_vocabularies

pytestmark = pytest.mark.unit


#: Every CHECK built from a vocabulary: (table, constraint, column, enum).
CONSTRAINED_COLUMNS = [
    (
        "conversion_drafts",
        "conversion_drafts_severity_check",
        "severity",
        RunbookSeverity,
    ),
    ("knowledge_items", "knowledge_items_scope_check", "scope", KnowledgeScope),
    ("conversion_jobs", "conversion_jobs_scope_check", "scope", KnowledgeScope),
]


def _check_values(sqltext: str, column: str) -> list[str]:
    """The quoted values of ``<column> IN (...)`` in a CHECK, in order."""
    match = re.search(rf"\b{column} IN \(([^)]*)\)", sqltext)
    assert match, sqltext
    return re.findall(r"'([^']*)'", match.group(1))


# ---------------------------------------------------------------------------
# The spec and the enums
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field,enum_cls", TAXONOMY_FIELDS, ids=lambda x: str(x))
def test_every_vocabulary_is_the_spec_table(field: str, enum_cls: type[Enum]):
    """Same values, same order. Mutation: drop ``INFO`` from
    ``RunbookSeverity`` (or a value from the table) and this fails."""
    assert list(vocabulary(enum_cls)) == spec_vocabularies()[field]


def test_the_spec_table_is_read_at_all():
    """A positive control: a parser that found no rows would pass the test
    above vacuously for every field it then failed to look up."""
    table = spec_vocabularies()
    assert {field for field, _ in TAXONOMY_FIELDS} <= set(table)
    assert "info" in table["severity"]


def test_the_spec_names_the_code_owner():
    assert "faultmaven/modules/knowledge/taxonomy.py" in SPEC.read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# The ORM's CHECK constraints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table,name,column,enum_cls", CONSTRAINED_COLUMNS, ids=lambda x: str(x)
)
def test_orm_check_admits_exactly_the_vocabulary(table, name, column, enum_cls):
    """Mutation: hard-code the baseline's four-value severity CHECK in the ORM
    and this fails."""
    (constraint,) = [
        c for c in Base.metadata.tables[table].constraints if c.name == name
    ]
    assert _check_values(str(constraint.sqltext), column) == list(vocabulary(enum_cls))


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


def _with(field: str, value: str) -> str:
    """The sample runbook with one frontmatter line replaced or added."""
    content = valid_runbook("Connection Pool Exhausted On The API")
    head, body = content.split("\n---\n", 1)
    lines = [line for line in head.splitlines() if not line.startswith(f"{field}:")]
    lines.append(f"{field}: {value}")
    return "\n".join(lines) + "\n---\n" + body


def _errors_for(content: str, field: str) -> list[str]:
    result = RunbookValidator().validate_content(content)
    return [e for e in result.errors if f"Invalid {field}" in e]


SCALAR_FIELDS = [
    (field, enum_cls) for field, enum_cls in TAXONOMY_FIELDS if field != "symptom_class"
]


@pytest.mark.parametrize("field,enum_cls", SCALAR_FIELDS, ids=lambda x: str(x))
def test_the_validator_passes_every_allowed_value(field, enum_cls):
    for value in vocabulary(enum_cls):
        assert _errors_for(_with(field, value), field) == [], value


@pytest.mark.parametrize("field,enum_cls", SCALAR_FIELDS, ids=lambda x: str(x))
@pytest.mark.parametrize(
    "bad",
    ["bogus", "UPPER", "3"],
    ids=["off-vocabulary", "case-variant", "non-string"],
)
def test_the_validator_rejects_a_value_outside_the_enum(field, enum_cls, bad):
    """``UPPER`` stands for the first allowed value upper-cased: the gate used
    to lower-case before checking and so passed ``High``, which the column's
    CHECK then refused at verify. ``3`` is a YAML int, which the gate used to
    skip. ``difficulty`` is optional and was never checked at all."""
    value = vocabulary(enum_cls)[0].upper() if bad == "UPPER" else bad
    errors = _errors_for(_with(field, value), field)
    assert errors, f"{field}: {value!r} passed"
    # The allowed list the author is shown is the spec's, whole.
    assert ", ".join(spec_vocabularies()[field]) in errors[0]


def test_the_validator_rejects_an_off_vocabulary_symptom_class():
    result = RunbookValidator().validate_content(
        _with("symptom_class", "[split_brain]")
    )
    assert any("Invalid symptom_class 'split_brain'" in e for e in result.errors)


def test_member_value_admits_only_exact_members():
    assert member_value(RunbookSeverity, "info") == "info"
    for raw in ("Info", "urgent", None, 3, ""):
        assert member_value(RunbookSeverity, raw) is None


# ---------------------------------------------------------------------------
# The prompts
# ---------------------------------------------------------------------------


# Each prompt is compared with the SPEC's vocabulary, never with
# ``render_vocabulary`` of the enum: that would compare the code with itself,
# and a renderer that dropped ``info`` would pass (#1886 review).


def test_the_analysis_prompt_offers_the_spec_vocabularies():
    spec = spec_vocabularies()
    assert f'"severity": "{"|".join(spec["severity"])}"' in ANALYSIS_SYSTEM_PROMPT
    assert f'"domain": "{"|".join(spec["domain"])}"' in ANALYSIS_SYSTEM_PROMPT
    assert ", ".join(spec["symptom_class"]) in ANALYSIS_SYSTEM_PROMPT
    assert "__" not in ANALYSIS_SYSTEM_PROMPT.replace("__init__", "")


def test_the_conversion_prompt_offers_the_spec_symptom_vocabulary():
    assert ", ".join(spec_vocabularies()["symptom_class"]) in CONVERSION_SYSTEM_PROMPT
    assert "__SYMPTOM_CLASS_VOCAB__" not in CONVERSION_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# The request model
# ---------------------------------------------------------------------------


def _author() -> DevUser:
    return DevUser(
        user_id="u1",
        username="author",
        email="author@example.com",
        display_name="Author",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        roles=["user"],
    )


@pytest.fixture
def create_client():
    app = FastAPI()
    app.include_router(cr.router)
    service = MagicMock()
    service.create_runbook_from_template = AsyncMock(
        return_value={
            "conversion_id": "conv_1",
            "draft": MagicMock(model_dump=lambda: {"draft_id": "draft_1"}),
        }
    )
    app.dependency_overrides[cr._get_conversion_service] = lambda: service
    app.dependency_overrides[cr._require_auth] = _author
    return TestClient(app), service


_BODY = {
    "title": "Connection Pool Exhausted On The API",
    "domain": "database",
    "service": "postgresql",
    "symptom_class": ["latency"],
    "severity": "info",
    "scope": "personal",
    "symptom_recognition": "x" * 20,
    "applicability": "x" * 20,
    "diagnostic_steps": "x" * 20,
    "causes": "x" * 20,
    "prevention": "x" * 20,
}


@pytest.mark.parametrize(
    "field,bad",
    [
        ("severity", "urgent"),
        ("severity", "High"),
        ("domain", "kubernetes"),
        ("scope", "everyone"),
        ("difficulty", "trivial"),
        ("symptom_class", ["split_brain"]),
    ],
)
def test_the_create_request_refuses_an_off_vocabulary_value(create_client, field, bad):
    client, service = create_client
    resp = client.post("/knowledge/runbooks/create", json={**_BODY, field: bad})
    assert resp.status_code == 422, resp.text
    service.create_runbook_from_template.assert_not_awaited()


def test_the_create_request_hands_the_service_plain_values(create_client):
    """The service writes these into frontmatter with an f-string, and a
    ``str`` enum member formats as ``RunbookSeverity.INFO`` there."""
    client, service = create_client
    resp = client.post("/knowledge/runbooks/create", json=_BODY)
    assert resp.status_code == 201, resp.text
    kwargs = service.create_runbook_from_template.await_args.kwargs
    for key in ("domain", "severity", "scope", "difficulty"):
        assert type(kwargs[key]) is str, key
    assert kwargs["severity"] == "info"
    assert kwargs["difficulty"] == "intermediate"
    assert [type(item) for item in kwargs["symptom_class"]] == [str]


def test_the_create_request_schema_publishes_the_vocabularies():
    schema = cr.RunbookCreateRequest.model_json_schema()
    defs = schema["$defs"]
    assert defs["RunbookSeverity"]["enum"] == list(vocabulary(RunbookSeverity))
    assert defs["RunbookDomain"]["enum"] == list(vocabulary(RunbookDomain))
    assert defs["KnowledgeScope"]["enum"] == list(vocabulary(KnowledgeScope))
    assert defs["SymptomClass"]["enum"] == list(vocabulary(SymptomClass))


# ---------------------------------------------------------------------------
# The reranker
# ---------------------------------------------------------------------------


def test_the_reranker_weights_every_lifecycle_status():
    assert set(_STATUS_WEIGHTS) == set(RunbookStatus)


def test_the_published_contract_carries_every_vocabulary_the_request_uses():
    """What the clients generate from: ``openapi.json`` (regenerated, never
    hand-edited) publishes each enum with the owner's values in its order."""
    import json

    spec = json.loads(
        (
            Path(__file__).resolve().parents[4] / "docs/reference/api/openapi.json"
        ).read_text(encoding="utf-8")
    )
    schemas = spec["components"]["schemas"]
    for name, enum_cls in (
        ("RunbookDomain", RunbookDomain),
        ("SymptomClass", SymptomClass),
        ("RunbookSeverity", RunbookSeverity),
        ("KnowledgeScope", KnowledgeScope),
        ("RunbookDifficulty", RunbookDifficulty),
    ):
        assert schemas[name]["enum"] == list(vocabulary(enum_cls)), name
