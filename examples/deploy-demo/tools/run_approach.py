#!/Users/savirdillikar/yc-hack/delta-memory/.venv/bin/python
"""One approach to the same problem, run inside its own Superset workspace (own worktree).

    tools/run_approach.py <explorer|memory> <go-file> <run-id> <cursor|scripted> <out-dir>

Problem: the deploy script moved; config.yaml still points at the old path, so deploys fail.
  explorer : finds the script by searching the repository
  memory   : asks shared delta memory first
Both post checkpoints to delta memory (fix_<rid>.<approach>) and record what they found in a
shared fact (fix_<rid>.script_location), so memory converges on one answer.

Writes <out-dir>/<approach>.trace.json: every tool call, tokens, duration, final answer.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dmfmt import summarize  # noqa: E402

REPO = Path.cwd()  # the workspace's own worktree
approach, go_file, rid, mode, out_dir = sys.argv[1:6]
NAME = {"explorer": "Explorer", "memory": "Memory-first"}[approach]
os.environ["DM_AGENT_NAME"] = f"{approach} ({mode} pid {os.getpid()})"

PROBLEM = ("Deploys are failing: config.yaml's `deploy_script` points at a script that no longer exists. "
           "Fix config.yaml so it points at the correct, working deploy script, then run "
           "`sh <that path> staging` to prove the deploy works.")
CHECKPOINTS = (
    f"Post short progress checkpoints to shared memory as you go, with "
    f"`tools/dm write fix_{rid} {approach} \"<what you are doing now>\"` "
    f"(e.g. \"starting\", \"found the script\", \"config fixed\", \"deployed\"). "
    f"When you know the script's location, record it for the team with "
    f"`tools/dm write fix_{rid} script_location <path> --value-type path`."
)
STRATEGY = {
    "explorer": "Your approach: find the script by searching the repository yourself (find, grep, reading "
                "files). Do NOT read deploy_script from shared memory.",
    "memory": "Your approach: ask shared memory first. `tools/dm read deploy_script path` returns where the "
              "team last recorded the script. Verify it exists, then use it.",
}


def prompt() -> str:
    return (f"{PROBLEM}\n\n{STRATEGY[approach]}\n\n{CHECKPOINTS}\n\nWork only in the current directory. "
            f"Do not commit. Be efficient. Finish with one line saying which path you used.")


trace = {"approach": approach, "name": NAME, "mode": mode, "rid": rid, "repo": str(REPO),
         "tool_calls": [], "tokens": None, "duration_ms": None, "answer": "", "started": None}


def out(line: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {line}", flush=True)


def save() -> None:
    Path(out_dir, f"{approach}.trace.json").write_text(json.dumps(trace, indent=1))


def dm(*args: str) -> str:
    return sh(" ".join([f"tools/dm"] + [a if " " not in a else json.dumps(a) for a in args]))


def sh(cmd: str) -> str:
    out(f"$ {cmd}")
    t0 = time.time()
    p = subprocess.run(cmd, shell=True, cwd=REPO, capture_output=True, text=True)
    res = summarize(p.stdout) if p.stdout.strip() else p.stderr.strip()[:160]
    out(f"  → {res}" + (f"  (exit {p.returncode})" if p.returncode else ""))
    trace["tool_calls"].append({"name": "Bash", "input": {"command": cmd},
                                "result": {"exit_code": p.returncode, "ok": p.returncode == 0},
                                "t": t0, "ms": int((time.time() - t0) * 1000)})
    save()
    return p.stdout


def scripted() -> None:
    """Same two strategies as plain commands (no model, so no token count)."""
    dm("write", f"fix_{rid}", approach, "starting")
    if approach == "explorer":
        dm("write", f"fix_{rid}", approach, "searching the repository")
        found = [l for l in sh("find . -name 'deploy*.sh' -not -path './.git/*'").split() if l]
        good = None
        for f in found:
            sh(f"cat {f}")
            if subprocess.run(["sh", f, "staging"], cwd=REPO, capture_output=True).returncode == 0:
                sh(f"sh {f} staging")
                good = f.removeprefix("./")
                break
            sh(f"sh {f} staging")
    else:
        dm("write", f"fix_{rid}", approach, "asking shared memory")
        good = json.loads(sh("tools/dm read deploy_script path"))["value"]["data"]
        sh(f"test -f {good} && echo exists")
    dm("write", f"fix_{rid}", approach, f"found the script")
    dm("write", f"fix_{rid}", "script_location", good, "--value-type", "path")
    sh(f"sed -i '' 's#^deploy_script: .*#deploy_script: {good}#' config.yaml && grep deploy_script config.yaml")
    dm("write", f"fix_{rid}", approach, "config fixed")
    sh(f"sh {good} staging")
    dm("write", f"fix_{rid}", approach, "deployed")
    trace["answer"] = f"used {good}"


def cursor() -> None:
    p = subprocess.Popen(["cursor-agent", "-p", "-f", "--output-format", "stream-json", prompt()],
                         cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    pending: dict[str, dict] = {}
    for line in p.stdout:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            out(f"cursor agent started · model {ev.get('model')}")
        elif t == "tool_call":
            call = ev.get("tool_call", {})
            kind, body = next(iter(call.items())) if call else ("tool", {})
            args = body.get("args", {})
            if ev.get("subtype") == "started":
                what = args.get("command") or args.get("path") or ""
                out(f"$ {what}" if kind == "shellToolCall" else f"[{kind.replace('ToolCall', '')}] {what}")
                pending[ev.get("call_id")] = {"kind": kind, "args": args, "t": time.time()}
            elif ev.get("subtype") == "completed":
                st = pending.pop(ev.get("call_id"), {"kind": kind, "args": args, "t": time.time()})
                ok = (body.get("result") or {}).get("success") or {}
                code = ok.get("exitCode", 0 if ok else 1)
                if kind == "shellToolCall":
                    so = ok.get("stdout") or ""
                    res = summarize(so) if so.strip() else (ok.get("stderr") or "").strip()[:160] or "ok"
                    out(f"  → {res}" + (f"  (exit {code})" if code else ""))
                    tc = {"name": "Bash", "input": {"command": st["args"].get("command", "")},
                          "result": {"exit_code": code, "ok": code == 0}}
                elif "read" in kind.lower():
                    tc = {"name": "Read", "input": {"file_path": st["args"].get("path", "")}}
                elif "edit" in kind.lower() or "write" in kind.lower():
                    tc = {"name": "Edit", "input": {"file_path": st["args"].get("path", "")}, "result": {"ok": bool(ok)}}
                else:
                    tc = {"name": kind.replace("ToolCall", ""), "input": {k: v for k, v in st["args"].items()
                                                                         if k in ("pattern", "path", "query")}}
                tc.update(t=st["t"], ms=int((time.time() - st["t"]) * 1000))
                trace["tool_calls"].append(tc)
                save()
        elif t == "assistant":
            text = "".join(c.get("text", "") for c in ev.get("message", {}).get("content", []))
            if text.strip():
                trace["answer"] = text.strip()
                for l in text.strip().splitlines()[-3:]:
                    out(f"» {l[:200]}")
        elif t == "result":
            trace["tokens"] = ev.get("usage")
            trace["duration_ms"] = ev.get("duration_ms")
            u = ev.get("usage") or {}
            out(f"finished · {u.get('inputTokens', 0) + u.get('outputTokens', 0)} tokens "
                f"({u.get('cacheReadTokens', 0)} cached) · {ev.get('duration_ms', 0) / 1000:.1f}s")
    p.wait()


out(f"{NAME} approach in its own Superset workspace · waiting for the start signal")
while not os.path.exists(go_file):
    time.sleep(0.1)
time.sleep(max(0.0, float(open(go_file).read()) - time.time()))
trace["started"] = time.time()
save()
out("go")
(cursor if mode == "cursor" else scripted)()
if trace["duration_ms"] is None:
    trace["duration_ms"] = int((time.time() - trace["started"]) * 1000)
trace["finished"] = time.time()
save()
out("done")
