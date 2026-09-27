"""Memorable -> Delta Memory: failure-triggered correction.

Reads a procedure Memorable recorded, re-runs its verify command, and when a verify
that previously PASSED now FAILS, re-probes reality and writes a correction to the live
Delta Memory server with source="failure_triggered:<slug>".

How the procedure is read: through the Memorable CLI (`memorable show <slug>`).
~/.memorable/procedures.jsonl is encrypted per line (MEMv1: AES-256-GCM), so there
is no plain JSON file to read. The --cache file below is this hook's own export of the
last procedure it read, used only if the CLI is unreachable.

The procedure -> slot mapping is hardcoded for the demo (deploy procedure ->
deploy_script.path). Inferring which slot a failing step depended on is out of scope.

    python memorable_hook.py --repo ~/yc-hack/deploy-demo
    python memorable_hook.py --repo ~/yc-hack/deploy-demo --slug procedures/7c07007c-deploy-from-config-yaml-to-staging
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

URL = os.environ.get("DELTA_MEMORY_URL", "http://127.0.0.1:8787/mcp")
MEMORABLE = ["npx", "-y", "memorable-cli@latest"]
CACHE = Path(__file__).parent / ".memorable_procedure_cache.json"

# Hardcoded for the demo: which slot the deploy procedure's verify step depends on.
PROCEDURE_SLOT = {"deploy": ("deploy_script", "path")}

B, G, R, Y, D, X = "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def say(msg: str) -> None:
    print(msg, flush=True)


# ------------------------------------------------------------ read the procedure
def memorable(*args: str) -> str:
    p = subprocess.run([*MEMORABLE, *args], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return re.sub(r"\x1b\[[0-9;]*m", "", p.stdout)


def find_slug(intent: str) -> str:
    listing = json.loads(memorable("list", "--json") or "[]")
    for entry in listing:
        slug = entry.get("preferred") or (entry.get("revisions") or [{}])[0].get("slug")
        if slug and intent in slug:
            return slug
    raise LookupError(f"no Memorable procedure whose slug mentions {intent!r}")


def read_procedure(slug: str | None, intent: str) -> dict:
    try:
        slug = slug or find_slug(intent)
        text = memorable("show", slug)
        verify = re.search(r"Verified last time by:\s*(.+)", text)
        proc = {
            "slug": slug,
            "title": (re.search(r"near-identical task:\s*(.+)", text) or [None, slug])[1].strip(),
            "verify": verify.group(1).strip() if verify else None,
            # Memorable only prints "Verified last time by" when the recorded run's
            # verify command succeeded; that is the "previously passed" signal.
            "previously_passed": bool(verify),
            "source": "memorable show (live CLI)",
        }
        CACHE.write_text(json.dumps(proc, indent=2))
        return proc
    except Exception as e:  # CLI/network unreachable: fall back to our last export
        if CACHE.exists():
            proc = json.loads(CACHE.read_text())
            proc["source"] = f"local cache {CACHE.name} (CLI unavailable: {e})"
            return proc
        raise


# ---------------------------------------------------------------- delta memory
async def delta(tool: str, **args) -> dict:
    async with streamablehttp_client(URL) as (r, w, _):
        name = os.environ.get("DM_AGENT_NAME") or f"memorable-hook pid {os.getpid()}"
        async with ClientSession(r, w, client_info=Implementation(name=name, version="1")) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
    return json.loads(res.content[0].text)


def run(cmd: str, repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True, timeout=120)


def probe_path(repo: Path, assumed: str) -> str | None:
    """Ground truth for a path slot: where does that file actually live now?"""
    if (repo / assumed).exists():
        return assumed
    hits = sorted(p for p in repo.rglob(Path(assumed).name)
                  if p.is_file() and ".git" not in p.parts)
    return hits[0].relative_to(repo).as_posix() if len(hits) == 1 else None


# ------------------------------------------------------------------------ main
async def main(a: argparse.Namespace) -> int:
    repo = Path(a.repo).expanduser().resolve()
    entity, predicate = PROCEDURE_SLOT[a.intent]

    proc = read_procedure(a.slug, a.intent)
    say(f"{B}recalled{X} {proc['slug']}  {D}via {proc['source']}{X}")
    say(f"  verify command: {proc['verify']}  (previously passed: {proc['previously_passed']})")
    if not proc["verify"]:
        say(f"{Y}procedure has no recorded verify command; nothing to check{X}")
        return 0

    result = run(proc["verify"], repo)
    if result.returncode == 0:
        say(f"  re-run verify -> {G}PASS{X} {result.stdout.strip()}  (memory untouched)")
        return 0
    say(f"  re-run verify -> {R}FAIL (exit {result.returncode}){X} {result.stderr.strip()}")
    if not proc["previously_passed"]:
        say("  it never passed before, so this is not evidence memory went stale; no write")
        return 1

    current = await delta("delta_read", entity_id=entity, predicate=predicate)
    if not current.get("found"):
        say(f"{Y}{entity}.{predicate} is not in Delta Memory; no write{X}")
        return 1
    stored = current["value"]["data"]
    # The verify step depends on the script path. Take the path the procedure used
    # (from its verify command) and see where that file really is now.
    used = next((t for t in shlex.split(proc["verify"]) if t.endswith(Path(stored).name)), stored)
    truth = probe_path(repo, used)
    say(f"  delta memory says {entity}.{predicate} = {stored} @ v{current['version']}; "
        f"re-probe finds {truth}")
    if truth is None:
        say(f"{Y}re-probe found no ground truth (missing or ambiguous); memory left unchanged{X}")
        return 1
    if truth == used and truth == stored:
        say("  re-probe agrees with memory: a flake, not stale memory; no write")
        return 1

    w = await delta("delta_write", entity_hint=entity, predicate_hint=predicate, value=truth,
                    value_type="path", source=f"failure_triggered:{proc['slug']}",
                    expected_version=current["version"])
    color = G if w["status"] == "corrected" else Y
    say(f"  delta_write -> {color}{B}{w['status']}{X} {w['message']}")

    fixed = proc["verify"].replace(used, truth)
    again = run(fixed, repo)
    say(f"  re-run verify with corrected path `{fixed}` -> "
        + (f"{G}PASS{X} {again.stdout.strip()}" if again.returncode == 0 else f"{R}FAIL{X} {again.stderr.strip()}"))
    return 0 if again.returncode == 0 else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--slug")
    p.add_argument("--intent", default="deploy", choices=sorted(PROCEDURE_SLOT))
    sys.exit(asyncio.run(main(p.parse_args())))
