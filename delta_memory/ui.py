"""Live terminal-style UI, served by the persistent server next to the MCP endpoint.

    http://127.0.0.1:8787/ui

Routes (all read or write the same `dm` the MCP tools use; nothing is simulated):
    GET  /ui            the page (delta_memory/ui/index.html)
    GET  /api/state     slots, recent corrections, open reconciliations, server stats
    GET  /api/compose   path + S and S@S association grids for a transition namespace
    POST /api/cmd       one terminal command line -> output lines
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

from . import composition as comp

ROOT = Path(__file__).resolve().parents[1]
RUN_LOGS = Path.home() / "yc-hack" / "superset-run"
ROLES = ("builder", "mover", "deployer")
RUN_FILE = RUN_LOGS / "run.json"
AGENT_LINE = re.compile(r"^(?P<t>\d\d:\d\d:\d\d\.\d+) \[(?P<role>\w+) pid (?P<pid>\d+)\] (?P<msg>.*)$")
RUN = {"proc": None, "status": "idle", "started": None, "out": [], "code": None, "screens": {}}
PAGE = Path(__file__).parent / "ui" / "index.html"
STARTED = time.time()
ANSI = re.compile(r"\x1b\[[0-9;]*m")

HELP = """commands
  read <entity> <predicate>                      current value, version, source
  write <entity> <predicate> <value> [--v N]     typed write; --v = expected_version (CAS)
  history <entity> <predicate>                   append-only correction log
  pending                                        open reconciliation queue
  accept <id> | reject <id>                      resolve a queued conflict
  relate <src> <dst> [--ctx C] [--ns N]          record a transition src -> dst
  compose <start> [goal] [--ns N]                compose transitions (opens compose view)
  demo                                           fresh isolated namespace: build->container->kubernetes
  race                                           two real OS processes race one slot on this server
  clear                                          clear the terminal"""


def _transition_namespaces(dm) -> list[str]:
    seen = []
    for e in dm.store.list_entities():
        if e.startswith(comp.PREFIX) and "_" in e:
            ns = e[len(comp.PREFIX):e.index("_")]
            if ns not in seen:
                seen.append(ns)
    return seen


def _fmt_slot(s) -> str:
    return (f"{s.entity_id}.{s.predicate} = {s.value.data!r} ({s.value.type.value}) "
            f"@ v{s.version}  source={s.source} conf={s.confidence:.2f}")


def _local(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return ""


SUPERSET = str(Path.home() / "superset" / "bin" / "superset")
WS_FILE = RUN_LOGS / "workspaces.json"


async def _superset(*args: str) -> dict:
    p = await asyncio.create_subprocess_exec(SUPERSET, *args, "--json", stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    import json as _json
    return _json.loads(out or b"{}")


async def _read_screens() -> None:
    """Read each workspace's real Superset terminal screen while the run is going."""
    import json as _json
    terminals: dict[str, str] = {}
    while True:
        running = RUN["status"] == "running"
        if WS_FILE.exists() and WS_FILE.stat().st_mtime >= (RUN["started"] or 0) - 1:
            for role, w in _json.loads(WS_FILE.read_text()).items():
                try:
                    if role not in terminals:
                        ses = (await _superset("terminals", "list", "--local", "--workspace", w["id"])).get("sessions", [])
                        if not ses:
                            continue
                        terminals[role] = ses[0]["terminalId"]
                    t = await _superset("terminals", "read", "--local", "--workspace", w["id"],
                                        "--terminal", terminals[role])
                    text = next((v for v in t.values() if isinstance(v, str) and "\n" in v), "")
                    rows = text.splitlines()
                    # the screen is hard-wrapped at the terminal's width: rows that fill the full
                    # width continue on the next row, so join them back into logical lines
                    width = max((len(r) for r in rows), default=0)
                    lines, buf = [], ""
                    for r_ in rows:
                        buf += r_
                        if len(r_) < width or width == 0:
                            lines.append(buf.rstrip()); buf = ""
                    if buf:
                        lines.append(buf.rstrip())
                    while lines and not lines[-1]:
                        lines.pop()
                    RUN["screens"][role] = {"workspace": w["name"], "workspace_id": w["id"],
                                            "terminal_id": terminals[role], "lines": lines[-30:],
                                            "read_at": time.strftime("%H:%M:%S")}
                except Exception:
                    pass
        if not running:
            return  # one final read after the run ended
        await asyncio.sleep(0.8)


