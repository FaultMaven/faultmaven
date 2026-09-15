"""``POST /cases/search`` applies ``state`` — the parts the generic guard cannot see.

``CaseSearchRequest.state`` was declared, published in
``docs/reference/api/openapi.json``, accepted by Pydantic — and read by nothing.
``CaseService.search_cases`` called ``repository.search(query=, user_id=,
limit=, shared_case_ids=, restrict_case_ids=)`` and looked at no other field,
and ``ICaseRepository.search`` declared no ``state`` parameter for it to reach.
A search asking for ``resolved`` answered **200 with resolved and unresolved
cases alike**.

**That the field now DISCRIMINATES is not asserted here.**
``test_declared_filters_reach_the_query.py`` asserts it generically, for every
declared filter on every repository that can be stood up, and this change
re-classifies its ``SEARCH_REQUEST_RULES["state"]`` row from ``field_exempt``
to ``narrows`` to say so. Restating it here would mean two corpora encoding one
arrangement invariant, where a change to the in-memory relevance scoring could
silently invalidate one while the other kept passing. What is left in this file
is what that guard does not and cannot cover:

1. **Placement — the predicate runs BEFORE the limit.** The guard records, in
   its own registry, that it cannot gather the faultmaven#1409 evidence on this
   surface at all: search publishes no total, so there is no count for a
   misplaced predicate to disagree with. Placement therefore needs a
   differently-shaped assertion, and gets one below: a corpus arranged so the
   rows the limit selects are ALL in the other state, which a post-limit filter
   answers empty.
2. **Composition** — a new predicate must AND with the visibility scope and the
   text match, not replace either.
3. **Signature parity and keyword forwarding** across the contract, the base
   class and all four implementations. The guard exercises behaviour through
   repositories it can construct; it does not read signatures, and the
   sessionless wrapper's forwarding call is invisible to it.

Against the REAL ``InMemoryCaseRepository``, never a mock, for everything
behavioural. ``tests/integration/modules/case/test_sqlite_case_repository.py``
and ``tests/integration/test_postgresql_repository_roundtrip.py`` make the
placement assertion against real SQL, where the LIMIT is the database's.
"""

import inspect
from types import SimpleNamespace

import pytest

from faultmaven.models.api_models import CaseSearchRequest
from faultmaven.modules.case.contracts import ICaseRepository
from faultmaven.modules.case.domain.models import Case, CaseState, InquiryData
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure import sessionless_case_repository
from faultmaven.modules.case.infrastructure.case_repository import (
    CaseRepository,
    InMemoryCaseRepository,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (
    PostgreSQLHybridCaseRepository,
)
from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
    SessionlessCaseRepository,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)

SEED_OWNER = "user_search_state"
SEED_ENTERPRISE = "ent_search_state"

#: Every seeded title carries this, so the text predicate matches the whole
#: corpus and ``state`` is the only thing that can separate it.
TOKEN = "widget"


