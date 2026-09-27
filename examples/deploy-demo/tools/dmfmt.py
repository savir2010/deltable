"""Readable one-line summaries of what a command printed (shared by the agent runners)."""
import json


def summarize(stdout: str) -> str:
    """One line for what a command printed: plain text lines, plus a summary of any JSON
    tools/dm returned (an agent may chain several commands into one)."""
    parts, i, dec = [], 0, json.JSONDecoder()
    while i < len(stdout):
        j = stdout.find("{", i)
        text = stdout[i:] if j < 0 else stdout[i:j]
        parts += [l.strip() for l in text.splitlines() if l.strip()]
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(stdout, j)
            parts.append(_one(obj))
            i = end
        except ValueError:
            parts.append(stdout[j:].strip().splitlines()[0])
            break
    return " · ".join(parts)[:220]


def _one(d: dict) -> str:
    if "path" in d and "start" in d:  # compose result
        hops = " → ".join([d["start"]] + [h["dst"] for h in d["path"]])
        op = d.get("composite_operator") or {}
        return (f"IMPLIED: {hops}  (composite operator lands on {op.get('lands_on')}, score {op.get('score')})"
                if d["path"] else "no path")
    if "status" in d and "message" in d:
        return f"{d['status'].upper()}: {d['message'][:140]}"
    if "found" in d and "value" in d:
        return f"{d['value']['data']!r} @ v{d['version']}"
    if "found" in d:
        return "not found"
    return json.dumps(d)[:160]
