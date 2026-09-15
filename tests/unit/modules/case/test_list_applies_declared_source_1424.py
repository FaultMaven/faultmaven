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

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.models.api_models import CaseListFilter
from faultmaven.modules.case.domain.models import Case, CaseState, InquiryData
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
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


def _case(
    index: int,
    *,
    source: str,
    activity_rank: int,
    state: CaseState = CaseState.INQUIRY,
    user_id: str = SEED_OWNER,
) -> Case:
    """One seeded case.

    ``activity_rank`` is the position this case takes in the repository's sort
    (``last_activity_at`` descending): rank 0 is first on the page. It is what
    lets the placement corpus below be arranged deliberately instead of
    depending on insertion order.
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
        created_at=_BASE - timedelta(days=1),
        last_activity_at=activity,
        updated_at=activity,
        **extra,
    )


def _corpus() -> list[Case]:
    """Five cases across three sources, with the two ``copilot`` rows FIRST.

    The order is the arrangement the placement test depends on: a ``limit=2``
    with no predicate takes both ``copilot`` rows, so a ``source=slack`` request
    at that limit can only find anything if the predicate ran before the slice.
    ``test_the_rows_a_limit_would_take_really_are_the_other_source`` pins it by
    reading the unfiltered page back rather than trusting this comment.

    Three sources, not two, so "filtered" cannot be mistaken for "the other
    half": ``api`` is the row that must disappear from BOTH ``copilot`` and
    ``slack`` answers.
    """
    return [
        _case(1, source="copilot", activity_rank=0),
        _case(2, source="copilot", activity_rank=1),
        _case(3, source="slack", activity_rank=2),
        _case(4, source="slack", activity_rank=3, state=CaseState.INVESTIGATING),
        _case(5, source="api", activity_rank=4),
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
        stranger = _case(6, source="slack", activity_rank=5, user_id="a-stranger")
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

    async def test_source_ands_with_the_creation_window(self, repository):
        """The window keeps its own effect under a source filter.

        The corpus shares one ``created_at``, so a window that excludes it must
        empty the ``slack`` answer rather than leaving it at two.
        """
        after_everything = _BASE + timedelta(days=1)
        page, total = await repository.list(
            user_id=SEED_OWNER, source="slack", created_after=after_everything
        )

        assert page == []
        assert total == 0


# ============================================================
# The empty string, on both kinds of repository
# ============================================================


def _sqlite_template(path: Path) -> None:
    """The schema plus the tenancy rows ``cases`` carries foreign keys to.

    Built with a SYNC engine: ``create_all`` issues DDL for every table and
    aiosqlite hops to a worker thread per statement, for identical output.
    """
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO enterprises (enterprise_id, name, slug) "
                    "VALUES (:eid, 'Source Enterprise', 'source-enterprise')"
                ),
                {"eid": SEED_ENTERPRISE},
            )
            connection.execute(
                text(
                    "INSERT INTO users (user_id, enterprise_id, username, "
                    "email, display_name) "
                    "VALUES (:uid, :eid, :uid, :email, 'Seed Owner')"
                ),
                {
                    "uid": SEED_OWNER,
                    "eid": SEED_ENTERPRISE,
                    "email": f"{SEED_OWNER}@test",
                },
            )
    finally:
        engine.dispose()


@pytest.fixture
async def sqlite_repository(tmp_path):
    """A real ``SQLiteCaseRepository`` over the same corpus.

    Present for ONE question — what an empty ``source`` means — because that is
    a question about agreement between two implementations, and neither of them
    can answer it alone.
    """
    database = tmp_path / "source-filter.db"
    _sqlite_template(database)
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
class TestTheEmptyStringMeansNoFilter:
    """``if source:``, not ``if source is not None:`` — and the same on all four.

    The SQL repositories build their WHERE clause under ``if source:``, so an
    empty string appends no clause and the query is unfiltered. The in-memory
    repository follows that spelling rather than the other one, because the
    alternative is a deployment-dependent answer to ``?source=``: a filter for
    cases whose source is the empty string — of which there are none, ``source``
    being a ``Literal['copilot', 'slack', 'api']`` with a default — on one
    backend, and the whole list on the others. That is the defect this file is
    about, reintroduced at a different input.

    Both arms run the same corpus and are asserted to ANSWER ALIKE, not merely
    to each satisfy a number written here twice.
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
