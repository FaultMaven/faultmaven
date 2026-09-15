"""``GET /cases?source=`` on the in-memory repository — the parts the guard cannot see.

``InMemoryCaseRepository.list`` declared ``source`` and never read it. The body
filtered on ``user_id``/``shared_case_ids``, ``restrict_case_ids``, ``state``,
``include_empty`` and the creation-date window, then sorted and paginated;
``source`` appeared in the signature and nowhere else in the method. The other
three implementations applied it, so the endpoint's answer depended on which
backend was configured — and ``create_case_repository`` selects this one
whenever ``DATABASE_URL`` is unset or ``:memory:``, which is a real deployment
rather than a test double. In that deployment
``GET /api/v1/cases?source=slack`` answered **200 with every case**.

**That the field now DISCRIMINATES is not asserted here.**
``test_declared_filters_reach_the_query.py`` asserts it generically, for both
``CaseListFilter`` surfaces on every repository it can stand up, and this change
deletes its ``dropped_by={"InMemoryCaseRepository": ...}`` gap to say so.
Restating that here would mean two corpora encoding one arrangement invariant.
What is left in this file is what the guard does not and cannot cover:

1. **Which rows** — the guard asks whether the page MOVED, which a predicate
   matching the wrong column satisfies just as well as the right one. Here the
   returned ids are named.
2. **Placement, by construction** — a corpus whose top rows in sort order are
   all of the *other* source, so a filter applied after the slice answers empty
   rather than merely differently. The guard's count check (faultmaven#1409) is
   the generic form of this; the arrangement below is the one a reader can see.
3. **The three numbers a total can be** — filtered match count, page length, and
   unfiltered total, kept distinct on purpose so ``total_count`` cannot satisfy
   the assertion by being either of the other two.
4. **The empty string**, which the registry never sends. All four
   implementations must treat ``source=""`` as "no filter", because the SQL ones
   spell the predicate ``if source:`` — asserted here against BOTH the in-memory
   repository and a real SQLite one, since agreement between two repositories is
   not a claim either one can make alone.
5. **The service path** — ``CaseService.list_user_cases`` reading
   ``CaseListFilter.source`` and forwarding it, with the summaries it returns
   named.

Against the REAL ``InMemoryCaseRepository``, never a mock: a mock has no WHERE
clause, so it cannot tell a field that reached a query from one that reached a
function signature, and that distinction is the whole subject.
"""

from __future__ import annotations

import inspect
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.api.routes.admin_cases import list_all_cases
from faultmaven.models.api_models import CaseListFilter
from faultmaven.modules.case.api.routes import list_cases
from faultmaven.modules.case.contracts import ICaseRepository
from faultmaven.modules.case.domain.models import Case, CaseState, InquiryData
from faultmaven.modules.case.domain.services.case_service import CaseService
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

SEED_OWNER = "user_list_source"
SEED_ENTERPRISE = "ent_list_source"

#: A fixed clock. ``Case`` refuses ``created_at`` after ``last_activity_at`` and
#: ``save`` stamps ``updated_at`` with the wall clock, so the corpus sits in the
#: past and is ordered by hand rather than by how fast the test runs.
_BASE = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _day(days_ago: int) -> datetime:
    """A ``created_at``, that many whole days before the activity anchor."""
    return _BASE - timedelta(days=days_ago)


