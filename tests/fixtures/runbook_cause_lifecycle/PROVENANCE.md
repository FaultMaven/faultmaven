# runbook_cause_lifecycle fixture — provenance

`expected_pack_causes.json` is a **byte-identical vendored copy** of the golden
produced by the `faultmaven-kb-toolkit` repo from a real authored v4 runbook
(`tests/fixtures/runbook_cause_lifecycle/sample_runbook.md` there). It is the
shared artifact that joins the two halves of the cross-repo
AUTHORING → PACK → INGEST cause-shape contract test:

- **Front half (kb-toolkit)** `tests/unit/test_pack_causes_contract.py` — runs the
  real `build_pack` over `sample_runbook.md`, asserts each emitted Cause has
  exactly the contract field set, and that the output equals this golden.
- **Back half (this repo)**
  `tests/integration/modules/knowledge/test_runbook_causes_contract.py` —
  ingests this golden via the real `ingest_runbook(causes=...)` and asserts the
  persisted `metadata['causes']` reads back losslessly with exactly the contract
  field set.

## Keeping it in sync

Nothing imports across the two repos, so this copy is kept current by hand. Both
halves pin the same literal field set (`CAUSE_RECORD_FIELDS`), so a field rename
cannot pass both silently. To update after an intentional cause-shape change:

1. In kb-toolkit, run `python tests/fixtures/runbook_cause_lifecycle/regen_golden.py`.
2. Copy the regenerated `expected_pack_causes.json` over this file.
3. Update `CAUSE_RECORD_FIELDS` and `EXPECTED_GOLDEN_SHA256` in both repos'
   contract tests together.
