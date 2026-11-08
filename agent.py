"""Multi-agent cloud deployment system (entry point).

  python agent.py deploy      Planner analyzes requirements and decomposes the goal; Deployer and
                              Validator execute it (sensitive steps need your approval).
  python agent.py validate    Validator checks the live site. On failure: Diagnostician investigates,
                              you approve the proposed fix, Deployer applies it, Validator re-checks.
"""
import argparse
import json
import os
import sys
import time

from dotenv import load_dotenv

from agents.base import LLM
from agents.deployer import build_deployer, FIXABLE_TOOLS
from agents.diagnostician import build_diagnostician
from agents.planner import Planner
from agents.validator import build_validator
from core.bus import MessageBus
from core.config import load_config
from core.logger import Logger
from core.messages import Task
from tools import approval
from tools.aws_tools import AwsTools
from tools.check_tools import Checker

load_dotenv()
MAX_RETRIES = 2


class System:
    def __init__(self, cfg, llm=None, aws=None, checker=None, logger=None):
        self.cfg = cfg
        self.logger = logger or Logger(cfg.get("logs_dir", "logs"))
        approval.init(self.logger)
        llm = llm or LLM(cfg["model"])
        aws = aws or AwsTools(cfg)
        checker = checker or Checker(cfg)
        self.bus = MessageBus(self.logger)
        self.planner = Planner(llm, self.logger, cfg["model"])
        self.deployer = build_deployer(aws, llm, self.logger)
        self.validator = build_validator(checker, llm, self.logger)
        self.diagnostician = build_diagnostician(aws, llm, self.logger)
        for a in (self.deployer, self.validator, self.diagnostician):
            self.bus.register(a)

    # ------------------------------------------------------------------ deploy
    def deploy(self) -> bool:
        c = self.cfg
        goal = (f"Build a five-page static website with a custom domain, valid HTTPS certificate, "
                f"private S3 origin, CloudFront delivery, and HTTP-to-HTTPS redirects. "
                f"Domain: {c['domain']}. S3 bucket: {c['bucket']}. Use Route 53 for DNS and ACM "
                f"for the certificate. Finish by validating the deployment.")
        tasks = self.planner.plan(goal, list(self.deployer.tools))
        for task in tasks:
            for attempt in range(MAX_RETRIES + 1):
                result = self.bus.dispatch(task)
                decision = self.planner.review(task, result)
                if decision["decision"] == "continue":
                    break
                if decision["decision"] == "abort" or attempt == MAX_RETRIES:
                    self.logger.log("planner", "deployment_aborted",
                                    {"task": task.action, "reason": decision["reason"]})
                    return False
        report = self.read_report()
        return bool(report and report["all_passed"])

    # ---------------------------------------------------------------- validate
    def read_report(self):
        path = os.path.join(self.cfg.get("reports_dir", "reports"), "validation_report.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def run_validation(self):
        task = Task("planner", "validator", "validate_deployment",
                    "Run all validation checks against the live site and save the report.")
        self.bus.dispatch(task)
        return self.read_report()      # ground truth = recorded evidence, not LLM text

    def validate_and_heal(self) -> bool:
        max_fix = self.cfg.get("max_fix_attempts", 2)
        for attempt in range(max_fix + 1):
            report = self.run_validation()
            if report and report["all_passed"]:
                self.logger.log("planner", "validation_passed", {"attempt": attempt})
                return True
            failed = [r for r in (report or {}).get("results", []) if not r["passed"]]
            self.logger.log("planner", "validation_failed",
                            {"attempt": attempt, "failed": [r["check"] for r in failed]})
            if attempt >= max_fix:
                break

            diag = self.bus.dispatch(Task(
                "planner", "diagnostician", "diagnose_failure",
                "Find the root cause of these failed checks and propose ONE fix.",
                {"failed_checks": failed}))
            fix = (diag.data or {}).get("proposed_fix")
            if not diag.ok or not fix or fix.get("tool") not in FIXABLE_TOOLS:
                self.logger.log("planner", "no_usable_fix", {"diagnosis": diag.data})
                return False

            print("\n" + "=" * 60)
            print("DIAGNOSIS:", diag.data.get("root_cause"))
            for e in diag.data.get("evidence", []):
                print("  evidence:", e)
            print(f"PROPOSED FIX: {fix['tool']}({fix.get('args', {})})  risk={diag.data.get('risk')}")
            print("  ", fix.get("explanation"))
            ok = input("Apply this fix? [y/N] ").strip().lower() == "y"
            self.logger.log("human", "fix_approval", {"fix": fix, "approved": ok})
            if not ok:
                return False

            approval.grant(fix["tool"])
            try:
                res = self.bus.dispatch(Task(
                    "planner", "deployer", "apply_fix",
                    f"Call the tool {fix['tool']} with exactly these arguments: "
                    f"{json.dumps(fix.get('args', {}))}. Report the outcome.",
                    {"tool": fix["tool"], "args": fix.get("args", {})}))
            finally:
                approval.clear_grants()
            if not res.ok:
                return False
            print("Fix applied. Waiting 30s for CloudFront/DNS to settle, then re-validating...")
            time.sleep(30)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["deploy", "validate"])
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    system = System(load_config(args.config))
    ok = system.deploy() if args.command == "deploy" else system.validate_and_heal()
    print("\nRESULT:", "ALL CHECKS PASSED" if ok else "NOT HEALTHY - see logs/agent_log.jsonl "
          "and reports/validation_report.json")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
