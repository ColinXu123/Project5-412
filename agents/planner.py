"""Planner: LLM-only agent (no AWS tools).
Step 1 analyzes the requirements (the starter's analyze()/save_analysis() tool),
step 2 decomposes them into tasks, and review() decides what happens after every result."""
import json
from datetime import datetime, timezone
from pathlib import Path

from agents.base import extract_json
from core.messages import Task

OUTPUT = Path("outputs")

# ---- from the starter agent.py (unchanged prompt + output validation) -------------------
ANALYST_SYSTEM = """You are a cloud requirements analyst for a student cloud project.
Extract concrete requirements and identify dependencies and unanswered questions.
Never claim you have deployed, configured, or tested any infrastructure.
Use only these AWS services: S3, CloudFront, Route 53, ACM.
Return JSON with keys: summary (string), requirements (array of strings),
service_dependencies (array of strings), risks (array of strings),
questions (array of strings). No markdown fences."""

EXPECTED = {"summary": str, "requirements": list, "service_dependencies": list,
            "risks": list, "questions": list}


def save_analysis(analysis, model="unknown"):
    """A local tool that writes a structured result for downstream agents."""
    OUTPUT.mkdir(exist_ok=True)
    path = OUTPUT / "requirements.json"
    path.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model, "analysis": analysis}, indent=2), encoding="utf-8")
    return str(path)
# -----------------------------------------------------------------------------------------

PLAN_PROMPT = """
You are the PLANNER agent in a multi-agent cloud deployment system. You have no tools.
You break a high-level goal into an ordered list of small technical tasks and assign each to
exactly one agent:
- "deployer": creates/changes AWS resources (S3, ACM, Route 53, CloudFront) via tools.
- "validator": runs real HTTP/TLS/DNS/S3 checks and records evidence.
You are given the goal, a requirements analysis and the deployer's tool names.
Reply with JSON only:
{"tasks": [{"agent": "deployer"|"validator", "action": "<short_snake_case>",
            "description": "<precise instruction>"}]}
Dependencies to respect when deploying a private S3 + CloudFront + custom domain site:
hosted zone (and its nameservers) -> private bucket -> upload site -> ACM certificate request ->
DNS validation records -> wait for ISSUED certificate -> OAC + CloudFront distribution ->
wait for distribution -> bucket policy for that distribution -> Route 53 alias records ->
cache invalidation -> validation. Keep to 8-12 tasks.
"""

REVIEW_PROMPT = """
You are the PLANNER agent reviewing the result of one task. Decide what happens next.
Reply with JSON only: {"decision": "continue" | "retry" | "abort", "reason": "<short>"}
- continue: the task succeeded with evidence in its data.
- retry: it failed for a reason that looks transient or fixable by repeating (max 2 retries).
- abort: it failed for a reason a retry cannot fix, or a human denied approval.
"""

ALLOWED_AGENTS = {"deployer", "validator"}


class Planner:
    name = "planner"

    def __init__(self, llm, logger, model="unknown"):
        self.llm, self.logger, self.model = llm, logger, model

    def _ask(self, system, content):
        return extract_json(self.llm.complete_json(system, content))

    def analyze(self, request: str) -> dict:
        for attempt in range(1, 3):
            data = self._ask(ANALYST_SYSTEM, request)
            bad = [k for k, kind in EXPECTED.items()
                   if not data or k not in data or not isinstance(data[k], kind)]
            if not bad:
                path = save_analysis(data, self.model)
                self.logger.log(self.name, "requirements_analyzed", {"saved": path, **data})
                return data
            self.logger.log(self.name, "analysis_invalid", {"attempt": attempt, "bad_keys": bad})
        raise RuntimeError("Planner failed to produce a valid requirements analysis")

    def plan(self, goal: str, deployer_tools: list) -> list:
        analysis = self.analyze(goal)
        parsed = self._ask(PLAN_PROMPT, f"GOAL: {goal}\nREQUIREMENTS ANALYSIS: "
                           f"{json.dumps(analysis)}\nDEPLOYER TOOLS: {deployer_tools}")
        tasks = []
        for t in (parsed or {}).get("tasks", []):
            if t.get("agent") in ALLOWED_AGENTS and t.get("description"):
                tasks.append(Task(self.name, t["agent"], t.get("action", "task"), t["description"]))
        self.logger.log(self.name, "plan_created", {"goal": goal,
                        "tasks": [{"id": t.id, "to": t.recipient, "action": t.action,
                                   "description": t.description} for t in tasks]})
        if not tasks:
            raise RuntimeError("Planner returned no valid tasks")
        return tasks

    def review(self, task: Task, result) -> dict:
        # Hard guard that does not depend on the LLM: never continue past a human denial.
        if result.status == "denied":
            decision = {"decision": "abort", "reason": "human denied approval"}
        else:
            decision = self._ask(REVIEW_PROMPT, f"TASK: {task.to_json()}\nRESULT: {result.to_json()}")
            if not decision or decision.get("decision") not in ("continue", "retry", "abort"):
                decision = {"decision": "continue" if result.ok else "abort",
                            "reason": "planner output unparseable; fell back on result status"}
            if not result.ok and decision["decision"] == "continue":
                decision = {"decision": "abort", "reason": "task failed; cannot continue"}
        self.logger.log(self.name, "review", {"task": task.id, "action": task.action, **decision})
        return decision
