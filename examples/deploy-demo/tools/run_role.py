#!/Users/savirdillikar/yc-hack/delta-memory/.venv/bin/python
"""Run one agent role inside its Superset workspace terminal.

    tools/run_role.py <role> <go-file> <run-id> <cursor|scripted>

Waits for the shared go-file (so all three start together), then:
  cursor   -> starts a real Cursor agent (`cursor-agent -p -f`) with the role's instructions and
              prints its stream as it happens: every shell command it runs, and the result.
  scripted -> runs the role's steps as plain shell commands, printed the same way.
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
from roles import ROLES, prompt  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
role, go_file, rid, mode = sys.argv[1:5]
ns = f"r{rid}"
os.environ["DM_AGENT_NAME"] = f"{role} ({'cursor' if mode == 'cursor' else 'script'} pid {os.getpid()})"


def out(line: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {line}", flush=True)


def sh(cmd: str) -> str:
    out(f"$ {cmd}")
    p = subprocess.run(cmd, shell=True, cwd=REPO, capture_output=True, text=True)
    res = summarize(p.stdout) if p.stdout.strip() else (p.stderr.strip()[:160] or f"exit {p.returncode}")
    out(f"  → {res}" + (f"  (exit {p.returncode})" if p.returncode else ""))
    return p.stdout


def read_path():
    d = json.loads(sh("tools/dm read deploy_script path"))
    return d["value"]["data"], d["version"]


def scripted() -> None:
    if role == "builder":
        v, n = read_path()
        sh(f"test -f {v} && echo exists")
        sh(f"tools/dm write deploy_script path {v} --value-type path --expected-version {n}")
        sh(f"tools/dm write release_{rid} agents builder --cardinality multi")
        sh(f"tools/dm relate source image --ns {ns}")
    elif role == "mover":
        sh(f"tools/dm write release_{rid} agents mover --cardinality multi")
        sh(f"tools/dm relate image registry --ns {ns}")
        sh(f"tools/dm wait-members release_{rid} agents --count 3 --timeout 60")
        v, n = read_path()
        new = f"scripts/release-{rid}/deploy.sh"
        sh(f"mkdir -p scripts/release-{rid} && mv {v} {new} && "
           f"sed -i '' 's#^deploy_script: .*#deploy_script: {new}#' config.yaml && echo moved")
        sh(f"tools/dm write deploy_script path {new} --value-type path --expected-version {n}")
    elif role == "deployer":
        v, n = read_path()
        sh(f"tools/dm write release_{rid} agents deployer --cardinality multi")
        sh(f"tools/dm relate registry cluster --ns {ns}")
        sh(f"tools/dm wait deploy_script path --newer-than {n} --timeout 60")
        sh(f"tools/dm write deploy_script path {v} --value-type path --expected-version {n}")
        cur, _ = read_path()
        sh(f"sh {cur} staging")
        sh(f"tools/dm compose source cluster --ns {ns}")


def cursor() -> None:
    cmd = ["cursor-agent", "-p", "-f", "--output-format", "stream-json", prompt(role, rid, ns)]
    out(f"cursor-agent -p -f  (task: {ROLES[role]['does']})")
    p = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                out(line.rstrip()[:200])
            continue
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            out(f"cursor agent started · model {ev.get('model')} · session {ev.get('session_id', '')[:8]}")
        elif t == "tool_call":
            call = ev.get("tool_call", {})
            kind, body = next(iter(call.items())) if call else ("tool", {})
            if ev.get("subtype") == "started":
                args = body.get("args", {})
                what = args.get("command") or args.get("path") or json.dumps(args)[:120]
                out(f"$ {what}" if kind == "shellToolCall" else f"[{kind.replace('ToolCall', '')}] {what}")
            elif ev.get("subtype") == "completed":
                res = body.get("result", {})
                ok = res.get("success") or {}
                if kind == "shellToolCall" and ok:
                    so = ok.get("stdout", "")
                    code = ok.get("exitCode", 0)
                    out(f"  → {summarize(so) if so.strip() else (ok.get('stderr', '').strip()[:160] or 'ok')}"
                        + (f"  (exit {code})" if code else ""))
                elif res.get("error") or res.get("failure"):
                    out(f"  → error: {json.dumps(res)[:160]}")
        elif t == "assistant":
            text = "".join(c.get("text", "") for c in ev.get("message", {}).get("content", []))
            if text.strip():
                for l in text.strip().splitlines():
                    out(f"» {l}")
        elif t == "result":
            out(f"cursor agent finished in {ev.get('duration_ms', 0) / 1000:.1f}s")
    p.wait()


out(f"{role} agent in its Superset workspace · task: {ROLES[role]['does']} · waiting for shared start")
while not os.path.exists(go_file):
    time.sleep(0.1)
time.sleep(max(0.0, float(open(go_file).read()) - time.time()))
out("go")
(cursor if mode == "cursor" else scripted)()
out("done")
