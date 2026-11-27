"""A fake `aws` CLI for the deploy/teardown script tests. Never touches the network.

Run as `python fake_aws.py <service> <command> [--flag value ...]` (tests put a shim named
`aws` on PATH). Every call is appended to $FAKE_AWS_LOG as a JSON argv list. Resources live in
the JSON "world" file $FAKE_AWS_WORLD, so describe/get calls reflect earlier creates/deletes
and missing resources fail the way the real CLI does (exit 254, "An error occurred (...)").

Knobs: $FAKE_AWS_FAIL_ON (comma-separated "service command" pairs that fail with
AccessDenied, or with an account-limit error for put-function-concurrency) and
$FAKE_AWS_ROLE_NOT_READY (create-function/create-schedule fail that many times with the
"role cannot be assumed" error first), $FAKE_AWS_EXTRA_TAGGED (comma-separated ARNs that
get-resources always lists).
"""

import json
import os
import sys
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
    "counters": {},
}


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
        if value.startswith("file://"):
            value = Path(value.removeprefix("file://")).read_text()
        flags[arg] = value
        i += 2
    return flags


def not_found(op: str, what: str) -> AwsError:
    return AwsError("ResourceNotFoundException", op, f"{what} not found")


def lambda_arn(name: str) -> str:
    return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}"


def handle(world: dict[str, Any], svc: str, cmd: str, f: dict[str, Any]) -> Any:  # noqa: C901
    key = f"{svc} {cmd}"
    counters = world["counters"]

    if key == "sts get-caller-identity":
        return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/dev", "UserId": "X"}

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
        if cmd == "describe-secret":
            return {
                "Name": sid,
                "ARN": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{sid}-AbCdEf",
            }
        if cmd == "delete-secret":
            world["secrets"].remove(sid)
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