def _case(
    index: int,
    *,
    source: str,
    activity_rank: int,
    created_days_ago: int,
    state: CaseState = CaseState.INQUIRY,
    user_id: str = SEED_OWNER,
) -> Case:
    """One seeded case, with both of its clocks set by hand.

    ``activity_rank`` is the position this case takes in the repository's sort
    (``last_activity_at`` descending): rank 0 is first on the page. It is what
    lets the placement corpus below be arranged deliberately instead of
    depending on insertion order.

    ``created_days_ago`` is what the creation-window composition test splits on,
    and it is a PER-CASE value rather than a constant for a reason: when every
    row shared one ``created_at``, no window could separate them, so the only
    windows available either matched everything or nothing. A window matching
    nothing answers ``([], 0)`` whatever ``source`` does, which is a test that
    passes with the production predicate deleted — measured, and it is why this
    parameter exists. Every value here is at least a day before every
    ``last_activity_at``, because ``Case`` refuses ``created_at`` after it.
    """
    extra = {}
    if state is CaseState.INVESTIGATING:
        # Case's own validators: INVESTIGATING needs a confirmed problem
        # statement and a decision to investigate.
        extra["inquiry"] = InquiryData(
            proposed_problem_statement="seeded problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        )
    activity = _BASE - timedelta(hours=activity_rank)
    return Case(
        case_id=f"case_{index:012d}",
        user_id=user_id,
        enterprise_id=SEED_ENTERPRISE,
        title=f"case {index}",
        description="seeded",
        source=source,
        state=state,
        current_turn=1,
        created_at=_day(created_days_ago),
        last_activity_at=activity,
        updated_at=activity,
        **extra,
    )


def _corpus() -> list[Case]:
    """Five cases across three sources, with the two ``copilot`` rows FIRST.

    The sort order is the arrangement the placement test depends on: a
    ``limit=2`` with no predicate takes both ``copilot`` rows, so a
    ``source=slack`` request at that limit can only find anything if the
    predicate ran before the slice.
    ``test_the_rows_a_limit_would_take_really_are_the_other_source`` pins it by
    reading the unfiltered page back rather than trusting this comment.

    Three sources, not two, so "filtered" cannot be mistaken for "the other
    half": ``api`` is the row that must disappear from BOTH ``copilot`` and
    ``slack`` answers.

    The creation dates INTERLEAVE the sources, which is the second arrangement
    the file depends on and the one that makes the window a real co-predicate:

    ======  ========  ==============  ====================
    case    source    created (ago)   in a window of…
    ======  ========  ==============  ====================
    1       copilot   5 days          the two OLDEST
    3       slack     4 days          the two OLDEST
    5       api       3 days
    2       copilot   2 days          the two NEWEST
    4       slack     1 day           the two NEWEST
    ======  ========  ==============  ====================

    So each end of the window selects one ``copilot`` row and one ``slack`` row.
    Either predicate alone matches two cases and their intersection is one — the
    property ``test_source_ands_with_state_when_they_select_disjoint_sets``
    relies on for ``state``, and the property the window composition test had no
    way to have while every row shared a ``created_at``. It is asserted, not
    assumed, by ``test_each_half_of_the_window_pair_matches_two_on_its_own``.
    """
    return [
        _case(1, source="copilot", activity_rank=0, created_days_ago=5),
        _case(2, source="copilot", activity_rank=1, created_days_ago=2),
        _case(3, source="slack", activity_rank=2, created_days_ago=4),
        _case(
            4,
            source="slack",
            activity_rank=3,
            created_days_ago=1,
            state=CaseState.INVESTIGATING,
        ),
        _case(5, source="api", activity_rank=4, created_days_ago=3),
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
# Which rows come back
# ============================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestSourceSelectsTheRowsItNames:
    """The page is the matching cases — all of them, and only them."""

    @pytest.mark.parametrize(
        "source,expected",
        [
            ("copilot", {"case_000000000001", "case_000000000002"}),
            ("slack", {"case_000000000003", "case_000000000004"}),
            ("api", {"case_000000000005"}),
        ],
    )
    async def test_each_source_returns_exactly_its_own_cases(
        self, repository, source, expected
    ):
        page, total = await repository.list(user_id=SEED_OWNER, source=source)

        assert {case.case_id for case in page} == expected
        assert total == len(expected)

    async def test_no_source_returns_the_whole_corpus(self, repository):
        """The default is unfiltered, so the numbers above mean something."""
        page, total = await repository.list(user_id=SEED_OWNER)

        assert len(page) == 5
        assert total == 5

    async def test_a_source_nothing_carries_matches_nothing(self, repository):
        """Not "everything", which is what the defect answered."""
        page, total = await repository.list(user_id=SEED_OWNER, source="teams")

        assert page == []
        assert total == 0


# ============================================================
# Placement: the predicate runs BEFORE the slice
# ============================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestSourceIsAppliedBeforeThePage:
    """Where the predicate runs, measured two ways.

    ``total_count`` is the generic evidence (faultmaven#1409): a predicate
    applied after the LIMIT moves the page and leaves the count describing a
    different set. The corpus arrangement is the local evidence, and it fails
    louder — a post-slice filter answers with nothing at all.
    """

    async def test_source_survives_a_limit_the_other_source_would_fill(
        self, repository
    ):
        """``limit=2`` on a corpus whose top two rows are both ``copilot``.

        Applied in the query, this returns the two ``slack`` cases. Applied
        after the slice, it returns nothing: the limit has already handed back
        two ``copilot`` rows and the filter deletes both. The endpoint would
        answer 200 with an empty list, and "you have no Slack cases" would be a
        lie about a corpus that holds two.
        """
        page, _ = await repository.list(user_id=SEED_OWNER, source="slack", limit=2)

        assert {case.case_id for case in page} == {
            "case_000000000003",
            "case_000000000004",
        }

    async def test_the_rows_a_limit_would_take_really_are_the_other_source(
        self, repository
    ):
        """Pins the arrangement the test above depends on.

        If the corpus ever reorders so a ``slack`` row lands in the top two, the
        test above keeps passing while proving nothing. CAPTURED from the
        repository — this is the unfiltered ranking, read back.
        """
        page, _ = await repository.list(user_id=SEED_OWNER, limit=2)

        assert [case.source for case in page] == ["copilot", "copilot"]

    async def test_the_total_is_the_filtered_count_not_the_page_length(
        self, repository
    ):
        """Three numbers, all different, so only the right one satisfies this.

        Two ``slack`` cases, taken one at a time, out of five rows. A total that
        is the page length answers 1; a total computed before the predicate
        answers 5; the match count answers 2. On a corpus where the filtered set
        fits inside the limit, the first two are indistinguishable from the
        third — which is how a post-slice count survives a test suite.
        """
        page, total = await repository.list(
            user_id=SEED_OWNER, source="slack", limit=1, offset=0
        )

        assert len(page) == 1
        assert total == 2

    async def test_the_pages_of_a_filtered_set_sum_to_its_total(self, repository):
        """Walk the whole filtered set page by page: the total is stable, the
        pages do not overlap, and nothing from another source surfaces on any of
        them. A count that disagreed with the page would strand the caller
        paging past the end — or hide a row before it."""
        _, total = await repository.list(user_id=SEED_OWNER, source="slack")
        seen: list[Case] = []
        offset = 0
        while offset < total:
            page, page_total = await repository.list(
                user_id=SEED_OWNER, source="slack", limit=1, offset=offset
            )
            assert page_total == total
            seen.extend(page)
            offset += 1

        assert [case.case_id for case in seen] == [
            "case_000000000003",
            "case_000000000004",
        ]
        assert all(case.source == "slack" for case in seen)


# ============================================================
# Composition with the rest of the predicate set
# ============================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestSourceComposesWithTheOtherPredicates:
    """A new predicate must AND with the existing ones, not replace them."""

    async def test_source_does_not_widen_the_owner_scope(self, repository):
        """Somebody else's ``slack`` case stays invisible."""
        stranger = _case(
            6,
            source="slack",
            activity_rank=5,
            created_days_ago=1,
            user_id="a-stranger",
        )
        await repository.save(stranger)

        page, total = await repository.list(user_id=SEED_OWNER, source="slack")

        assert {case.case_id for case in page} == {
            "case_000000000003",
            "case_000000000004",
        }
        assert total == 2

    async def test_source_ands_with_state(self, repository):
        """Both slack cases, one state each: the pair selects one row.

        A predicate that replaced ``state`` would answer with two; one that was
        replaced BY ``state`` would answer with the single INVESTIGATING case
        whatever its source — and here those are the same row, which is why the
        inverse pairing below is asserted too.
        """
        page, total = await repository.list(
            user_id=SEED_OWNER, source="slack", state=CaseState.INVESTIGATING
        )

        assert {case.case_id for case in page} == {"case_000000000004"}
        assert total == 1

    async def test_source_ands_with_state_when_they_select_disjoint_sets(
        self, repository
    ):
        """``copilot`` holds no INVESTIGATING case, so the pair matches nothing.

        Either predicate alone matches something (two rows each), so an answer
        of zero can only come from both being applied.
        """
        page, total = await repository.list(
            user_id=SEED_OWNER, source="copilot", state=CaseState.INVESTIGATING
        )

        assert page == []
        assert total == 0

    async def test_each_half_of_the_window_pair_matches_two_on_its_own(
        self, repository
    ):
        """Pins the arrangement the two window tests below depend on.

        Both predicates must match SOMETHING separately, or their intersection
        proves nothing about either. This is the property the earlier version of
        this test lacked: every row shared one ``created_at``, so the only
        window available matched nothing at all, and the composed assertion
        (``[] , 0``) held with the production predicate deleted. CAPTURED from
        the repository, so a corpus that drifts breaks here rather than
        silently hollowing out the tests below.
        """
        _, newest_two = await repository.list(user_id=SEED_OWNER, created_after=_day(2))
        _, oldest_two = await repository.list(
            user_id=SEED_OWNER, created_before=_day(3)
        )
        _, slack = await repository.list(user_id=SEED_OWNER, source="slack")
        _, copilot = await repository.list(user_id=SEED_OWNER, source="copilot")

        assert newest_two == oldest_two == slack == copilot == 2

    async def test_source_ands_with_the_lower_bound(self, repository):
        """``created_after`` and ``source`` each match two; together, one.

        The two newest cases are one ``copilot`` and one ``slack``, so a
        repository that dropped ``source`` answers with both — which is exactly
        what it did before this change. A repository that dropped the bound
        answers with the two slack cases. Only both applied give case 4.
        """
        page, total = await repository.list(
            user_id=SEED_OWNER, source="slack", created_after=_day(2)
        )

        assert {case.case_id for case in page} == {"case_000000000004"}
        assert total == 1

    async def test_source_ands_with_the_upper_bound(self, repository):
        """The same at the other end of the half-open window, and the other source.

        Covered separately because the two bounds are separate predicates with
        separate spellings — the upper one is exclusive — and a composition
        proved at one end is not proved at the other.
        """
        page, total = await repository.list(
            user_id=SEED_OWNER, source="copilot", created_before=_day(3)
        )

        assert {case.case_id for case in page} == {"case_000000000001"}
        assert total == 1


# ============================================================
# The empty string, on both kinds of repository
# ============================================================


@pytest.fixture
async def sqlite_repository(tmp_path, case_schema_template):
    """A real ``SQLiteCaseRepository`` over the same corpus.

    Present for ONE question — what a FALSY ``source`` means — because that is a
    question about agreement between two implementations, and neither of them
    can answer it alone.

    The schema is built ONCE per session by ``case_schema_template``
    (``tests/unit/modules/case/conftest.py``) and copied here, which is ~1ms
    against ~139ms to re-issue DDL for 41 tables per test. That fixture exists
    because this file had its own verbatim copy of the sibling guard's builder;
    the builder moved, both files now call it, and the reasoning lives with it.
    """
    template = case_schema_template(SEED_ENTERPRISE, SEED_OWNER)
    database = tmp_path / "source-filter.db"
    shutil.copyfile(template, database)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    try:
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            repo = SQLiteCaseRepository(session)
            for case in _corpus():
                await repo.save(case)
            yield repo
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
class TestAFalsySourceMeansNoFilter:
    """``if source:``, not ``if source is not None:`` — on the two arms run here.

    **No route can send an empty ``source``.** Both list routes declare
    ``source: Optional[Literal["copilot", "slack", "api"]] = Query(None)``, so
    ``GET /api/v1/cases?source=`` is a 422 at validation and never reaches a
    repository; measured, not assumed. The two spellings are therefore
    indistinguishable for every request the API accepts today, and nothing in
    this class is a claim about the endpoint.

    What it IS a claim about is implementation parity, and the reachable half of
    it is ``None``. ``CaseListFilter.source`` is a bare ``Optional[str]`` and so
    is every repository signature, so the routes are not the only possible
    caller — ``faultmaven/modules/auth/api/session.py`` already constructs a
    ``CaseListFilter`` directly — and a repository is entitled to be told what a
    falsy value means rather than deciding for itself. The SQL repositories
    decided it first, under ``if source:``; the in-memory one now matches, and
    ``ICaseRepository.list`` records it so a fifth implementation has something
    normative to read instead of a precedent to guess at.

    Two arms, not four. ``SessionlessCaseRepository`` forwards by keyword to
    whichever concrete repository the session resolves to, and
    ``PostgreSQLHybridCaseRepository`` needs a live server (its coverage is the
    signature guard below plus the postgres-marked suite). The two here are the
    two that hold predicate logic and can be stood up in-process, and they are
    asserted to ANSWER ALIKE rather than to each satisfy a number written twice.
    """

    async def test_both_repositories_treat_an_empty_source_as_unfiltered(
        self, repository, sqlite_repository
    ):
        in_memory_page, in_memory_total = await repository.list(
            user_id=SEED_OWNER, source=""
        )
        sqlite_page, sqlite_total = await sqlite_repository.list(
            user_id=SEED_OWNER, source=""
        )

        assert {case.case_id for case in in_memory_page} == {
            case.case_id for case in sqlite_page
        }
        assert in_memory_total == sqlite_total == 5

    async def test_both_repositories_agree_on_a_real_source_too(
        self, repository, sqlite_repository
    ):
        """The agreement above is only interesting if the two arms can disagree.

        Before this change they DID, on exactly this call: SQLite answered with
        two cases and the in-memory repository with five.
        """
        in_memory_page, in_memory_total = await repository.list(
            user_id=SEED_OWNER, source="slack"
        )
        sqlite_page, sqlite_total = await sqlite_repository.list(
            user_id=SEED_OWNER, source="slack"
        )

        assert {case.case_id for case in in_memory_page} == {
            case.case_id for case in sqlite_page
        }
        assert in_memory_total == sqlite_total == 2


# ============================================================
# The service path
# ============================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheFilterReachesTheRepositoryFromTheService:
    """``CaseListFilter.source`` → ``list_user_cases`` → ``repository.list``.

    Driven at the service rather than the repository, because the repository
    being right is not the same claim as the endpoint being right: a service
    that stopped reading ``filters.source`` would leave every repository-level
    assertion in this file green while the route answered with everything
    again. ``list_user_cases`` ends in ``except Exception: return [], 0``, so a
    break on this path is silent by construction.
    """

    async def test_list_user_cases_returns_only_the_requested_source(self, service):
        summaries, total = await service.list_user_cases(
            SEED_OWNER, CaseListFilter(source="slack")
        )

        assert {summary.case_id for summary in summaries} == {
            "case_000000000003",
            "case_000000000004",
        }
        assert total == 2

    async def test_the_service_total_is_the_filtered_count(self, service):
        """Same three-way discrimination as at the repository, one layer up:
        page length 1, filtered total 2, unfiltered total 5."""
        summaries, total = await service.list_user_cases(
            SEED_OWNER, CaseListFilter(source="slack", limit=1)
        )

        assert len(summaries) == 1
        assert total == 2

    async def test_no_source_on_the_filter_lists_everything(self, service):
        summaries, total = await service.list_user_cases(SEED_OWNER, CaseListFilter())

        assert len(summaries) == 5
        assert total == 5

    async def test_both_repositories_treat_a_none_source_as_unfiltered(
        self, repository, sqlite_repository
    ):
        """The reachable half of the same rule.

        ``None`` is what every route sends when the caller supplies no
        ``source``, so this is the falsy value that actually arrives, and the
        two arms must agree on it for the same reason they must agree on ``""``.
        """
        in_memory_page, in_memory_total = await repository.list(
            user_id=SEED_OWNER, source=None
        )
        sqlite_page, sqlite_total = await sqlite_repository.list(
            user_id=SEED_OWNER, source=None
        )

        assert {case.case_id for case in in_memory_page} == {
            case.case_id for case in sqlite_page
        }
        assert in_memory_total == sqlite_total == 5


# ============================================================
# What the routes can actually send
# ============================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "route", [list_cases, list_all_cases], ids=["GET /cases", "GET /admin/cases"]
)
@pytest.mark.parametrize(
    "value,accepted",
    [(None, True), ("slack", True), ("copilot", True), ("api", True), ("", False)],
)
def test_the_routes_accept_no_empty_source(route, value, accepted) -> None:
    """Measures the claim ``TestAFalsySourceMeansNoFilter`` opens with.

    That class's docstring says no route can send ``source=""``, and a docstring
    that says so is worth exactly as much as the claim this whole effort has
    been closing — nothing, until something checks it. So the real route's real
    annotation is validated here, rather than an app being stood up or the
    sentence being trusted.

    If a route ever widens ``source`` to a bare ``str``, this goes red and the
    empty-string case becomes reachable — at which point the parity that class
    pins stops being a courtesy to a future caller and starts being load-bearing
    for the endpoint.
    """
    annotation = inspect.signature(route).parameters["source"].annotation
    adapter = TypeAdapter(annotation)

    if accepted:
        assert adapter.validate_python(value) == value
    else:
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


# ============================================================
# Signature parity, across every implementation
# ============================================================
#
# Behaviour is only measurable on the repositories this suite can construct, and
# ``PostgreSQLHybridCaseRepository`` is not one of them: its SQL is
# PostgreSQL-only, so ``test_declared_filters_reach_the_query.py`` excludes it
# from ``REPOSITORY_ARMS`` and its only ``source`` coverage needs a live server.
#
# That leaves a live hazard with nothing on the default CI path watching it.
# ``CaseService.list_user_cases`` calls ``repository.list(source=...)`` BY
# KEYWORD inside ``except Exception: return [], 0``, so renaming or dropping the
# parameter on any implementation raises ``TypeError`` into a swallow and the
# endpoint answers "you have no cases" with nothing in the log. That is the #405
# failure, and it reached production once already.
#
# The sibling this file is modelled on guards ``search`` the same way
# (``test_every_search_declares_state``), and
# ``test_case_repository_list_state_filter.py`` guards ``state``,
# ``include_empty`` and the date bounds on ``list``. ``source`` had no such
# guard until now.

#: Every type that exposes a case ``list()``.
_LIST_PROVIDERS = [
    ICaseRepository,
    CaseRepository,
    InMemoryCaseRepository,
    SessionlessCaseRepository,
    SQLiteCaseRepository,
    PostgreSQLHybridCaseRepository,
]


@pytest.mark.unit
@pytest.mark.parametrize("provider", _LIST_PROVIDERS, ids=lambda c: c.__name__)
def test_every_list_declares_source(provider) -> None:
    """The contract, the base class and every implementation take ``source``."""
    params = inspect.signature(provider.list).parameters

    assert "source" in params, (
        f"{provider.__name__}.list() has no 'source' parameter — the service "
        f"passes source=… by keyword, so this raises TypeError into a swallow "
        f"and the endpoint answers with an empty list"
    )
    assert params["source"].default is None, (
        f"{provider.__name__}.list() makes 'source' required; a list with no "
        f"source filter is the common case"
    )
