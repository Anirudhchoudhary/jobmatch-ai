"""A fake `aws` CLI for the deploy/teardown script tests. Never touches the network.

Run as `python fake_aws.py <service> <command> [--flag value ...]` (tests put a shim named
`aws` on PATH). Every call is appended to $FAKE_AWS_LOG as a JSON argv list. Resources live in
the JSON "world" file $FAKE_AWS_WORLD, so describe/get calls reflect earlier creates/deletes
and missing resources fail the way the real CLI does (exit 254, "An error occurred (...)").

Knobs: $FAKE_AWS_FAIL_ON (comma-separated "service command" pairs that fail with
AccessDenied, or with an account-limit error for put-function-concurrency) and
$FAKE_AWS_ROLE_NOT_READY (create-function/create-schedule fail that many times with the
"role cannot be assumed" error first), $FAKE_AWS_EXTRA_TAGGED (comma-separated ARNs that
get-resources always lists), $FAKE_AWS_ACM_PENDING (certificates never validate).

Global services: CloudFront and Route 53 (any --region; the scripts use us-east-1) and ACM in
us-east-1 only (CloudFront certificates). Route 53 hosted zones are only ever read or have
record sets changed: there is deliberately no create/delete-hosted-zone here, so a script that
calls either fails loudly.
"""

import fnmatch
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

ACCOUNT = "123456789012"
REGION = "us-west-2"
BOOLEAN_FLAGS = {
    "--force",
    "--invoked-via-function-url",
    "--no-invoked-via-function-url",
    "--force-delete-without-recovery",
    "--no-cli-pager",
    "--only-show-errors",
}
EMPTY_WORLD: dict[str, Any] = {
    "ecr": [],
    "buckets": {},
    "log_groups": [],
    "roles": {},
    "functions": {},
    "schedules": [],
    "budgets": [],
    "secrets": [],
    "secret_values": {},
    "secret_versions": {},
    "bucket_cors": {},
    "bucket_policies": {},
    "certificates": {},
    "cf_oacs": {},
    "cf_functions": {},
    "cf_distributions": {},
    "cf_invalidations": [],
    "hosted_zones": {},
    "counters": {},
}
EDGE_REGION = "us-east-1"
CLOUDFRONT_ZONE_ID = "Z2FDTNDATAQYW2"


class AwsError(Exception):
    def __init__(self, code: str, op: str, message: str, exit_code: int = 254) -> None:
        super().__init__(code)
        self.code, self.op, self.message, self.exit_code = code, op, message, exit_code


def parse_flags(args: list[str]) -> dict[str, Any]:
    flags: dict[str, Any] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("--"):
            raise SystemExit(f"fake aws: unexpected positional argument {arg!r}")
        if arg in BOOLEAN_FLAGS:
            flags[arg] = True
            i += 1
            continue
        value = args[i + 1]
        for prefix in ("file://", "fileb://"):
            if value.startswith(prefix):
                value = Path(value.removeprefix(prefix)).read_text()
        flags[arg] = value
        i += 2
    return flags


def not_found(op: str, what: str) -> AwsError:
    return AwsError("ResourceNotFoundException", op, f"{what} not found")


def lambda_arn(name: str) -> str:
    return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}"


def new_etag(obj: dict[str, Any]) -> str:
    obj["etag"] = "E" + uuid.uuid4().hex[:12].upper()
    return str(obj["etag"])


def check_etag(obj: dict[str, Any], f: dict[str, Any], op: str) -> None:
    if f.get("--if-match") != obj["etag"]:
        raise AwsError("PreconditionFailed", op, "The If-Match version is missing or not valid.")


def record_key(name: str) -> tuple[list[str], str]:
    return list(reversed(name.rstrip(".").split("."))), name