async def _pump(proc) -> None:
    async for raw in proc.stdout:
        RUN["out"].append(ANSI.sub("", raw.decode(errors="replace")).rstrip())
    RUN["code"] = await proc.wait()
    RUN["status"] = "done" if RUN["code"] == 0 else "failed"
    await asyncio.sleep(0.5)
    await _read_screens()


def _fmt_args(a: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in a.items() if k not in ("entity", "predicate")) or ""


BRANCH = {"inserted": "insert", "noop": "noop", "appended": "append", "corrected": "overwrite",
          "conflict": "queue", "needs_review": "queue"}


def _run_state(dm, calls) -> dict:
    import json as _json
    idle = RUN["status"] == "idle"
    started = RUN["started"] or 0
    meta = {}
    if not idle and RUN_FILE.exists() and RUN_FILE.stat().st_mtime >= started - 1:
        meta = _json.loads(RUN_FILE.read_text())
    rid, ns = meta.get("rid"), meta.get("ns", "")

    log, branches = [], {k: {"count": 0, "last": None} for k in ("insert", "noop", "append", "overwrite", "queue")}
    for c in (calls if not idle else []):
        if c["t"] < started or c["client"] == "orchestrator":
            continue
        t = time.strftime("%H:%M:%S", time.localtime(c["t"]))
        who = c["client"].split(" (")[0]
        status = c["result"].split(" ")[0]
        br = BRANCH.get(status) if c["tool"] in ("delta_write", "delta_relate") else None
        if c["tool"] == "delta_relate":
            key, val = f"step {c['args'].get('src')} → {c['args'].get('dst')}", ""
        else:
            key = f"{c['args'].get('entity', '')}.{c['args'].get('predicate', '')}"
            val = c["args"].get("value", "")
        if br:
            branches[br]["count"] += 1
            branches[br]["last"] = {"who": who, "key": key, "value": val, "t": t, "result": c["result"]}
        log.append({"t": t, "seq": c["seq"], "who": who, "client": c["client"], "tool": c["tool"],
                    "args": _fmt_args(c["args"]), "result": c["result"], "branch": br})

    facts = []
    for e, p in [("deploy_script", "path")] + ([(f"release_{rid}", "agents")] if rid else []):
        sl = dm.store.get_slot(e, p)
        if sl:
            facts.append({"key": f"{e}.{p}", "value": sl.value.data, "version": sl.version,
                          "cardinality": sl.cardinality.value, "source": sl.source})

    implied = None
    if ns:
        mem = comp.load(dm, namespace=ns)
        steps = []
        for (ctx, src), evs in mem.evidence.items():
            if evs:
                dst, score = mem.recall(src, ctx)
                steps.append({"src": src, "dst": evs[-1]["dst"], "by": evs[-1]["episode"].split(" (")[0],
                              "score": round(score, 3)})
        path = mem.compose("source", "cluster") if mem.S else []
        one = mem.recall("source") if mem.S else (None, 0.0)
        op = mem.apply("source", [h.context for h in path]) if path else (None, 0.0)
        implied = {"steps": steps, "path": [h.dst for h in path], "found": bool(path),
                   "recall": {"state": one[0] if one[1] > 0.5 else None, "score": round(one[1], 3)},
                   "operator": {"lands_on": op[0], "score": round(op[1], 3), "power": len(path)}}

    checks = [{"ok": l.strip().startswith("PASS"), "text": l.strip()[4:].strip()}
              for l in RUN["out"] if l.strip().startswith(("PASS", "FAIL"))]
    phase = ("idle" if idle else "creating" if not RUN.get("screens") else
             "running" if RUN["status"] == "running" else RUN["status"])
    return {"status": RUN["status"], "phase": phase, "mode": RUN.get("mode"), "rid": rid,
            "elapsed": round(time.time() - started, 1) if started else 0,
            "screens": RUN.get("screens", {}), "branches": branches, "log": log, "facts": facts,
            "implied": implied, "checks": checks,
            "error": "\n".join(RUN["out"][-8:]) if RUN["status"] == "failed" else None}


