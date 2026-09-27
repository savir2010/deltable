"""Scripted demo: seed -> break -> correct -> fixed.

    python demo/break_fix.py            # uses the LLM extractor for the Slack message if credentials exist
    DELTA_MEMORY_OFFLINE=1 python demo/break_fix.py

Beats:
  1. Seed memory from structured sources (config + repo layout).
  2. Free text (Slack) says the refund window changed -> one extraction call -> lower
     confidence, so it can't silently overwrite the structured value: it's queued.
  3. The config diff lands in git -> zero-LLM correction, 30 -> 14, logged.
  4. A refactor moves deploy.sh. The deploy procedure fails. The failure triggers a
     re-probe, memory is corrected, the retry passes.
  5. history() shows the audit trail; nothing was destroyed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delta_memory import DeltaMemory, SqliteStore  # noqa: E402
from delta_memory.adapters import ingest_git_diff, ingest_text  # noqa: E402
from delta_memory.failure import DependencyRegistry, find_file_probe, on_procedure_failure  # noqa: E402
from delta_memory.gbrain import MarkdownMirror  # noqa: E402

WS = Path(__file__).parent / "_workspace"
B, D, G, R, Y, X = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def step(n, title):
    print(f"\n{B}[{n}] {title}{X}")


def show(r):
    color = {"inserted": G, "corrected": G, "appended": G, "noop": D,
             "conflict": Y, "needs_review": Y}.get(r.status, "")
    print(f"    {color}{r.status:<12}{X} {r.message}")


def deploy(dm: DeltaMemory, repo: Path) -> bool:
    """The agent's procedure: it trusts memory for where the deploy script lives."""
    path = dm.read("deploy_script", "path").value.data
    proc = subprocess.run(["sh", path], cwd=repo, capture_output=True, text=True)
    ok = proc.returncode == 0
    print(f"    agent runs `sh {path}` -> "
          + (f"{G}OK{X} {proc.stdout.strip()}" if ok else f"{R}FAIL{X} {proc.stderr.strip()}"))
    return ok


def main():
    shutil.rmtree(WS, ignore_errors=True)
    repo = WS / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "deploy.sh").write_text("echo 'deployed v1.4.2'\n")

    store = SqliteStore(str(WS / "delta_memory.db"))
    dm = DeltaMemory(store)
    dm.hooks.append(MarkdownMirror(store, WS / "brain"))

    step(1, "Seed memory from structured sources")
    show(dm.write("refund_policy", "window_days", 30, source="tool_call:config"))
    show(dm.write("refund_policy", "max_amount", 500, source="tool_call:config"))
    show(dm.write("deploy_script", "path", "scripts/deploy.sh", value_type="path", source="tool_call:ls"))
    show(dm.write("deploy_script", "owners", "alice", cardinality="multi", source="tool_call:codeowners"))
    deploy(dm, repo)

    step(2, "Slack: free text claims a change (one extraction call, confidence < 1)")
    msg = "Heads up from support: the refund window is now 14 days, effective Monday."
    print(f"    {D}\"{msg}\"{X}")
    for r in ingest_text(dm, msg):
        show(r)
    print(f"    memory still says window_days = {dm.read('refund_policy', 'window_days').value.data}"
          f" (unverified text can't overwrite a structured source)")

    step(3, "The config change lands in git (zero LLM calls)")
    diff = ("diff --git a/config/refund_policy.yaml b/config/refund_policy.yaml\n"
            "--- a/config/refund_policy.yaml\n+++ b/config/refund_policy.yaml\n"
            "@@ -1,2 +1,2 @@\n-window_days: 30\n+window_days: 14\n max_amount: 500\n")
    for r in ingest_git_diff(dm, diff):
        show(r)
    pend = dm.reconcile("refund_policy", "window_days")["pending"]
    for p in pend:
        out = dm.reconcile(accept=p["id"])  # the Slack claim is now just a restatement
        print(f"    reconcile pending #{p['id']}: {out['status']} ({out.get('message', '')})")

    step(4, "A refactor moves the deploy script; the procedure breaks")
    (repo / "scripts" / "release").mkdir()
    (repo / "scripts" / "deploy.sh").rename(repo / "scripts" / "release" / "deploy.sh")
    reg = DependencyRegistry()
    reg.register("deploy", "run_script", "deploy_script", "path", find_file_probe(repo))
    if not deploy(dm, repo):
        out = on_procedure_failure(dm, reg, "deploy", "run_script", "sh: no such file")
        print(f"    failure-triggered re-probe -> {G}{out['status']}{X}: {out.get('message', '')}")
        deploy(dm, repo)

    step(5, "Audit trail (append-only; every value is superseded, never destroyed)")
    for e, p in [("refund_policy", "window_days"), ("deploy_script", "path")]:
        print(f"    {B}{e}.{p}{X}")
        for c in dm.history(e, p):
            print(f"      v{c.version_from}->v{c.version_to} {c.kind:<10} {c.reason}")
    print(f"\n    GBrain page mirror: {WS / 'brain' / 'delta-memory'}")


if __name__ == "__main__":
    main()
