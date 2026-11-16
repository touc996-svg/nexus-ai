# Evaluating the real NexusRAG (Phase 2 adapter)

This document describes how the evaluation harness measures the **production** `NexusRAG` retrieval pipeline, what
the measurements were, how a data-loss incident during the work was recovered and verified, and what is not covered.

## 1. What is real, what is replayed, what is not covered

| Layer | Status in these evaluations |
|---|---|
| `NexusRAG` ingestion (validation, word chunking 1000/150, content hash, metadata), filtered cosine search, score threshold 0.15 | **Real production code**, unmodified (`app/` is byte-identical to the commit before this work) |
| Embedding code path `NexusRAG.embed` (Ollama request, dimension and finite checks) | Real code. On a cache miss it calls a live Ollama. Otherwise the stored vector is replayed, so CI needs no model. |
| Qdrant | **`qdrant-client` in-process local mode**, one fresh isolated collection per run. **Not a Qdrant server.** |
| Qdrant server (Docker) | **Not tested.** Docker was not running. `test_against_a_real_qdrant_server` is skipped unless `EVALS_QDRANT_URL` is set. |
| Hybrid retrieval and reranking | **Not part of production `NexusRAG`** (it is dense-only). The "hybrid" and "rerank" rows are harness experiments that fuse or rerank the production dense results. They are not production behaviour. |

Adapter: `evals/nexus_adapter.py`. It builds an isolated collection named `nexus_eval_<id>` with a unique tenant id, creates
its own Qdrant client (it never reads `QDRANT_URL`), refuses the production collection name, and removes its collection on close.
Tenant isolation is exercised during evaluation: a decoy copy of a document stored for another tenant is never returned.

## 2. Frozen configuration (no parameter was tuned)

Recorded in `evals/frozen_nexus_config.json` (sha `e5bad09980fb8f5f`) and committed before the held-out run, with a test that fails if the
production RAG code, its defaults, the prompts or any dataset file change. Production defaults: embedding model
`nomic-embed-text` (768 dimensions), score threshold 0.15,
chunks 1000/150, cosine distance. Harness: search depth 10, answer context k=3, RRF k=60,
rerank depth 5, rerank model `qwen3:1.7b`, generator `qwen3:4b` with schema-constrained output
(fingerprint `293938d9180f4474`).

## 3. Results

Dev split (25 answerable + 4 unanswerable questions) was used for comparison. The held-out split (21 + 6) was run once
after the configuration was frozen. Reports are machine-readable in `evals/reports/`. A difference of one question moves a
metric by about 0.04, so treat differences smaller than that as noise.

### Retrieval (independent of the answerer)

**Dev**

| System | Hit@1 | Recall@3 | MRR | nDCG@3 |
|---|---|---|---|---|
| BM25 (offline) | 0.880 | 0.920 | 0.910 | 0.905 |
| Lexical hybrid (offline) | 0.880 | 0.920 | 0.910 | 0.905 |
| Dense, prefixed (offline) | 0.840 | 0.980 | 0.920 | 0.931 |
| Hybrid + LLM rerank (offline) | 0.920 | 1.000 | 0.953 | 0.962 |
| **NexusRAG dense (production)** | 0.880 | 0.900 | 0.918 | 0.895 |
| NexusRAG + lexical hybrid (RRF) | 0.880 | 0.940 | 0.921 | 0.917 |
| NexusRAG hybrid + LLM rerank | 0.920 | 0.960 | 0.950 | 0.942 |

**Held-out**

| System | Hit@1 | Recall@3 | MRR | nDCG@3 |
|---|---|---|---|---|
| BM25 (offline) | 0.905 | 0.976 | 0.952 | 0.946 |
| Lexical hybrid (offline) | 0.905 | 0.976 | 0.944 | 0.940 |
| Dense, prefixed (offline) | 0.857 | 0.952 | 0.917 | 0.913 |
| Hybrid + LLM rerank (offline) | 1.000 | 1.000 | 1.000 | 0.996 |
| **NexusRAG dense (production)** | 0.905 | 0.976 | 0.952 | 0.946 |
| NexusRAG + lexical hybrid (RRF) | 0.905 | 0.976 | 0.952 | 0.946 |
| NexusRAG hybrid + LLM rerank | 1.000 | 0.976 | 1.000 | 0.982 |

### Grounded answers (qwen3:4b, structured output, fails closed)

Columns: answer correct, citation precision, lexical faithfulness, correct abstention (unanswerable questions), false abstention (answerable questions refused).

**Dev**

