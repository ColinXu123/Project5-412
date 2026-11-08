# CSCE 412 Project 5 - Multi-Agent Secure AWS Website

Four LLM agents plan, deploy, verify and repair a static site on S3 (private) + CloudFront +
Route 53 + ACM. Built from the "Your First Cloud Requirements AI Agent" starter: its requirements
analyst and `save_analysis` tool are now the Planner's first step.

```
                     +-----------+
   goal -----------> |  Planner  |  analyze requirements -> decompose -> review every result
                     +-----+-----+
          Task/Result (JSON over MessageBus, every message logged)
        +-----------------+------------------+
        v                 v                  v
  +-----------+     +-----------+     +---------------+
  | Deployer  |     | Validator |     | Diagnostician |
  | boto3     |     | HTTP/TLS/ |     | read-only     |
  | (gated)   |     | DNS/S3    |     | boto3 + LLM   |
  +-----------+     +-----------+     +---------------+
```

| Agent | Responsibility | Tools |
|---|---|---|
| Planner | Requirements analysis, task decomposition, continue/retry/abort per task | `save_analysis` (local) |
| Deployer | Creates/changes AWS resources; sensitive tools need a human `y` | `tools/aws_tools.py` |
| Validator | Runs 8 real checks and saves evidence JSON | `tools/check_tools.py` |
| Diagnostician | Reads AWS config + failed evidence, proposes one fix (cannot apply it) | read-only subset of `aws_tools` |

Pass/fail is never LLM opinion: the Validator's tools record raw status codes, headers and
certificate data, `save_validation_report` writes them to `reports/validation_report.json`, and
`agent.py` reads that file to decide whether the site is healthy.

## Prerequisites
Python 3.10+, an OpenAI API key, an AWS account (least-privilege IAM user, see
`iam/agent-policy.json`), a registered domain, and a terminal.

## Setup
```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env  # On Windows, copy .env.example .env
aws configure --profile project5
```
Edit `.env` to supply `OPENAI_API_KEY`. Never commit `.env` to Git.
Edit `config.yaml` (netid, domain, bucket, `aws_profile: project5`). Put your five pages,
`404.html` and assets in `site/`.

## Run
```bash
python agent.py deploy      # analyze -> plan -> deploy -> validate (asks approval for sensitive steps)
python agent.py validate    # validate; on failure: diagnose -> you approve -> fix -> re-validate
python tests/test_offline.py
```

## Manual steps (cannot be automated)
1. Register `csce412<netid>.me` at Namecheap.
2. The Deployer prints 4 Route 53 nameservers - set them at Namecheap (Custom DNS). ACM cannot
   validate the certificate until this propagates.
3. Create the IAM user and keys.
4. Type `y` at approval prompts (create_distribution, bucket policy, DNS records, CloudFront edits).

## Part E failure demo
1. `python agent.py deploy` until every check passes.
2. `python scripts/break_bucket_policy.py --profile <your-own-admin-profile>` - deletes the bucket
   policy and invalidates the cache. Wait ~2 minutes.
3. `python agent.py validate` - checks fail, the Diagnostician reads the policy and finds none,
   proposes `apply_oac_bucket_policy`, you approve, the Deployer applies it, the Validator passes.
4. Evidence: `logs/agent_log.jsonl`, `reports/validation_report.json`. Commit both.

## Security
Never print or commit secrets (the logger redacts keys and tokens). LLM output never runs as a
shell command: agents can only call the fixed tools registered for them. Agents use a
least-privilege IAM policy, not admin, and destructive/security-sensitive tools require human
approval. `outputs/` (scratch requirements analysis) is git-ignored; `logs/` and `reports/` are
deliverables and are committed.

## Layout
`agent.py` entry point - `agents/` base loop + 4 agents - `core/` messages, bus, logger, config -
`tools/` aws, checks, approval - `iam/` policy - `scripts/` failure injection - `tests/` offline tests
