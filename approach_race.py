"""Two approaches, one problem, in parallel. Measure both; save only the better one to Memorable.

    python approach_race.py --mode cursor      # real Cursor agents
    python approach_race.py --mode scripted    # same two strategies as plain commands

1. Breaks the deploy on a fresh branch: the script moves to scripts/archive-<x>/, a decoy
   scripts/legacy/deploy.sh (no longer works) stays behind, config.yaml still points at the old
   path. Delta memory holds the right path (recorded earlier by a teammate).
2. Two Superset workspaces, each on its own worktree of that branch:
     explorer -> searches the repository;   memory -> asks shared delta memory first.
   Both post checkpoints and the location they found to delta memory.
3. Verifies each worktree independently (config.yaml points at the working script, and it runs).
4. Scores: correct outcome first, then fewer tokens (cursor) / fewer steps (scripted).
5. Ingests ONLY the winner's trace into Memorable (`memorable ingest -`).

Writes ~/yc-hack/superset-run/race.json for the UI.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

SUPERSET = str(Path.home() / "superset" / "bin" / "superset")
PROJECT = "4c40fe82-c512-4f97-b7ab-15c50f8cb3f8"
REPO = Path.home() / "yc-hack" / "deploy-demo"
LOGS = Path.home() / "yc-hack" / "superset-run"
STATE, GO = LOGS / "race.json", LOGS / "race-go"
URL = "http://127.0.0.1:8787/mcp"
APPROACHES = ["explorer", "memory"]
GIT = ["git", "-c", "user.email=hello@codecircle.org", "-c", "user.name=Delta Memory Demo"]


def git(*a, cwd=REPO, check=True):
    return subprocess.run([*GIT, *a], cwd=cwd, check=check, capture_output=True, text=True)


def superset(*args: str) -> dict:
    p = subprocess.run([SUPERSET, *args, "--json"], capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"superset {' '.join(args[:2])} failed: {p.stderr or p.stdout}")
    return json.loads(p.stdout or "{}")


async def _delta(tool: str, **a) -> dict:
    async with streamablehttp_client(URL) as (r, w, _):
        async with ClientSession(r, w, client_info=Implementation(name="orchestrator", version="1")) as s:
            await s.initialize()
            return json.loads((await s.call_tool(tool, a)).content[0].text)


def delta(tool: str, **a) -> dict:
    return asyncio.run(_delta(tool, **a))


def state(**kv) -> None:
    cur = json.loads(STATE.read_text()) if STATE.exists() else {}
    cur.update(kv)
    STATE.write_text(json.dumps(cur, indent=1))


def cleanup() -> None:
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    ids = [w["id"] for w in old.get("workspaces", {}).values()]
    if ids:
        try:
            superset("ws", "delete", *ids, "--local")
        except Exception as e:
            print(f"(could not delete old workspaces: {e})")
    git("worktree", "prune", check=False)
    for b in git("branch", "--list", "race-*", "broken-*").stdout.split():
        if b != "*":
            git("branch", "-D", b, check=False)
    for f in [GO, *LOGS.glob("explorer.*"), *LOGS.glob("memory.*")]:
        f.unlink(missing_ok=True)
    STATE.unlink(missing_ok=True)


def break_deploy(rid: str, hexid: str) -> str:
    """Create branch broken-<rid> with the moved script and a decoy, without touching main."""
    tmp = LOGS / f"wt-{rid}"
    shutil.rmtree(tmp, ignore_errors=True)
    git("worktree", "add", "-q", "-b", f"broken-{rid}", str(tmp), "main")
    real = f"scripts/archive-{hexid}/deploy.sh"
    (tmp / real).parent.mkdir(parents=True)
    git("mv", "scripts/deploy.sh", real, cwd=tmp)
    (tmp / "scripts/legacy").mkdir(parents=True)
    (tmp / "scripts/legacy/deploy.sh").write_text(
        "#!/bin/sh\necho 'legacy deploy: retired, use the archived pipeline' >&2\nexit 1\n")
    git("add", "scripts/legacy/deploy.sh", cwd=tmp)
    git("commit", "-qm", f"release {rid}: move deploy script (config not updated)", cwd=tmp)
    git("worktree", "remove", "--force", str(tmp))
    return real


def verify(repo: str, real: str) -> dict:
    """Independent check of the expected end state in that approach's own worktree."""
    cfg = Path(repo, "config.yaml")
    line = next((l for l in cfg.read_text().splitlines() if l.startswith("deploy_script:")), "") if cfg.exists() else ""
    path = line.split(":", 1)[1].strip() if line else ""
    runs = subprocess.run(["sh", path, "staging"], cwd=repo, capture_output=True).returncode == 0 if path else False
    return {"config_points_to": path, "is_correct_script": path == real, "deploy_works": runs,
            "correct": path == real and runs}


