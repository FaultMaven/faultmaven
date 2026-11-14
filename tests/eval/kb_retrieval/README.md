# KB retrieval — labelled corpora

Two labelled retrieval sets, kept as the repo's only on-domain / off-domain
ground truth for the hybrid KB retrieval path (`KnowledgeVectorStore.hybrid_search`,
the IDF reranker, the case-folded keyword arm). They were built for the KB
cause seeder's admission measurements (#1144, #1272, #1285, #1293) and outlived
it: the seeder was removed in fm#1295, but the retrieval they measure is live
and fm#1272's domain-skew defect is still open.

| file | what it is |
|---|---|
| `labelled-statements.json` | 24 problem statements: 16 with the runbook-title fragments that count as an on-domain match, 8 content-free ones where the correct retrieval outcome is "nothing on-domain". |
| `recorded-hybrid-retrievals.json` | 113 (statement, runbook) pairs recorded from the real hybrid pipeline against the shipped 1297-chunk pack, with the retrieved chunk texts and every query term's document frequency. A recording, not a construction — see the provenance note. |
| `RECORDED-HYBRID-RETRIEVALS-PROVENANCE.md` | How the pairs were selected and recorded, and how to regenerate them. |
| `question-lookups.json` | The knowledge-base question lookup's search (`kb_question_lookup.py`, `orchestration-capabilities.md` §5.6) for 16 bare topics the shipped pack covers and 6 it does not, recorded as the lookup searches: its fetch limit, admitted at its candidate floor, every result's raw cosine and rerank score. Replayed through the lookup by `tests/unit/core/investigation/test_kb_question_floor_recording.py`. |
| `record_question_lookups.py` | Records `question-lookups.json` offline (the pack's pre-built vectors in a scratch Chroma, the local BGE-M3 for the query). Re-record when the pack, the retrieval or the lookup's floor or limit changes. |

The first two sets have no driver here. The drivers that consumed them measured seeding
admission and went with the seeder; the measurements they produced are recorded
in `docs/archive/2026/09/kb-cause-seeder/`. A retrieval-quality driver that
reads these files is the missing piece for re-sizing the reranker weights or
closing fm#1272.
