"""The degraded case service hides no case the real one would have returned.

``MinimalCaseService`` is what the container falls back to when no case
repository is available, so every line of it runs in production the moment the
repository is missing — and its own docstring claims it "mirrors
CaseService.list_user_cases".

It did not. Two blocks sat at the top of its filtering::

    if not getattr(filters, "include_deleted", False):
        ...drop CLOSED...
    if not getattr(filters, "include_terminal", False):
        ...drop RESOLVED and CLOSED...

``CaseListFilter`` declares neither field, so neither ``getattr`` could ever
see anything but its ``False`` default: the stand-in dropped every RESOLVED and
CLOSED case **unconditionally**, whatever the caller asked for. The real
``CaseService.list_user_cases`` applies ``state``, ``source``, ``team_id``,
``include_empty`` and the creation-date window, and excludes no terminal state
at all.

That is #1431's second half, and the opposite sign of the defect this file's
neighbours guard: not a filter declared and never applied, but a filter applied
that nobody requested. Same silence either way — 200, a plausible list, and a
resolved case that has vanished from it.

The no-filters branch said the same thing without a filter object to blame it
on: it narrowed to ``INQUIRY``/``INVESTIGATING`` and dropped empty cases, where
the real service passes ``state=None`` and ``include_empty=True``.

THE SAME QUESTION, ASKED OF EVERY FILTER. Once "does the stand-in answer what
the service answers?" is the test, the terminal exclusions are not the only
wrong answer it finds, so this file asks it of the whole filter surface:

* ``source`` reached no predicate at all, so ``GET /cases?source=slack``
  answered 200 with every case on the degraded path — the #1424 defect, in the
  code that stands in for the layer where it was found.
* ``list_all_cases`` dropped ``source`` the same way, and
  ``GET /api/v1/admin/cases`` declares it.
* ``team_id`` reached no predicate either. The real service resolves it to an
  allowlist of shared case ids and returns ``([], 0)`` when that resolves
  empty; this stand-in cannot consult the allowlist (``resource_shares`` lives
  in the repository it replaces), so no share can exist and the empty page IS
  the mirror.
* ``count_user_cases`` selected on ``case.owner_id``, which ``Case`` does not
  declare, so it raised ``AttributeError`` for any caller with a case. The two
  methods are named a parity PAIR in ``contract_version.py`` and in this file,
  and that pairing was asserted in prose and never executed — so every
  assertion here is made against both.
* ``filters.priority`` and ``filters.owner_id`` were guarded by ``hasattr`` for
  fields ``CaseListFilter`` does not declare, so neither could ever fire. They
  are gone, and their going is why the missing ``source`` became visible: four
  filter blocks in a row read as coverage.
"""

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    set_current_billing_organization_id,
    set_current_enterprise_id,
)
from faultmaven.models.api_models import CaseListFilter
from faultmaven.modules.case.domain.models import CaseState

pytestmark = [pytest.mark.unit]

OWNER = "user_owner"


@pytest.fixture
def service():
    # Via the package facade, not ``_container_impl`` directly: that module
    # re-enters ``faultmaven.container``, and importing it first is a cycle.
    from faultmaven.container import DIContainer

    return object.__new__(DIContainer)._create_minimal_case_service()


@pytest.fixture
async def one_of_each_state(service):
    """One case in each of the four states, all owned by OWNER."""
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)

    cases = {}
    for state in (
        CaseState.INQUIRY,
        CaseState.INVESTIGATING,
        CaseState.RESOLVED,
        CaseState.CLOSED,
    ):
        case = await service.create_case(title=state.value, owner_id=OWNER)
        # ``Case`` sets ``validate_assignment`` and guards terminal transitions,
        # so the state is planted rather than transitioned into — this test is
        # about what the LISTING does with a state, not about how it was
        # reached.
        object.__setattr__(case, "state", state)
        cases[state] = case
    return cases


@pytest.fixture
async def one_of_each_source(service):
    """One case per accepted source, all owned by OWNER."""
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)

    for source in ("copilot", "slack", "api"):
        await service.create_case(title=f"from-{source}", owner_id=OWNER, source=source)


