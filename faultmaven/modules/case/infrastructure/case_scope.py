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
) -> Optional[str]:
    """Return the SQL predicate scoping ``cases`` reads to a principal, or ``None``.

    Mutates ``params`` in place with the bound values it references.

    - ``user_id`` falsy → returns ``None``: the caller adds no scope clause. This
      is the cross-tenant platform-admin path (``list_all_cases`` passes
      ``user_id=None``); cloud tenant isolation still applies via RLS (ADR-006).
    - ``user_id`` set, no ``shared_case_ids`` → ``"user_id = :user_id"`` (the
      unchanged owner-only filter).
    - ``user_id`` set, with ``shared_case_ids`` →
      ``"(user_id = :user_id OR case_id IN (:shared_case_0, ...))"``.

    ``col_prefix`` qualifies the columns for queries that alias the ``cases``
    table (e.g. ``"c."`` in the PostgreSQL full-text search).
    """
    if not user_id:
        return None
    params["user_id"] = user_id
    owner_clause = f"{col_prefix}user_id = :user_id"
    if not shared_case_ids:
        return owner_clause
    placeholder_names = []
    for i, case_id in enumerate(shared_case_ids):
        key = f"shared_case_{i}"
        params[key] = case_id
        placeholder_names.append(f":{key}")
    placeholders = ", ".join(placeholder_names)
    return f"({owner_clause} OR {col_prefix}case_id IN ({placeholders}))"
