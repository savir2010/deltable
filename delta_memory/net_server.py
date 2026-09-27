"""Persistent, networked Delta Memory server (Streamable HTTP, one long-lived process).

Unlike server.py (stdio: every client spawns its own private server process), this is
ONE process on a fixed port that many independent clients connect to over HTTP. Two
agents racing on a slot are then two separate OS processes hitting the same live
server and the same SQLite file, through the one connection this process owns.

How a race is decided: FastMCP runs these sync tools one at a time on its event loop,
so two concurrent delta_write calls are applied one after the other. Both carry the
same expected_version; the second to arrive fails the `WHERE version = ?` check and
lands in pending_reconciliation. The outcome depends on the loser's stale version being
caught, not on the writes running truly in parallel (they don't, and WAL doesn't make
them; see store/sqlite.py).

Design boundary: this is a symbolic key-value store. Only the write discipline (read
current state, compute the error, write the correction) is borrowed from DeltaNet's
delta rule; nothing here is a neural network or a learned update.

    python -m delta_memory.net_server            # http://127.0.0.1:8787/mcp
Env: DELTA_MEMORY_PORT, DELTA_MEMORY_HOST, DELTA_MEMORY_DB (default ./delta_memory_net.db)
"""
from __future__ import annotations

import itertools
import os
import time
from collections import deque
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import composition as comp
from . import race_ui, ui
from .engine import DeltaMemory
from .store.sqlite import SqliteStore

HOST = os.environ.get("DELTA_MEMORY_HOST", "127.0.0.1")
PORT = int(os.environ.get("DELTA_MEMORY_PORT", "8787"))
DB = os.environ.get("DELTA_MEMORY_DB", "delta_memory_net.db")

dm = DeltaMemory(SqliteStore(DB, single_connection=True))
_arrival = itertools.count(1)
stats = {"calls": 0}
# Every tool call this server receives, as it received it (the run view shows these raw).
CALLS: deque = deque(maxlen=5000)
CALL_LOG = os.environ.get("DELTA_MEMORY_CALL_LOG", "server_calls.jsonl")  # survives restarts
try:
    import json as _json
    with open(CALL_LOG) as _f:
        for _line in _f.readlines()[-5000:]:
            CALLS.append(_json.loads(_line))
except (OSError, ValueError):
    pass
_call_seq = itertools.count((CALLS[-1]["seq"] + 1) if CALLS else 1)


def _client(ctx: Context | None) -> str:
    try:
        info = ctx.session.client_params.clientInfo
        return info.name
    except Exception:
        return "unknown"


def _record(ctx: Context | None, tool: str, args: dict, result: str) -> None:
    entry = {"seq": next(_call_seq), "t": time.time(), "client": _client(ctx), "tool": tool,
             "args": {k: v for k, v in args.items() if v is not None}, "result": result}
    CALLS.append(entry)
    try:
        import json as _json
        with open(CALL_LOG, "a") as f:
            f.write(_json.dumps(entry, default=str) + "\n")
    except OSError:
        pass

mcp = FastMCP(
    "delta-memory",
    host=HOST,
    port=PORT,
    instructions=(
        "Shared, versioned facts with an audit log. Call delta_read before relying on a fact. "
        "When you correct one, pass the version you read as expected_version: if another agent "
        "changed it since, you get status 'conflict' and your value is queued in reconciliation "
        "instead of overwriting theirs."
    ),
)


def _stamp(extra: dict | None = None) -> dict:
    stats["calls"] += 1
    return {"server_pid": os.getpid(), "server_received_ns": time.time_ns(),
            "arrival_seq": next(_arrival), **(extra or {})}


@mcp.tool()
def ping(message: str) -> str:
    """Reachability check."""
    return f"delta-memory received: {message} (server pid {os.getpid()})"


@mcp.tool()
def delta_write(entity_hint: str, predicate_hint: str, value: Any, source: str = "manual",
                value_type: str | None = None, cardinality: str = "single",
                expected_version: int | None = None, confidence: float | None = None,
                ctx: Context | None = None) -> dict:
    """Write a fact. Resolves the key, computes a typed error against the current value, then
    inserts, corrects (compare-and-swap on version), NOOPs a restatement (no log row), or
    queues a conflict in pending_reconciliation. Never overwrites silently.

    value_type: int|float|date|path|id|enum|string|text (inferred if omitted).
    expected_version: the version you read. If the slot moved since, status is 'conflict'.
    source: e.g. 'tool_call:git_diff', 'failure_triggered', 'manual'.
    """
    stamp = _stamp()
    r = dm.write(entity_hint, predicate_hint, value, source=source, value_type=value_type,
                 cardinality=cardinality, expected_version=expected_version, confidence=confidence)
    res = r.status
    if r.slot:
        res += f" · slot now v{r.slot.version}"
    if r.pending_id:
        res += f" · pending #{r.pending_id}"
    _record(ctx, "delta_write", {"entity": entity_hint, "predicate": predicate_hint, "value": value,
                                 "expected_version": expected_version, "source": source}, res)
    return {**r.to_dict(), "server": {**stamp, "server_done_ns": time.time_ns()}}