def handle_route53(world: dict[str, Any], cmd: str, f: dict[str, Any]) -> Any:
    zones = world["hosted_zones"]
    if cmd == "list-hosted-zones-by-name":
        start = f.get("--dns-name", "")
        listed = sorted(
            (z for z in zones.items() if z[1]["Name"] >= start), key=lambda z: (z[1]["Name"], z[0])
        )
        return {
            "HostedZones": [
                {
                    "Id": f"/hostedzone/{zid}",
                    "Name": z["Name"],
                    "Config": {"PrivateZone": z["Private"]},
                }
                for zid, z in listed
            ]
        }
    zid = f["--hosted-zone-id"].removeprefix("/hostedzone/")
    if zid not in zones:
        raise AwsError("NoSuchHostedZone", cmd, f"No hosted zone found with ID: {zid}")
    records: list[dict[str, Any]] = zones[zid]["records"]
    if cmd == "list-resource-record-sets":
        ordered = sorted(records, key=lambda r: (record_key(r["Name"]), r["Type"]))
        start = f.get("--start-record-name")
        if start:
            start_key = (record_key(start), f.get("--start-record-type", ""))
            ordered = [r for r in ordered if (record_key(r["Name"]), r["Type"]) >= start_key]
        if "--max-items" in f:
            ordered = ordered[: int(f["--max-items"])]
        return {"ResourceRecordSets": ordered}
    if cmd == "change-resource-record-sets":
        batch = json.loads(f["--change-batch"])
        work = [dict(r) for r in records]
        for change in batch["Changes"]:
            rrs = change["ResourceRecordSet"]
            if not rrs["Name"].endswith("."):
                raise AwsError("InvalidInput", cmd, "names must be fully qualified")
            same = [r for r in work if r["Name"] == rrs["Name"] and r["Type"] == rrs["Type"]]
            action = change["Action"]
            if action == "CREATE" and same:
                raise AwsError("InvalidChangeBatch", cmd, "record set already exists")
            if action == "DELETE":
                if not same or same[0] != rrs:
                    raise AwsError(
                        "InvalidChangeBatch",
                        cmd,
                        "Tried to delete resource record set but it was not found, or the "
                        "values provided do not match the current values",
                    )
                work.remove(same[0])
                continue
            if any(
                r["Name"] == rrs["Name"] and "CNAME" in (r["Type"], rrs["Type"])
                for r in work
                if r not in same
            ):
                raise AwsError("InvalidChangeBatch", cmd, "CNAME conflicts with another record")
            for r in same:
                work.remove(r)
            work.append(rrs)
        zones[zid]["records"] = work
        change_id = "C" + uuid.uuid4().hex[:12].upper()
        return {"ChangeInfo": {"Id": f"/change/{change_id}", "Status": "PENDING"}}
    raise SystemExit(f"fake aws: unhandled command 'route53 {cmd}'")


def validation_record(arn: str) -> dict[str, str]:
    token = arn.rsplit("/", 1)[-1].replace("-", "")
    return {
        "Name": f"_{token}.jobmatch.ntrye.com.",
        "Type": "CNAME",
        "Value": f"_{token[::-1]}.xyz.acm-validations.aws.",
    }


def certificate_status(world: dict[str, Any], arn: str) -> str:
    if os.environ.get("FAKE_AWS_ACM_PENDING"):
        return "PENDING_VALIDATION"
    want = validation_record(arn)
    for zone in world["hosted_zones"].values():
        if zone["Private"]:
            continue
        for r in zone["records"]:
            values = [v["Value"] for v in r.get("ResourceRecords", [])]
            if r["Name"] == want["Name"] and r["Type"] == "CNAME" and values == [want["Value"]]:
                return "ISSUED"
    return "PENDING_VALIDATION"


def handle_acm(world: dict[str, Any], cmd: str, f: dict[str, Any]) -> Any:
    if f.get("--region") != EDGE_REGION:
        raise AwsError("ValidationException", cmd, "CloudFront certificates must be in us-east-1")
    certs = world["certificates"]
    if cmd == "request-certificate":
        if f.get("--validation-method") != "DNS":
            raise AwsError("ValidationException", cmd, "expected DNS validation")
        arn = f"arn:aws:acm:{EDGE_REGION}:{ACCOUNT}:certificate/{uuid.uuid4()}"
        certs[arn] = {"domain": f["--domain-name"], "tags": f.get("--tags", ""), "described": 0}
        return {"CertificateArn": arn}
    if cmd == "list-certificates":
        return {
            "CertificateSummaryList": [
                {"CertificateArn": a, "DomainName": c["domain"]} for a, c in certs.items()
            ]
        }
    arn = f["--certificate-arn"]
    if arn not in certs:
        raise AwsError("ResourceNotFoundException", cmd, f"Could not find certificate {arn}.")
    cert = certs[arn]
    if cmd == "describe-certificate":
        cert["described"] += 1
        option: dict[str, Any] = {"DomainName": cert["domain"], "ValidationMethod": "DNS"}
        if cert["described"] > 1:  # like ACM, the record appears a moment after the request
            option["ResourceRecord"] = validation_record(arn)
        return {
            "Certificate": {
                "CertificateArn": arn,
                "DomainName": cert["domain"],
                "Status": certificate_status(world, arn),
                "DomainValidationOptions": [option],
            }
        }
    if cmd == "list-tags-for-certificate":
        tags = []
        for part in [p for p in cert["tags"].split(" ") if p]:
            kv = dict(item.split("=", 1) for item in part.split(","))
            tags.append({"Key": kv["Key"], "Value": kv["Value"]})
        return {"Tags": tags}
    if cmd == "delete-certificate":
        for dist in world["cf_distributions"].values():
            if dist["config"]["ViewerCertificate"].get("ACMCertificateArn") == arn:
                raise AwsError("ResourceInUseException", cmd, f"Certificate {arn} in use.")
        del certs[arn]
        return None
    raise SystemExit(f"fake aws: unhandled command 'acm {cmd}'")


