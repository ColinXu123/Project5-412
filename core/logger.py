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
        text = json.dumps(rec["data"], default=str)
        if len(text) > 200:
            text = text[:197] + "..."
        print(f"[{actor}] {event}: {text}")
