"""Three agents, three real Superset workspaces, one live delta-memory server.

    python superset_multiagent.py --mode cursor      # real Cursor agents (cursor-agent -p -f)
    python superset_multiagent.py --mode scripted    # same steps as plain shell commands (fast)

Task (tools/roles.py in deploy-demo): ship one release. builder revalidates the deploy script
path, mover relocates it, deployer deploys; each records one pipeline step. Together they touch
every branch of the delta rule, and memory composes a pipeline none of them stored.

Requires: python -m delta_memory.net_server, and `superset start`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
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
WS_FILE, GO_FILE, RUN_FILE = LOGS / "workspaces.json", LOGS / "go", LOGS / "run.json"
URL = "http://127.0.0.1:8787/mcp"
ROLES = ["builder", "mover", "deployer"]


def sh(*args: str) -> dict:
    p = subprocess.run([SUPERSET, *args, "--json"], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"superset {' '.join(args[:2])} failed: {p.stderr or p.stdout}")
    return json.loads(p.stdout or "{}")


async def delta(tool: str, **a) -> dict:
    async with streamablehttp_client(URL) as (r, w, _):
        async with ClientSession(r, w, client_info=Implementation(name="orchestrator", version="1")) as s:
            await s.initialize()
            return json.loads((await s.call_tool(tool, a)).content[0].text)


def call(tool: str, **a) -> dict:
    return asyncio.run(delta(tool, **a))


def cleanup_previous() -> None:
    if WS_FILE.exists():
        ids = [w["id"] for w in json.loads(WS_FILE.read_text()).values()]
        if ids:
            try:
                sh("ws", "delete", *ids, "--local")
            except Exception as e:
                print(f"  (could not delete previous workspaces: {e})")
        WS_FILE.unlink()
    for f in [GO_FILE, *LOGS.glob("*.log")]:
        f.unlink(missing_ok=True)


def reset() -> None:
    """Starting state: script at scripts/deploy.sh on disk and in memory; empty review queue."""
    subprocess.run(["git", "checkout", "--", "config.yaml", "scripts"], cwd=REPO, check=True)
    subprocess.run(["git", "clean", "-fdq", "--", "scripts"], cwd=REPO, check=True)
    cur = call("delta_read", entity_id="deploy_script", predicate="path")
    if not cur.get("found") or cur["value"]["data"] != "scripts/deploy.sh":
        call("delta_write", entity_hint="deploy_script", predicate_hint="path", value="scripts/deploy.sh",
             value_type="path", source="manual:demo_reset",
             expected_version=cur.get("version") if cur.get("found") else None)
    for p in call("delta_reconcile", entity_id="deploy_script", predicate="path")["pending"]:
        call("delta_reconcile", reject=p["id"])


def deployed_from(log: str, path: str) -> bool:
    """The deployer ran `sh <path> staging` with the path memory ended at, and it exited 0."""
    lines = log.splitlines()
    for i, l in enumerate(lines):
        if f"$ sh {path} staging" in l:
            nxt = next((x for x in lines[i + 1:] if "→" in x), "")
            return "(exit" not in nxt
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["cursor", "scripted"], default="cursor")
    mode = ap.parse_args().mode
    LOGS.mkdir(exist_ok=True)
    cleanup_previous()
    reset()
    rid = f"{random.randint(1000, 9999)}"
    ns = f"r{rid}"
    v0 = call("delta_read", entity_id="deploy_script", predicate="path")["version"]
    RUN_FILE.write_text(json.dumps({"rid": rid, "ns": ns, "mode": mode, "v0": v0, "started": time.time()}))
    print(f"task: ship release {rid} of deploy-demo · mode {mode} · deploy_script.path starts at v{v0}")

    workspaces = {}
    for role in ROLES:
        log = LOGS / f"{role}.log"
        cmd = f"tools/run_role.py {role} {GO_FILE} {rid} {mode} 2>&1 | tee {log}"
        out = sh("ws", "create", "--local", "--project", PROJECT, "--name", f"{role}-{rid}",
                 "--checkout", "local", "--command", cmd)
        ws = out["workspace"]
        workspaces[role] = {"id": ws["id"], "name": ws["name"], "log": log}
        print(f"  superset workspace {ws['name']} {ws['id']}")
    WS_FILE.write_text(json.dumps({r: {"id": w["id"], "name": w["name"]} for r, w in workspaces.items()}))

    deadline = time.time() + 30
    while time.time() < deadline and not all(w["log"].exists() for w in workspaces.values()):
        time.sleep(0.2)
    GO_FILE.write_text(f"{time.time() + 1.0:.3f}")
    print("go: all three agents start now, in parallel")

    deadline = time.time() + (420 if mode == "cursor" else 90)
    while time.time() < deadline and not all(
            w["log"].exists() and w["log"].read_text().rstrip().endswith("done") for w in workspaces.values()):
        time.sleep(0.5)

    # ---- what actually happened, read back from the live server
    hist = [c for c in call("delta_history", entity_id="deploy_script", predicate="path")["corrections"]
            if c["version_from"] >= v0]
    pend = [p for p in call("delta_reconcile", entity_id="deploy_script", predicate="path")["pending"]
            if p["read_version"] >= v0]
    agents = call("delta_read", entity_id=f"release_{rid}", predicate="agents")
    comp = call("delta_compose", start="source", goal="cluster", namespace=ns)
    final = call("delta_read", entity_id="deploy_script", predicate="path")
    members = agents["value"]["data"] if agents.get("found") else []
    dlog = workspaces["deployer"]["log"].read_text() if workspaces["deployer"]["log"].exists() else ""

    checks = {
        "all three agents joined the release set (1 insert, 2 appends)": sorted(members) == sorted(ROLES),
        "the relocation was one overwrite: version +1, the old value kept in the log":
            len(hist) == 1 and hist[0]["kind"] == "correction",
        "memory matches the disk (the path it holds exists)": (REPO / final["value"]["data"]).exists(),
        "the stale write was blocked and queued, not applied": len(pend) >= 1,
        "memory composed source → cluster, a path no agent stored": bool(comp.get("path")),
        "the deploy ran from the path memory held": deployed_from(dlog, final["value"]["data"]),
    }
    for k, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {k}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
