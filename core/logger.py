"""Append-only logging of every message, tool call and decision.

One call to Logger.log() writes the SAME record to several places, as it happens:

  logs/agent_log.jsonl            full structured record (one JSON object per line)
  logs/01-planner-agent.log ... 04-diagnostician-agent.log, 08-human-approvals.log
                                  one readable file per agent
  logs/05-agent-communication.log VERBOSE Task / Result messages between agents (readable)
  logs/05-agent-communication.jsonl  the same messages as JSON, one per line
  logs/06-tool-execution.log      each tool call: who asked, tool, arguments, raw result,
                                  the agent's summary and how the Planner used it
  logs/07-failure-recovery.log    every record from the first failed validation until the
                                  validation that passes, labeled by phase (failure observed,
                                  diagnosis, proposed fix + human approval, recovery, verified)

Secrets are redacted before anything touches disk. Nothing here is written by hand: every
line comes from a real event in a real run.
"""
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

AGENT_FILES = {"planner": "01-planner-agent.log", "deployer": "02-deployer-agent.log",
               "validator": "03-validator-agent.log",
               "diagnostician": "04-diagnostician-agent.log", "human": "08-human-approvals.log"}

COMM_EVENTS = ("task_sent", "result_returned")
PLANNER_OUTCOMES = ("review", "validation_failed", "validation_passed", "no_usable_fix",
                    "fix_failed", "deployment_aborted", "invalid_json_retry")

PHASES = {
    "validation_failed": "1. FAILURE OBSERVED",
    "no_usable_fix": "2b. NO USABLE FIX IN THE DIAGNOSIS (asking again)",
    "invalid_json_retry": "2b. MODEL REPLY NOT VALID JSON (asking again)",
    "fix_approval": "3. PROPOSED FIX + HUMAN APPROVAL",
    "approval": "3. HUMAN APPROVAL OF A SENSITIVE TOOL",
    "fix_failed": "4b. FIX DID NOT SUCCEED",
    "validation_passed": "5. VERIFIED - ALL CHECKS PASSED",
    "human_action": "0. HUMAN ACTION",
}


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


def _pretty(data) -> str:
    text = json.dumps(data, indent=2, default=str, ensure_ascii=False)
    return "\n".join("    " + ln for ln in text.splitlines())


