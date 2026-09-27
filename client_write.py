"""Standalone one-shot client: ONE delta_write call to the running server, then exit.

    python client_write.py refund_policy window_days 14 --expected-version 1
    python client_write.py refund_policy window_days 14 --expected-version 1 --go-at-ns 1790000000000000000

--go-at-ns: connect and finish the MCP handshake first, then sleep until this wall-clock
instant before sending the write, so several processes launched together fire their
writes at (as close as possible to) the same moment. Prints one JSON line with this
process's pid and send/receive timestamps next to the server's reply.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("DELTA_MEMORY_URL", "http://127.0.0.1:8787/mcp")


async def main(a: argparse.Namespace) -> None:
    args = {"entity_hint": a.entity, "predicate_hint": a.predicate, "value": a.value,
            "source": a.source}
    if a.expected_version is not None:
        args["expected_version"] = a.expected_version
    if a.value_type:
        args["value_type"] = a.value_type

    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            if a.go_at_ns:
                await asyncio.sleep(max(0.0, (a.go_at_ns - time.time_ns()) / 1e9))
            sent = time.time_ns()
            res = await s.call_tool("delta_write", args)
            recv = time.time_ns()

    reply = json.loads(res.content[0].text)
    print(json.dumps({"client_pid": os.getpid(), "value": a.value, "client_sent_ns": sent,
                      "client_recv_ns": recv, "reply": reply}))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("entity")
    p.add_argument("predicate")
    p.add_argument("value")
    p.add_argument("--expected-version", type=int)
    p.add_argument("--value-type")
    p.add_argument("--source", default="manual")
    p.add_argument("--go-at-ns", type=int)
    asyncio.run(main(p.parse_args()))
