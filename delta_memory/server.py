"""MCP server. Four core tools plus two adapter entry points.

    delta-memory-mcp                      # stdio, SQLite at ./delta_memory.db
    DELTA_MEMORY_DSN=postgres://... delta-memory-mcp   # inside GBrain's Postgres

Env:
    DELTA_MEMORY_DB          SQLite path (default delta_memory.db)
    DELTA_MEMORY_DSN         Postgres DSN (GBrain's database); overrides DELTA_MEMORY_DB
    DELTA_MEMORY_GBRAIN_DIR  also mirror entities as markdown pages into this brain dir
    DELTA_MEMORY_DEPS        JSON file of procedure-step -> slot dependencies (report_failure)
    DELTA_MEMORY_OFFLINE     skip the LLM; free text uses the heuristic extractor
"""
from __future__ import annotations

import utils.transport
import utils.session
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import adapters
from .engine import DeltaMemory
from .failure import DependencyRegistry, find_file_probe, on_procedure_failure
from .gbrain import MarkdownMirror
from .store import store_from_env


def build_memory() -> DeltaMemory:
    store = store_from_env()
    dm = DeltaMemory(store)
    if os.environ.get("DELTA_MEMORY_GBRAIN_DIR"):
        dm.hooks.append(MarkdownMirror(store, os.environ["DELTA_MEMORY_GBRAIN_DIR"]))
    return dm


def load_registry(path: str | None) -> DependencyRegistry:
    """[{"procedure": "deploy", "step": "run_script", "entity": "deploy_script",
         "predicate": "path", "probe": {"kind": "find_file", "root": "/repo"}}]"""
    reg = DependencyRegistry()
    if not path or not os.path.exists(path):
        return reg
    for d in json.load(open(path)):
        probe = d.get("probe", {})
        if probe.get("kind") == "find_file":
            fn = find_file_probe(probe["root"], probe.get("filename"))
        else:
            raise ValueError(f"unknown probe kind {probe.get('kind')!r}")
        reg.register(d["procedure"], d["step"], d["entity"], d["predicate"], fn)
    return reg


dm = build_memory()
registry = load_registry(os.environ.get("DELTA_MEMORY_DEPS"))
mcp = FastMCP(
    "delta-memory",
    instructions=(
        "Typed, versioned facts with an audit log. read() before relying on a fact; pass the "
        "version you read as expected_version when you write a correction, so a concurrent "
        "change by another agent is detected instead of overwritten. A 'conflict' or "
        "'needs_review' status means your write was queued, not applied: see reconcile()."
    ),
)


@mcp.tool()
def write(entity_hint: str, predicate_hint: str, value: Any, source: str = "manual",
          value_type: str | None = None, cardinality: str = "single",
          expected_version: int | None = None, confidence: float | None = None) -> dict:
    """Write a fact. Resolves the key (exact, alias, or similarity >= threshold), computes a
    typed error against the current value, and either inserts, corrects (CAS on version),
    no-ops a restatement, or queues a conflict for reconciliation.

    value_type: int|float|date|path|id|enum|string|text (inferred if omitted).
    cardinality: 'multi' makes the slot a set; writes append instead of correcting.
    expected_version: the version you read; if the slot has moved since, you get 'conflict'.
    source: e.g. 'tool_call:git_diff', 'llm_extract', 'manual'. Drives default confidence.
    """
    return dm.write(entity_hint, predicate_hint, value, source=source, value_type=value_type,
                    cardinality=cardinality, expected_version=expected_version,
                    confidence=confidence).to_dict()


@mcp.tool()
def read(entity_id: str, predicate: str) -> dict:
    """Direct lookup of the current value, version and confidence. No similarity search."""
    slot = dm.read(entity_id, predicate)
    if slot is None:
        return {"found": False, "entity_id": entity_id, "predicate": predicate}
    return {"found": True, **slot.to_dict()}


@mcp.tool()
def history(entity_id: str, predicate: str) -> dict:
    """Append-only correction log for one slot, oldest first."""
    return {"entity_id": entity_id, "predicate": predicate,
            "corrections": [c.to_dict() for c in dm.history(entity_id, predicate)]}


@mcp.tool()
def reconcile(entity_id: str | None = None, predicate: str | None = None,
              accept: int | None = None, reject: int | None = None) -> dict:
    """List open conflicts (optionally for one slot), or resolve one: accept=<pending_id>
    applies the proposal against the *current* version; reject=<pending_id> discards it."""
    return dm.reconcile(entity_id, predicate, accept=accept, reject=reject)


@mcp.tool()
def ingest(content: str, kind: str = "text") -> dict:
    """Adapter entry point. kind='git_diff' parses renames/config changes with no LLM call;
    kind='text' extracts typed facts from free text with one LLM call (confidence < 1)."""
    if kind == "git_diff":
        results = adapters.ingest_git_diff(dm, content)
    elif kind == "text":
        results = adapters.ingest_text(dm, content)
    else:
        return {"status": "error", "message": f"unknown kind {kind!r}"}
    return {"results": [r.to_dict() for r in results]}


@mcp.tool()
def report_failure(procedure_id: str, failed_step: str, detail: str = "") -> dict:
    """A previously-passing procedure failed. Re-probes the slot that step depends on and
    commits a correction only if reality disagrees with memory (flakes commit nothing)."""
    return on_procedure_failure(dm, registry, procedure_id, failed_step, detail)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