class Logger:
    def __init__(self, logs_dir: str = "logs", filename: str = "agent_log.jsonl"):
        os.makedirs(logs_dir, exist_ok=True)
        self.dir = logs_dir
        self.path = os.path.join(logs_dir, filename)
        self.agent_dir = logs_dir
        self._lock = threading.Lock()
        self._pending_tool = {}          # actor -> its latest tool_call record
        self._incident = False
        self._incident_no = 0
        self._tool_no = 0
        stamp = datetime.now(timezone.utc).isoformat()
        for name in ("05-agent-communication.log", "06-tool-execution.log", "07-failure-recovery.log"):
            self._append(name, f"\n===== RUN STARTED {stamp} (UTC) =====\n\n")

    # ------------------------------------------------------------------ helpers
    def _append(self, name, text):
        with open(os.path.join(self.dir, name), "a") as f:
            f.write(text)

    def _comm(self, rec):
        d = rec["data"] or {}
        if rec["event"] == "task_sent":
            head = (f"[{rec['ts']}] TASK    {d.get('sender')} -> {d.get('recipient')}"
                    f"   action={d.get('action')}   task_id={d.get('id')}")
        else:
            head = (f"[{rec['ts']}] RESULT  {d.get('sender')} -> {d.get('to')}"
                    f"   status={d.get('status')}   task_id={d.get('task_id')}")
        self._append("05-agent-communication.log", head + "\n" + _pretty(d) + "\n\n")
        line = {"ts": rec["ts"], "type": "Task" if rec["event"] == "task_sent" else "Result",
                "message": d}
        self._append("05-agent-communication.jsonl", json.dumps(line, default=str) + "\n")

    def _tool(self, rec):
        d, actor = rec["data"] or {}, rec["actor"]
        if rec["event"] == "tool_call":
            self._pending_tool[actor] = rec
            return
        if rec["event"] == "tool_result":
            req = self._pending_tool.pop(actor, None)
            self._tool_no += 1
            out = [f"=== TOOL EXECUTION #{self._tool_no} ===",
                   f"requested by   : {actor} agent",
                   f"time requested : {req['ts'] if req else '(not recorded)'}",
                   f"tool           : {d.get('tool')}",
                   f"arguments      : {json.dumps((req or {}).get('data', {}).get('args'), default=str)}",
                   f"time finished  : {rec['ts']}",
                   f"error flag     : {d.get('error')}",
                   "result         :", _pretty(d.get("output"))]
            self._append("06-tool-execution.log", "\n".join(out) + "\n")
        elif rec["event"] == "result_returned":
            self._append("06-tool-execution.log",
                         f"  -> {actor} reported to {d.get('to')}: [{d.get('status')}] {d.get('summary')}\n\n")
        elif actor == "planner" and rec["event"] in PLANNER_OUTCOMES:
            self._append("06-tool-execution.log",
                         f"  -> planner {rec['event']}: {json.dumps(d, default=str)}\n\n")

    def _failure(self, rec):
        d, actor, ev = rec["data"] or {}, rec["actor"], rec["event"]
        if ev == "human_action" and not self._incident:
            self._append("07-failure-recovery.log",
                         f"--- {PHASES['human_action']} ---\n[{rec['ts']}] {actor} :: {ev}\n"
                         f"{_pretty(d)}\n\n")
            return
        if ev == "validation_failed" and not self._incident:
            self._incident = True
            self._incident_no += 1
            self._append("07-failure-recovery.log",
                         f"##### INCIDENT {self._incident_no} OPENED {rec['ts']} #####\n\n")
        if not self._incident:
            return
        phase = PHASES.get(ev)
        if phase is None:
            if actor == "diagnostician":
                phase = "2. AGENT DIAGNOSIS"
            elif actor == "deployer" and ev in ("tool_call", "tool_result", "result_returned"):
                phase = "4. RECOVERY OPERATION"
            elif actor == "validator":
                phase = "1/5. VALIDATION CHECKS"
            else:
                phase = "   (message routing / decision)"
        text = f"--- {phase} ---\n[{rec['ts']}] {actor} :: {ev}\n{_pretty(d)}\n"
        if ev == "result_returned" and actor == "diagnostician":
            data = d.get("data") or {}
            if data.get("root_cause"):
                pf = data.get("proposed_fix") or {}
                text += (f"    >> DIAGNOSIS    : {data.get('root_cause')}\n"
                         f"    >> PROPOSED FIX : {pf.get('tool')} {pf.get('args')} "
                         f"(risk {data.get('risk')})\n")
        self._append("07-failure-recovery.log", text + "\n")
        if ev == "validation_passed":
            self._incident = False
            self._append("07-failure-recovery.log",
                         f"##### INCIDENT {self._incident_no} RESOLVED {rec['ts']} #####\n\n")

    # --------------------------------------------------------------------- log
    def log(self, actor: str, event: str, data=None):
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "event": event,
            "data": redact(data),
        }
        line = json.dumps(rec, default=str)
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(actor))
        agent_file = AGENT_FILES.get(safe, f"09-{safe}.log")
        with self._lock:
            with open(self.path, "a") as f:
                f.write(line + "\n")
            with open(os.path.join(self.agent_dir, agent_file), "a") as f:
                f.write(f"[{rec['ts']}] {event}\n{_pretty(rec['data'])}\n\n")
            if event in COMM_EVENTS:
                self._comm(rec)
            if event in ("tool_call", "tool_result") or (
                    event == "result_returned") or (
                    actor == "planner" and event in PLANNER_OUTCOMES):
                self._tool(rec)
            self._failure(rec)

        # Console: agent-to-agent messages are printed in FULL; other records are shortened.
        if event in COMM_EVENTS:
            d = rec["data"] or {}
            if event == "task_sent":
                print(f"\n[{actor}] TASK -> {d.get('recipient')}  action={d.get('action')}  "
                      f"id={d.get('id')}\n{_pretty(d)}")
            else:
                print(f"\n[{actor}] RESULT -> {d.get('to')}  status={d.get('status')}  "
                      f"task_id={d.get('task_id')}\n{_pretty(d)}")
            return
        text = json.dumps(rec["data"], default=str)
        if len(text) > 200:
            text = text[:197] + "..."
        print(f"[{actor}] {event}: {text}")