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

There is no driver here. The drivers that consumed these sets measured seeding
admission and went with the seeder; the measurements they produced are recorded
in `docs/archive/2026/09/kb-cause-seeder/`. A retrieval-quality driver that
reads these files is the missing piece for re-sizing the reranker weights or
closing fm#1272.
