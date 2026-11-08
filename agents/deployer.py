"""Deployer: the only agent allowed to change AWS. Sensitive tools require human approval."""
from agents.base import Agent, make_tool
from tools import approval

SYSTEM = """
You are the DEPLOYER agent. You change AWS infrastructure using ONLY your tools.
Rules:
- Do exactly what the task says; do not add extra steps or invent resource names.
- Tools are idempotent; it is safe to call them again.
- If a tool returns {"status": "denied_by_human"}, stop and report status "denied".
- If a tool errors, read the error, try a sensible fix with your tools at most twice, then report
  "failed" with the exact error.
- The ACM certificate cannot be issued until the domain's nameservers at Namecheap point to the
  Route 53 hosted zone. If the certificate stays pending, report this clearly; it is a manual step.
- When asked to apply a specific fix, call exactly that tool with exactly those arguments.
Put resource IDs, ARNs and nameservers you obtained in "data".
"""


def build_deployer(aws, llm, logger) -> Agent:
    G = approval.gated
    tools = [
        make_tool(aws.ensure_hosted_zone, "Create/find the Route 53 hosted zone; returns the nameservers to set at Namecheap."),
        make_tool(aws.create_private_bucket, "Create the S3 bucket with all public access blocked."),
        make_tool(aws.upload_site, "Upload the local site directory to the S3 bucket."),
        make_tool(aws.request_certificate, "Request a DNS-validated ACM certificate for the domain (us-east-1)."),
        make_tool(aws.add_cert_validation_records, "Create the ACM DNS validation CNAME in Route 53."),
        make_tool(aws.wait_for_certificate, "Poll until the ACM certificate is ISSUED (or fails/times out)."),
        make_tool(aws.create_oac, "Create/find the CloudFront Origin Access Control."),
        make_tool(G("create_distribution", aws.create_distribution,
                    "creates a public-facing CloudFront distribution"),
                  "Create the CloudFront distribution (HTTPS redirect, OAC, default root, 404 page). Needs ISSUED cert. REQUIRES HUMAN APPROVAL.",
                  name="create_distribution"),
        make_tool(aws.wait_for_distribution, "Poll until the CloudFront distribution status is Deployed."),
        make_tool(G("apply_oac_bucket_policy", aws.apply_oac_bucket_policy,
                    "changes S3 bucket permissions (security-sensitive)"),
                  "Attach the bucket policy that lets ONLY this CloudFront distribution read the bucket. REQUIRES HUMAN APPROVAL.",
                  name="apply_oac_bucket_policy"),
        make_tool(G("upsert_alias_records", aws.upsert_alias_records,
                    "changes public DNS records"),
                  "Point the domain's A/AAAA alias records at CloudFront. REQUIRES HUMAN APPROVAL.",
                  name="upsert_alias_records"),
        make_tool(G("set_default_root_object", aws.set_default_root_object,
                    "modifies a live CloudFront distribution"),
                  "Set the distribution's default root object (e.g. index.html). REQUIRES HUMAN APPROVAL.",
                  name="set_default_root_object"),
        make_tool(aws.invalidate_cache, "Invalidate the CloudFront cache (/*)."),
        make_tool(aws.get_distribution_summary, "Read the CloudFront distribution settings."),
        make_tool(aws.get_bucket_policy, "Read the S3 bucket policy."),
        make_tool(aws.list_dns_records, "List Route 53 records."),
    ]
    return Agent("deployer", SYSTEM, tools, llm, logger)


# Tools the Diagnostician is allowed to propose as a fix
FIXABLE_TOOLS = {"apply_oac_bucket_policy", "upsert_alias_records", "set_default_root_object",
                 "add_cert_validation_records", "invalidate_cache", "create_distribution", "create_private_bucket", "upload_site"}
