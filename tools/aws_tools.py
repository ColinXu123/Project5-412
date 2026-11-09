"""boto3 tools used by the Deployer (read/write) and Diagnostician (read-only).
Every method is idempotent: it looks up existing resources by domain/bucket name first."""
import hashlib
import json
import mimetypes
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

CLOUDFRONT_ALIAS_ZONE = "Z2FDTNDATAQYW2"           # fixed AWS zone id for CloudFront alias records


class AwsTools:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.domain = cfg["domain"]
        self.bucket = cfg["bucket"]
        self.region = cfg.get("region", "us-east-1")
        session = boto3.Session(profile_name=cfg.get("aws_profile"))
        self.s3 = session.client("s3", region_name=self.region)
        self.cf = session.client("cloudfront")
        self.r53 = session.client("route53")
        self.acm = session.client("acm", region_name="us-east-1")   # CloudFront requires us-east-1
        self.sts = session.client("sts")

    # ---------------------------------------------------------------- helpers
    def _zone(self):
        r = self.r53.list_hosted_zones_by_name(DNSName=self.domain, MaxItems="1")
        zones = r.get("HostedZones", [])
        if zones and zones[0]["Name"] == self.domain + ".":
            return zones[0]
        return None

    def _zone_id(self):
        z = self._zone()
        if not z:
            raise RuntimeError("No Route 53 hosted zone for domain; run ensure_hosted_zone first")
        return z["Id"].split("/")[-1]

    def _find_cert_arn(self):
        pages = self.acm.get_paginator("list_certificates").paginate(
            CertificateStatuses=["PENDING_VALIDATION", "ISSUED"])
        for p in pages:
            for c in p["CertificateSummaryList"]:
                if c["DomainName"] == self.domain:
                    return c["CertificateArn"]
        return None

    def _find_distribution(self):
        for page in self.cf.get_paginator("list_distributions").paginate():
            for d in (page.get("DistributionList") or {}).get("Items", []) or []:
                aliases = (d.get("Aliases") or {}).get("Items") or []
                if self.domain in aliases:
                    return d
        return None

    def _require_distribution(self):
        d = self._find_distribution()
        if not d:
            raise RuntimeError("No CloudFront distribution found for this domain")
        return d

    def _cache_behavior_settings(self):
        """Look up AWS's managed CachingOptimized policy by NAME instead of trusting a hard-coded
        ID. If the lookup is not permitted or finds nothing, fall back to legacy cache settings,
        which need no cache policy at all."""
        try:
            resp = self.cf.list_cache_policies(Type="managed")
            for item in resp["CachePolicyList"].get("Items", []):
                if item["CachePolicy"]["CachePolicyConfig"]["Name"] == "Managed-CachingOptimized":
                    return {"CachePolicyId": item["CachePolicy"]["Id"]}
        except Exception:
            pass
        return {"ForwardedValues": {"QueryString": False, "Cookies": {"Forward": "none"}},
                "MinTTL": 0, "DefaultTTL": 86400, "MaxTTL": 31536000}

    def _change(self, changes):
        r = self.r53.change_resource_record_sets(
            HostedZoneId=self._zone_id(),
            ChangeBatch={"Changes": changes})
        return r["ChangeInfo"]["Id"]

    # ------------------------------------------------------------------- DNS
    def ensure_hosted_zone(self):
        """Create the Route 53 hosted zone if missing. Returns the 4 nameservers that must be
        set manually at Namecheap (custom DNS)."""
        z = self._zone()
        created = False
        if not z:
            self.r53.create_hosted_zone(Name=self.domain, CallerReference=str(uuid.uuid4()))
            z = self._zone()
            created = True
        ns = self.r53.get_hosted_zone(Id=z["Id"])["DelegationSet"]["NameServers"]
        return {"hosted_zone_id": z["Id"].split("/")[-1], "created": created, "nameservers": ns,
                "manual_step": "Set these nameservers at Namecheap -> Domain -> Custom DNS"}

    def list_dns_records(self):
        recs = []
        for p in self.r53.get_paginator("list_resource_record_sets").paginate(
                HostedZoneId=self._zone_id()):
            for r in p["ResourceRecordSets"]:
                recs.append({"name": r["Name"], "type": r["Type"],
                             "alias": (r.get("AliasTarget") or {}).get("DNSName"),
                             "values": [v["Value"] for v in r.get("ResourceRecords", [])]})
        return {"records": recs}

    def upsert_alias_records(self):
        """Point the apex A and AAAA records at the CloudFront distribution."""
        d = self._require_distribution()
        changes = [{"Action": "UPSERT", "ResourceRecordSet": {
            "Name": self.domain, "Type": t,
            "AliasTarget": {"HostedZoneId": CLOUDFRONT_ALIAS_ZONE,
                            "DNSName": d["DomainName"], "EvaluateTargetHealth": False}}}
            for t in ("A", "AAAA")]
        change_id = self._change(changes)
        status, deadline = "PENDING", time.time() + 120
        while time.time() < deadline:      # Route 53 needs up to ~60s to publish a change
            status = self.r53.get_change(Id=change_id)["ChangeInfo"]["Status"]
            if status == "INSYNC":
                break
            time.sleep(5)
        return {"change_id": change_id, "target": d["DomainName"], "status": status}

    # ------------------------------------------------------------------- S3
    def _retry_s3(self, fn, tries=8, delay=3):
        """A brand-new (or re-created) bucket can answer NoSuchBucket for a few seconds."""
        last = None
        for i in range(tries):
            try:
                return fn()
            except ClientError as e:
                last = e
                if e.response["Error"]["Code"] not in ("NoSuchBucket", "OperationAborted",
                                                       "NoSuchKey", "404"):
                    raise
                time.sleep(delay)
        raise last

    def create_private_bucket(self):
        """Create the bucket (if missing) with ALL public access blocked."""
        exists = True
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except ClientError:
            exists = False
        if not exists:
            kwargs = {"Bucket": self.bucket}
            if self.region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
            self._retry_s3(lambda: self.s3.create_bucket(**kwargs))
            # wait until S3 reports the bucket as existing before configuring it
            self.s3.get_waiter("bucket_exists").wait(
                Bucket=self.bucket, WaiterConfig={"Delay": 3, "MaxAttempts": 20})
        self._retry_s3(lambda: self.s3.put_public_access_block(
            Bucket=self.bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True}))
        self._retry_s3(lambda: self.s3.put_bucket_ownership_controls(
            Bucket=self.bucket,
            OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}))
        return {"bucket": self.bucket, "created": not exists, "public_access_blocked": True}

    def upload_site(self):
        root = self.cfg.get("site_dir", "site")
        if not os.path.isdir(root):
            raise RuntimeError(f"Site directory '{root}' not found")
        uploaded = []
        for dirpath, _, files in os.walk(root):
            for fn in files:
                path = os.path.join(dirpath, fn)
                key = os.path.relpath(path, root).replace(os.sep, "/")
                ctype = mimetypes.guess_type(fn)[0] or "application/octet-stream"
                self.s3.upload_file(path, self.bucket, key, ExtraArgs={"ContentType": ctype})
                uploaded.append(key)
        return {"uploaded_count": len(uploaded), "keys": sorted(uploaded)}

    def list_bucket_objects(self):
        """List the keys in the site bucket (read-only)."""
        try:
            resp = self.s3.list_objects_v2(Bucket=self.bucket)
            keys = [o["Key"] for o in resp.get("Contents", [])]
            return {"count": len(keys), "keys": sorted(keys)}
        except ClientError as e:
            return {"error": e.response["Error"]["Code"]}

    def get_public_access_block(self):
        try:
            c = self.s3.get_public_access_block(Bucket=self.bucket)["PublicAccessBlockConfiguration"]
            return c
        except ClientError as e:
            return {"error": e.response["Error"]["Code"]}

    def get_bucket_policy(self):
        try:
            return {"policy": json.loads(self.s3.get_bucket_policy(Bucket=self.bucket)["Policy"])}
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "NoSuchBucketPolicy":
                return {"policy": None, "note": "Bucket has NO policy attached"}
            return {"error": code}

    def apply_oac_bucket_policy(self):
        """Allow ONLY this CloudFront distribution (via OAC) to read the bucket."""
        d = self._require_distribution()
        account = self.sts.get_caller_identity()["Account"]
        arn = f"arn:aws:cloudfront::{account}:distribution/{d['Id']}"
        cond = {"StringEquals": {"AWS:SourceArn": arn}}
        principal = {"Service": "cloudfront.amazonaws.com"}
        policy = {"Version": "2012-10-17", "Statement": [
            {"Sid": "AllowCloudFrontRead", "Effect": "Allow", "Principal": principal,
             "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{self.bucket}/*",
             "Condition": cond},
            {"Sid": "AllowCloudFrontList", "Effect": "Allow", "Principal": principal,
             "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{self.bucket}",
             "Condition": cond}]}
        self.s3.put_bucket_policy(Bucket=self.bucket, Policy=json.dumps(policy))
        return {"applied_for_distribution": d["Id"], "source_arn": arn}

    # ------------------------------------------------------------------ ACM
    def request_certificate(self):
        arn = self._find_cert_arn()
        if arn:
            return {"certificate_arn": arn, "created": False}
        token = hashlib.md5(self.domain.encode()).hexdigest()[:32]
        arn = self.acm.request_certificate(
            DomainName=self.domain, ValidationMethod="DNS", IdempotencyToken=token)["CertificateArn"]
        return {"certificate_arn": arn, "created": True}

    def get_certificate_status(self):
        arn = self._find_cert_arn()
        if not arn:
            return {"status": "NOT_FOUND"}
        c = self.acm.describe_certificate(CertificateArn=arn)["Certificate"]
        return {"certificate_arn": arn, "status": c["Status"], "domain": c["DomainName"],
                "validation": [{"domain": o["DomainName"], "status": o.get("ValidationStatus"),
                                "record": o.get("ResourceRecord")}
                               for o in c.get("DomainValidationOptions", [])]}

    def add_cert_validation_records(self):
        """Create the ACM DNS-validation CNAME in the Route 53 zone."""
        arn = self._find_cert_arn()
        if not arn:
            raise RuntimeError("No certificate requested yet")
        for _ in range(12):       # ACM can take a few seconds to publish the record
            c = self.acm.describe_certificate(CertificateArn=arn)["Certificate"]
            recs = [o["ResourceRecord"] for o in c.get("DomainValidationOptions", [])
                    if "ResourceRecord" in o]
            if recs:
                break
            time.sleep(5)
        else:
            raise RuntimeError("ACM did not provide validation records in time")
        changes = [{"Action": "UPSERT", "ResourceRecordSet": {
            "Name": r["Name"], "Type": r["Type"], "TTL": 300,
            "ResourceRecords": [{"Value": r["Value"]}]}} for r in recs]
        return {"change_id": self._change(changes), "records": [r["Name"] for r in recs]}

    def wait_for_certificate(self, timeout_seconds: int = 900):
        """Poll until the ACM certificate is ISSUED. Requires nameservers set at Namecheap."""
        deadline = time.time() + min(int(timeout_seconds), 1800)
        while time.time() < deadline:
            st = self.get_certificate_status()
            if st["status"] == "ISSUED":
                return st
            if st["status"] in ("FAILED", "VALIDATION_TIMED_OUT", "NOT_FOUND"):
                return st
            time.sleep(20)
        return {**self.get_certificate_status(), "note": "timed out waiting for ISSUED"}

    # ----------------------------------------------------------- CloudFront
    def create_oac(self):
        name = f"oac-{self.bucket}"[:64]
        for o in self.cf.list_origin_access_controls()["OriginAccessControlList"].get("Items", []):
            if o["Name"] == name:
                return {"oac_id": o["Id"], "created": False}
        r = self.cf.create_origin_access_control(OriginAccessControlConfig={
            "Name": name, "Description": "OAC for private S3 origin",
            "SigningProtocol": "sigv4", "SigningBehavior": "always",
            "OriginAccessControlOriginType": "s3"})
        return {"oac_id": r["OriginAccessControl"]["Id"], "created": True}

    def create_distribution(self):
        existing = self._find_distribution()
        if existing:
            return {"distribution_id": existing["Id"], "domain_name": existing["DomainName"],
                    "created": False}
        cert = self.get_certificate_status()
        if cert["status"] != "ISSUED":
            raise RuntimeError(f"Certificate not ISSUED yet (status={cert['status']})")
        oac_id = self.create_oac()["oac_id"]
        origin_id = "s3-origin"
        cfg = {
            "CallerReference": f"{self.domain}-{int(time.time())}",
            "Comment": f"Static site for {self.domain}",
            "Enabled": True,
            "Aliases": {"Quantity": 1, "Items": [self.domain]},
            "DefaultRootObject": "index.html",
            "HttpVersion": "http2",
            "IsIPV6Enabled": True,
            "PriceClass": "PriceClass_100",
            "Origins": {"Quantity": 1, "Items": [{
                "Id": origin_id,
                "DomainName": f"{self.bucket}.s3.{self.region}.amazonaws.com",
                "OriginAccessControlId": oac_id,
                "S3OriginConfig": {"OriginAccessIdentity": ""}}]},
            "DefaultCacheBehavior": {
                "TargetOriginId": origin_id,
                "ViewerProtocolPolicy": "redirect-to-https",
                "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                "Compress": True,
                **self._cache_behavior_settings()},
            "CustomErrorResponses": {"Quantity": 1, "Items": [{
                "ErrorCode": 404, "ResponsePagePath": "/" + self.cfg.get("error_page", "404.html"),
                "ResponseCode": "404", "ErrorCachingMinTTL": 10}]},
            "ViewerCertificate": {
                "ACMCertificateArn": cert["certificate_arn"],
                "SSLSupportMethod": "sni-only",
                "MinimumProtocolVersion": "TLSv1.2_2021"},
        }
        d = self.cf.create_distribution(DistributionConfig=cfg)["Distribution"]
        return {"distribution_id": d["Id"], "domain_name": d["DomainName"], "created": True,
                "status": d["Status"]}

    def wait_for_distribution(self, timeout_seconds: int = 1200):
        d = self._require_distribution()
        deadline = time.time() + min(int(timeout_seconds), 1800)
        while time.time() < deadline:
            st = self.cf.get_distribution(Id=d["Id"])["Distribution"]["Status"]
            if st == "Deployed":
                return {"distribution_id": d["Id"], "status": st}
            time.sleep(20)
        return {"distribution_id": d["Id"], "status": "InProgress", "note": "timed out"}

    def invalidate_cache(self):
        d = self._require_distribution()
        r = self.cf.create_invalidation(DistributionId=d["Id"], InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": str(time.time())})
        return {"invalidation_id": r["Invalidation"]["Id"], "status": r["Invalidation"]["Status"]}

    def get_distribution_summary(self):
        d = self._find_distribution()
        if not d:
            return {"error": "no distribution found for domain"}
        resp = self.cf.get_distribution_config(Id=d["Id"])
        c = resp["DistributionConfig"]
        return {
            "id": d["Id"], "domain_name": d["DomainName"], "enabled": c["Enabled"],
            "status": d["Status"], "aliases": c["Aliases"].get("Items", []),
            "default_root_object": c.get("DefaultRootObject"),
            "viewer_protocol_policy": c["DefaultCacheBehavior"]["ViewerProtocolPolicy"],
            "origins": [{"domain": o["DomainName"], "oac_id": o.get("OriginAccessControlId")}
                        for o in c["Origins"]["Items"]],
            "custom_errors": [{"code": e["ErrorCode"], "page": e.get("ResponsePagePath")}
                              for e in (c.get("CustomErrorResponses") or {}).get("Items", [])],
            "certificate_arn": c["ViewerCertificate"].get("ACMCertificateArn"),
        }

    def set_default_root_object(self, value: str = "index.html"):
        d = self._require_distribution()
        resp = self.cf.get_distribution_config(Id=d["Id"])
        c = resp["DistributionConfig"]
        c["DefaultRootObject"] = value
        self.cf.update_distribution(Id=d["Id"], IfMatch=resp["ETag"], DistributionConfig=c)
        return {"distribution_id": d["Id"], "default_root_object": value}