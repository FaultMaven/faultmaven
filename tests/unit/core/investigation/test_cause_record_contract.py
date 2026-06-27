"""Cross-repo cause-shape contract drift-guard (Phase 1).

`CauseRecord` (faultmaven, the consumer's parse target) is the single canonical
v4 cause shape. kb-toolkit's `pack_builder._extract_causes` is the producer; the
two repos cannot import each other, so the contract is held by **drift detection**
rather than a shared import:

  * THIS test is the backend (consumer-side) backstop: every `causes[]` entry in
    the real, vendored, kb-toolkit-produced pack must carry exactly
    `CauseRecord`'s fields and parse as a `CauseRecord`.
  * kb-toolkit carries the producer-side mirror (a snapshot test on
    `_extract_causes` keys against the same field list).

Scope + known limit (be honest about what this does and doesn't catch):
  * It validates the **vendored** pack artifact, not a freshly-built one. A
    producer change in kb-toolkit is caught here only once the pack is re-vendored
    — re-vendoring is the cross-repo trigger (pack freshness vs runbook source is
    enforced separately by ``scripts/check_kb_pack.py``).
  * It guards fork site #2 (the pack producer) only. The other manual mirrors of
    the cause shape (`runbook_grammar.py`, `runbook_validator.py`,
    `conversion_service.py`) are consolidated/guarded by the Phase-1 field-set
    migration, not here.
"""

import pytest

from faultmaven.core.investigation.cause_schemas import CauseRecord

pytestmark = pytest.mark.unit

# The frozen canonical field set. Mirrored in kb-toolkit's snapshot test; if you
# change CauseRecord's fields you must update BOTH (and the producer) on purpose.
# Only `test_canonical_field_set_is_frozen` reads this literal — the pack guard
# below compares against the live ``CauseRecord.model_fields`` so it tracks the
# source, not a copy that could silently lag it.
_CANONICAL_FIELDS = {
    "cause_letter",
    "cause_name",
    "cause_statement",
    "chain_nodes",
    "chain_edges",
    "rung_indicators",
    "match_predicates",
    "interventions",
    "is_fallback_cause",
}


def test_canonical_field_set_is_frozen():
    """Trip-wire: `CauseRecord`'s fields == the frozen set the producer mirrors.
    A change here is the signal to update the producer + kb-toolkit snapshot
    deliberately (the repos can't import each other, so the mirror is manual)."""
    assert set(CauseRecord.model_fields) == _CANONICAL_FIELDS


def _vendored_pack_causes():
    """Return (relpath, cause_entry) for every cause in the vendored pack via the
    real loader. Skip only when the pack ARTIFACT is absent (a legitimately slim
    checkout — same convention as ``test_shipped_pack_carries_causes_if_vendored``);
    a pack that is present but carries no causes is itself drift → let the caller
    fail, don't skip it away."""
    from faultmaven.bootstrap.data_init import get_project_root
    from faultmaven.bootstrap.kb_pack import KbPack, baseline_pack_dir

    pack = KbPack.load(baseline_pack_dir(get_project_root()))
    if pack is None:
        pytest.skip("vendored KB pack not present (slim checkout)")
    return [(rb.relpath, entry) for rb in pack.runbooks for entry in (rb.causes or [])]


def test_every_pack_cause_matches_causerecord():
    """The cross-repo backstop: every kb-toolkit-produced cause entry carries
    EXACTLY `CauseRecord`'s fields (no producer key the consumer drops, none it
    requires that the producer omits) AND round-trips through `CauseRecord`.

    The key-set check is what catches add/drop drift — `CauseRecord(**entry)`
    ignores unknown keys (pydantic default), so the parse alone can't; it's run on
    every entry to catch *value*-level drift (wrong type for a present field)."""
    causes = _vendored_pack_causes()
    # A present pack with zero causes is drift, not a reason to skip.
    assert causes, "vendored pack present but carries no causes[] — shape drift"

    canonical = set(CauseRecord.model_fields)
    problems: list[str] = []
    for relpath, entry in causes:
        keys = set(entry)
        if keys != canonical:
            problems.append(
                f"{relpath} cause {entry.get('cause_letter', '?')}: "
                f"extra={sorted(keys - canonical)} missing={sorted(canonical - keys)}"
            )
        try:
            CauseRecord(**entry)
        except Exception as exc:  # value-level drift on an otherwise-shaped entry
            problems.append(
                f"{relpath} cause {entry.get('cause_letter', '?')}: "
                f"does not parse as CauseRecord — {exc}"
            )
    assert not problems, "pack cause shape drifted from CauseRecord:\n" + "\n".join(
        problems
    )