| System | Correct | Citation precision | Faithfulness | Abstain (correct) | Abstain (false) |
|---|---|---|---|---|---|
| NexusRAG dense | 0.840 | 0.917 | 0.906 | 1.00 | 0.04 |
| NexusRAG + lexical hybrid | 0.840 | 1.000 | 0.931 | 1.00 | 0.08 |
| NexusRAG hybrid + rerank | 0.840 | 0.957 | 0.878 | 1.00 | 0.08 |
| Offline hybrid + rerank (PR #3 baseline) | 0.920 | 1.000 | 0.907 | 1.00 | 0.08 |

**Held-out**

| System | Correct | Citation precision | Faithfulness | Abstain (correct) | Abstain (false) |
|---|---|---|---|---|---|
| NexusRAG dense | 0.762 | 1.000 | 0.842 | 1.00 | 0.14 |
| NexusRAG + lexical hybrid | 0.809 | 1.000 | 0.862 | 1.00 | 0.10 |
| NexusRAG hybrid + rerank | 0.809 | 1.000 | 0.843 | 1.00 | 0.10 |
| Offline hybrid + rerank (PR #3 baseline) | 0.809 | 1.000 | 0.846 | 1.00 | 0.10 |

What the numbers support, and what they do not:
- Production dense retrieval performs about like the offline lexical systems on this corpus (on held-out it equals BM25 exactly). Against the offline dense baseline, which adds `nomic-embed-text` task prefixes that production does not, the evidence is mixed: on dev the prefixed baseline found relevant documents production missed on two questions (Recall@3 0.980 vs 0.900), while on held-out production did better (Hit@1 0.905 vs 0.857, Recall@3 0.976 vs 0.952). Each gap is one or two questions, so no conclusion about prefixes can be drawn from this corpus, and changing production embedding behaviour would belong in its own change.
- Reranking the production results helped top-1 on both splits; the answer-level differences between systems are within one or two questions.
- Faithfulness is a lexical proxy. The corpus has 13 short documents and I wrote both question sets, so these are regression guards, not an independent benchmark.

### Latency and memory

Production retrieval for one query (`nexus_dense`), from the recorded reports: dev p50 25.755 ms, p95 35.603 ms; held-out p50 33.529 ms, p95 49.519 ms.
The breakdown below is from the dev report. It is the Qdrant call measured at run time (about 0.196 ms p50, in-process local mode, **not representative of a Qdrant server over a network**)
plus the embedding latency recorded when each vector was first computed live (25.54 ms p50 for `nomic-embed-text`). Replays do not re-measure embedding time.
Peak Ollama memory: 3116 MB (dev), 3155 MB (held-out), 3118 MB (recovery); peak Python 117-127 MB.
One local model was loaded at a time on an 8 GB machine (sampled with `ps`, so approximate).

## 4. Reproducing

```bash
python -m pytest -q                                     # offline: replays from the committed caches, inference blocked
python -m evals.run --check                             # dev regression gate
python -m evals.freeze_nexus_config --check             # frozen configuration unchanged
evals/run_nexus_staged.sh dev                           # live, one model at a time (needs Ollama); held-out only once
EVALS_QDRANT_URL=http://localhost:6333 python -m pytest -q -m qdrant_server   # optional: a real Qdrant server
```

Test markers: `integration_local_qdrant` (real NexusRAG + qdrant-client local mode, embeddings replayed), `unit_no_qdrant`, `qdrant_server` (skipped without a server).

## 5. Cache design and the incident

Cached values (embeddings, rerank judgements, generations) are keyed by a fingerprint of everything that determines them: model, prompt, schema,
options, embedding dimension, the production `rag.py` hash and, for generations, the exact retrieved context in order. Retrieval results are never
cached, so collection, tenant, threshold and limit cannot go stale.

**Incident.** While finishing the held-out work, a stray `git checkout evals/cache` reverted uncommitted cache entries, and two defects in the cache
tooling then prevented a clean rebuild: (1) every cache object flushed its whole in-memory dictionary, so one object overwrote entries written by another;
(2) cleanup kept only entries that had been read again, so freshly written entries were removed. Result: 44 held-out generations were missing and three
held-out reports could not be replayed. Nothing was tuned and no original report was touched.

**Fixes (with regression tests, `tests/test_eval_cache.py`, 16 tests).** Flushes now merge with what is on disk under a file lock; entries read or written during a
run count as used; cleanup flushes pending writes first and refuses to run if the workload cannot be replayed; tests run with model inference blocked.
Each defect was re-introduced on purpose and the tests failed, then passed again once restored.

**Recovery.** `evals/recover_heldout_llm.py` rebuilt only the 44 missing generations (22 dense, 18 hybrid, 10 rerank) with the frozen generator configuration, compared every
answer, citation and abstention flag with the original report, and stores an entry only if it matched. Result: **44 of 44 matched exactly, 0 differed, nothing was quarantined**
(`evals/recovery/heldout_llm_rebuild.json`; 136.9 s). No nondeterminism was observed; the determinism probe only runs for entries that differ, so
this shows these 44 calls reproduced, not that the model is deterministic in general. After the rebuild, **all 12 recorded reports (6 dev, 6 held-out) replay exactly**, field by
field, with inference blocked. The 15 original report and configuration fingerprints were verified unchanged.

## 6. Known limitations

- Qdrant server integration was **not** tested (local client only).
- Hybrid and reranking are harness experiments, not production features.
- Small corpus, one author for both question sets, lexical faithfulness; results are regression guards, not benchmarks.
- Latency is not representative of a networked Qdrant, and embedding latency is recorded rather than re-measured on replay.
- Production embeds raw text without the task prefixes `nomic-embed-text` documents. Whether that affects retrieval quality is unresolved: the dev and held-out comparisons point in opposite directions, each by one or two questions.

### Cleanup limitation: eight exploratory workload combinations (separate, out of scope here)

`python -m evals.prune_cache` replays every system, answerer and split before it removes anything, and refuses (exit code 1, nothing changed) if any combination cannot
be replayed from the cache. Eight combinations are not replayable, so cleanup currently refuses:

- dev: `dense` with the LLM answerer, `broken` with the LLM answerer;
- held-out: `bm25`, `tfidf`, `hybrid`, `dense`, `hybrid_dense` and `broken`, each with the LLM answerer.

No test and no recorded report uses them, and most were never computed (the held-out split was only evaluated with the LLM answerer for the baseline system
and the three NexusRAG systems). The tool's workload definition is broader than what the project actually relies on. This commit does not narrow it and does not rebuild
those combinations; the fix (restrict the workload to the combinations that tests and reports use) is a separate change.
