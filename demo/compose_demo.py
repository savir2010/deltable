"""Composable transitions demo, against the LIVE Delta Memory server over MCP.

  1. Two separately learned episodes: build -> container, container -> kubernetes.
  2. Retrieval is not implication: one-hop recall of `build` returns container, not kubernetes.
  3. Composition: build -> container -> kubernetes, and the composite operator S @ S applied
     directly (no clean-up) lands on kubernetes too: T_AC = T_BC o T_AB. Each hop names
     the episode that taught it.
  4. A correction: container now leads to nomad (new episode). The composed path re-routes;
     the old episode is still in delta_history, superseded not destroyed.
  5. Conditional relations: build -[arm]-> arm_image, arm_image -[eu]-> eu_cluster compose
     as S_eu @ S_arm; the reverse order does not.
  6. Real Memorable procedures (read through the Memorable CLI) as transitions.

Episodes in 1, 4 and 5 are fixtures from the design example, labelled as such; 6 is real.

    python demo/compose_demo.py        # requires: python -m delta_memory.net_server
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from memorable_hook import delta  # noqa: E402

B, G, Y, R, D, X = "\033[1m", "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
NS = f"r{int(time.time())}"  # fresh namespace per run: its own matrices, no leftover interference


def step(n, title):
    print(f"\n{B}[{n}] {title}{X}")


def show_path(r):
    if not r["path"]:
        print(f"    {Y}no path{X} from {r['start']} to {r['goal']}")
        return
    for h in r["path"]:
        ctx = f" [{h['context']}]" if h["context"] != "default" else ""
        print(f"    {h['src']} -{ctx}-> {h['dst']}   score {h['score']:.3f}   {D}taught by {', '.join(h['episodes'])}{X}")
    op = r.get("composite_operator")
    if op:
        mark = G + "agrees" if op["agrees"] else R + "DISAGREES"
        print(f"    composite operator {' @ '.join('S_' + c for c in reversed(op['contexts']))} "
              f"applied to {r['start']} directly -> {op['lands_on']} ({op['score']:.3f}) {mark}{X}")


async def relate(src, dst, ctx="default", ep="fixture"):
    r = await delta("delta_relate", src=src, dst=dst, context=ctx, episode=ep, namespace=NS)
    print(f"    {src} -> {dst}{'' if ctx == 'default' else f' [{ctx}]'}   {r['status']}   {D}episode {ep}{X}")


async def compose(**a):
    return await delta("delta_compose", namespace=NS, **a)


async def main():
    print(f"{D}isolated run namespace: {NS}{X}")
    step(1, "Two separately learned episodes (fixtures from the design example)")
    await relate("build", "container", ep="fixture:deploy-service-x")
    await relate("container", "kubernetes", ep="fixture:deploy-to-k8s")

    step(2, "Retrieval is not implication")
    r = await compose(start="build", goal="kubernetes")
    one = r["one_hop_recall"]["default"]
    print(f"    one-hop recall of build -> {B}{one['state']}{X} ({one['score']:.3f}); "
          f"kubernetes is never returned by retrieval alone")

    step(3, "Composition: build -> kubernetes, never stored as one memory")
    show_path(r)

    step(4, "A correction re-routes composition; the old episode stays recoverable")
    await relate("container", "nomad", ep="fixture:migrate-to-nomad")
    show_path(await compose(start="build", goal="nomad"))
    gone = await compose(start="build", goal="kubernetes")
    print(f"    build -> kubernetes now: {'found' if gone['path'] else 'no path (superseded)'}")
    for c in (await delta("delta_history", entity_id=f"tr{NS}_default_container", predicate="next"))["corrections"]:
        print(f"    {D}history v{c['version_from']}->v{c['version_to']} {c['kind']}: {c['reason']}{X}")

    step(5, "Conditional relations compose only in the right order")
    await relate("build", "arm_image", ctx="arm", ep="fixture:arm-build")
    await relate("arm_image", "eu_cluster", ctx="eu", ep="fixture:eu-rollout")
    show_path(await compose(start="build", goal="eu_cluster"))
    wrong = await compose(start="build", goal="eu_cluster", contexts=["eu"])
    print(f"    restricted to context eu only: {'found' if wrong['path'] else 'no path'} (needs arm first)")

    step(6, "Real Memorable procedures as transitions (read via the Memorable CLI; shared default namespace)")
    out = subprocess.run([sys.executable, str(ROOT / "memorable_transitions.py")], cwd=ROOT,
                         capture_output=True, text=True)
    print("    " + "\n    ".join(l for l in out.stdout.splitlines() if l.endswith(")") or "procedures/" in l))
    show_path(await delta("delta_compose", start="run superset auth", goal="run superset ws"))
    cross = await delta("delta_compose", start="config.yaml", goal="run superset auth")
    print(f"    across the two real procedures (config.yaml -> run superset auth): "
          f"{'found' if cross['path'] else 'no path'}: they share no state, so nothing is invented")
    print(f"\n    {D}{cross['transitions_stored']} transition writes replayed into S (d={cross['d']}){X}")


if __name__ == "__main__":
    asyncio.run(main())