def register(mcp, dm, stats: dict, calls=()) -> None:
    @mcp.custom_route("/api/run", methods=["GET", "POST"])
    async def run(req: Request):
        if req.method == "POST":
            if RUN["status"] == "running":
                return JSONResponse({"error": "a run is already in progress"}, status_code=409)
            try:
                mode = (await req.json()).get("mode", "cursor")
            except Exception:
                mode = "cursor"
            mode = mode if mode in ("cursor", "scripted") else "cursor"
            RUN.update(status="running", started=time.time(), out=[], code=None, screens={}, mode=mode)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "superset_multiagent.py", "--mode", mode, cwd=str(ROOT),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            loop = asyncio.get_running_loop()
            loop.create_task(_pump(proc))
            loop.create_task(_read_screens())
            return JSONResponse({"status": "running"})
        return JSONResponse(_run_state(dm, calls))

    @mcp.custom_route("/console", methods=["GET"])
    async def page(_: Request):
        return FileResponse(PAGE)

    @mcp.custom_route("/api/state", methods=["GET"])
    async def state(_: Request):
        slots = []
        for e in dm.store.list_entities():
            for p in dm.store.list_predicates(e):
                s = dm.store.get_slot(e, p)
                if s:
                    slots.append({**s.to_dict(), "transition": e.startswith(comp.PREFIX) and "_" in e})
        log = [c.to_dict() for c in dm.store.recent_corrections(200)]
        pend = [p.to_dict() for p in dm.store.list_pending()]
        return JSONResponse({
            "server": {"pid": os.getpid(), "uptime_s": round(time.time() - STARTED),
                       "db": getattr(dm.store, "path", "postgres"), "writes": stats["calls"]},
            "slots": slots, "log": log, "pending": pend,
            "namespaces": _transition_namespaces(dm),
        })

    @mcp.custom_route("/api/compose", methods=["GET"])
    async def compose(req: Request):
        q = req.query_params
        ns = q.get("ns", "")
        mem = comp.load(dm, namespace=ns)
        start = comp.norm_state(q.get("start", "")) if q.get("start") else None
        goal = comp.norm_state(q.get("goal", "")) if q.get("goal") else None
        ctxs = sorted(mem.S)
        path = mem.compose(start, goal) if start else []
        states = list(mem.space._vecs)[:14]
        grids = []
        if ctxs:
            # stored: every edge actually written, summed over contexts
            stored = None
            for c_ in ctxs:
                g = comp.association_grid(mem, [c_], states, 1)["grid"]
                stored = g if stored is None else [[x + y for x, y in zip(r1, r2)] for r1, r2 in zip(stored, g)]
            grids.append({"label": "S  (stored)", "grid": stored})
            # implied: the composite operator along the found path (default: S @ S)
            use = [h.context for h in path] if path else [ctxs[0], ctxs[0]]
            implied = comp.association_grid(mem, use, states, 1)["grid"]
            grids.append({"label": " @ ".join("S_" + x for x in reversed(use)) + "  (implied)", "grid": implied})
        out = {"namespace": ns, "contexts": ctxs, "start": start, "goal": goal, "grids": grids,
               "states": states,
               "path": [{"src": h.src, "dst": h.dst, "context": h.context, "score": round(h.score, 3),
                         "episodes": h.episodes} for h in path]}
        if start:
            out["one_hop"] = [{"context": c, "state": n, "score": round(s, 3)}
                              for c in ctxs for n, s in [mem.recall(start, c)]]
            if path:
                end, score = mem.apply(start, [h.context for h in path])
                out["operator"] = {"lands_on": end, "score": round(score, 3), "agrees": end == path[-1].dst}
        return JSONResponse(out)

    @mcp.custom_route("/api/cmd", methods=["POST"])
    async def cmd(req: Request):
        line = (await req.json()).get("line", "").strip()
        try:
            return JSONResponse(await run_command(dm, line))
        except Exception as e:  # show the error in the terminal, don't 500
            return JSONResponse({"lines": [["err", f"error: {e}"]]})


