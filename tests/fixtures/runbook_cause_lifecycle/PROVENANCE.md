# runbook_cause_lifecycle fixture — provenance

`expected_pack_causes.json` is a **byte-identical vendored copy** of the golden
produced by the `faultmaven-kb-toolkit` repo from a real authored v4 runbook
(`tests/fixtures/runbook_cause_lifecycle/sample_runbook.md` there). It is the
shared artifact that joins the two halves of the cross-repo
AUTHORING → PACK → INGEST → RETRIEVE → APPLY cause-shape contract test:

- **Front half (kb-toolkit)** `tests/unit/test_pack_causes_contract.py` — runs the
  real `build_pack` over `sample_runbook.md`, asserts each emitted Cause has
  exactly `CauseRecord`'s field set, and that the output equals this golden.
- **Back half (this repo)**
  `tests/integration/modules/knowledge/test_runbook_cause_lifecycle_e2e.py` —
  ingests this golden via the real `ingest_runbook(causes=...)`, resolves it back
  via the real `get_runbook_causes`, loads it into `CauseRecord`, and instantiates
  it through the real runbook cause matcher.

## Keeping it in sync

Nothing imports across the two repos, so this copy is kept current by hand. Both
halves pin to `CauseRecord`'s field set, so a field rename cannot pass both
silently. To update after an intentional cause-shape change:

1. In kb-toolkit, run `python tests/fixtures/runbook_cause_lifecycle/regen_golden.py`.
2. Copy the regenerated `expected_pack_causes.json` over this file.
3. Update `faultmaven/core/investigation/cause_schemas.py::CauseRecord` and the
   field-set mirror in both tests together.
