#!/Users/savirdillikar/yc-hack/delta-memory/.venv/bin/python
"""Scripted agent for the Superset multi-workspace test (no LLM: deterministic steps).

Each Superset workspace runs one role in its own terminal / OS process. All three talk
to the same live Delta Memory server; files are shared via --checkout local.

  mover : read path@v -> move scripts/deploy.sh to scripts/release/ -> update config.yaml
          -> write new path with expected_version=v
  user  : read path@v -> block until the slot's version changes -> run the script at the
          path memory NOW says (proves it sees the other workspace's correction live)
  racer : read path@v at the same moment as mover -> a few seconds later write a
          different path with that now-stale version -> expect 'conflict', not overwrite
"""
import asyncio
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
dm = SourceFileLoader("dm", str(REPO / "tools" / "dm")).load_module()
E, P = "deploy_script", "path"
ROLE = sys.argv[1]
GO = sys.argv[2] if len(sys.argv) > 2 else ""  # epoch seconds, or a go-file holding them
TAG = f"[{ROLE} pid {os.getpid()}]"
os.environ["DM_AGENT_NAME"] = f"{ROLE} (pid {os.getpid()})"  # how the server's call log names us


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {TAG} {msg}", flush=True)


def call(tool, **a):
    return asyncio.run(dm.call(tool, **a))


def read():
    r = call("delta_read", entity_id=E, predicate=P)
    log(f"delta_read -> {r['value']['data']} @ v{r['version']}")
    return r["value"]["data"], r["version"]


def write(value, v):
    r = call("delta_write", entity_hint=E, predicate_hint=P, value=value, value_type="path",
             expected_version=v, source=f"tool_call:superset_{ROLE}")
    extra = f" (pending #{r['pending_id']})" if r.get("pending_id") else ""
    log(f"delta_write {value} expected_version={v} -> {r['status'].upper()}{extra}: {r['message']}")
    return r


def mover():
    path, v = read()
    new = f"scripts/release-{random.randbytes(2).hex()}/deploy.sh"  # a new location every run
    (REPO / new).parent.mkdir(parents=True, exist_ok=True)
    (REPO / path).rename(REPO / new)
    cfg = REPO / "config.yaml"
    cfg.write_text(cfg.read_text().replace(f"deploy_script: {path}", f"deploy_script: {new}"))
    log(f"mv {path} {new}; config.yaml deploy_script: {new}")
    write(new, v)


def user():
    path, v = read()
    log(f"waiting for another workspace to change the path (version > {v})...")
    deadline = time.time() + 120
    while time.time() < deadline:
        r = call("delta_read", entity_id=E, predicate=P)
        if r["version"] > v:
            break
        time.sleep(0.5)
    else:
        log("timed out waiting; no workspace changed the path"); sys.exit(2)
    path = r["value"]["data"]
    log(f"memory changed live: now {path} @ v{r['version']} (source {r['source']})")
    out = subprocess.run(["sh", path, "staging"], cwd=REPO, capture_output=True, text=True)
    log(f"sh {path} staging -> exit {out.returncode} {out.stdout.strip() or out.stderr.strip()}")


def racer():
    path, v = read()
    plan = str(Path(path).with_name("deploy_v2.sh"))  # its own plan, based on what it read
    work = round(random.uniform(2.0, 4.0), 1)
    log(f"planning rename {path} -> {plan} (working {work}s)")
    time.sleep(work)
    write(plan, v)


log(f"started in Superset workspace terminal, cwd {os.getcwd()}; waiting for shared start")
if GO and not GO.replace(".", "").isdigit():  # go-file: written once all workspaces exist
    while not os.path.exists(GO):
        time.sleep(0.1)
    go_at = float(open(GO).read())
else:
    go_at = float(GO) if GO else time.time()
time.sleep(max(0.0, go_at - time.time()))
{"mover": mover, "user": user, "racer": racer}[ROLE]()
log("done")