def _opt(args: list[str], name: str, default=None):
    if name in args:
        i = args.index(name)
        val = args[i + 1] if i + 1 < len(args) else default
        del args[i:i + 2]
        return val
    return default


async def run_command(dm, line: str) -> dict:
    if not line:
        return {"lines": []}
    args = shlex.split(line)
    c, a = args[0].lower(), args[1:]
    out: list[list[str]] = []

    if c in ("help", "?"):
        out = [["dim", l] for l in HELP.splitlines()]
    elif c == "clear":
        return {"lines": [], "clear": True}
    elif c == "read" and len(a) == 2:
        s = dm.read(*a)
        out = [["ok", _fmt_slot(s)]] if s else [["warn", f"{a[0]}.{a[1]} not found"]]
    elif c == "write" and len(a) >= 3:
        v = _opt(a, "--v"); t = _opt(a, "--type")
        r = dm.write(a[0], a[1], " ".join(a[2:]), source="tool_call:ui", value_type=t,
                     expected_version=int(v) if v else None)
        cls = {"corrected": "ok", "inserted": "ok", "noop": "dim"}.get(r.status, "warn")
        out = [[cls, f"{r.status.upper()}  {r.message}"]]
        if r.pending_id:
            out.append(["warn", f"queued as pending #{r.pending_id}: run `pending`, then accept/reject"])
    elif c == "history" and len(a) == 2:
        h = dm.history(*a)
        out = [["dim", f"v{x.version_from}->v{x.version_to} {x.kind:<10} {x.reason}"] for x in h] \
            or [["warn", "no history"]]
    elif c == "pending":
        ps = dm.store.list_pending()
        out = [["warn", f"#{p.id} {p.entity_id}.{p.predicate}: proposed {p.proposed_value.data!r} "
                        f"vs current {(p.current_value.data if p.current_value else '?')!r} "
                        f"(read v{p.read_version}, slot v{p.current_version}) from {p.source}"] for p in ps] \
            or [["dim", "reconciliation queue is empty"]]
    elif c in ("accept", "reject") and len(a) == 1:
        r = dm.reconcile(**{c: int(a[0].lstrip("#"))})
        out = [["ok" if r["status"] in ("accepted", "rejected") else "warn",
                f"{r['status']}  {r.get('message', '')}"]]
    elif c == "relate" and len(a) >= 2:
        ctx = _opt(a, "--ctx", "default"); ns = _opt(a, "--ns", "")
        r = comp.record_transition(dm, a[0], a[1], ctx, "ui", namespace=ns)
        out = [["ok", f"{r.status.upper()}  {r.message}"]]
    elif c == "compose" and a:
        ns = _opt(a, "--ns", "")
        return {"lines": [["dim", f"composing {a[0]} -> {a[1] if len(a) > 1 else '...'} in namespace "
                                  f"{ns or 'default'}"]],
                "compose": {"ns": ns, "start": a[0], "goal": a[1] if len(a) > 1 else ""}}
    elif c == "demo":
        ns = f"r{int(time.time())}"
        steps = [("build", "container", "default", "fixture:deploy-service-x"),
                 ("container", "kubernetes", "default", "fixture:deploy-to-k8s")]
        for s_, d_, ctx, ep in steps:
            comp.record_transition(dm, s_, d_, ctx, ep, namespace=ns)
            out.append(["ok", f"relate {s_} -> {d_}   ({ep})"])
        out.append(["dim", f"isolated namespace {ns}; two separately learned episodes"])
        return {"lines": out, "compose": {"ns": ns, "start": "build", "goal": "kubernetes"}}
    elif c == "race":
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "race_orchestrator.py", cwd=str(ROOT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        raw, _ = await proc.communicate()  # awaited, so this server keeps serving the racers
        for l in ANSI.sub("", raw.decode()).splitlines():
            cls = "ok" if "PASS" in l or "corrected" in l else "err" if "FAIL" in l else \
                  "warn" if "conflict" in l else "dim"
            out.append([cls, l])
    else:
        out = [["err", f"unknown or incomplete command: {line}  (type help)"]]
    return {"lines": out}