def validate_distribution(world: dict[str, Any], cfg: dict[str, Any], op: str) -> None:
    arn = cfg["ViewerCertificate"].get("ACMCertificateArn")
    if arn not in world["certificates"] or certificate_status(world, arn) != "ISSUED":
        raise AwsError("InvalidViewerCertificate", op, "The certificate is not valid.")
    for origin in cfg["Origins"]["Items"]:
        oac = origin.get("OriginAccessControlId")
        if oac and oac not in world["cf_oacs"]:
            raise AwsError("NoSuchOriginAccessControl", op, "The OAC does not exist.")
    for assoc in cfg["DefaultCacheBehavior"].get("FunctionAssociations", {}).get("Items", []):
        name = assoc["FunctionARN"].rsplit("/", 1)[-1]
        fn = world["cf_functions"].get(name)
        if not fn or not fn["published"]:
            raise AwsError("NoSuchFunctionExists", op, "The function is not published.")


def distribution_view(dist_id: str, dist: dict[str, Any]) -> dict[str, Any]:
    return {
        "Id": dist_id,
        "ARN": f"arn:aws:cloudfront::{ACCOUNT}:distribution/{dist_id}",
        "DomainName": f"{dist_id.lower()}.cloudfront.net",
        "Status": dist["status"],
        "DistributionConfig": dist["config"],
    }


