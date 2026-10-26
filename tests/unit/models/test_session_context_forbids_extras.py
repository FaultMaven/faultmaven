"""``SessionContext`` refuses a field it does not declare (#1638).

Under pydantic's default ``extra='ignore'``, a keyword the model does not
declare was dropped without a word. ``RedisSessionStore.create_session`` passed
``data_uploads`` and ``case_history``, and the shared ``sample_session_context``
fixture passed four more. Nothing read any of them. ``extra='forbid'`` makes
such a keyword a construction error.

The second half drives the production writer itself over FakeRedis: the
session service, through the store's ``save`` and its read back. A model-level
check says nothing about whether the code that builds the model still
constructs under ``forbid``. (The store's own ``create_session`` went in
#2301: nothing called it.)
"""

import pytest
from pydantic import ValidationError

from faultmaven.models.common import SessionContext
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)

#: Every undeclared keyword a writer was found passing before #1638.
PHANTOM_FIELDS = [
    "data_uploads",
    "case_history",
    "agent_state",
    "conversation_history",
    "uploaded_data",
    "insights",
]


@pytest.mark.unit
def test_the_declared_fields_construct():
    session = SessionContext(session_id="s", user_id="u")

    assert (session.session_id, session.user_id) == ("s", "u")


@pytest.mark.unit
@pytest.mark.parametrize("field", PHANTOM_FIELDS)
def test_an_undeclared_field_is_refused_as_extra_forbidden(field):
    with pytest.raises(ValidationError) as exc_info:
        SessionContext(session_id="s", user_id="u", **{field: []})

    assert [(e["type"], e["loc"]) for e in exc_info.value.errors()] == [
        ("extra_forbidden", (field,))
    ]


@pytest.fixture
def store():
    import fakeredis.aioredis as fakeredis_aio

    return RedisSessionStore(
        fakeredis_aio.FakeRedis(decode_responses=True), session_ttl_seconds=1800
    )


@pytest.mark.unit
async def test_the_session_service_writer_constructs_under_forbid(store):
    service = AuthSessionService(session_store=store)

    session, resumed = await service.create_session(
        "u1", client_id="c1", metadata={"source": "test"}
    )
    again, resumed_again = await service.create_session("u1", client_id="c1")

    assert resumed is False
    assert resumed_again is True
    assert again.session_id == session.session_id
    assert again.metadata == {"source": "test"}