async def _titles(service, filters):
    """The page, its total, AND the count — all three, agreeing.

    ``count_user_cases`` is CALLED, not merely named: it is documented as
    ``list_user_cases``'s pair, and the only reason a selector on a field
    ``Case`` does not have could sit in it unnoticed is that nothing ran it.

    The total is compared against ``len(page)``, NOT against the size of the
    title SET. A set de-duplicates, and the page-versus-total distinction is
    the very thing this code is about — ``create_case`` defaults the title to
    "New Chat", so two untitled cases would have collapsed to one and failed a
    CORRECT implementation. The equality only holds while the page is not
    truncated, which every fixture here ensures (≤4 cases against a limit of
    50); the case where they genuinely differ has its own test below, because
    an assertion that can never see ``total > limit`` is not testing
    pagination.
    """
    page, total = await service.list_user_cases(OWNER, filters)
    assert total == len(page), "unpaginated: the page and its total agree"
    counted = await service.count_user_cases(OWNER, filters)
    assert counted == total, "count_user_cases must agree with list_user_cases"
    return {c.title for c in page}


@pytest.mark.asyncio
async def test_terminal_cases_are_listed_with_a_default_filter(
    service, one_of_each_state
):
    """A plain ``CaseListFilter()`` hides nothing — RESOLVED and CLOSED included."""
    assert await _titles(service, CaseListFilter()) == {
        "inquiry",
        "investigating",
        "resolved",
        "closed",
    }


@pytest.mark.asyncio
async def test_terminal_cases_are_listed_with_no_filter_at_all(
    service, one_of_each_state
):
    """``filters=None`` means "narrow on nothing", as it does on the real service."""
    assert await _titles(service, None) == {
        "inquiry",
        "investigating",
        "resolved",
        "closed",
    }


@pytest.mark.asyncio
async def test_a_declared_filter_still_narrows(service, one_of_each_state):
    """The fix removes phantom predicates, not the real one.

    ``state`` IS a field on ``CaseListFilter`` and IS read by
    ``CaseService.list_user_cases``, so the stand-in must keep applying it —
    otherwise this change would have traded one half of the defect class for
    the other.
    """
    assert await _titles(service, CaseListFilter(state=CaseState.RESOLVED)) == {
        "resolved"
    }
    assert await _titles(service, CaseListFilter(state=CaseState.CLOSED)) == {"closed"}


@pytest.mark.asyncio
async def test_an_undeclared_filter_field_changes_nothing(service, one_of_each_state):
    """Setting the removed names on a filter object must not resurrect them.

    ``CaseListFilter`` ignores extras, so this is what a caller that still
    believes in ``include_terminal`` actually sends. Before the fix the answer
    was the same either way — which was the bug. After it, it is the same
    because nothing reads them.
    """
    believer = CaseListFilter(**{"include_terminal": False, "include_deleted": False})
    assert not hasattr(believer, "include_terminal")
    assert await _titles(service, believer) == {
        "inquiry",
        "investigating",
        "resolved",
        "closed",
    }


@pytest.mark.asyncio
async def test_source_narrows_the_list(service, one_of_each_source):
    """``source`` reaches a predicate, as it does on the real service.

    ``CaseService.list_user_cases`` forwards ``filters.source`` to
    ``repository.list``. The stand-in had no source block at all, so this
    returned all three cases and a total of 3 — 200, no warning, an unfiltered
    list behind a filter the caller set.
    """
    assert await _titles(service, CaseListFilter(source="slack")) == {"from-slack"}
    assert await _titles(service, CaseListFilter(source="copilot")) == {"from-copilot"}
    assert await _titles(service, CaseListFilter()) == {
        "from-copilot",
        "from-slack",
        "from-api",
    }


@pytest.mark.asyncio
async def test_the_admin_list_narrows_on_source_too(service, one_of_each_source):
    """``GET /api/v1/admin/cases`` declares ``source`` and builds the filter with it.

    ``CaseService.list_all_cases`` forwards ``state`` and ``source``; the
    stand-in read only ``state``, so an operator's source-filtered cross-tenant
    list came back whole.
    """
    page, total = await service.list_all_cases(CaseListFilter(source="slack"))
    assert {c.title for c in page} == {"from-slack"}
    assert total == 1

    page, total = await service.list_all_cases(CaseListFilter())
    assert total == 3