def handle_cloudfront(world: dict[str, Any], cmd: str, f: dict[str, Any]) -> Any:  # noqa: C901
    oacs, fns, dists = world["cf_oacs"], world["cf_functions"], world["cf_distributions"]
    # ---- origin access controls
    if cmd == "list-origin-access-controls":
        items = [{"Id": i, "Name": o["config"]["Name"]} for i, o in oacs.items()]
        return {"OriginAccessControlList": {"Quantity": len(items), "Items": items or None}}
    if cmd == "create-origin-access-control":
        cfg = json.loads(f["--origin-access-control-config"])
        if any(o["config"]["Name"] == cfg["Name"] for o in oacs.values()):
            raise AwsError("OriginAccessControlAlreadyExists", cmd, "exists")
        oac_id = "E" + uuid.uuid4().hex[:13].upper()
        oacs[oac_id] = {"config": cfg}
        etag = new_etag(oacs[oac_id])
        return {
            "OriginAccessControl": {"Id": oac_id, "OriginAccessControlConfig": cfg},
            "ETag": etag,
        }
    if cmd in ("get-origin-access-control", "delete-origin-access-control"):
        oac_id = f["--id"]
        if oac_id not in oacs:
            raise AwsError(
                "NoSuchOriginAccessControl", cmd, "The origin access control does not exist."
            )
        if cmd == "get-origin-access-control":
            o = oacs[oac_id]
            return {
                "OriginAccessControl": {"Id": oac_id, "OriginAccessControlConfig": o["config"]},
                "ETag": o["etag"],
            }
        check_etag(oacs[oac_id], f, cmd)
        for dist in dists.values():
            if any(
                o.get("OriginAccessControlId") == oac_id for o in dist["config"]["Origins"]["Items"]
            ):
                raise AwsError("OriginAccessControlInUse", cmd, "in use by a distribution")
        del oacs[oac_id]
        return None
    # ---- CloudFront Functions
    if cmd in (
        "create-function",
        "describe-function",
        "update-function",
        "publish-function",
        "delete-function",
    ):
        name = f["--name"]
        arn = f"arn:aws:cloudfront::{ACCOUNT}:function/{name}"
        if cmd == "create-function":
            if name in fns:
                raise AwsError("FunctionAlreadyExists", cmd, "exists")
            json.loads(f["--function-config"])
            fns[name] = {"code": f["--function-code"], "published": False}
            etag = new_etag(fns[name])
            return {
                "FunctionSummary": {"Name": name, "FunctionMetadata": {"FunctionARN": arn}},
                "ETag": etag,
            }
        if name not in fns:
            raise AwsError("NoSuchFunctionExists", cmd, "The function does not exist.")
        fn = fns[name]
        if cmd == "describe-function":
            return {
                "FunctionSummary": {"Name": name, "FunctionMetadata": {"FunctionARN": arn}},
                "ETag": fn["etag"],
            }
        check_etag(fn, f, cmd)
        if cmd == "update-function":
            fn["code"] = f["--function-code"]
            json.loads(f["--function-config"])
        elif cmd == "publish-function":
            fn["published"] = True
        else:  # delete-function
            for dist in dists.values():
                items = dist["config"]["DefaultCacheBehavior"].get("FunctionAssociations", {})
                if any(a["FunctionARN"] == arn for a in items.get("Items", [])):
                    raise AwsError("FunctionInUse", cmd, "associated with a distribution")
            del fns[name]
            return None
        etag = new_etag(fn)
        return {
            "FunctionSummary": {"Name": name, "FunctionMetadata": {"FunctionARN": arn}},
            "ETag": etag,
        }
    # ---- distributions
    if cmd == "list-distributions":
        items = [
            {
                **{k: v for k, v in distribution_view(i, d).items() if k != "DistributionConfig"},
                "Comment": d["config"]["Comment"],
                "Aliases": d["config"]["Aliases"],
            }
            for i, d in dists.items()
        ]
        return {"DistributionList": {"Quantity": len(items), "Items": items or None}}
    if cmd == "create-distribution-with-tags":
        payload = json.loads(f["--distribution-config-with-tags"])
        cfg = payload["DistributionConfig"]
        validate_distribution(world, cfg, "CreateDistributionWithTags")
        for d in dists.values():
            if set(d["config"]["Aliases"].get("Items", [])) & set(cfg["Aliases"]["Items"]):
                raise AwsError("CNAMEAlreadyExists", cmd, "alias in use")
        dist_id = "E" + uuid.uuid4().hex[:13].upper()
        # Like CloudFront, fill in the defaults it adds on create; an update must send them back.
        cfg.setdefault(
            "Logging", {"Enabled": False, "IncludeCookies": False, "Bucket": "", "Prefix": ""}
        )
        for behavior in [
            cfg["DefaultCacheBehavior"],
            *cfg.get("CacheBehaviors", {}).get("Items", []),
        ]:
            behavior.setdefault("SmoothStreaming", False)
        for origin in cfg["Origins"]["Items"]:
            origin.setdefault("OriginShield", {"Enabled": False})
        dists[dist_id] = {"config": cfg, "status": "InProgress", "tags": payload["Tags"]}
        etag = new_etag(dists[dist_id])
        return {"Distribution": distribution_view(dist_id, dists[dist_id]), "ETag": etag}
    if cmd == "create-invalidation":
        dist_id = f["--distribution-id"]
        if dist_id not in dists:
            raise AwsError("NoSuchDistribution", cmd, "The specified distribution does not exist.")
        inv_id = "I" + uuid.uuid4().hex[:12].upper()
        world["cf_invalidations"].append({"distribution": dist_id, "paths": f.get("--paths")})
        return {"Invalidation": {"Id": inv_id, "Status": "InProgress"}}
    dist_id = f["--id"]
    if dist_id not in dists:
        raise AwsError("NoSuchDistribution", cmd, "The specified distribution does not exist.")
    dist = dists[dist_id]
    if cmd == "wait":  # distribution-deployed
        dist["status"] = "Deployed"
        return None
    if cmd == "get-distribution":
        return {"Distribution": distribution_view(dist_id, dist), "ETag": dist["etag"]}
    if cmd == "get-distribution-config":
        return {"DistributionConfig": dist["config"], "ETag": dist["etag"]}
    check_etag(dist, f, cmd)
    if cmd == "update-distribution":
        cfg = json.loads(f["--distribution-config"])
        behaviors = [cfg["DefaultCacheBehavior"], *cfg.get("CacheBehaviors", {}).get("Items", [])]
        if any("SmoothStreaming" not in b for b in behaviors):
            raise AwsError("InvalidArgument", cmd, "The parameter SmoothStreaming flag is missing.")
        if any("OriginShield" not in o for o in cfg["Origins"]["Items"]):
            raise AwsError("InvalidArgument", cmd, "OriginShield is missing.")
        if "Logging" not in cfg:
            raise AwsError("IllegalUpdate", cmd, "Logging is missing for the resource")
        if cfg["CallerReference"] != dist["config"]["CallerReference"]:
            raise AwsError("IllegalUpdate", cmd, "CallerReference cannot change")
        if cfg["Enabled"]:
            validate_distribution(world, cfg, "UpdateDistribution")
        dist["config"], dist["status"] = cfg, "InProgress"
        etag = new_etag(dist)
        return {"Distribution": distribution_view(dist_id, dist), "ETag": etag}
    if cmd == "delete-distribution":
        if dist["config"]["Enabled"] or dist["status"] != "Deployed":
            raise AwsError(
                "DistributionNotDisabled", cmd, "The distribution must be disabled and deployed."
            )
        del dists[dist_id]
        return None
    raise SystemExit(f"fake aws: unhandled command 'cloudfront {cmd}'")


