"""Memorable procedures -> composable transitions in Delta Memory.

Memorable stores and chains explicit procedures (its `chain` links a procedure that WRITES
a file to one that READS it). This adds the other half of the idea: each procedure also
becomes a sequence of state transitions (s_i -> s_{i+1}), recorded on the live Delta
Memory server with the procedure slug as the episode. Separately recorded procedures
then compose through the delta-rule associative memory: A->B from one procedure and B->C
from another give a path A->C that was never stored as one procedure, and every hop
still names the procedure that taught it.

States are the artifacts a procedure's steps touch, in order: files read for context,
then each decisive step's file or command. Two procedures compose when one's last state
is another's first (e.g. both touch the same file).

    python memorable_transitions.py                  # all procedures in your Memorable store
    python memorable_transitions.py --dry-run        # print transitions, record nothing
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys

from memorable_hook import delta, find_slug, memorable  # noqa: F401  (shared CLI + MCP helpers)
import json

STEP = re.compile(r"^\s*\d+\.\s*\[(?P<cls>\w+)\]\s*(?P<tool>\w+):\s*(?P<arg>.+?)\s*$")
CONTEXT = re.compile(r"Context gathered last time[^:]*:\s*(?P<files>.+?)\)?\s*$")


_SKIP = ("cd", "export", "echo", "sleep", "rm", "mkdir", "ls", "cat", "for", "do", "done", "then")


def command_state(cmd: str) -> str | None:
    """`cd x && .venv/bin/python superset_multiagent.py` -> `run python superset_multiagent.py`.
    Resolves `S=/path/superset; $S ws ...` to `run superset ws`. None for pure navigation."""
    env: dict[str, str] = {}
    for seg in re.split(r"&&|\|\||;|\|", cmd):
        toks = seg.split()
        while toks and re.match(r"^[A-Za-z_]\w*=", toks[0]):  # VAR=value prefixes
            k, _, v = toks.pop(0).partition("=")
            env[k] = v
        toks = [env.get(t[1:], t) if t.startswith("$") else t for t in toks]
        if not toks or toks[0] in _SKIP or toks[0].startswith("$"):
            continue
        prog = toks[0].rstrip("/").split("/")[-1]
        rest = [t for t in toks[1:] if not t.startswith(("-", "<", ">", "'", '"'))]
        if prog.startswith("python"):
            return f"run python {rest[0].split('/')[-1]}" if rest else "run python inline"
        return "run " + " ".join([prog] + [r.split("/")[-1] for r in rest[:1]])
    return None


def state_name(tool: str, arg: str) -> str | None:
    """A step's artifact as a state: files by basename, commands by program + subcommand."""
    if tool.lower() in ("bash", "shell", "execute"):
        return command_state(arg)
    return arg.rstrip("/").split("/")[-1]


def transitions_from_show(text: str) -> list[tuple[str, str]]:
    states: list[str] = []
    for line in text.splitlines():
        m = CONTEXT.search(line)
        if m:
            ctx_files = [f.strip() for f in m.group("files").split(",") if f.strip()]
            states = [state_name("read", f) for f in ctx_files] + states
    for line in text.splitlines():
        m = STEP.match(line)
        if m:
            st = state_name(m.group("tool"), m.group("arg"))
            if st:
                states.append(st)
    dedup = [s for i, s in enumerate(states) if i == 0 or s != states[i - 1]]
    return list(zip(dedup, dedup[1:]))


def all_slugs() -> list[str]:
    out = []
    for entry in json.loads(memorable("list", "--json") or "[]"):
        slug = entry.get("preferred") or (entry.get("revisions") or [{}])[0].get("slug")
        if slug:
            out.append(slug)
    return out


async def main(a: argparse.Namespace) -> int:
    slugs = a.slug or all_slugs()
    if not slugs:
        print("no procedures in Memorable yet")
        return 1
    for slug in slugs:
        edges = transitions_from_show(memorable("show", slug))
        print(f"{slug}: {len(edges)} transition(s)")
        for src, dst in edges:
            if a.dry_run:
                print(f"    {src} -> {dst}")
                continue
            r = await delta("delta_relate", src=src, dst=dst, context=a.context, episode=slug)
            print(f"    {src} -> {dst}   {r['status']}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--slug", action="append")
    p.add_argument("--context", default="default")
    p.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args())))