@pytest.mark.asyncio
async def test_a_team_filter_resolves_to_nothing_rather_than_to_everything(
    service, one_of_each_source
):
    """A team filter matches no case here, because no share can exist here.

    The real service resolves ``team_id`` to an allowlist of shared case ids
    and short-circuits ``return [], 0`` when that allowlist is empty. This
    stand-in cannot consult the allowlist — ``resource_shares`` lives in the
    repository it is standing in for — so an empty result is the faithful
    answer. What it did instead was ignore the field and return the caller's
    own cases, which is a filter accepted and applied to nothing.
    """
    assert await _titles(service, CaseListFilter(team_id="team_anything")) == set()


@pytest.mark.asyncio
async def test_count_user_cases_runs_at_all(service, one_of_each_source):
    """The pair is exercised, not just asserted in prose.

    ``count_user_cases`` selected on ``case.owner_id``; ``Case`` declares
    ``user_id``. Every call with a non-empty store raised ``AttributeError``.
    """
    assert await service.count_user_cases(OWNER, CaseListFilter()) == 3
    assert await service.count_user_cases(OWNER, CaseListFilter(source="api")) == 1


@pytest.mark.asyncio
async def test_undeclared_filter_fields_are_not_consulted(service, one_of_each_source):
    """``priority`` and ``owner_id`` are not fields, so nothing may key on them.

    Both had ``hasattr``-guarded blocks that could never fire. This pins the
    absence rather than the blocks: if either is ever added to
    ``CaseListFilter``, this fails and someone decides deliberately whether the
    stand-in should honour it.

    It does NOT pin the field set as a whole. An equality assertion here would
    fail on every future field, under a docstring promising to guard two
    names — a test that goes red for a reason it does not describe teaches
    people to edit the assertion without reading it.
    """
    declared = set(CaseListFilter.model_fields)
    assert "priority" not in declared
    assert "owner_id" not in declared


@pytest.mark.asyncio
async def test_a_falsy_user_id_is_refused_by_both_methods(service, one_of_each_state):
    """Neither listing counts cases the caller did not ask for.

    ``count_user_cases`` answered a falsy ``user_id`` by counting EVERY user's
    cases — ``user_cases = list(self.cases.values())``, under a comment reading
    "Return all cases if no user filter" — while ``list_user_cases`` in the
    same class raised for the same input, and the real
    ``CaseService.count_user_cases`` raises ``ValidationException("User ID is
    required")``. Measured before the fix: one case for alice and two for bob,
    and ``count_user_cases("")`` returned 3.

    That survived a commit dedicated to the parity of these two methods
    because no test ever passed a falsy id. This one does.
    """
    from faultmaven.exceptions import ValidationException

    other = await service.create_case(title="someone-else", owner_id="other_user")
    assert other.user_id != OWNER

    for falsy in ("", None):
        with pytest.raises(ValidationException):
            await service.count_user_cases(falsy, CaseListFilter())
        with pytest.raises(ValidationException):
            await service.list_user_cases(falsy, CaseListFilter())

    # And the owner's own count still excludes the other user's case.
    assert await service.count_user_cases(OWNER, CaseListFilter()) == 4


@pytest.mark.asyncio
async def test_the_total_is_the_match_count_not_the_page_length(service):
    """``total`` counts every match; the page is one slice of them.

    The two are equal in every other test here because no fixture makes more
    cases than the limit — so an assertion comparing them could not, on its
    own, tell a true total from ``len(page)``. This drives a limit below the
    match count so they genuinely differ, and checks that
    ``count_user_cases`` reports the TOTAL rather than the page.
    """
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)
    for i in range(5):
        await service.create_case(title=f"case-{i}", owner_id=OWNER)

    page, total = await service.list_user_cases(OWNER, CaseListFilter(limit=2))
    assert len(page) == 2
    assert total == 5, "the total is every match, not the page"
    assert await service.count_user_cases(OWNER, CaseListFilter(limit=2)) == 5

    second, total = await service.list_user_cases(
        OWNER, CaseListFilter(limit=2, offset=2)
    )
    assert len(second) == 2 and total == 5
    assert {c.case_id for c in page}.isdisjoint({c.case_id for c in second})

    tail, total = await service.list_user_cases(
        OWNER, CaseListFilter(limit=2, offset=4)
    )
    assert len(tail) == 1 and total == 5


