"""Append-only JSONL log of every message, tool call and decision.
Secrets are redacted before anything touches disk."""
import json
import os
import re
import threading
from datetime import datetime, timezone

_PATTERNS = [
    re.compile(r"(AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
]
_SECRET_KEYS = re.compile(r"secret|token|password|api[_-]?key|authorization", re.I)


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("***REDACTED***" if _SECRET_KEYS.search(str(k)) else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        for p in _PATTERNS:
            obj = p.sub("***REDACTED***", obj)
        return obj
    return obj


class Logger:
    def __init__(self, logs_dir: str = "logs", filename: str = "agent_log.jsonl"):
        os.makedirs(logs_dir, exist_ok=True)
        self.path = os.path.join(logs_dir, filename)
        self.agent_dir = os.path.join(logs_dir, "by_agent")     # one readable file per agent
        os.makedirs(self.agent_dir, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, actor: str, event: str, data=None):
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "event": event,
            "data": redact(data),
        }
        line = json.dumps(rec, default=str)
        with self._lock, open(self.path, "a") as f:
            f.write(line + "\n")
        # the same record, written to this agent's own file (planner.log, deployer.log, ...)
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(actor))
        pretty = json.dumps(rec["data"], indent=2, default=str, ensure_ascii=False)
        with self._lock, open(os.path.join(self.agent_dir, f"{safe}.log"), "a") as f:
            f.write(f"[{rec['ts']}] {event}\n")
            f.write("\n".join("    " + ln for ln in pretty.splitlines()) + "\n\n")
        text = json.dumps(rec["data"], default=str)
        if len(text) > 200:
            text = text[:197] + "..."
        print(f"[{actor}] {event}: {text}")