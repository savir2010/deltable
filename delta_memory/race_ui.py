"""Backend for the audience-friendly demo page (/ui): two approaches, one problem.

    POST /api/race {"mode": "cursor"|"scripted", "save": true}   start approach_race.py
    GET  /api/race                                                live state for the page
Everything is read from real sources: race.json (the orchestrator), each agent's trace file,
delta memory slots, and the server's own call log. Plain-language labels are derived from the
actual commands; nothing is invented.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parents[1]
LOGS = Path.home() / "yc-hack" / "superset-run"
STATE = LOGS / "race.json"
PAGE = Path(__file__).parent / "ui" / "demo.html"
APPROACHES = ("explorer", "memory")
MILESTONES = ["starting", "found", "fixed", "deployed"]
RACE = {"status": "idle", "started": None, "out": [], "code": None}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def friendly(c: dict) -> dict:
    """One plain-language line for a real tool call."""
    cmd = (c.get("input") or {}).get("command", "")
    path = (c.get("input") or {}).get("file_path", "")
    ok = (c.get("result") or {}).get("ok")
    m = re.search(r"tools/dm write fix_\d+ (\w+) (.+?)(?: --|$)", cmd)
    if m and m.group(1) == "script_location":
        return {"text": "told the team where the script is", "kind": "memory"}
    if m:
        return {"text": f"posted a checkpoint: {m.group(2).strip(chr(34))}", "kind": "checkpoint"}
    if "tools/dm read" in cmd:
        return {"text": "asked shared memory where the script is", "kind": "memory"}
    if re.search(r"\bsh \S+ staging", cmd):
        return {"text": "ran the deploy: it worked" if ok else "ran the deploy: it failed", "kind": "ok" if ok else "bad"}
    if re.search(r"\b(sed|perl|awk)\b.*config\.yaml|>.*config\.yaml", cmd) or (c["name"] == "Edit" and "config" in path):
        return {"text": "fixed the settings file (config.yaml)", "kind": "ok"}
    if re.search(r"\b(find|grep|rg|ls|fd)\b", cmd) or c["name"] in ("grep", "glob", "ls"):
        return {"text": "searched the files", "kind": "search"}
    if re.search(r"\b(cat|head|tail|less)\b", cmd) or c["name"] == "Read":
        f = path or (cmd.split()[-1] if cmd.split() else "")
        return {"text": f"read {f.split('/')[-1] or 'a file'}", "kind": "read"}
    if cmd.startswith("test "):
        return {"text": "checked the file exists", "kind": "read"}
    return {"text": "ran a command" if cmd else f"used {c['name']}", "kind": "other"}


def category(c: dict) -> str:
    cmd = (c.get("input") or {}).get("command", "")
    if "tools/dm write" in cmd: return "mem-write"
    if "tools/dm read" in cmd or "tools/dm wait" in cmd: return "mem-read"
    if re.search(r"\bsh \S+ staging", cmd): return "exec"
    if re.search(r"\b(sed|perl|awk)\b|>\s*config", cmd) or c["name"] == "Edit": return "edit"
    if re.search(r"\b(find|grep|rg|ls|fd)\b", cmd) or c["name"] in ("grep", "glob", "ls"): return "search"
    if re.search(r"\b(cat|head|tail|test)\b", cmd) or c["name"] == "Read": return "read"
    return "other"


def milestone(text: str) -> int:
    t = text.lower()
    for i, m in reversed(list(enumerate(["start", "found", "fixed", "deploy"]))):
        if m in t:
            return i
    return 0 if t else -1


def _state(dm, calls) -> dict:
    st = json.loads(STATE.read_text()) if STATE.exists() else {}  # last run survives a restart
    rid = st.get("rid")
    lanes = {}
    for a in APPROACHES:
        tf = LOGS / f"{a}.trace.json"
        tr = json.loads(tf.read_text()) if (rid and tf.exists()) else {}
        if tr.get("rid") != rid:
            tr = {}
        cp = dm.store.get_slot(f"fix_{rid}", a) if rid else None
        hist = dm.history(f"fix_{rid}", a) if cp else []
        u = tr.get("tokens") or {}
        started = tr.get("started")
        end = tr.get("finished") or time.time()
        steps = [friendly(c) for c in tr.get("tool_calls", [])]
        go = st.get("go") or started or 0
        raw = [{"t": round(c.get("t", go) - go, 2), "ms": c.get("ms", 0), "cat": category(c),
                "tool": c["name"], "cmd": (c.get("input") or {}).get("command")
                or (c.get("input") or {}).get("file_path") or json.dumps(c.get("input") or {})[:120],
                "exit": (c.get("result") or {}).get("exit_code"), "ok": (c.get("result") or {}).get("ok")}
               for c in tr.get("tool_calls", [])]
        lanes[a] = {
            "checkpoint": cp.value.data if cp else None,
            "checkpoint_version": cp.version if cp else 0,
            "checkpoints": [h.new_value.data for h in hist],
            "milestone": max([milestone(h.new_value.data) for h in hist] or [-1]),
            "steps": steps[-12:], "step_count": len(steps), "trace": raw,
            "input_tokens": u.get("inputTokens") if u else None, "output_tokens": u.get("outputTokens") if u else None,
            "wrong_turns": sum(1 for s in steps if s["kind"] == "bad"),
            "seconds": round(((tr.get("duration_ms") or 0) / 1000) if tr.get("finished") and tr.get("duration_ms")
                             else (end - started if started else 0), 1),
            "tokens": (u.get("inputTokens", 0) + u.get("outputTokens", 0)) if u else None,
            "cached": u.get("cacheReadTokens") if u else None,
            "done": bool(tr.get("finished")), "answer": (tr.get("answer") or "")[-240:],
        }

    # how shared memory converged on "where is the script?"
    conv = []
    if rid:
        for c in calls:
            a = c["args"]
            if c["tool"] == "delta_write" and a.get("entity") == f"fix_{rid}" and a.get("predicate") == "script_location":
                who = c["client"].split(" (")[0]
                status = c["result"].split(" ")[0]
                conv.append({"who": who, "value": a.get("value"), "status": status,
                             "t": time.strftime("%H:%M:%S", time.localtime(c["t"]))})
    loc = dm.store.get_slot(f"fix_{rid}", "script_location") if rid else None
    go = st.get("go") or 0
    mem_events = []
    if rid and go:  # a skipped run never started: no timeline
        for c in calls:
            a = c["args"]
            if c["tool"] in ("delta_write", "delta_read") and c["client"] != "orchestrator" and c["t"] >= go - 1 and (
                    str(a.get("entity", "")).startswith(f"fix_{rid}") or a.get("entity") == "deploy_script"):
                mem_events.append({"t": round(c["t"] - go, 2), "who": c["client"].split(" (")[0], "tool": c["tool"],
                                   "key": f"{a.get('entity')}.{a.get('predicate')}", "value": a.get("value"),
                                   "branch": c["result"].split(" ")[0] if c["tool"] == "delta_write" else "read",
                                   "result": c["result"]})

    return {
        "status": RACE["status"], "rid": rid, "mode": st.get("mode"), "phase": st.get("phase", "idle"),
        "problem": st.get("problem"), "lanes": lanes,
        "shared": {"value": loc.value.data if loc else None, "version": loc.version if loc else 0,
                   "writes": conv}, "mem_events": mem_events,
        "t_now": (round(time.time() - go, 2) if RACE["status"] == "running" else
                  round(max([l_["seconds"] for l_ in lanes.values()] or [0]), 2)) if go else 0,
        "results": st.get("results"), "winner": st.get("winner"), "saved": st.get("saved"),
        "discarded": st.get("discarded"), "reason": st.get("reason"),
        "economics": st.get("economics"), "recalled": st.get("recalled"),
        "elapsed": round(time.time() - RACE["started"], 1) if RACE["started"] else 0,
        "error": "\n".join(RACE["out"][-6:]) if RACE["status"] == "failed" else None,
    }


async def _pump(proc) -> None:
    async for raw in proc.stdout:
        RACE["out"].append(ANSI.sub("", raw.decode(errors="replace")).rstrip())
    RACE["code"] = await proc.wait()
    RACE["status"] = "done" if RACE["code"] == 0 else "failed"


def register(mcp, dm, calls) -> None:
    @mcp.custom_route("/ui", methods=["GET"])
    async def page(_: Request):
        return FileResponse(PAGE)

    @mcp.custom_route("/api/race", methods=["GET", "POST"])
    async def race(req: Request):
        if req.method == "GET":
            return JSONResponse(_state(dm, calls))
        if RACE["status"] == "running":
            return JSONResponse({"error": "already running"}, status_code=409)
        try:
            body = await req.json()
        except Exception:
            body = {}
        mode = body.get("mode") if body.get("mode") in ("cursor", "scripted") else "cursor"
        args = [sys.executable, "approach_race.py", "--mode", mode]
        if not body.get("save", True):
            args.append("--no-save")
        if body.get("force"):
            args.append("--force")
        RACE.update(status="running", started=time.time(), out=[], code=None)
        STATE.unlink(missing_ok=True)
        proc = await asyncio.create_subprocess_exec(*args, cwd=str(ROOT), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        asyncio.get_running_loop().create_task(_pump(proc))
        return JSONResponse({"status": "running"})
