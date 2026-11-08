"""Validator: runs the Part D checks. Evidence comes from tool output, never from the LLM."""
from agents.base import Agent, make_tool

SYSTEM = """
You are the VALIDATOR agent. You verify a deployed website using ONLY your check tools.
Run EVERY check once: check_dns, check_https_domain, check_http_redirect, check_certificate,
check_pages, check_custom_404, check_s3_direct_denied, check_cloudfront_headers.
Then call save_validation_report exactly once (it writes the recorded evidence to disk).
Do not guess or infer results. In your final JSON, "data" must contain "all_passed",
"failed_checks" and, for each failed check, the key evidence (status codes / error text) copied
verbatim from the tool output.
"""


def build_validator(checker, llm, logger) -> Agent:
    tools = [
        make_tool(checker.check_dns, "DNS: does the domain resolve?"),
        make_tool(checker.check_https_domain, "HTTPS: GET https://domain/ with cert verification, expect 200."),
        make_tool(checker.check_http_redirect, "HTTP request must redirect (301) to https://."),
        make_tool(checker.check_certificate, "TLS cert: SAN contains the domain, not expired, issuer."),
        make_tool(checker.check_pages, "All required pages return 200."),
        make_tool(checker.check_custom_404, "Missing path returns 404 with the custom error page."),
        make_tool(checker.check_s3_direct_denied, "Anonymous direct S3 object URL must be 403."),
        make_tool(checker.check_cloudfront_headers, "Response has CloudFront via / x-cache headers."),
        make_tool(checker.save_validation_report, "Write all recorded results to reports/validation_report.json."),
        make_tool(checker.check_bucket_contents, "S3 bucket must contain every required page."),
    ]
    return Agent("validator", SYSTEM, tools, llm, logger)