@pytest.mark.asyncio
async def test_the_page_is_summaries_converted_one_by_one(service, one_of_each_state):
    """``list_user_cases`` returns ``CaseSummary``, as the real service does.

    It returned raw ``Case`` entities, and the route compensated with an
    UNGUARDED ``CaseSummary.from_case(item)`` inside a handler-wide
    ``except Exception`` — so one case whose conversion failed turned
    ``GET /cases`` from "one case missing" into a 500 with no cases at all.
    """
    from faultmaven.models.api_models import CaseSummary

    page, _ = await service.list_user_cases(OWNER, CaseListFilter())
    assert page and all(isinstance(c, CaseSummary) for c in page)


@pytest.mark.asyncio
async def test_include_empty_keys_on_current_turn(service):
    """``include_empty`` means ``current_turn > 0`` — the repository's predicate.

    The stand-in keyed on ``message_count`` while the repository applies
    ``current_turn > 0`` and the route publishes "Include cases with
    current_turn == 0", so degraded mode returned a case the real service
    hides. ``create_case`` also left ``current_turn`` at 0 for a case it had
    given a message, so the two fields disagreed about the same fact.
    """
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)
    empty = await service.create_case(title="no-message", owner_id=OWNER)
    spoken = await service.create_case(
        title="has-message", owner_id=OWNER, initial_message="hello"
    )

    assert empty.current_turn == 0 and empty.message_count == 0
    assert spoken.current_turn == 1 and spoken.message_count == 1

    assert await _titles(service, CaseListFilter(include_empty=False)) == {
        "has-message"
    }
    assert await _titles(service, CaseListFilter()) == {"no-message", "has-message"}

    # Now make the two fields DISAGREE, which is the only way to see which one
    # the predicate reads. `create_case` keeps them in step, so while it does,
    # keying on either gives the same answer and a test built only on cases it
    # made cannot tell them apart — the mutation that swapped the predicate
    # back to `message_count` passed until this case existed.
    object.__setattr__(empty, "message_count", 7)  # busy by the wrong measure
    assert empty.current_turn == 0  # ...and still empty by the right one
    assert await _titles(service, CaseListFilter(include_empty=False)) == {
        "has-message"
    }, "include_empty must follow current_turn, not message_count"


@pytest.mark.asyncio
async def test_create_case_sets_the_state_field_that_exists(service):
    """``state=``, not ``status=``.

    ``Case`` declares no ``status``, so ``extra='ignore'`` dropped the kwarg
    and the INQUIRY that appeared to come from it came from the field default
    — #1431's mechanism inside the class this file guards. Pinned by asking
    the model, so it fails if ``status`` is ever reintroduced as an alias.
    """
    from faultmaven.modules.case.domain.models import Case

    assert "status" not in Case.model_fields
    assert "state" in Case.model_fields

    # The trap itself, executable: an undeclared `status=` is DROPPED, and the
    # value that survives is the field default. That is why passing
    # `status=CaseState.INQUIRY` looked correct — it agreed with the default by
    # luck. A different value would have vanished.
    planted = Case(
        case_id="case_aaaaaaaaaaaa",
        title="t",
        user_id="u",
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        status=CaseState.RESOLVED,
    )
    assert (
        planted.state is CaseState.INQUIRY
    ), "`status=` is inert on Case; anything passing it is setting nothing"

    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)
    case = await service.create_case(title="fresh", owner_id=OWNER)
    assert case.state is CaseState.INQUIRY


@pytest.mark.asyncio
async def test_a_filter_missing_a_declared_field_raises(service, one_of_each_state):
    """The stand-in reads declared fields directly, so a missing one is an error.

    This is the rule the surrounding commits state — "a field that stops
    existing becomes an error instead of a silent no-op" — made executable.
    The reads were ``getattr(filters, "include_empty", False)`` against a
    field whose declared default is **True**, so renaming the field would have
    flipped the default and started dropping every empty case with no error at
    all: the exact silence this file exists to end. A `getattr` default that
    contradicts the declared one is worse than no default.
    """
    from types import SimpleNamespace

    # Everything `list_user_cases` reads, except `include_empty`.
    crippled = SimpleNamespace(
        state=None,
        source=None,
        team_id=None,
        created_after=None,
        created_before=None,
        limit=50,
        offset=0,
    )
    with pytest.raises(AttributeError):
        await service.list_user_cases(OWNER, crippled)