def metrics(tr: dict) -> dict:
    u = tr.get("tokens") or {}
    calls = tr.get("tool_calls", [])
    return {"tokens": (u.get("inputTokens", 0) + u.get("outputTokens", 0)) if u else None,
            "cached_tokens": u.get("cacheReadTokens") if u else None,
            "seconds": round((tr.get("duration_ms") or 0) / 1000, 1),
            "steps": len(calls),
            "failed_steps": sum(1 for c in calls if (c.get("result") or {}).get("ok") is False)}


def ingest(tr: dict, rid: str) -> str | None:
    payload = {"session_id": f"race-{rid}-{tr['approach']}", "harness": "cursor-agent" if tr["mode"] == "cursor" else "script",
               "prompt": "fix the broken deploy config and prove the deploy works",
               "task_description": "fix the broken deploy config and prove the deploy works",
               "tool_calls": [{k: v for k, v in c.items() if k in ("name", "input", "result")} for c in tr["tool_calls"]]}
    p = subprocess.run(["npx", "-y", "memorable-cli@latest", "ingest", "-"], input=json.dumps(payload),
                       capture_output=True, text=True, cwd=REPO, timeout=180)
    text = (p.stdout + p.stderr).strip()
    print(f"memorable: {text}")
    for tok in text.split():
        if tok.startswith("procedures/"):
            return tok.rstrip(",")
    return None


TASK = "fix the broken deploy config and prove the deploy works"


def recall() -> str | None:
    """Does Memorable already remember this task? Then racing would be wasted money."""
    p = subprocess.run(["npx", "-y", "memorable-cli@latest", "recall", TASK], capture_output=True, text=True,
                       cwd=REPO, timeout=120)
    text = (p.stdout or "").strip()
    return None if not text or text.lower().startswith("no matching") else text


