"""A population of accounts larger than the account list ever fetched at once.

``UserService.list_users`` used to read at most 1,000 accounts — enterprise and
active status applied by the query — and filter and page that window in Python.
This builds more accounts than that in one enterprise, more than that active
and more than that inactive, with every attribute a filter reads varying by
index so each filter's matches spread across the whole list; and the answer
each listing must give, computed from the listing's rules rather than from its
code.

Shared by the SQLite/in-memory run
(``tests/unit/infrastructure/persistence/test_user_list_filters_before_paging.py``)
and the PostgreSQL run (``tests/integration/test_user_list_paging_postgres.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import List, Optional

from faultmaven.infrastructure.persistence.user_repository import User

#: The window the service used to filter inside.
OLD_WINDOW = 1000

#: Roles by index: none at all (listed as ``member``), each role alone, two at
#: once, and a capitalised one that is not ``admin``.
ROLE_CYCLE = ([], ["admin"], ["member"], ["viewer"], ["viewer", "admin"], ["Admin"])


def shape(*, token: str, enterprise_a: str, enterprise_b: str) -> SimpleNamespace:
    """What names the population — its enterprises, id prefix and search
    needle — without building it; enough to name the cases at collection.

    ``token`` makes every id, email and the needle unique to one population, so
    a database other tests share can be searched without matching their rows.
    """
    return SimpleNamespace(
        enterprise_a=enterprise_a,
        enterprise_b=enterprise_b,
        prefix=f"ulp{token}_",
        needle=f"needle{token}",
        token=token,
    )


def build(
    named: SimpleNamespace,
    *,
    created_from: datetime,
    in_a: int = 2100,
    in_b: int = 30,
) -> List[User]:
    """``in_a`` accounts in enterprise A and ``in_b`` in enterprise B.

    Half the accounts are active, in a period (4) that crosses the role
    cycle's (6), so every role has active and inactive holders. Three accounts
    share each creation instant, so page boundaries fall among ties.
    """

    def account(index: int, enterprise_id: str, kind: str) -> User:
        user_id = f"{named.prefix}{kind}{index:05d}"
        email_needle = f"NeEdLe{named.token}-" if index % 11 == 0 else ""
        name_needle = f"Needle{named.token} " if index % 7 == 0 else ""
        return User(
            user_id=user_id,
            username=user_id,
            email=f"{email_needle}{user_id}@example.com",
            display_name=f"{name_needle}Person {index}",
            enterprise_id=enterprise_id,
            is_active=index % 4 < 2,
            roles=list(ROLE_CYCLE[index % len(ROLE_CYCLE)]),
            created_at=created_from + timedelta(seconds=index // 3),
            updated_at=created_from,
        )

    return [account(i, named.enterprise_a, "a") for i in range(in_a)] + [
        account(i, named.enterprise_b, "b") for i in range(in_b)
    ]


def cases(named: SimpleNamespace) -> List[dict]:
    """Each filter alone and in combination, within enterprise A and across
    both enterprises. The cross-enterprise cases search for the population's
    id prefix — in every one of its emails — or its needle, so no row outside
    the population can match."""
    a = named.enterprise_a
    return [
        {"enterprise_id": a},
        {"enterprise_id": a, "is_active": True},
        {"enterprise_id": a, "is_active": False},
        {"enterprise_id": a, "role": "admin"},
        {"enterprise_id": a, "role": "member"},
        {"enterprise_id": a, "role": "viewer"},
        {"enterprise_id": a, "role": "Admin"},
        {"enterprise_id": a, "search": named.needle.upper()},
        {
            "enterprise_id": a,
            "is_active": True,
            "role": "member",
            "search": named.needle,
        },
        {"search": named.prefix},
        {"is_active": False, "role": "viewer", "search": named.prefix.upper()},
        {"role": "admin", "search": named.needle},
    ]


def case_id(filters: dict) -> str:
    """A readable test id that does not carry the run's random token."""
    named = []
    for key, value in filters.items():
        if key == "enterprise_id":
            value = "A"
        elif key == "search":
            value = "needle" if "needle" in value.lower() else "prefix"
        named.append(f"{key}={value}")
    return "-".join(named) or "unfiltered"


