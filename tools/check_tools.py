"""Validation checks. Each check makes a REAL network request and records raw evidence.
Results are stored in-process and written by save_validation_report(), so neither the LLM
nor anyone else can edit the evidence between the check and the report."""
import json
import os
import socket
import ssl
import time
from datetime import datetime, timezone

import boto3
import dns.resolver
import requests

TIMEOUT = 15

# Every one of these must have been run before a report can be saved.
REQUIRED = ["dns_resolves", "https_domain_reachable", "http_redirects_to_https",
            "certificate_matches_domain", "required_pages_accessible", "custom_404_page",
            "s3_direct_access_denied", "cloudfront_serves_content", "bucket_has_site_files"]


class Checker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.domain = cfg["domain"]
        self.results = []
        self._remove_stale_report()      # an old report must never be mistaken for a new one

    def reset(self):
        """Forget every earlier result. Called before each validation round so a check that
        is skipped or fails to run can never be satisfied by an OLD result."""
        self.results = []
        self._remove_stale_report()

    def _report_path(self):
        return os.path.join(self.cfg.get("reports_dir", "reports"), "validation_report.json")

    def _remove_stale_report(self):
        try:
            os.remove(self._report_path())
        except FileNotFoundError:
            pass

    def _record(self, name, passed, evidence):
        rec = {"check": name, "passed": bool(passed), "evidence": evidence,
               "ts": datetime.now(timezone.utc).isoformat()}
        self.results = [r for r in self.results if r["check"] != name] + [rec]
        return rec

    # 1 + 2: reachable over HTTPS with certificate verification ON
    def check_https_domain(self):
        try:
            r = requests.get(f"https://{self.domain}/", timeout=TIMEOUT, verify=True)
            return self._record("https_domain_reachable", r.status_code == 200,
                                {"url": r.url, "status_code": r.status_code, "tls_verified": True})
        except Exception as e:
            return self._record("https_domain_reachable", False, {"error": repr(e)})

    # 3: HTTP -> HTTPS redirect
    def check_http_redirect(self):
        try:
            r = requests.get(f"http://{self.domain}/", timeout=TIMEOUT, allow_redirects=False)
            loc = r.headers.get("Location", "")
            ok = r.status_code in (301, 302, 307, 308) and loc.startswith("https://")
            return self._record("http_redirects_to_https", ok,
                                {"status_code": r.status_code, "location": loc})
        except Exception as e:
            return self._record("http_redirects_to_https", False, {"error": repr(e)})

    # 4: certificate matches domain (handshake with hostname verification, then inspect cert)
    def check_certificate(self):
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((self.domain, 443), timeout=TIMEOUT) as sock:
                with ctx.wrap_socket(sock, server_hostname=self.domain) as s:
                    cert, tls = s.getpeercert(), s.version()
            sans = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
            expires = ssl.cert_time_to_seconds(cert["notAfter"])
            now = datetime.now(timezone.utc).timestamp()
            issuer = dict(x[0] for x in cert.get("issuer", ()))
            ok = self.domain in sans and expires > now
            return self._record("certificate_matches_domain", ok, {
                "san": sans, "not_after": cert["notAfter"], "tls_version": tls,
                "issuer": issuer.get("organizationName") or issuer.get("commonName"),
                "hostname_verified_by_handshake": True})
        except Exception as e:
            return self._record("certificate_matches_domain", False, {"error": repr(e)})

    # 5: every required page returns 200
    def check_pages(self):
        out, ok = {}, True
        for page in self.cfg["pages"]:
            try:
                r = requests.get(f"https://{self.domain}/{page}", timeout=TIMEOUT)
                out[page] = r.status_code
                ok &= r.status_code == 200
            except Exception as e:
                out[page] = repr(e)
                ok = False
        return self._record("required_pages_accessible", ok, {"status_codes": out})

    def check_custom_404(self):
        try:
            r = requests.get(f"https://{self.domain}/this-page-does-not-exist-xyz", timeout=TIMEOUT)
            return self._record("custom_404_page", r.status_code == 404 and len(r.text) > 0,
                                {"status_code": r.status_code, "body_bytes": len(r.content)})
        except Exception as e:
            return self._record("custom_404_page", False, {"error": repr(e)})

    # 6: anonymous direct S3 access must be denied
    def check_s3_direct_denied(self):
        region = self.cfg.get("region", "us-east-1")
        url = f"https://{self.cfg['bucket']}.s3.{region}.amazonaws.com/index.html"
        try:
            r = requests.get(url, timeout=TIMEOUT)
            return self._record("s3_direct_access_denied", r.status_code in (401, 403),
                                {"url": url, "status_code": r.status_code})
        except Exception as e:
            return self._record("s3_direct_access_denied", False, {"url": url, "error": repr(e)})

    # 9: the ORIGIN really holds the site files (CloudFront's cache can hide an empty bucket).
    # A new or just-filled bucket can lag for a few seconds, so retry before failing.
    def check_bucket_contents(self):
        tries = int(self.cfg.get("bucket_retries", 4))
        delay = int(self.cfg.get("bucket_retry_delay", 5))
        evidence = {}
        for attempt in range(1, tries + 1):
            try:
                session = boto3.Session(profile_name=self.cfg.get("aws_profile"))
                s3 = session.client("s3", region_name=self.cfg.get("region", "us-east-1"))
                keys = set()
                for page in s3.get_paginator("list_objects_v2").paginate(Bucket=self.cfg["bucket"]):
                    keys |= {o["Key"] for o in page.get("Contents", [])}
                missing = [p for p in self.cfg["pages"] if p not in keys]
                evidence = {"object_count": len(keys), "missing_pages": missing,
                            "attempts": attempt}
                if not missing:
                    return self._record("bucket_has_site_files", True, evidence)
            except Exception as e:
                evidence = {"error": repr(e), "attempts": attempt}
            if attempt < tries:
                time.sleep(delay)
        return self._record("bucket_has_site_files", False, evidence)

    # 7: content is really served by CloudFront
    def check_cloudfront_headers(self):
        try:
            r = requests.get(f"https://{self.domain}/", timeout=TIMEOUT)
            h = {k.lower(): v for k, v in r.headers.items()}
            ok = r.status_code == 200 and "cloudfront" in h.get("via", "").lower() \
                and "x-cache" in h
            return self._record("cloudfront_serves_content", ok, {
                "status_code": r.status_code, "via": h.get("via"), "x-cache": h.get("x-cache"),
                "x-amz-cf-pop": h.get("x-amz-cf-pop"), "server": h.get("server")})
        except Exception as e:
            return self._record("cloudfront_serves_content", False, {"error": repr(e)})

    # DNS resolves. New records can take a minute or two to appear, so retry before failing.
    def check_dns(self):
        tries = int(self.cfg.get("dns_retries", 6))
        delay = int(self.cfg.get("dns_retry_delay", 10))
        last = None
        for attempt in range(1, tries + 1):
            try:
                a = [str(x) for x in dns.resolver.resolve(self.domain, "A", lifetime=TIMEOUT)]
                if a:
                    return self._record("dns_resolves", True, {"A": a, "attempts": attempt})
            except Exception as e:
                last = e
            if attempt < tries:
                time.sleep(delay)
        return self._record("dns_resolves", False, {"error": repr(last), "attempts": tries})

    def save_validation_report(self):
        """Write the recorded results (not LLM text) to reports/validation_report.json."""
        missing = [n for n in REQUIRED if n not in {r["check"] for r in self.results}]
        if missing:
            return {"error": f"Report NOT saved. These checks have not been run yet: {missing}. "
                             "Run every check, even if an earlier one failed, then save again."}
        os.makedirs(self.cfg.get("reports_dir", "reports"), exist_ok=True)
        path = self._report_path()
        report = {"domain": self.domain, "generated": datetime.now(timezone.utc).isoformat(),
                  "all_passed": bool(self.results) and all(r["passed"] for r in self.results),
                  "failed_checks": [r["check"] for r in self.results if not r["passed"]],
                  "results": self.results}
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        return {"saved_to": path, "all_passed": report["all_passed"],
                "failed_checks": report["failed_checks"], "checks_run": len(self.results)}