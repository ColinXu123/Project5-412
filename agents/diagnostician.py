"""Diagnostician: read-only investigation of failed checks; proposes (never applies) a fix."""
from agents.base import Agent, make_tool

SYSTEM = """
You are the DIAGNOSTICIAN agent for an AWS static site: private S3 bucket + CloudFront (OAC)
+ Route 53 + ACM. You receive failed validation checks with their evidence. You may ONLY use
read-only tools. You CANNOT change anything; you propose a fix.

Reasoning hints:
- CloudFront 403 on pages while direct-S3 is also 403 -> check the bucket policy; a missing or
  wrong policy (no cloudfront.amazonaws.com principal / wrong distribution ARN) is a classic cause.
- 403/404 only on "/" -> check default_root_object.
- Domain does not resolve / wrong target -> list_dns_records (missing A/AAAA alias to CloudFront).
- TLS errors -> certificate status and the distribution's certificate/aliases.
- Always gather evidence with tools BEFORE concluding. Do not guess.
- get_bucket_policy or get_public_access_block returns NoSuchBucket, or direct-S3 returns 404
  with a NoSuchBucket error -> the bucket itself is missing. Propose create_private_bucket.
  Do NOT propose create_distribution: the distribution already exists and the tool only reuses it.
- Bucket exists but is empty, or pages return 404 while the bucket policy is correct -> propose
  upload_site.
- Propose create_distribution only if get_distribution_summary shows no distribution exists.

The proposed fix must use exactly one of these Deployer tools:
apply_oac_bucket_policy (no args), upsert_alias_records (no args),
set_default_root_object (args: {"value": "index.html"}), add_cert_validation_records (no args),
invalidate_cache (no args), create_distribution (no args),
create_private_bucket (no args), upload_site (no args).

"data" must be:
{"root_cause": "<specific cause>", "evidence": ["<tool finding>", ...],
 "proposed_fix": {"tool": "<name>", "args": {...}, "explanation": "<why this fixes it>"},
 "risk": "low" | "medium" | "high"}
"""


def build_diagnostician(aws, llm, logger) -> Agent:
    tools = [
        make_tool(aws.get_bucket_policy, "Read the S3 bucket policy (returns policy=null if none)."),
        make_tool(aws.get_public_access_block, "Read S3 Block Public Access settings."),
        make_tool(aws.get_distribution_summary, "Read CloudFront settings: aliases, root object, origins/OAC, errors, cert."),
        make_tool(aws.list_dns_records, "List Route 53 records for the domain."),
        make_tool(aws.get_certificate_status, "Read ACM certificate status."),
        make_tool(aws.list_bucket_objects, "List the files stored in the site bucket."),
    ]
    return Agent("diagnostician", SYSTEM, tools, llm, logger, max_steps=15)