def sync_filters(args: list[str]) -> list[tuple[str, str]]:
    return [(a, args[i + 1]) for i, a in enumerate(args) if a in ("--exclude", "--include")]


def sync_included(key: str, filters: list[tuple[str, str]]) -> bool:
    """`aws s3 sync` filters: everything is included, then the last matching filter wins."""
    included = True
    for kind, pattern in filters:
        if fnmatch.fnmatch(key, pattern):
            included = kind == "--include"
    return included


def handle_s3_sync(world: dict[str, Any], args: list[str], delete: bool) -> None:
    """`aws s3 sync <dir> s3://bucket[/prefix] [--delete] [--exclude p | --include p]...
    [--cache-control v] [--content-type v]`. Filters match paths relative to the source and
    destination; excluded objects are neither uploaded nor deleted."""
    src, dest, *rest = args
    filters = sync_filters(rest)
    bucket, _, prefix = dest.removeprefix("s3://").partition("/")
    if bucket not in world["buckets"]:
        raise AwsError("NoSuchBucket", "ListObjectsV2", "The specified bucket does not exist")
    prefix = prefix.rstrip("/") + "/" if prefix else ""
    relative = sorted(str(p.relative_to(src)) for p in Path(src).rglob("*") if p.is_file())
    local = [prefix + k for k in relative if sync_included(k, filters)]
    keys: list[str] = world["buckets"][bucket]
    if delete:
        keys[:] = [
            k
            for k in keys
            if not k.startswith(prefix)
            or k in local
            or not sync_included(k.removeprefix(prefix), filters)
        ]
    keys.extend(k for k in local if k not in keys)


