"""One decode for ``JsonBlob``, and the surfaces over it must not drift (fm#1107).

``JsonBlob`` is ``Text().with_variant(JSONB, "postgresql")``, so a stored value
comes back as a JSON *string* or an already-decoded *dict* depending on backend
and writer. Handling one shape and not the other loses it **silently** — the KB
bootstrap's causes comparison once read ``None`` on every deployment, so no
runbook could ever compare unchanged and all of them re-ingested on every boot.

That decode existed three times over: in the bootstrap, in the knowledge service,
and in the item repository. Each copy was deliberate — bootstrap and a domain
service may not reach into a repository's private helpers, and the repository,
being infrastructure, may not import the domain service (``lint-imports``
contract 4) — but three copies of a silent-loss decode meant the next divergence
had three places to hide, and one of them sits under the KB cause seeder's
integrity check, where a wrong "no metadata" answer reads as "prose runbook" and
disables the check for the affected shape.

These tests pin the shared decode AND the deltas each surface deliberately keeps,
so a future edit to one of them fails here rather than diverging quietly.
"""

import json

import pytest

from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
)
from faultmaven.utils.serialization import decode_json_blob

pytestmark = [pytest.mark.unit]

RECORD = {"description": "a stored object", "tags": ["a", "b"]}

# Every shape a JsonBlob column has been observed to hand back, plus the ways a
# value can be unusable. `None` means "nothing usable here".
SHAPES = [
    pytest.param(RECORD, RECORD, id="jsonb-decoded-dict"),
    pytest.param(json.dumps(RECORD), RECORD, id="sqlite-json-string"),
    pytest.param({}, {}, id="empty-dict-is-not-nothing"),
    pytest.param("{}", {}, id="empty-json-object-is-not-nothing"),
    pytest.param(None, None, id="absent"),
    pytest.param("", None, id="empty-string"),
    pytest.param("not json at all", None, id="undecodable"),
    pytest.param("[1, 2, 3]", None, id="valid-json-wrong-container"),
    pytest.param("null", None, id="json-null"),
    pytest.param(12345, None, id="non-str-non-dict"),
]


@pytest.mark.parametrize("value,expected", SHAPES)
def test_the_shared_decode_covers_every_stored_shape(value, expected):
    assert decode_json_blob(value) == expected


@pytest.mark.parametrize("value,expected", SHAPES)
def test_the_repository_surface_is_the_shared_decode_plus_a_copy(value, expected):
    """``_parse_json_dict`` differs in exactly one way: it copies. It keeps
    ``None`` for "nothing usable" — its caller distinguishes that from ``{}``."""
    repo = DatabaseKnowledgeItemRepository.__new__(DatabaseKnowledgeItemRepository)
    assert repo._parse_json_dict(value) == expected


def test_an_empty_object_is_never_collapsed_into_absent():
    """The distinction the repository relies on, and the reason the dict check
    precedes the falsy one. "stored an empty object" and "stored nothing" are
    different facts."""
    assert decode_json_blob({}) == {}
    assert decode_json_blob("{}") == {}
    assert decode_json_blob(None) is None
    assert decode_json_blob("") is None


# ---------------------------------------------------------------------------
# The copy semantics — a PostgreSQL-only bug that never reproduces on SQLite
# ---------------------------------------------------------------------------


def test_the_repository_never_hands_out_an_alias_of_the_orm_attribute():
    """``_to_domain`` hands the result out. On JSONB the dict branch would ALIAS
    the session-bound attribute, so a caller mutating the returned dict would
    dirty the row — and it is invisible on SQLite, where the value is a string
    and every decode is naturally fresh."""
    stored = {"causes": [{"cause_letter": "A"}]}
    repo = DatabaseKnowledgeItemRepository.__new__(DatabaseKnowledgeItemRepository)

    handed_out = repo._parse_json_dict(stored)
    handed_out["causes"].append({"cause_letter": "Z"})

    assert stored == {"causes": [{"cause_letter": "A"}]}, (
        "mutating the returned dict reached the stored value — the copy is what "
        "stops a caller dirtying a session-bound ORM attribute"
    )


def test_the_read_only_surfaces_do_not_pay_for_a_copy():
    """The default is no-copy on purpose: these callers walk one value per row at
    startup, and a deepcopy per row is waste. The contract is that they must not
    mutate — asserted here so a future 'just copy everywhere' change is a
    deliberate one."""
    stored = {"causes": [{"cause_letter": "A"}]}
    assert decode_json_blob(stored) is stored
