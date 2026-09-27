"""The three agents' task, defined once.

cursor mode  : the steps become a real Cursor agent's instructions (it decides and runs them).
scripted mode: the same steps run as plain shell commands (fast, deterministic fallback).

Every memory operation goes through `tools/dm`, i.e. the live delta-memory server.
The task is shaped so one run touches every branch of the delta rule:
  builder  revalidates the deploy path       -> no-op (same value, nothing written)
  all 3    add themselves to the release set -> insert (first) / append (others)
  mover    moves the script, writes the path -> overwrite (version +1, old value kept)
  deployer pins the path from a stale read   -> queue (blocked, not applied)
and records one pipeline step each (source->image->registry->cluster), so memory can
compose source->cluster, a path none of them stored.
"""
from __future__ import annotations

TASK = "ship release {rid} of deploy-demo"

ROLES = {
    "builder": {
        "does": "revalidates the deploy script, records source → image",
        "steps": [
            "Read the deploy script path from memory: `tools/dm read deploy_script path`. Note its value and version.",
            "Check that file exists: `test -f <value> && echo exists`.",
            "If it exists, revalidate it in memory by writing the SAME value back with the version you read: "
            "`tools/dm write deploy_script path <value> --value-type path --expected-version <version>`.",
            "Join this release: `tools/dm write release_{rid} agents builder --cardinality multi`.",
            "Record the pipeline step you own: `tools/dm relate source image --ns {ns}`.",
        ],
    },
    "mover": {
        "does": "waits for the team, moves the deploy script, records image → registry",
        "steps": [
            "Join this release: `tools/dm write release_{rid} agents mover --cardinality multi`.",
            "Record the pipeline step you own: `tools/dm relate image registry --ns {ns}`.",
            "Wait until all three agents have joined before changing anything: "
            "`tools/dm wait-members release_{rid} agents --count 3 --timeout 180`.",
            "Read the deploy script path from memory: `tools/dm read deploy_script path`. Note its value and version.",
            "Move the script: `mkdir -p scripts/release-{rid} && mv <value> scripts/release-{rid}/deploy.sh`, then set "
            "`deploy_script: scripts/release-{rid}/deploy.sh` in config.yaml (e.g. with sed).",
            "Record the new path with the version you read: "
            "`tools/dm write deploy_script path scripts/release-{rid}/deploy.sh --value-type path --expected-version <version>`.",
        ],
    },
    "deployer": {
        "does": "deploys, records registry → cluster, asks memory what it implies",
        "steps": [
            "Read the deploy script path from memory: `tools/dm read deploy_script path`. Note its value V and version N.",
            "Join this release: `tools/dm write release_{rid} agents deployer --cardinality multi`.",
            "Record the pipeline step you own: `tools/dm relate registry cluster --ns {ns}`.",
            "Wait until another agent changes the path: `tools/dm wait deploy_script path --newer-than <N> --timeout 180`.",
            "Try to pin the path you validated earlier, using your ORIGINAL version N: "
            "`tools/dm write deploy_script path <V> --value-type path --expected-version <N>`. Report the status it returns.",
            "Read the path again (`tools/dm read deploy_script path`) and deploy with it: `sh <path> staging`.",
            "Ask memory for the whole pipeline, which no single agent recorded: `tools/dm compose source cluster --ns {ns}`.",
        ],
    },
}


def prompt(role: str, rid: str, ns: str) -> str:
    r = ROLES[role]
    steps = "\n".join(f"{i}. {s.format(rid=rid, ns=ns)}" for i, s in enumerate(r["steps"], 1))
    return (
        f"You are the {role} agent, one of three agents working IN PARALLEL on the same task: "
        f"{TASK.format(rid=rid)}. The agents share one memory server, reached only through the "
        f"`tools/dm` command in this repo. Work in the current directory. Do exactly these steps, "
        f"in order, running each command yourself, then stop and summarise in two lines what "
        f"memory returned at each write:\n{steps}\nDo not commit, do not edit anything else."
    )