@mcp.tool()
def delta_read(entity_id: str, predicate: str, ctx: Context | None = None) -> dict:
    """Current value, version and confidence by direct lookup (no similarity search)."""
    slot = dm.read(entity_id, predicate)
    _record(ctx, "delta_read", {"entity": entity_id, "predicate": predicate},
            f"{slot.value.data!r} @ v{slot.version}" if slot else "not found")
    if slot is None:
        return {"found": False, "entity_id": entity_id, "predicate": predicate}
    return {"found": True, **slot.to_dict()}


@mcp.tool()
def delta_history(entity_id: str, predicate: str, ctx: Context | None = None) -> dict:
    """Append-only correction log for one slot, oldest first."""
    _record(ctx, "delta_history", {"entity": entity_id, "predicate": predicate}, "ok")
    return {"entity_id": entity_id, "predicate": predicate,
            "corrections": [c.to_dict() for c in dm.history(entity_id, predicate)]}


@mcp.tool()
def delta_reconcile(entity_id: str | None = None, predicate: str | None = None,
                    accept: int | None = None, reject: int | None = None,
                    ctx: Context | None = None) -> dict:
    """List open conflicts (optionally for one slot), or resolve one: accept=<pending_id> applies
    the proposal against the current version; reject=<pending_id> discards it."""
    out = dm.reconcile(entity_id, predicate, accept=accept, reject=reject)
    _record(ctx, "delta_reconcile", {"entity": entity_id, "predicate": predicate, "accept": accept,
                                     "reject": reject},
            out.get("status", "") + (f" · {len(out['pending'])} open" if "pending" in out else ""))
    return out


@mcp.tool()
def delta_relate(src: str, dst: str, context: str = "default", episode: str = "manual",
                 expected_version: int | None = None, namespace: str = "",
                 ctx: Context | None = None) -> dict:
    """Record a transition src -> dst (e.g. a procedure step: build -> container), under a
    context for conditional relations. Stored as slot tr_<context>_<src>.next, so changing
    what src leads to is a typed, versioned correction with its episode as the source."""
    r = comp.record_transition(dm, src, dst, context, episode, expected_version, namespace=namespace)
    _record(ctx, "delta_relate", {"entity": "transition", "src": src, "dst": dst, "namespace": namespace,
                                  "context": context},
            r.status + (f" · slot now v{r.slot.version}" if r.slot else ""))
    return r.to_dict()


@mcp.tool()
def delta_compose(start: str, goal: str | None = None, contexts: list[str] | None = None,
                  max_hops: int = 8, namespace: str = "", ctx: Context | None = None) -> dict:
    """Compose stored transitions. Rebuilds the delta-rule associative matrix S from the
    transition log (stateless), then walks start -> ... -> goal. Returns the path, each hop's
    score and the episodes that taught it, plus two contrasts: what ONE-hop recall of `start`
    returns (retrieval alone), and whether the composite operator S_n...S_1 applied directly,
    without clean-up, lands on the same end state (T_AC = T_BC o T_AB)."""
    mem = comp.load(dm, namespace=namespace)
    start_n = comp.norm_state(start)
    goal_n = comp.norm_state(goal) if goal else None
    ctxs = [comp.norm_context(c) for c in contexts] if contexts else None
    path = mem.compose(start_n, goal_n, ctxs, max_hops)
    one_hop = {c: mem.recall(start_n, c) for c in (ctxs or sorted(mem.S))}
    out = {
        "start": start_n, "goal": goal_n, "found": bool(path) if goal_n else None,
        "path": [{"src": h.src, "dst": h.dst, "context": h.context, "score": round(h.score, 4),
                  "episodes": h.episodes} for h in path],
        "one_hop_recall": {c: {"state": n, "score": round(s, 4)} for c, (n, s) in one_hop.items()},
        "transitions_stored": sum(len(v) for v in mem.evidence.values()),
        "d": mem.space.d,
    }
    if path:
        end, score = mem.apply(start_n, [h.context for h in path])
        out["composite_operator"] = {"contexts": [h.context for h in path], "lands_on": end,
                                     "score": round(score, 4), "agrees": end == path[-1].dst}
    _record(ctx, "delta_compose", {"entity": "transition", "start": start_n, "goal": goal_n,
                                   "namespace": namespace},
            (" → ".join([start_n] + [h.dst for h in path]) if path else "no path"))
    return out


ui.register(mcp, dm, stats, CALLS)       # technical console at /console
race_ui.register(mcp, dm, CALLS)         # audience demo at /ui  # live terminal UI at /ui, same dm as the tools


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
