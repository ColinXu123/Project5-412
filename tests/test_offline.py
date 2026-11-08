"""Offline tests: a scripted fake LLM drives the real agent loop, tools, bus, logger, approval."""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.base import Agent, make_tool
from agents.planner import Planner
from core.bus import MessageBus
from core.logger import Logger, redact
from core.messages import Task
from tools import approval
from tools.check_tools import Checker


def text(s):
    return NS(content=s, tool_calls=None)


def tool_use(name, **args):
    call = NS(id="t1", function=NS(name=name, arguments=json.dumps(args)))
    return NS(content=None, tool_calls=[call])


class FakeLLM:
    """chat() returns scripted assistant messages; complete_json() returns scripted JSON text."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, system, messages, tools=None):
        self.calls.append(messages[-1])
        return self.replies.pop(0)

    def complete_json(self, system, user):
        return self.replies.pop(0).content


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.logger = Logger(self.tmp)
        approval.init(self.logger)

    def test_agent_runs_tool_and_returns_result(self):
        calls = []

        def add(a: int, b: int):
            calls.append((a, b))
            return {"sum": a + b}

        llm = FakeLLM([tool_use("add", a=2, b=3),
                       text('{"status":"success","summary":"done","data":{"sum":5}}')])
        agent = Agent("t", "sys", [make_tool(add, "adds")], llm, self.logger)
        res = agent.run(Task("planner", "t", "x", "add"))
        self.assertTrue(res.ok)
        self.assertEqual(res.data["sum"], 5)
        self.assertEqual(calls, [(2, 3)])
        # the tool result was fed back to the LLM
        self.assertIn("5", json.dumps(llm.calls[-1]["content"]))

    def test_tool_error_is_reported_not_raised(self):
        def boom():
            raise RuntimeError("nope")

        llm = FakeLLM([tool_use("boom"), text('{"status":"failed","summary":"err","data":{}}')])
        res = Agent("t", "s", [make_tool(boom, "b")], llm, self.logger).run(Task("p", "t", "x", "y"))
        self.assertEqual(res.status, "failed")
        self.assertIn("RuntimeError: nope", llm.calls[-1]["content"])

    def test_approval_gate_denies(self):
        ran = []
        g = approval.gated("danger", lambda: ran.append(1) or {"ok": 1}, "test")
        with mock.patch("builtins.input", return_value="n"):
            out = g()
        self.assertEqual(out["status"], "denied_by_human")
        self.assertEqual(ran, [])

    def test_approval_gate_allows_and_grant_is_one_time(self):
        ran = []
        g = approval.gated("danger", lambda: ran.append(1) or {"ok": 1}, "test")
        with mock.patch("builtins.input", return_value="y"):
            self.assertEqual(g(), {"ok": 1})
        approval.grant("danger")
        self.assertEqual(g(), {"ok": 1})          # pre-approved, no prompt
        with mock.patch("builtins.input", return_value="n"):
            self.assertEqual(g()["status"], "denied_by_human")   # grant was consumed

    def test_planner_plan_and_denial_guard(self):
        plan = json.dumps({"tasks": [
            {"agent": "deployer", "action": "make_bucket", "description": "create bucket"},
            {"agent": "hacker", "action": "bad", "description": "ignored: unknown agent"},
            {"agent": "validator", "action": "validate", "description": "check"}]})
        analysis = json.dumps({"summary": "s", "requirements": ["r"], "service_dependencies": [],
                               "risks": [], "questions": []})
        import agents.planner as planner_mod
        from pathlib import Path
        planner_mod.OUTPUT = Path(self.tmp) / "outputs"
        p = Planner(FakeLLM([text(analysis), text(plan)]), self.logger)
        tasks = p.plan("goal", ["x"])
        self.assertTrue((Path(self.tmp) / "outputs" / "requirements.json").exists())
        self.assertEqual([t.recipient for t in tasks], ["deployer", "validator"])
        denied = Task("planner", "deployer", "a", "d")
        from core.messages import Result
        d = p.review(denied, Result(denied.id, "deployer", "denied", "no"))
        self.assertEqual(d["decision"], "abort")   # no LLM call needed

    def test_bus_logs_messages_and_unknown_agent(self):
        bus = MessageBus(self.logger)
        res = bus.dispatch(Task("planner", "ghost", "a", "b"))
        self.assertEqual(res.status, "failed")
        events = [json.loads(l)["event"] for l in open(self.logger.path)]
        self.assertIn("task_sent", events)
        self.assertIn("result_returned", events)

    def test_redaction(self):
        out = redact({"aws_secret_access_key": "abc", "k": "AKIAABCDEFGHIJKLMNOP", "ok": "fine"})
        self.assertEqual(out["aws_secret_access_key"], "***REDACTED***")
        self.assertEqual(out["k"], "***REDACTED***")
        self.assertEqual(out["ok"], "fine")

    def test_checker_records_real_failures_and_report(self):
        cfg = {"domain": "nonexistent.invalid", "bucket": "b", "region": "us-east-1",
               "pages": ["index.html"], "reports_dir": self.tmp}
        c = Checker(cfg)
        for fn in (c.check_https_domain, c.check_http_redirect, c.check_certificate,
                   c.check_pages, c.check_cloudfront_headers, c.check_dns):
            self.assertFalse(fn()["passed"])
        summary = c.save_validation_report()
        self.assertFalse(summary["all_passed"])
        report = json.load(open(os.path.join(self.tmp, "validation_report.json")))
        self.assertGreaterEqual(len(report["failed_checks"]), 6)
        self.assertIn("error", report["results"][0]["evidence"])


if __name__ == "__main__":
    unittest.main()
