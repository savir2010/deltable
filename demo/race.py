"""Two-agent race over MCP: "agents police each other's memory".

Spawns two independent delta-memory MCP server processes (think: two Superset
workspaces) pointed at the same store. Both read refund_policy.window_days at the same
version, both try to correct it. One commits; the other gets a version conflict and
lands in the reconciliation queue instead of silently clobbering.

    python demo/race.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "demo" / "_workspace" / "race.db"
B, G, Y, D, X = "\033[1m", "\033[32m", "\033[33m", "\033[2m", "\033[0m"


def params() -> StdioServerParameters:
    env = {**os.environ, "DELTA_MEMORY_DB": str(DB), "DELTA_MEMORY_OFFLINE": "1", "FASTMCP_LOG_LEVEL": "WARNING",
           "PYTHONPATH": str(ROOT)}
    return StdioServerParameters(command=sys.executable, args=["-m", "delta_memory.server"], env=env)


async def call(s: ClientSession, tool: str, **args) -> dict:
    res = await s.call_tool(tool, args)
    return json.loads(res.content[0].text)


async def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    for f in DB.parent.glob("race.db*"):
        f.unlink()

    async with stdio_client(params()) as (ra, wa), stdio_client(params()) as (rb, wb):
        async with ClientSession(ra, wa) as a, ClientSession(rb, wb) as b:
            await a.initialize()
            await b.initialize()
            tools = [t.name for t in (await a.list_tools()).tools]
            print(f"{D}MCP tools: {', '.join(tools)}{X}\n")

            await call(a, "write", entity_hint="refund_policy", predicate_hint="window_days",
                       value=30, source="tool_call:config")

            ra_ = await call(a, "read", entity_id="refund_policy", predicate="window_days")
            rb_ = await call(b, "read", entity_id="refund_policy", predicate="window_days")
            print(f"workspace A reads window_days = {ra_['value']['data']} @ v{ra_['version']}")
            print(f"workspace B reads window_days = {rb_['value']['data']} @ v{rb_['version']}\n")

            wa_, wb_ = await asyncio.gather(
                call(a, "write", entity_hint="refund_policy", predicate_hint="window_days",
                     value=14, source="tool_call:billing_api", expected_version=ra_["version"]),
                call(b, "write", entity_hint="refund_policy", predicate_hint="window_days",
                     value=21, source="tool_call:support_tool", expected_version=rb_["version"]),
            )
            for name, r in (("A", wa_), ("B", wb_)):
                c = G if r["status"] == "corrected" else Y
                print(f"workspace {name} write -> {c}{B}{r['status']}{X}  {r['message']}")

            final = await call(a, "read", entity_id="refund_policy", predicate="window_days")
            print(f"\nstore: window_days = {final['value']['data']} @ v{final['version']} (nothing clobbered)")

            q = await call(b, "reconcile", entity_id="refund_policy", predicate="window_days")
            for p in q["pending"]:
                print(f"reconciliation queue #{p['id']}: proposed {p['proposed_value']['data']} "
                      f"from {p['source']} (read v{p['read_version']}, slot at v{p['current_version']})")

            hist = await call(a, "history", entity_id="refund_policy", predicate="window_days")
            print(f"\nhistory: " + " | ".join(f"v{c['version_to']} {c['kind']}" for c in hist["corrections"]))


if __name__ == "__main__":
    asyncio.run(main())
