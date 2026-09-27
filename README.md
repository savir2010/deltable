# Deltable

A stateless correction layer between an agent and GBrain. It stores facts as typed, versioned **slots** and keeps an append-only **correction log**. Writes are disciplined (resolve → typed error → decide → compare-and-swap commit). Reads are direct lookups with no retrieval step.

```
Agent (any MCP client / Superset workspace)
   │  MCP: write · read · history · reconcile  (+ ingest · report_failure)
   ▼
Delta Memory  — delta_memory/  (no state of its own)
   │  typed rows in schema `delta_memory`, optional markdown page mirror
   ▼
GBrain  — Postgres/pgvector + markdown pages
```

## Quick start

```bash
cd delta-memory
uv venv && uv pip install -e '.[llm,dev]'      # add ,postgres for the GBrain backend
.venv/bin/pytest -q                            # 25 tests (43 with DELTA_MEMORY_TEST_DSN set)
DELTA_MEMORY_OFFLINE=1 .venv/bin/python demo/break_fix.py
.venv/bin/python demo/race.py
```

To hook it into an MCP client, copy `.mcp.json.example` to `.mcp.json`.

To put it inside GBrain's Postgres instead of the local SQLite file:

```bash
uv pip install -e '.[postgres]'
DELTA_MEMORY_DSN="$GBRAIN_DATABASE_URL" delta-memory-mcp   # creates schema delta_memory on first run
```

## Map from design to code

| Design | Code |
|---|---|
| Slot / Correction model | `delta_memory/types.py`, `store/postgres_schema.sql` |
| Stage 1: source adapters | `adapters.py`. `parse_git_diff`, `from_config_diff` and `from_api_response` make no LLM calls. `LLMExtractor` makes one call and assigns confidence 0.8. `HeuristicExtractor` is the offline fallback, at 0.6. |
| Stage 2: key resolution | `resolve.py`. The entity is resolved first, then the predicate within that entity. Each step tries exact → learned alias → similarity ≥ θ → new. Rename diffs resolve by the old value. |
| Stage 3: typed error | `errors.py`. Numbers and dates use a signed delta. Paths, ids, enums and strings use a binary exact match. Only text uses cosine. |
| Stage 4: decision | `engine.py` `DeltaMemory.write`: multi-cardinality → append; within tolerance → NOOP; otherwise → commit |
| Stage 5: CAS commit | `store/*.py` `cas_update`: `UPDATE … WHERE version = ?` plus one log row, in a single transaction |
| Concurrency | A stale `expected_version` or a lost CAS goes to `pending_reconciliation`. Identical racing writes collapse to a NOOP. |
| Read path | `DeltaMemory.read`: exact key or learned alias only. No embedding search. |
| Failure-triggered correction | `failure.py`: dependency registry → re-probe reality → commit only if memory disagrees. A flake commits nothing. |
| MCP surface | `server.py` (FastMCP, stdio) |
| GBrain integration | `store/postgres.py` (tables in GBrain's DB) and `gbrain.py` (optional page mirror, `DELTA_MEMORY_GBRAIN_DIR`) |

## Additions beyond the original sketch

- **Confidence guard.** A lower-confidence source can't overwrite a higher-confidence one; the write goes to review. This lets a Slack message saying "the window is now 14 days" wait until the config diff confirms it, instead of overwriting it immediately.
- **Learned aliases.** Once `refund policies` has resolved to `refund_policy` by similarity, later lookups for it are exact matches. This also lets reads use that alias without doing any similarity search.
- **Append-only enforced in Postgres.** A trigger rejects `UPDATE` and `DELETE` on `corrections`.
- **Multi-slot appends retry and never go to the queue**, because set appends commute.

## Known limitations

- **θ = 0.75 is hand-tuned, not learned.** It comes from `scripts/tune_thresholds.py` running against the built-in n-gram embedder: positives score ≥ 0.782 and negatives ≤ 0.707. The spec's 0.85 would have missed `refund_window_days → window_days`. If you swap in a real embedder, re-run the script. Per-type tolerances are also constants (`errors.py`).
- **Text slots** still rely on one embedding distance, which can't tell "changed" apart from "refined".
- **`infer_dependency` is a hardcoded registry**, one entry per demo procedure step.
- **The default embedder is character n-grams, not a semantic model.** It's good at spelling and formatting variants, weak at synonyms (`return` vs `refund` correctly stays separate, but `cancellation window` would not match `refund window`).
- **Embeddings aren't stored.** Resolution runs across the entity and predicate names in Python, which is fine for demo scale. At fleet scale, move candidate search into pgvector.
- **The GBrain page mirror assumes GBrain ingests markdown pages from a directory.** Adjust the path and frontmatter to match your GBrain setup.
- **`LLMExtractor` has not been run live yet.** It is written against the installed SDK's `messages.parse`, but these tests use a fake extractor and the offline fallback.

## Composable transitions (`delta_memory/composition.py`)

This part asks whether Memorable's procedures can compose, so that A→B from one procedure and B→C from another give A→C even though that path was never stored.

- **Mechanism:** a linear associative memory S, one d×d matrix per context, written with the delta rule `S += β(v − S·k)kᵀ`. It is **not a trained DeltaNet**. There is no network and no learning. States are fixed, near-orthogonal random vectors.
- **Retrieval is not implication.** S·k_A returns B, not C.
- **Why composition works here:** values come from the same space as keys, so S maps states to states and `S·S·k_A ≈ k_C`. That is T_AC = T_BC ∘ T_AB. This constraint is imposed by design, not learned.
- **Conditional relations** use one S per context and compose in order, as S_y·S_x. The wrong order finds nothing.
- **Evidence stays recoverable.** Each transition is a Delta Memory slot `tr_<ctx>_<state>.next`. A changed next state is a typed, versioned correction whose source is the procedure that taught it, and S is rebuilt by replaying that log. Every hop names the episodes behind it, and superseded ones remain in `delta_history`.
- **Capacity (d = 512, random chains):**

  | transitions | one-hop recall | two-hop composition |
  |---|---|---|
  | 256 | 100% | 100% |
  | 512 | 100% | 90% |
  | 768 | 99% | 59% |
  | 1024 | 88% | 44% |

  Composition degrades well before recall does, because errors compound across hops.
- **Each state has one next state per context.** A state that branches needs separate contexts.

MCP tools: `delta_relate(src, dst, context, episode)` and `delta_compose(start, goal, contexts)`.
`memorable_transitions.py` turns real Memorable procedures, read through its CLI, into transitions.
Demo: `python demo/compose_demo.py`.