def expected_ids(
    users: List[User],
    *,
    enterprise_id: Optional[str] = None,
    is_active: Optional[bool] = None,
    role: Optional[str] = None,
    search: Optional[str] = None,
) -> List[str]:
    """Filter the WHOLE set, then order it — what every page is a slice of.

    The listing's rules as stated: an account holding no role counts as
    ``member`` and a role matches exactly; search is a case-insensitive literal
    substring of the email or the display name; newest first, ``user_id``
    breaking ties.
    """
    needle = search.lower() if search is not None else None
    rows = [
        u
        for u in users
        if (enterprise_id is None or u.enterprise_id == enterprise_id)
        and (is_active is None or u.is_active == is_active)
        and (role is None or role in (u.roles or ["member"]))
        and (
            needle is None
            or needle in u.email.lower()
            or needle in u.display_name.lower()
        )
    ]
    rows.sort(key=lambda u: u.user_id)
    rows.sort(key=lambda u: u.created_at, reverse=True)
    return [u.user_id for u in rows]


def beyond_old_window(users: List[User], filters: dict) -> set:
    """The matches a listing that filtered only the first ``OLD_WINDOW``
    accounts of its query (enterprise and active status applied) never served.
    A case with none could not tell that listing from a correct one."""
    read = expected_ids(
        users,
        enterprise_id=filters.get("enterprise_id"),
        is_active=filters.get("is_active"),
    )[:OLD_WINDOW]
    return set(expected_ids(users, **filters)) - set(read)


def sample_offsets(matches: int, page: int) -> List[int]:
    """The pages a case reads: the first, the one holding the old window's
    edge, the last, and the one past the end."""
    last = (matches - 1) // page * page if matches else 0
    edge = OLD_WINDOW // page * page
    return sorted({0, min(edge, last), last, matches})


#: ``dev_roles`` texts the writer never produces, by account suffix, oldest
#: first. Every one but the last two holds no roles: it is not a JSON array of
#: strings in the form PostgreSQL's ``jsonb`` accepts.
RAW_ROLES = {
    "bare": "admin",
    "string": '"admin"',
    "object": "{}",
    "broken": "[admin",
    "null": "null",
    "number": "[1]",
    "nul_escape": '["\\u0000"]',
    "lone_surrogate": '["\\ud800"]',
    "empty_text": "",
    "empty_array": "[]",
    "admin": '["admin"]',
    "pair": '["\\ud83d\\ude00", "admin"]',
}

#: What the role filter lists over ``RAW_ROLES``, newest first — written out,
#: not derived from the rule under test.
RAW_ROLES_LISTED = {
    "member": [
        "empty_array",
        "empty_text",
        "lone_surrogate",
        "nul_escape",
        "number",
        "null",
        "broken",
        "object",
        "string",
        "bare",
    ],
    "admin": ["pair", "admin"],
    "\U0001f600": ["pair"],
}


def raw_roles_accounts(
    prefix: str, enterprise_id: str, created_from: datetime
) -> List[User]:
    """One account per ``RAW_ROLES`` entry, holding no roles as written — the
    test overwrites ``dev_roles`` with the raw text afterwards."""
    return [
        User(
            user_id=f"{prefix}{suffix}",
            username=f"{prefix}{suffix}",
            email=f"{prefix}{suffix}@example.com",
            display_name=suffix,
            enterprise_id=enterprise_id,
            roles=[],
            created_at=created_from + timedelta(minutes=minutes),
            updated_at=created_from,
        )
        for minutes, suffix in enumerate(RAW_ROLES)
    ]