def economics(results: dict, winner: str, mode: str) -> dict:
    """Is racing worth it? Extra cost = the loser's run. Saving per later run = what the
    alternative costs minus what the remembered approach costs. Estimate, from this run."""
    unit = "tokens" if mode == "cursor" and all(results[a]["metrics"]["tokens"] for a in results) else "steps"
    cost = {a: results[a]["metrics"][unit] for a in results}
    loser = next(a for a in results if a != winner)
    extra, saving = cost[loser], cost[loser] - cost[winner]
    return {"unit": unit, "race_cost": sum(cost.values()), "extra_cost": extra, "winner_cost": cost[winner],
            "saving_per_reuse": saving, "judge_cost": 0,
            "break_even_reuses": math.ceil(extra / saving) if saving > 0 else None,
            "note": "estimate: assumes later runs of this task cost what the remembered approach cost now; "
                    "the judge is a programmatic check (reruns the deploy), so it costs no model tokens"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["cursor", "scripted"], default="cursor")
    ap.add_argument("--no-save", action="store_true", help="judge, but do not ingest into Memorable")
    ap.add_argument("--force", action="store_true", help="race even if Memorable already remembers the task")
    opts = ap.parse_args()
    mode = opts.mode
    LOGS.mkdir(exist_ok=True)
    cleanup()
    rid, hexid = str(random.randint(1000, 9999)), random.randbytes(2).hex()
    known = recall()
    state(rid=rid, mode=mode, phase="recall", started=time.time(), recalled=known)
    if known and not opts.force:
        state(phase="skipped", reason="Memorable already remembers this task: reuse it instead of racing")
        print(f"already remembered, race skipped:\n{known[:400]}")
        return 0
    state(phase="setup")

    real = break_deploy(rid, hexid)
    cur = delta("delta_read", entity_id="deploy_script", predicate="path")
    delta("delta_write", entity_hint="deploy_script", predicate_hint="path", value=real, value_type="path",
          source="tool_call:teammate", expected_version=cur.get("version") if cur.get("found") else None)
    state(problem={"real": real, "decoy": "scripts/legacy/deploy.sh", "stale": "scripts/deploy.sh"})
    print(f"problem ready on branch broken-{rid}: script moved to {real}; decoy scripts/legacy/deploy.sh")

    workspaces = {}
    for a in APPROACHES:
        cmd = f"tools/run_approach.py {a} {GO} {rid} {mode} {LOGS} 2>&1 | tee {LOGS}/{a}.log"
        w = superset("ws", "create", "--local", "--project", PROJECT, "--name", f"{a}-{rid}",
                     "--branch", f"race-{rid}-{a}", "--base-branch", f"broken-{rid}", "--skip-branch-prefix",
                     "--command", cmd)["workspace"]
        workspaces[a] = {"id": w["id"], "name": w["name"]}
        print(f"superset workspace {w['name']} on its own worktree (branch race-{rid}-{a})")
    state(workspaces=workspaces, phase="starting")

    deadline = time.time() + 60
    while time.time() < deadline and not all((LOGS / f"{a}.log").exists() for a in APPROACHES):
        time.sleep(0.2)
    GO.write_text(f"{time.time() + 1.0:.3f}")
    state(phase="racing", go=time.time() + 1.0)

    deadline = time.time() + (360 if mode == "cursor" else 60)
    done = lambda a: (LOGS / f"{a}.log").exists() and (LOGS / f"{a}.log").read_text().rstrip().endswith("done")
    while time.time() < deadline and not all(done(a) for a in APPROACHES):
        time.sleep(0.5)

    results = {}
    for a in APPROACHES:
        tf = LOGS / f"{a}.trace.json"
        tr = json.loads(tf.read_text()) if tf.exists() else {"approach": a, "tool_calls": [], "repo": ""}
        results[a] = {"finished": done(a), "metrics": metrics(tr),
                      "outcome": verify(tr["repo"], real) if tr.get("repo") else {"correct": False}}
        print(f"{a}: {results[a]}")
    state(results=results, phase="judging")

    def key(a):  # correct first, then cheaper
        m = results[a]["metrics"]
        return (not results[a]["outcome"]["correct"], m["tokens"] if m["tokens"] is not None else m["steps"],
                m["seconds"])
    winner = min(APPROACHES, key=key)
    if not results[winner]["outcome"]["correct"]:
        state(winner=None, saved=None, phase="done", reason="neither approach reached the expected state")
        print("no approach reached the expected state; nothing saved")
        return 1
    tr = json.loads((LOGS / f"{winner}.trace.json").read_text())
    slug = None if opts.no_save else ingest(tr, rid)
    econ = economics(results, winner, mode)
    print(f"economics: {econ}")
    state(winner=winner, saved=slug, discarded=[a for a in APPROACHES if a != winner], economics=econ,
          phase="done")
    print(f"winner: {winner}; saved to Memorable as {slug}; discarded the other approach")
    return 0


if __name__ == "__main__":
    sys.exit(main())
