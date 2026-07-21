"""Case read-scope SQL — the ``owned ∪ shared-to-my-teams`` visible-id allowlist.

The case analogue of the KB allowlist (``build_kb_scope_filter`` /
``KnowledgeItemRepository._inventory_visibility_clause``, ADR-013 §D4 / ADR-011
D3). The KB allowlist lives at the ChromaDB ``where`` filter because runbooks are
vector items; a case is a relational row, so the same *pattern* is applied at the
SQL ``WHERE`` level here.

A case is visible to a principal when the principal OWNS it (``cases.user_id``) or
it is SHARED to one of the principal's teams via ``resource_shares``. The share
table is the single source of truth for team visibility; the caller resolves the
shared ids (``IShareRepository.list_resource_ids(resource_type="case", ...)``) and
passes them here as ``shared_case_ids``. The scope is keyed entirely on the
caller's own principal/allowlist, so a filter built for user A can never surface
user B's non-shared case.

Behavior-neutral until case shares exist (U10): with an empty ``shared_case_ids``
the clause collapses to the pre-existing owner-only filter.
"""

from typing import Any, Dict, List, Optional


def case_scope_where(
    params: Dict[str, Any],
    user_id: Optional[str],
    shared_case_ids: Optional[List[str]] = None,
    *,
    col_prefix: str = "",
    restrict_case_ids: Optional[List[str]] = None,
) -> Optional[str]:
    """Return the SQL predicate scoping ``cases`` reads to a principal, or ``None``.

    Mutates ``params`` in place with the bound values it references.

    - ``user_id`` falsy → the visibility arm is dropped: the cross-tenant
      platform-admin path (``list_all_cases`` passes ``user_id=None``);
      multi-tenant isolation still applies via PostgreSQL RLS (ADR-010).
    - ``user_id`` set, no ``shared_case_ids`` → ``"user_id = :user_id"`` (the
      unchanged owner-only filter).
    - ``user_id`` set, with ``shared_case_ids`` →
      ``"(user_id = :user_id OR case_id IN (:shared_case_0, ...))"``.

    ``restrict_case_ids`` is the *filter-by-team* facet — an explicit case-id
    allowlist ANDed onto the visibility arm to narrow the result to one team's
    shares. It is distinct from ``shared_case_ids`` (which *widens* to shared
    cases): the caller has already resolved and authorized the team, so this only
    narrows. A non-``None`` empty list matches nothing (``case_id IN ()`` is
    invalid SQL, so a never-true predicate is emitted instead).

    Returns ``None`` only when no arm applies (admin path with no team facet);
    otherwise the arms are ANDed together.

    ``col_prefix`` qualifies the columns for queries that alias the ``cases``
    table (e.g. ``"c."`` in the PostgreSQL full-text search).
    """
    clauses: List[str] = []

    if user_id:
        params["user_id"] = user_id
        owner_clause = f"{col_prefix}user_id = :user_id"
        if shared_case_ids:
            placeholder_names = []
            for i, case_id in enumerate(shared_case_ids):
                key = f"shared_case_{i}"
                params[key] = case_id
                placeholder_names.append(f":{key}")
            placeholders = ", ".join(placeholder_names)
            clauses.append(
                f"({owner_clause} OR {col_prefix}case_id IN ({placeholders}))"
            )
        else:
            clauses.append(owner_clause)

    if restrict_case_ids is not None:
        if not restrict_case_ids:
            # Filter-by-team requested but the team has no shared cases (or the
            # caller is not a member): match nothing rather than emit invalid SQL.
            clauses.append("1 = 0")
        else:
            placeholder_names = []
            for i, case_id in enumerate(restrict_case_ids):
                key = f"restrict_case_{i}"
                params[key] = case_id
                placeholder_names.append(f":{key}")
            placeholders = ", ".join(placeholder_names)
            clauses.append(f"{col_prefix}case_id IN ({placeholders})")

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return " AND ".join(f"({c})" for c in clauses)