def handle(world: dict[str, Any], svc: str, cmd: str, f: dict[str, Any]) -> Any:  # noqa: C901
    key = f"{svc} {cmd}"
    counters = world["counters"]

    if key == "sts get-caller-identity":
        return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/dev", "UserId": "X"}
    if svc == "route53":
        return handle_route53(world, cmd, f)
    if svc == "acm":
        return handle_acm(world, cmd, f)
    if svc == "cloudfront":
        return handle_cloudfront(world, cmd, f)

    # ---- ECR
    if svc == "ecr":
        name = f.get("--repository-name") or f.get("--repository-names")
        if cmd == "get-login-password":
            return "FAKE-ECR-PASSWORD-PLANTED"
        if cmd in ("describe-repositories", "put-lifecycle-policy", "delete-repository"):
            if name not in world["ecr"]:
                raise AwsError("RepositoryNotFoundException", cmd, f"repository {name} not found")
            if cmd == "delete-repository":
                if not f.get("--force"):
                    raise AwsError("RepositoryNotEmptyException", cmd, "has images")
                world["ecr"].remove(name)
            uri = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{name}"
            return {"repositories": [{"repositoryName": name, "repositoryUri": uri}]}
        if cmd == "create-repository":
            world["ecr"].append(name)
            uri = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{name}"
            return {"repository": {"repositoryName": name, "repositoryUri": uri}}

    # ---- S3
    if svc == "s3api":
        bucket = f["--bucket"]
        buckets = world["buckets"]
        if cmd == "create-bucket":
            if bucket in buckets:
                raise AwsError("BucketAlreadyOwnedByYou", cmd, "exists")
            buckets[bucket] = ["uploads/u1/stale.pdf"]  # an object the lifecycle missed
            return {"Location": f"http://{bucket}.s3.amazonaws.com/"}
        if bucket not in buckets:
            if cmd == "head-bucket":
                raise AwsError("404", "HeadBucket", "Not Found")
            raise AwsError("NoSuchBucket", cmd, "The specified bucket does not exist")
        if cmd == "list-object-versions":
            objs = buckets[bucket]
            return {
                "Versions": [{"Key": k, "VersionId": "null"} for k in objs] or None,
                "DeleteMarkers": None,
            }
        if cmd == "delete-objects":
            payload = json.loads(f["--delete"])
            gone = {o["Key"] for o in payload["Objects"]}
            buckets[bucket] = [k for k in buckets[bucket] if k not in gone]
            return {"Deleted": [{"Key": k} for k in sorted(gone)]}
        if cmd == "delete-bucket":
            if buckets[bucket]:
                raise AwsError("BucketNotEmpty", cmd, "The bucket you tried to delete is not empty")
            del buckets[bucket]
            world["bucket_cors"].pop(bucket, None)
            world["bucket_policies"].pop(bucket, None)
            return None
        if cmd == "put-bucket-cors":
            world["bucket_cors"][bucket] = json.loads(f["--cors-configuration"])
            return None
        if cmd == "delete-bucket-cors":  # S3 answers 204 even when there is none
            world["bucket_cors"].pop(bucket, None)
            return None
        if cmd == "put-bucket-policy":
            world["bucket_policies"][bucket] = json.loads(f["--policy"])
            return None
        if cmd in (
            "head-bucket",
            "put-public-access-block",
            "put-bucket-encryption",
            "put-bucket-lifecycle-configuration",
            "put-bucket-tagging",
        ):
            return None

    # ---- CloudWatch Logs
    if svc == "logs":
        if cmd == "describe-log-groups":
            prefix = f["--log-group-name-prefix"]
            groups = [g for g in world["log_groups"] if g.startswith(prefix)]
            return {"logGroups": [{"logGroupName": g} for g in groups]}
        name = f["--log-group-name"]
        if cmd == "create-log-group":
            if name in world["log_groups"]:
                raise AwsError("ResourceAlreadyExistsException", cmd, "exists")
            world["log_groups"].append(name)
            return None
        if name not in world["log_groups"]:
            raise not_found(cmd, "The specified log group does not exist")
        if cmd == "delete-log-group":
            world["log_groups"].remove(name)
        return None

    # ---- IAM
    if svc == "iam":
        roles = world["roles"]
        name = f["--role-name"]
        if cmd == "create-role":
            roles[name] = {"policies": [], "attached": []}
            return {"Role": {"RoleName": name, "Arn": f"arn:aws:iam::{ACCOUNT}:role/{name}"}}
        if name not in roles:
            raise AwsError("NoSuchEntity", cmd, f"The role with name {name} cannot be found.", 254)
        role = roles[name]
        if cmd == "get-role":
            return {"Role": {"RoleName": name, "Arn": f"arn:aws:iam::{ACCOUNT}:role/{name}"}}
        if cmd == "put-role-policy":
            json.loads(f["--policy-document"])  # must be valid JSON
            if f["--policy-name"] not in role["policies"]:
                role["policies"].append(f["--policy-name"])
            return None
        if cmd == "list-role-policies":
            return {"PolicyNames": list(role["policies"])}
        if cmd == "list-attached-role-policies":
            return {"AttachedPolicies": []}
        if cmd == "delete-role-policy":
            if f["--policy-name"] not in role["policies"]:
                raise AwsError("NoSuchEntity", cmd, "policy not found")
            role["policies"].remove(f["--policy-name"])
            return None
        if cmd == "delete-role":
            if role["policies"]:
                raise AwsError("DeleteConflict", cmd, "Cannot delete entity, must delete policies")
            del roles[name]
            return None

    # ---- Lambda
    if svc == "lambda":
        fns = world["functions"]
        if cmd == "wait":
            return None
        name = f["--function-name"]
        if cmd == "create-function":
            roles_ready = counters.get("create-function", 0) >= int(
                os.environ.get("FAKE_AWS_ROLE_NOT_READY", "0")
            )
            counters["create-function"] = counters.get("create-function", 0) + 1
            if not roles_ready:
                raise AwsError(
                    "InvalidParameterValueException",
                    "CreateFunction",
                    "The role defined for the function cannot be assumed by Lambda.",
                )
            role_name = f["--role"].rsplit("/", 1)[-1]
            if role_name not in world["roles"]:
                raise AwsError("InvalidParameterValueException", cmd, "role does not exist")
            json.loads(f["--environment"])
            fns[name] = {"url": False, "permissions": [], "image": f["--code"]}
            return {"FunctionName": name, "FunctionArn": lambda_arn(name)}
        if name not in fns:
            raise not_found(cmd, f"Function not found: {lambda_arn(name)}")
        fn = fns[name]
        if cmd == "get-function":
            return {"Configuration": {"FunctionName": name, "FunctionArn": lambda_arn(name)}}
        if cmd == "update-function-code":
            fn["image"] = f["--image-uri"]
            return {"FunctionArn": lambda_arn(name)}
        if cmd in ("update-function-configuration", "put-function-concurrency"):
            return {"FunctionArn": lambda_arn(name)}
        if cmd == "create-function-url-config":
            fn["url"] = True
            return {"FunctionUrl": "https://abc123.lambda-url.us-west-2.on.aws/"}
        if cmd == "get-function-url-config":
            if not fn["url"]:
                raise not_found(cmd, "The resource you requested does not exist.")
            return {"FunctionUrl": "https://abc123.lambda-url.us-west-2.on.aws/"}
        if cmd == "update-function-url-config":
            if not fn["url"]:
                raise not_found(cmd, "The resource you requested does not exist.")
            return {"FunctionUrl": "https://abc123.lambda-url.us-west-2.on.aws/"}
        if cmd == "delete-function-url-config":
            if not fn["url"]:
                raise not_found(cmd, "The resource you requested does not exist.")
            fn["url"] = False
            return None
        if cmd == "add-permission":
            sid = f["--statement-id"]
            if sid in fn["permissions"]:
                raise AwsError(
                    "ResourceConflictException", cmd, "The statement id provided already exists."
                )
            fn["permissions"].append(sid)
            return {"Statement": "{}"}
        if cmd == "remove-permission":
            sid = f["--statement-id"]
            if sid not in fn["permissions"]:
                raise not_found(cmd, "The resource you requested does not exist.")
            fn["permissions"].remove(sid)
            return None
        if cmd == "delete-function":
            del fns[name]
            return None

    # ---- EventBridge Scheduler
    if svc == "scheduler":
        name = f["--name"]
        if cmd == "create-schedule":
            ready = counters.get("create-schedule", 0) >= int(
                os.environ.get("FAKE_AWS_ROLE_NOT_READY", "0")
            )
            counters["create-schedule"] = counters.get("create-schedule", 0) + 1
            if not ready:
                raise AwsError(
                    "ValidationException",
                    "CreateSchedule",
                    "The execution role you provide must allow AWS EventBridge Scheduler to "
                    "assume the role.",
                )
            json.loads(f["--target"])
            world["schedules"].append(name)
            return {"ScheduleArn": f"arn:aws:scheduler:{REGION}:{ACCOUNT}:schedule/default/{name}"}
        if name not in world["schedules"]:
            raise not_found(cmd, f"Schedule {name} does not exist.")
        if cmd == "delete-schedule":
            world["schedules"].remove(name)
        return {"Name": name}

    # ---- Budgets (global; must be called in us-east-1)
    if svc == "budgets":
        if f.get("--region") != "us-east-1":
            raise AwsError("InvalidParameterException", cmd, "budgets endpoint is us-east-1")
        if f["--account-id"] != ACCOUNT:
            raise AwsError("AccessDeniedException", cmd, "wrong account")
        if cmd == "create-budget":
            budget = json.loads(f["--budget"])
            json.loads(f["--notifications-with-subscribers"])
            world["budgets"].append(budget["BudgetName"])
            return None
        name = f["--budget-name"]
        if name not in world["budgets"]:
            raise AwsError("NotFoundException", cmd, f"Unable to get budget: {name}")
        if cmd == "delete-budget":
            world["budgets"].remove(name)
        return {"Budget": {"BudgetName": name}}

    # ---- Secrets Manager
    if svc == "secretsmanager":
        sid = f["--secret-id"]
        if sid not in world["secrets"]:
            raise not_found(cmd, "Secrets Manager can't find the specified secret.")
        # A secret seeded without a version gets a fixed first one.
        version = world["secret_versions"].setdefault(sid, "00000000-0000-4000-8000-000000000001")
        if cmd == "describe-secret":
            return {
                "Name": sid,
                "ARN": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{sid}-AbCdEf",
                "VersionIdsToStages": {version: ["AWSCURRENT"]},
            }
        if cmd == "get-secret-value":
            return {
                "Name": sid,
                "VersionId": version,
                "SecretString": world["secret_values"].get(sid, "{}"),
            }
        if cmd == "put-secret-value":
            value = f["--secret-string"]
            if not isinstance(json.loads(value), dict):
                raise AwsError("InvalidParameterException", cmd, "not a JSON object")
            world["secret_values"][sid] = value
            world["secret_versions"][sid] = str(uuid.uuid4())
            return {"Name": sid, "VersionId": world["secret_versions"][sid]}
        if cmd == "delete-secret":
            world["secrets"].remove(sid)
            world["secret_values"].pop(sid, None)
            world["secret_versions"].pop(sid, None)
            return {"Name": sid}

    # ---- Resource Groups Tagging API (regional; IAM, budgets and schedules are not listed)
    if key == "resourcegroupstaggingapi get-resources":
        arns = [f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{n}" for n in world["ecr"]]
        arns += [f"arn:aws:s3:::{b}" for b in world["buckets"]]
        arns += [f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{g}" for g in world["log_groups"]]
        arns += [lambda_arn(n) for n in world["functions"]]
        arns += [
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{s}-AbCdEf" for s in world["secrets"]
        ]
        # Simulates the tagging index lagging behind deletes (or a resource deploy doesn't know).
        arns += [a for a in os.environ.get("FAKE_AWS_EXTRA_TAGGED", "").split(",") if a]
        return {"ResourceTagMappingList": [{"ResourceARN": a} for a in arns]}

    raise SystemExit(f"fake aws: unhandled command {key!r}")


def render(result: Any, query: str | None, output: str) -> str:
    if query is not None:
        import jmespath  # only when needed: keeps each fake call fast

        result = jmespath.search(query, result)
    if output == "text":
        if result is None:
            return "None"
        if isinstance(result, list):
            return "\t".join("None" if r is None else str(r) for r in result)
        return str(result)
    return json.dumps(result, indent=2)


def main(argv: list[str]) -> int:
    log = os.environ["FAKE_AWS_LOG"]
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")
    svc, cmd, *rest = argv
    if cmd == "wait":
        rest = rest[1:]  # the waiter name, e.g. function-active-v2
    positional: list[str] = []
    if (svc, cmd) == ("s3", "sync"):
        positional, rest = rest[:2], rest[2:]
    sync_delete = "--delete" in rest  # a boolean for `s3 sync`, a JSON file for s3api
    if (svc, cmd) == ("s3", "sync"):
        rest = [a for a in rest if a != "--delete"]
    flags = parse_flags(rest)
    if flags.get("--region") is None:
        print("fake aws: every call must pass --region", file=sys.stderr)
        return 252
    world_path = Path(os.environ["FAKE_AWS_WORLD"])
    world = {**EMPTY_WORLD, **json.loads(world_path.read_text())}
    fail_on = {p.strip() for p in os.environ.get("FAKE_AWS_FAIL_ON", "").split(",") if p.strip()}
    try:
        if f"{svc} {cmd}" in fail_on:
            if cmd == "put-function-concurrency":
                raise AwsError(
                    "InvalidParameterValueException",
                    "PutFunctionConcurrency",
                    "Specified ReservedConcurrentExecutions for function decreases account's "
                    "UnreservedConcurrentExecution below its minimum value of [10].",
                )
            raise AwsError("AccessDeniedException", cmd, "injected failure")
        if (svc, cmd) == ("s3", "sync"):
            handle_s3_sync(world, positional + rest, sync_delete)
            result = None
        else:
            result = handle(world, svc, cmd, flags)
    except AwsError as err:
        print(
            f"\nAn error occurred ({err.code}) when calling the {err.op} operation: {err.message}",
            file=sys.stderr,
        )
        return err.exit_code
    finally:
        world_path.write_text(json.dumps(world))
    if result is not None:
        if cmd == "get-login-password":
            print(result)
        else:
            print(render(result, flags.get("--query"), flags.get("--output", "json")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
