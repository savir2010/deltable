"""Phase 5 proof: two separate OS processes race one slot on the live server.

Requires the server to be running already (python -m delta_memory.net_server).

1. Seeds the slot via a separate client process (so the slot exists).
2. Launches TWO client_write.py processes with subprocess.Popen, not awaited in turn.
   Both target the same slot with the same expected_version and different values.
   Each finishes its MCP handshake, then fires at the same agreed wall-clock instant.
3. Verifies against the SQLite file itself (read-only; WAL lets us read while the
   server writes): exactly one new `corrections` row for the winner, exactly one new
   `pending_reconciliation` row holding both candidate values, and no silent overwrite.

    python race_orchestrator.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("DELTA_MEMORY_DB", ROOT / "delta_memory_net.db"))
ENTITY, PRED = "refund_policy", "window_days"
PY = sys.executable
B, G, Y, R, D, X = "\033[1m", "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def db():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def val(js):
    return None if js is None else json.loads(js)["data"]


def fmt_ns(ns: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ns / 1e9)) + f".{ns % 10**9 // 1000:06d}"


def main() -> int:
    # 1. seed (its own process; a no-op if the slot already exists with this value)
    subprocess.run([PY, "client_write.py", ENTITY, PRED, "30", "--source", "tool_call:config"],
                   cwd=ROOT, check=True, capture_output=True)
    with db() as c:
        slot = c.execute("SELECT * FROM slots WHERE entity_id=? AND predicate=?", (ENTITY, PRED)).fetchone()
        base_corr = c.execute("SELECT COALESCE(MAX(id),0) FROM corrections").fetchone()[0]
        base_pend = c.execute("SELECT COALESCE(MAX(id),0) FROM pending_reconciliation").fetchone()[0]
    v, current = slot["version"], val(slot["value_json"])
    a_val, b_val = str(current + 1), str(current + 2)  # always differ from current: never a NOOP
    print(f"{B}slot{X} {ENTITY}.{PRED} = {current} @ v{v}")
    print(f"launching two processes: A writes {a_val}, B writes {b_val}, both with expected_version={v}\n")

    # 2. two genuinely separate processes, launched back to back, neither awaited first
    go_at = time.time_ns() + 2_500_000_000  # time for interpreter start + MCP handshake
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    procs = {}
    for name, value in (("A", a_val), ("B", b_val)):
        procs[name] = subprocess.Popen(
            [PY, "client_write.py", ENTITY, PRED, value, "--expected-version", str(v),
             "--source", f"tool_call:agent_{name}", "--go-at-ns", str(go_at)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    both_alive = all(p.poll() is None for p in procs.values())
    out = {}
    for name, p in procs.items():
        stdout, stderr = p.communicate(timeout=60)
        if p.returncode != 0:
            print(f"{R}process {name} failed:{X}\n{stderr}")
            return 1
        out[name] = json.loads(stdout.strip().splitlines()[-1])

    for name in ("A", "B"):
        o, s = out[name], out[name]["reply"]["server"]
        status = out[name]["reply"]["status"]
        color = G if status == "corrected" else Y
        print(f"process {name}: pid {o['client_pid']}  sent {fmt_ns(o['client_sent_ns'])}  "
              f"server got it {fmt_ns(s['server_received_ns'])} (arrival #{s['arrival_seq']})  "
              f"-> {color}{B}{status}{X}")

    a, b = out["A"], out["B"]
    send_gap_ms = abs(a["client_sent_ns"] - b["client_sent_ns"]) / 1e6
    overlap = (a["client_sent_ns"] < b["client_recv_ns"]) and (b["client_sent_ns"] < a["client_recv_ns"])
    first, second = sorted(out.values(), key=lambda o: o["reply"]["server"]["arrival_seq"])
    print(f"\n{B}concurrency evidence{X}")
    print(f"  distinct pids: {a['client_pid']} vs {b['client_pid']} "
          f"(orchestrator {os.getpid()}, server {a['reply']['server']['server_pid']})")
    print(f"  both processes alive at the same time after launch: {both_alive}")
    print(f"  sends {send_gap_ms:.3f} ms apart; in-flight windows overlap: {overlap}")
    print(f"  server applied them in order: pid {first['client_pid']} first, pid {second['client_pid']} second")

    # 3. verify from the file itself
    with db() as c:
        corr = c.execute("SELECT * FROM corrections WHERE id>? AND entity_id=? AND predicate=?",
                         (base_corr, ENTITY, PRED)).fetchall()
        pend = c.execute("SELECT * FROM pending_reconciliation WHERE id>? AND entity_id=? AND predicate=?",
                         (base_pend, ENTITY, PRED)).fetchall()
        final = c.execute("SELECT * FROM slots WHERE entity_id=? AND predicate=?", (ENTITY, PRED)).fetchone()

    winner = first["value"]
    loser = second["value"]
    checks = {
        "exactly one process corrected, one conflicted":
            sorted(o["reply"]["status"] for o in out.values()) == ["conflict", "corrected"],
        "exactly one new corrections row":
            len(corr) == 1,
        f"that row is v{v}->v{v+1}, {current} -> {winner} (the winner)":
            len(corr) == 1 and corr[0]["version_from"] == v and corr[0]["version_to"] == v + 1
            and str(val(corr[0]["new_value_json"])) == winner and val(corr[0]["old_value_json"]) == current,
        "exactly one new pending_reconciliation row":
            len(pend) == 1,
        f"pending row holds both candidates (proposed {loser}, current {winner}, read v{v})":
            len(pend) == 1 and str(val(pend[0]["proposed_value_json"])) == loser
            and str(val(pend[0]["current_value_json"])) == winner and pend[0]["read_version"] == v,
        f"slot is {winner} @ v{v+1}: loser did not overwrite":
            str(val(final["value_json"])) == winner and final["version"] == v + 1,
    }
    print(f"\n{B}database checks ({DB.name}){X}")
    for k, ok in checks.items():
        print(f"  {G + 'PASS' if ok else R + 'FAIL'}{X}  {k}")
    if not all(checks.values()):
        return 1
    print(f"\n{D}Both processes sent expected_version={v}. The server applied their writes one after the "
          f"other; the second one's version check failed (rowcount 0), so it went to "
          f"pending_reconciliation. The outcome comes from catching the stale version, not from "
          f"writes running in parallel.{X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
