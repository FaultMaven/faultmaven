"""Cross-repo cause-shape contract drift-guard (Phase 1).

`CauseRecord` (faultmaven, the consumer's parse target) is the single canonical
v4 cause shape. kb-toolkit's `pack_builder._extract_causes` is the producer; the
two repos cannot import each other, so the contract is held by **drift detection**
rather than a shared import:

  * THIS test is the backend (consumer-side) backstop: every `causes[]` entry in
    the real, vendored, kb-toolkit-produced pack must round-trip through
    `CauseRecord` with no extra and no dropped keys.
  * kb-toolkit carries the mirror (a snapshot test on `_extract_causes` keys).

If the producer adds/renames/drops a field, this test fails loudly — which is the
whole point: the shape is mirrored manually, so it needs a mechanical alarm.
"""

import json
from pathlib import Path

import pytest

from faultmaven.core.investigation.cause_schemas import CauseRecord

pytestmark = pytest.mark.unit

# The frozen canonical field set. Mirrored in kb-toolkit's snapshot test; if you
# change CauseRecord's fields you must update BOTH (and the producer) on purpose.
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
    """CauseRecord's fields == the frozen set the producer mirrors. A change here
    is the trip-wire: update the producer + kb-toolkit snapshot deliberately."""
    assert set(CauseRecord.model_fields) == _CANONICAL_FIELDS


def _load_pack_causes():
    """Yield (relpath, cause_entry) for every cause in the vendored pack, or skip
    if the pack isn't present (e.g. a slim checkout)."""
    from faultmaven.bootstrap.data_init import get_project_root

    pack_json = get_project_root() / "resources" / "knowledge" / "pack" / "pack.json"
    if not pack_json.exists():
        pytest.skip(f"vendored KB pack not present at {pack_json}")
    data = json.loads(Path(pack_json).read_text(encoding="utf-8"))
    out = []
    for rb in data.get("runbooks", []):
        for entry in rb.get("causes", []) or []:
            out.append((rb.get("relpath", "?"), entry))
    if not out:
        pytest.skip("vendored pack carries no causes[] records")
    return out


def test_every_pack_cause_roundtrips_through_causerecord():
    """The cross-repo backstop: every kb-toolkit-produced cause entry parses as a
    CauseRecord with EXACTLY the canonical keys — no producer field the consumer
    drops, no consumer field the producer omits."""
    causes = _load_pack_causes()
    mismatches = []
    for relpath, entry in causes:
        keys = set(entry)
        if keys != _CANONICAL_FIELDS:
            mismatches.append(
                f"{relpath} cause {entry.get('cause_letter','?')}: "
                f"extra={sorted(keys - _CANONICAL_FIELDS)} "
                f"missing={sorted(_CANONICAL_FIELDS - keys)}"
            )
            continue
        # Round-trip: the consumer must actually accept the producer's value.
        CauseRecord(**entry)
    assert not mismatches, "pack cause shape drifted from CauseRecord:\n" + "\n".join(
        mismatches
    )