def _case(index: int, *, title: str, state: CaseState, description: str) -> Case:
    """One seeded case. Built fresh per fixture — the in-memory repository
    stores the instance itself and bumps its version on save."""
    extra = {}
    if state is CaseState.INVESTIGATING:
        # Case's own validators: INVESTIGATING needs a confirmed problem
        # statement and a commitment to investigate.
        extra["inquiry"] = InquiryData(
            proposed_problem_statement="seeded problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        )
    return Case(
        case_id=f"case_{index:012d}",
        user_id=SEED_OWNER,
        enterprise_id=SEED_ENTERPRISE,
        title=title,
        description=description,
        state=state,
        current_turn=1,
        **extra,
    )


def _corpus() -> list[Case]:
    """Four cases, all matching ``widget``, two per state.

    The two INQUIRY rows carry the token in their DESCRIPTION as well as their
    title, which is what the in-memory repository ranks on (title 100 +
    description 10). So the top two rows a ``limit=2`` would take are both
    INQUIRY — and a search for INVESTIGATING at that limit can only find
    anything if the state reached the filter BEFORE the slice. That arrangement
    is the whole point of this corpus, and it is asserted rather than assumed by
    ``test_the_limit_the_filter_has_to_beat_...`` below.
    """
    return [
        _case(
            1,
            title=f"alpha {TOKEN}",
            state=CaseState.INQUIRY,
            description=f"an inquiry about a {TOKEN}",
        ),
        _case(
            2,
            title=f"beta {TOKEN}",
            state=CaseState.INQUIRY,
            description=f"another inquiry about a {TOKEN}",
        ),
        _case(
            3,
            title=f"gamma {TOKEN}",
            state=CaseState.INVESTIGATING,
            description="under investigation",
        ),
        _case(
            4,
            title=f"delta {TOKEN}",
            state=CaseState.INVESTIGATING,
            description="also under investigation",
        ),
    ]


@pytest.fixture
async def repository() -> InMemoryCaseRepository:
    repo = InMemoryCaseRepository()
    for case in _corpus():
        await repo.save(case)
    return repo


@pytest.fixture
def service(repository: InMemoryCaseRepository) -> CaseService:
    # No team service and no share repository: both degrade to owner-only,
    # which is the scope this test wants.
    return CaseService(case_repository=repository)


# ============================================================
# Placement: the predicate runs BEFORE the limit
# ============================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestStateIsAppliedInTheQuery:
    """The half ``test_declared_filters_reach_the_query.py`` cannot assert.

    That guard's count check is what normally catches a predicate applied after
    the LIMIT (faultmaven#1409) — and it records, in its own registry, that this
    surface publishes no total for it to check. So placement is measured here by
    construction instead.
    """

    async def test_state_survives_a_limit_the_other_state_would_fill(self, service):
        """``limit=2`` on a corpus whose top two rows are both INQUIRY.

        Applied in the query, this returns the two INVESTIGATING cases. Applied
        after the limit, it returns nothing at all — the limit has already
        handed back two INQUIRY rows and the filter deletes both. The endpoint
        would then answer 200 with an empty list, and "you have no investigating
        cases matching widget" would be a lie about a corpus that holds two.
        """
        result = await service.search_cases(
            CaseSearchRequest(query=TOKEN, state=CaseState.INVESTIGATING, limit=2),
            user_id=SEED_OWNER,
        )

        assert {case.case_id for case in result} == {
            "case_000000000003",
            "case_000000000004",
        }

    async def test_the_limit_the_filter_has_to_beat_really_is_the_other_state(
        self, repository
    ):
        """Pins the arrangement the test above depends on.

        If the corpus ever reorders so that an INVESTIGATING row lands in the
        top two, the test above keeps passing while proving nothing. CAPTURED
        from the repository rather than assumed: this is the unfiltered ranking,
        read back.
        """
        top_two, _ = await repository.search(query=TOKEN, user_id=SEED_OWNER, limit=2)

        assert [case.state for case in top_two] == [
            CaseState.INQUIRY,
            CaseState.INQUIRY,
        ]

    async def test_the_total_is_the_match_count_not_the_page_length(self, repository):
        """``search`` returns ``(page, total_count)``, and the total is a TOTAL.

        Discriminating on purpose: four matches under ``limit=2``. A repository
        returning ``len(page)`` — which three of the four did until this change
        — answers 2 here, and would be indistinguishable from a true count on
        any corpus smaller than the limit. Nothing reads this value today, which
        is precisely how it stayed wrong: a declared value that was not the value
        declared, one return slot over from the field this change is about.
        """
        page, total = await repository.search(query=TOKEN, user_id=SEED_OWNER, limit=2)

        assert len(page) == 2
        assert total == 4

    async def test_the_total_moves_with_the_state_predicate(self, repository):
        page, total = await repository.search(
            query=TOKEN, user_id=SEED_OWNER, state=CaseState.INVESTIGATING
        )

        assert len(page) == 2
        assert total == 2


# ============================================================
# Composition with the rest of the WHERE clause
# ============================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestStateComposesWithTheRestOfTheWhereClause:
    """A new predicate must AND with the existing ones, not replace them."""

    async def test_state_does_not_widen_the_owner_scope(self, repository):
        """Someone else's case in the requested state stays invisible."""
        stranger = _case(
            5,
            title=f"epsilon {TOKEN}",
            state=CaseState.INVESTIGATING,
            description="belongs to somebody else",
        )
        stranger.user_id = "a-stranger"
        await repository.save(stranger)

        result, total = await repository.search(
            query=TOKEN, user_id=SEED_OWNER, state=CaseState.INVESTIGATING
        )

        assert {case.case_id for case in result} == {
            "case_000000000003",
            "case_000000000004",
        }
        assert total == 2

    async def test_state_still_requires_the_text_match(self, repository):
        """The query predicate is not dropped when a state is supplied."""
        result, total = await repository.search(
            query="gadget", user_id=SEED_OWNER, state=CaseState.INVESTIGATING
        )

        assert result == []
        assert total == 0


# ============================================================
# Signature parity, and the forwarding wrapper
# ============================================================
#
# ``SessionlessCaseRepository`` is the wrapper actually wired in at runtime, and
# it forwards to the concrete repository. The #405 rename shipped a version of
# exactly this defect: the service called ``repo.list(state=...)`` by keyword
# while the wrapper still declared ``status``, so every list raised TypeError
# into an ``except Exception: return [], 0`` and the endpoint answered "you have
# no cases". ``search_cases`` has the same swallow, so the same mistake here is
# equally silent — and inserting a parameter into a signature whose forwarding
# call is POSITIONAL is a second way to arrive there, with ``limit`` landing in
# ``state``.

#: Every type that exposes a case ``search()``. The same list ``list()`` is
#: guarded with in test_case_repository_list_state_filter.py, for the same
#: reason.
_SEARCH_PROVIDERS = [
    ICaseRepository,
    CaseRepository,
    InMemoryCaseRepository,
    SessionlessCaseRepository,
    SQLiteCaseRepository,
    PostgreSQLHybridCaseRepository,
]


@pytest.mark.unit
@pytest.mark.parametrize("provider", _SEARCH_PROVIDERS, ids=lambda c: c.__name__)
def test_every_search_declares_state(provider):
    """The contract, the base class and every implementation take ``state``."""
    params = inspect.signature(provider.search).parameters

    assert "state" in params, (
        f"{provider.__name__}.search() has no 'state' parameter — the service "
        f"passes state=… by keyword, so this raises TypeError into a swallow "
        f"and the endpoint answers with an empty list"
    )
    assert params["state"].default is None, (
        f"{provider.__name__}.search() makes 'state' required; a search with no "
        f"state filter is the common case"
    )


class _RecordingRepository:
    """Records how the sessionless wrapper called it.

    Positional arguments land in ``args``, which is exactly what the assertions
    below are looking for.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def list(self, *args, **kwargs):
        self.calls.append(("list", args, kwargs))
        return [], 0

    async def search(self, *args, **kwargs):
        self.calls.append(("search", args, kwargs))
        return [], 0


@pytest.fixture
def recording_wrapper(monkeypatch):
    """A ``SessionlessCaseRepository`` whose session and repository are fakes.

    The wrapper opens a real database session and resolves a real repository,
    neither of which this assertion needs; both are replaced so the call it
    makes can be captured directly.
    """
    recorder = _RecordingRepository()

    class _NullSession:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        sessionless_case_repository, "get_db_session", lambda: _NullSession()
    )
    monkeypatch.setattr(
        sessionless_case_repository,
        "get_repository_for_session",
        lambda session: recorder,
    )
    return SessionlessCaseRepository(), recorder


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["list", "search"])
async def test_the_sessionless_wrapper_forwards_everything_by_keyword(
    recording_wrapper, method
):
    """Nothing is forwarded positionally, on either method.

    Positional forwarding is what makes inserting a parameter dangerous: a call
    spelled ``repo.list(user_id, enterprise_id, state, limit, offset, source)``
    binds by POSITION, so adding one parameter ahead of ``source`` in the
    underlying repository silently puts ``offset`` into ``source`` and ``limit``
    into ``offset``. Both methods are wrapped in
    ``except Exception: return [], 0`` one layer up, so the endpoint would
    answer "you have no cases" with nothing in the log.

    ``list`` is covered as well as ``search``, and that is the point: this change
    fixed ``search``'s forward, and leaving ``list`` positional would have left
    the hazard open on the busier of the two.

    CAPTURED from the call the wrapper actually makes, not read off its source:
    an earlier version of this test string-matched ``inspect.getsource``, which
    raises ``IndexError`` — a crash whose message says nothing about forwarding
    — the moment black reformats the call or the local is renamed.
    """
    wrapper, recorder = recording_wrapper

    if method == "list":
        await wrapper.list(user_id="u", state=CaseState.INQUIRY, limit=7, offset=3)
    else:
        await wrapper.search(query="q", user_id="u", state=CaseState.INQUIRY, limit=7)

    assert len(recorder.calls) == 1
    name, args, kwargs = recorder.calls[0]
    assert name == method
    assert args == (), (
        f"{method} forwarded {len(args)} argument(s) POSITIONALLY: {args!r}. "
        f"Every one must be passed by keyword — see the docstring."
    )
    # The values that were sent must be the values that arrive, under their own
    # names: "by keyword" is not worth much if the names are wrong.
    assert kwargs["user_id"] == "u"
    assert kwargs["state"] is CaseState.INQUIRY
    assert kwargs["limit"] == 7
    if method == "list":
        assert kwargs["offset"] == 3
    else:
        assert kwargs["query"] == "q"
