"""One-shot client: connect to the running server over HTTP, call ping once, exit.

    python client_ping.py "hello from terminal A"
"""
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("DELTA_MEMORY_URL", "http://127.0.0.1:8787/mcp")


async def main(msg: str) -> None:
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            res = await s.call_tool("ping", {"message": msg})
            print(f"[client pid {os.getpid()}] {res.content[0].text}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "ping"))
