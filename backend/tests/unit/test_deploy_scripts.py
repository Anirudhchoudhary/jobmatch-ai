"""infra/aws deploy/teardown scripts, driven against a fake `aws` and `docker` on PATH."""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INFRA = REPO_ROOT / "infra" / "aws"
FAKE_AWS = Path(__file__).with_name("fake_aws.py")
ACCOUNT = "123456789012"
BUCKET = f"jobmatch-uploads-{ACCOUNT}"
SITE_BUCKET = f"jobmatch-site-{ACCOUNT}"
BUDGET_EMAIL = "budget-alerts-PLANTED@example.com"
PLANTED_SECRET = "sk-ant-PLANTED-never-print-me"
DOMAIN = "jobmatch.ntrye.com"
FQDN = f"{DOMAIN}."
CLOUDFRONT_ZONE_ID = "Z2FDTNDATAQYW2"
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"
CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
# The author's existing secret (made by `make secrets-set`): deploy must keep every key.
EXISTING_SECRET = {
    "ANTHROPIC_API_KEY": PLANTED_SECRET,
    "DATABASE_URL": "postgresql+psycopg://u:PLANTED-db-password@db.example/x?sslmode=require",
    "SESSION_SIGNING_KEY": "PLANTED-session-signing-key-0123456789abcdef",
}
# The author's real zone holds the whole domain; deploy/teardown may only touch jobmatch names.
NTRYE_ZONE_ID = "Z0FAKENTRYEPUBLIC"
NTRYE_RECORDS = [
    {"Name": "ntrye.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "76.76.21.21"}]},
    {
        "Name": "ntrye.com.",
        "Type": "MX",
        "TTL": 3600,
        "ResourceRecords": [{"Value": "0 ntrye-com.mail.protection.outlook.com."}],
    },
    {
        "Name": "ntrye.com.",
        "Type": "NS",
        "TTL": 172800,
        "ResourceRecords": [{"Value": "ns-1.awsdns-00.com."}, {"Value": "ns-2.awsdns-01.net."}],
    },
    {
        "Name": "ntrye.com.",
        "Type": "SOA",
        "TTL": 900,
        "ResourceRecords": [{"Value": "ns-1.awsdns-00.com. awsdns-hostmaster.amazon.com. 1 7200"}],
    },
    {
        "Name": "ntrye.com.",
        "Type": "TXT",
        "TTL": 300,
        "ResourceRecords": [{"Value": '"v=spf1 include:secureserver.net -all"'}],
    },
    {
        "Name": "resend._domainkey.ntrye.com.",
        "Type": "TXT",
        "TTL": 300,
        "ResourceRecords": [{"Value": '"p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQ"'}],
    },
    {
        "Name": "www.ntrye.com.",
        "Type": "CNAME",
        "TTL": 300,
        "ResourceRecords": [{"Value": "cname.vercel-dns.com."}],
    },
]

BASH = shutil.which("bash") or "/bin/bash"
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None, reason="needs bash and jq"
)

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "docker $*" >> "$FAKE_DOCKER_LOG"
if [[ "$1" == login ]]; then cat > "$FAKE_DOCKER_STDIN"; fi
exit 0
"""

# `npm run build` writes a small static export, like `next build` with output: "export".
FAKE_NPM = """#!/usr/bin/env bash
printf '%s\\n' "npm $* (in $PWD)" >> "$FAKE_NPM_LOG"
if [[ "$1 $2" == "run build" ]]; then
  mkdir -p out/app out/_next/static/chunks
  echo '<html>home</html>' > out/index.html
  echo '<html>app</html>' > out/app/index.html
  echo 'rsc' > out/app/index.txt
  echo 'js' > out/_next/static/chunks/main-1a2b3c.js
fi
exit 0
"""


@dataclass
class Sandbox:
    root: Path
    env: dict[str, str]

    @property
    def state_file(self) -> Path:
        return self.root / "state" / "state.json"

    @property
    def world_file(self) -> Path:
        return self.root / "world.json"

    def world(self) -> dict[str, Any]:
        return dict(json.loads(self.world_file.read_text()))

    def aws_calls(self) -> list[list[str]]:
        log = self.root / "aws.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def reset_logs(self) -> None:
        for name in ("aws.log", "docker.log"):
            (self.root / name).unlink(missing_ok=True)

    def state(self) -> list[tuple[str, str]]:
        data = json.loads(self.state_file.read_text())
        return [(r["type"], r["id"]) for r in data["resources"]]

    def run(self, script: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [BASH, str(INFRA / script), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


def make_sandbox(tmp_path: Path) -> Sandbox:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws = bin_dir / "aws"
    aws.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "{FAKE_AWS}" "$@"\n')
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    npm = bin_dir / "npm"
    npm.write_text(FAKE_NPM)
    for exe in (aws, docker, npm):
        exe.chmod(0o755)
    (tmp_path / "tmp").mkdir()
    # The secret exists already (created by `make secrets-set`), and so does the author's
    # hosted zone, next to a private zone of the same name and an unrelated zone.
    world = {
        "secrets": ["jobmatch/prod"],
        "secret_values": {"jobmatch/prod": json.dumps(EXISTING_SECRET)},
        "hosted_zones": {
            NTRYE_ZONE_ID: {"Name": "ntrye.com.", "Private": False, "records": NTRYE_RECORDS},
            "Z0FAKENTRYEPRIVATE": {"Name": "ntrye.com.", "Private": True, "records": []},
            "Z0FAKEOTHERZONE": {"Name": "other-example.org.", "Private": False, "records": []},
        },
    }
    (tmp_path / "world.json").write_text(json.dumps(world))
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_AWS_LOG": str(tmp_path / "aws.log"),
        "FAKE_AWS_WORLD": str(tmp_path / "world.json"),
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAKE_DOCKER_STDIN": str(tmp_path / "docker.stdin"),
        "STATE_FILE": str(tmp_path / "state" / "state.json"),
        "BUDGET_EMAIL": BUDGET_EMAIL,
        "FAKE_NPM_LOG": str(tmp_path / "npm.log"),
        "FRONTEND_DIR": str(tmp_path / "frontend"),
        # Temp files (the distribution config holds the origin token) must not outlive a run.
        "TMPDIR": str(tmp_path / "tmp"),
        "DEPLOY_RETRY_SLEEP": "0",
        "DEPLOY_ACM_POLL_SLEEP": "0",
        "TEARDOWN_VERIFY_SLEEP": "0",
        # A secret in the caller's environment must never leak into output or AWS calls.
        "ANTHROPIC_API_KEY": PLANTED_SECRET,
    }
    return Sandbox(tmp_path, env)


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return make_sandbox(tmp_path)


DEPLOY_ARTIFACTS = ("world.json", "state/state.json", "aws.log", "docker.log", "docker.stdin")


@pytest.fixture(scope="module")
def deploy_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, subprocess.CompletedProcess[str]]:
    """One successful deploy per module; tests start from a copy of its fake account."""
    template = make_sandbox(tmp_path_factory.mktemp("deployed"))
    result = template.run("deploy.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    return template.root, result


@pytest.fixture
def deployed(
    sandbox: Sandbox, deploy_template: tuple[Path, subprocess.CompletedProcess[str]]
) -> subprocess.CompletedProcess[str]:
    """`sandbox` after a successful deploy.sh run; returns that run's result."""
    root, result = deploy_template
    for name in DEPLOY_ARTIFACTS:
        target = sandbox.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    return result


NOT_RESOURCES = {"counters", "hosted_zones", "secret_values", "cf_invalidations"}


def remaining(world: dict[str, Any]) -> dict[str, Any]:
    """What is left in the fake account. The hosted zone is the author's, not deploy's: tests
    check its records separately with `zone_records`."""
    return {k: v for k, v in world.items() if k not in NOT_RESOURCES and v}


def zone_records(world: dict[str, Any]) -> list[dict[str, Any]]:
    return list(world["hosted_zones"][NTRYE_ZONE_ID]["records"])


def added_records(world: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in zone_records(world) if r not in NTRYE_RECORDS]


def deploy_ok(sandbox: Sandbox) -> subprocess.CompletedProcess[str]:
    result = sandbox.run("deploy.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def created_resources(world: dict[str, Any]) -> set[tuple[str, str]]:
    """Everything the fake account holds, in the state file's (type, id) vocabulary."""
    out: set[tuple[str, str]] = set()
    out |= {("ecr_repository", n) for n in world.get("ecr", [])}
    out |= {("s3_bucket", n) for n in world.get("buckets", {})}
    out |= {("log_group", n) for n in world.get("log_groups", [])}
    for role, spec in world.get("roles", {}).items():
        out.add(("iam_role", role))
        out |= {("iam_role_policy", f"{role}/{p}") for p in spec["policies"]}
    for fn, spec in world.get("functions", {}).items():
        out.add(("lambda_function", fn))
        if spec["url"]:
            out.add(("function_url", fn))
        out |= {("lambda_permission", f"{fn}/{sid}") for sid in spec["permissions"]}
    out |= {("schedule", n) for n in world.get("schedules", [])}
    out |= {("budget", n) for n in world.get("budgets", [])}
    out |= {("s3_bucket_cors", b) for b in world.get("bucket_cors", {})}
    out |= {("acm_certificate", a) for a in world.get("certificates", {})}
    out |= {("cloudfront_oac", i) for i in world.get("cf_oacs", {})}
    out |= {("cloudfront_function", n) for n in world.get("cf_functions", {})}
    out |= {("cloudfront_distribution", i) for i in world.get("cf_distributions", {})}
    if "hosted_zones" in world:
        out |= {("route53_record", f"{r['Name']}|{r['Type']}") for r in added_records(world)}
    return out


def test_deploy_creates_the_stack_and_prints_url(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    result = deployed
    world = sandbox.world()
    assert world["ecr"] == ["jobmatch-api"]
    assert sorted(world["buckets"]) == sorted([BUCKET, SITE_BUCKET])
    assert sorted(world["log_groups"]) == [
        "/aws/lambda/jobmatch-api",
        "/aws/lambda/jobmatch-cleanup",
    ]
    assert set(world["roles"]) == {"jobmatch-lambda", "jobmatch-scheduler"}
    assert set(world["functions"]) == {"jobmatch-api", "jobmatch-cleanup"}
    api = world["functions"]["jobmatch-api"]
    assert api["url"] is True and len(api["permissions"]) == 2
    assert world["functions"]["jobmatch-cleanup"]["url"] is False
    assert world["schedules"] == ["jobmatch-cleanup-hourly"]
    assert sorted(world["budgets"]) == ["jobmatch-10", "jobmatch-25"]
    assert "https://abc123.lambda-url.us-west-2.on.aws/" in result.stdout
    state = json.loads(sandbox.state_file.read_text())
    assert state["function_url"] == "https://abc123.lambda-url.us-west-2.on.aws/"
    assert set(sandbox.state()) == created_resources(world)


def call(calls: list[list[str]], svc: str, cmd: str) -> list[list[str]]:
    return [c for c in calls if c[:2] == [svc, cmd]]


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_deploy_configures_functions_url_and_permissions(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    calls = sandbox.aws_calls()
    creates = {flag(c, "--function-name"): c for c in call(calls, "lambda", "create-function")}
    base_env = {
        "ENV": "prod",
        "SECRETS_SOURCE": "aws",
        "SECRET_ID": "jobmatch/prod",
        "LLM_PROVIDER": "anthropic",
        "EMBEDDER_PROVIDER": "bedrock",
        "RERANKER_PROVIDER": "bedrock",
        "BLOB_BACKEND": "s3",
        "UPLOADS_BUCKET": BUCKET,
        "EMAIL_SENDER": "resend",
        "PII_ENGINE": "presidio",
        "MIN_RELEVANCE": "0",
        "DB_CONNECT_TIMEOUT_S": "10",
    }
    api_env = json.loads(flag(creates["jobmatch-api"], "--environment"))["Variables"]
    cleanup_env = json.loads(flag(creates["jobmatch-cleanup"], "--environment"))["Variables"]
    assert api_env == base_env
    # EventBridge Scheduler invokes asynchronously, so the cleanup function is not streamed.
    assert cleanup_env == {**base_env, "CLEANUP_MODE": "true", "AWS_LWA_INVOKE_MODE": "buffered"}
    assert flag(creates["jobmatch-api"], "--timeout") == "300"
    assert flag(creates["jobmatch-cleanup"], "--timeout") == "120"
    assert flag(creates["jobmatch-api"], "--memory-size") == "2048"
    assert flag(creates["jobmatch-api"], "--package-type") == "Image"

    url = call(calls, "lambda", "create-function-url-config")[0]
    assert flag(url, "--auth-type") == "NONE"
    assert flag(url, "--invoke-mode") == "RESPONSE_STREAM"
    perms = {flag(c, "--action"): c for c in call(calls, "lambda", "add-permission")}
    assert flag(perms["lambda:InvokeFunctionUrl"], "--function-url-auth-type") == "NONE"
    assert "--invoked-via-function-url" in perms["lambda:InvokeFunction"]
    assert all(flag(p, "--principal") == "*" for p in perms.values())

    conc = call(calls, "lambda", "put-function-concurrency")[0]
    assert flag(conc, "--reserved-concurrent-executions") == "5"

    sched = call(calls, "scheduler", "create-schedule")[0]
    assert flag(sched, "--schedule-expression") == "rate(1 hour)"
    target = json.loads(flag(sched, "--target"))
    assert target["Arn"].endswith(":function:jobmatch-cleanup")
    assert target["RoleArn"].endswith(":role/jobmatch-scheduler")

    docker = (sandbox.root / "docker.log").read_text()
    assert "--platform linux/amd64" in docker
    sha = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert f"{ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/jobmatch-api:{sha}" in docker


def test_iam_policies_are_least_privilege(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    puts = {flag(c, "--role-name"): c for c in call(sandbox.aws_calls(), "iam", "put-role-policy")}
    lam = json.loads(flag(puts["jobmatch-lambda"], "--policy-document"))
    by_action: dict[str, set[str]] = {}
    for stmt in lam["Statement"]:
        assert stmt["Effect"] == "Allow"
        actions = [stmt["Action"]] if isinstance(stmt["Action"], str) else stmt["Action"]
        resources = [stmt["Resource"]] if isinstance(stmt["Resource"], str) else stmt["Resource"]
        for action in actions:
            by_action.setdefault(action, set()).update(resources)
    assert by_action["bedrock:InvokeModel"] == {
        "arn:aws:bedrock:us-west-2::foundation-model/amazon.titan-embed-text-v2:0",
        "arn:aws:bedrock:us-west-2::foundation-model/amazon.rerank-v1:0",
    }
    assert by_action["secretsmanager:GetSecretValue"] == {
        f"arn:aws:secretsmanager:us-west-2:{ACCOUNT}:secret:jobmatch/prod-??????"
    }
    for action in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject"):
        assert by_action[action] == {f"arn:aws:s3:::{BUCKET}/uploads/*"}
    for action in ("logs:CreateLogStream", "logs:PutLogEvents"):
        assert by_action[action] == {
            f"arn:aws:logs:us-west-2:{ACCOUNT}:log-group:/aws/lambda/jobmatch-api:*",
            f"arn:aws:logs:us-west-2:{ACCOUNT}:log-group:/aws/lambda/jobmatch-cleanup:*",
        }
    # ListBucket makes GetObject on a missing key a 404 (NoSuchKey) rather than a 403. No
    # s3:prefix condition: GetObject's implicit check may not carry that key (ruling R8).
    listing = [s for s in lam["Statement"] if s["Action"] == "s3:ListBucket"]
    assert listing == [
        {
            "Sid": listing[0]["Sid"],
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": f"arn:aws:s3:::{BUCKET}",
        }
    ]
    wildcard = {a for a, r in by_action.items() if "*" in r}
    assert wildcard == {"bedrock:Rerank"}  # Rerank has no resource-level permissions
    assert not any(a.endswith("*") for a in by_action)

    sched = json.loads(flag(puts["jobmatch-scheduler"], "--policy-document"))
    assert sched["Statement"] == [
        {
            "Effect": "Allow",
            "Action": "lambda:InvokeFunction",
            "Resource": f"arn:aws:lambda:us-west-2:{ACCOUNT}:function:jobmatch-cleanup",
        }
    ]


def test_everything_taggable_is_tagged(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    calls = sandbox.aws_calls()
    tag = "Key=Project,Value=jobmatch-ai"
    assert tag in call(calls, "ecr", "create-repository")[0]
    assert all(tag in c for c in call(calls, "iam", "create-role"))
    assert all("Project=jobmatch-ai" in c for c in call(calls, "logs", "create-log-group"))
    assert all("Project=jobmatch-ai" in c for c in call(calls, "lambda", "create-function"))
    assert all(tag in c for c in call(calls, "budgets", "create-budget"))
    tagging = flag(call(calls, "s3api", "put-bucket-tagging")[0], "--tagging")
    assert "Project" in tagging and "jobmatch-ai" in tagging


def test_deploy_is_idempotent_and_updates_the_image(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    first_state = sandbox.state()
    sandbox.reset_logs()
    deploy_ok(sandbox)
    calls = sandbox.aws_calls()
    assert [c[:2] for c in calls if c[1].startswith("create-")] == []
    updates = call(calls, "lambda", "update-function-code")
    assert {flag(c, "--function-name") for c in updates} == {"jobmatch-api", "jobmatch-cleanup"}
    assert sandbox.state() == first_state


def test_deploy_requires_budget_email(sandbox: Sandbox) -> None:
    result = sandbox.run("deploy.sh", BUDGET_EMAIL="")
    assert result.returncode != 0
    assert "BUDGET_EMAIL" in result.stderr
    assert sandbox.aws_calls() == []


def test_deploy_requires_the_secret(sandbox: Sandbox) -> None:
    sandbox.world_file.write_text(json.dumps({"secrets": []}))
    result = sandbox.run("deploy.sh")
    assert result.returncode != 0
    assert "make secrets-set" in result.stderr
    assert not [c for c in sandbox.aws_calls() if c[1].startswith("create-")]


@pytest.mark.parametrize(
    "fail_on",
    [
        "ecr put-lifecycle-policy",
        "s3api put-public-access-block",
        "iam put-role-policy",
        "lambda create-function",
        "lambda add-permission",
        "scheduler create-schedule",
        "ecr create-repository",
        "logs put-retention-policy",
        "s3api put-bucket-cors",
        "cloudfront create-origin-access-control",
        "cloudfront publish-function",
        "acm request-certificate",
        "route53 change-resource-record-sets",
        "cloudfront create-distribution-with-tags",
        "s3api put-bucket-policy",
    ],
)
def test_deploy_records_state_before_failure(sandbox: Sandbox, fail_on: str) -> None:
    result = sandbox.run("deploy.sh", FAKE_AWS_FAIL_ON=fail_on)
    assert result.returncode != 0
    world = sandbox.world()
    assert created_resources(world), "the failure point should come after some creates"
    assert set(sandbox.state()) == created_resources(world)

    # ...and teardown removes all of it, leaving the author's DNS records exactly as they were.
    teardown = sandbox.run("teardown.sh", "--yes")
    assert teardown.returncode == 0, teardown.stdout + teardown.stderr
    assert remaining(sandbox.world()) == {}
    assert zone_records(sandbox.world()) == NTRYE_RECORDS


def test_role_propagation_is_retried(sandbox: Sandbox) -> None:
    deploy_ok_result = sandbox.run("deploy.sh", FAKE_AWS_ROLE_NOT_READY="2")
    assert deploy_ok_result.returncode == 0, deploy_ok_result.stderr
    assert set(sandbox.world()["functions"]) == {"jobmatch-api", "jobmatch-cleanup"}


def test_reserved_concurrency_refusal_is_a_warning(sandbox: Sandbox) -> None:
    result = sandbox.run("deploy.sh", FAKE_AWS_FAIL_ON="lambda put-function-concurrency")
    assert result.returncode == 0, result.stderr
    assert "warning" in result.stderr.lower()
    assert "WITHOUT a concurrency cap" in result.stderr
    assert "Budgets" in result.stderr
    assert sandbox.world()["budgets"]  # deploy continued to the end


LAGGING_ARN = f"arn:aws:lambda:us-west-2:{ACCOUNT}:function:jobmatch-api"


def test_tagging_index_lag_is_a_warning_when_deleted_by_name(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    result = sandbox.run("teardown.sh", "--yes", FAKE_AWS_EXTRA_TAGGED=LAGGING_ARN)
    assert result.returncode == 0, result.stdout + result.stderr
    assert LAGGING_ARN in result.stderr and "index lag" in result.stderr
    assert remaining(sandbox.world()) == {}


def test_tagged_resource_not_confirmed_by_name_fails(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    stray = f"arn:aws:sqs:us-west-2:{ACCOUNT}:jobmatch-stray"
    result = sandbox.run("teardown.sh", "--yes", FAKE_AWS_EXTRA_TAGGED=stray)
    assert result.returncode == 1
    assert f"tagged {stray}" in result.stderr
    assert sandbox.state_file.exists()


def test_failed_object_batch_is_reported(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    result = sandbox.run("teardown.sh", "--yes", FAKE_AWS_FAIL_ON="s3api delete-objects")
    assert result.returncode == 1
    assert f"objects in S3 bucket {BUCKET}" in result.stderr
    assert sorted(sandbox.world()["buckets"]) == sorted([BUCKET, SITE_BUCKET])


def test_teardown_deletes_everything_deploy_created(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    deploy_calls = sandbox.aws_calls()
    sandbox.reset_logs()
    result = sandbox.run("teardown.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert remaining(sandbox.world()) == {}
    assert not sandbox.state_file.exists()

    teardown_calls = sandbox.aws_calls()
    deletes = {(c[0], c[1]) for c in teardown_calls}
    expected_delete = {
        ("ecr", "create-repository"): ("ecr", "delete-repository"),
        ("s3api", "create-bucket"): ("s3api", "delete-bucket"),
        ("logs", "create-log-group"): ("logs", "delete-log-group"),
        ("iam", "create-role"): ("iam", "delete-role"),
        ("iam", "put-role-policy"): ("iam", "delete-role-policy"),
        ("lambda", "create-function"): ("lambda", "delete-function"),
        ("lambda", "create-function-url-config"): ("lambda", "delete-function-url-config"),
        ("lambda", "add-permission"): ("lambda", "remove-permission"),
        ("scheduler", "create-schedule"): ("scheduler", "delete-schedule"),
        ("budgets", "create-budget"): ("budgets", "delete-budget"),
        ("s3api", "put-bucket-cors"): ("s3api", "delete-bucket-cors"),
        ("acm", "request-certificate"): ("acm", "delete-certificate"),
        ("cloudfront", "create-origin-access-control"): (
            "cloudfront",
            "delete-origin-access-control",
        ),
        ("cloudfront", "create-function"): ("cloudfront", "delete-function"),
        ("cloudfront", "create-distribution-with-tags"): ("cloudfront", "delete-distribution"),
        ("route53", "change-resource-record-sets"): ("route53", "change-resource-record-sets"),
    }
    creating = ("put-role-policy", "add-permission", "put-bucket-cors", "request-certificate")
    for argv in deploy_calls:
        cmd = argv[1]
        if cmd.startswith("create-") or cmd in creating or cmd == "change-resource-record-sets":
            assert (argv[0], cmd) in expected_delete, f"no teardown known for {argv[:2]}"
            assert expected_delete[(argv[0], cmd)] in deletes
    assert ("secretsmanager", "delete-secret") in deletes
    secret_delete = call(teardown_calls, "secretsmanager", "delete-secret")[0]
    assert "--force-delete-without-recovery" in secret_delete
    assert ("s3api", "delete-objects") in deletes  # the bucket was emptied first


def test_teardown_order_is_reverse_of_dependencies(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    sandbox.reset_logs()
    assert sandbox.run("teardown.sh", "--yes").returncode == 0
    calls = sandbox.aws_calls()

    def first(svc: str, cmd: str, needle: str = "") -> int:
        return next(
            i
            for i, c in enumerate(calls)
            if c[:2] == [svc, cmd] and (not needle or any(needle in a for a in c))
        )

    expected_order = [
        first("cloudfront", "delete-distribution"),  # CloudFront fronts everything else
        first("scheduler", "delete-schedule"),
        first("iam", "delete-role", "jobmatch-scheduler"),
        first("lambda", "delete-function", "jobmatch-cleanup"),
        first("lambda", "delete-function-url-config"),
        first("lambda", "remove-permission"),
        first("lambda", "delete-function", "jobmatch-api"),
        first("iam", "delete-role-policy", "jobmatch-lambda"),
        first("iam", "delete-role", "jobmatch-lambda"),
        first("logs", "delete-log-group"),
        first("s3api", "delete-bucket-cors"),
        first("s3api", "delete-objects"),
        first("s3api", "delete-bucket"),
        first("ecr", "delete-repository"),
        first("secretsmanager", "delete-secret"),
        first("budgets", "delete-budget"),  # last, once nothing billable is left
    ]
    assert expected_order == sorted(expected_order)
    assert first("iam", "delete-role-policy", "jobmatch-scheduler") < expected_order[2]


def test_teardown_without_state_file_sweeps_by_name(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    sandbox.state_file.unlink()
    result = sandbox.run("teardown.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert remaining(sandbox.world()) == {}


def test_teardown_with_corrupt_state_file_still_sweeps(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    sandbox.state_file.write_text("{not json")
    result = sandbox.run("teardown.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "unreadable" in result.stderr
    assert remaining(sandbox.world()) == {}


def test_deploy_refuses_a_corrupt_state_file(sandbox: Sandbox) -> None:
    sandbox.state_file.parent.mkdir(parents=True)
    sandbox.state_file.write_text("{not json")
    result = sandbox.run("deploy.sh")
    assert result.returncode != 0
    assert "unreadable" in result.stderr
    assert not [c for c in sandbox.aws_calls() if c[1].startswith("create-")]


def test_teardown_leaves_secrets_it_does_not_manage(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    world = sandbox.world()
    world["secrets"].append("jobmatch/dev")
    world["secret_values"]["jobmatch/dev"] = json.dumps({"ANTHROPIC_API_KEY": PLANTED_SECRET})
    sandbox.world_file.write_text(json.dumps(world))
    result = sandbox.run("teardown.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert remaining(sandbox.world()) == {"secrets": ["jobmatch/dev"]}
    assert "jobmatch/dev" in result.stdout


def test_teardown_on_empty_account_succeeds(sandbox: Sandbox) -> None:
    result = sandbox.run("teardown.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert remaining(sandbox.world()) == {}


def test_teardown_keep_secret(sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]) -> None:
    result = sandbox.run("teardown.sh", "--keep-secret")
    assert result.returncode == 0, result.stdout + result.stderr
    assert remaining(sandbox.world()) == {"secrets": ["jobmatch/prod"]}
    assert not call(sandbox.aws_calls(), "secretsmanager", "delete-secret")


def test_teardown_secret_needs_typed_confirmation(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    refused = subprocess.run(  # noqa: S603
        [BASH, str(INFRA / "teardown.sh")],
        env=sandbox.env,
        input="no\n",
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # The secret was kept, so the run reports it as remaining.
    assert refused.returncode == 1
    assert sandbox.world()["secrets"] == ["jobmatch/prod"]
    assert "jobmatch/prod" in refused.stdout + refused.stderr

    confirmed = subprocess.run(  # noqa: S603
        [BASH, str(INFRA / "teardown.sh")],
        env=sandbox.env,
        input="jobmatch/prod\n",
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert confirmed.returncode == 0, confirmed.stdout + confirmed.stderr
    assert remaining(sandbox.world()) == {}


def test_teardown_reports_leftovers_and_exits_1(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    result = sandbox.run("teardown.sh", "--yes", FAKE_AWS_FAIL_ON="lambda delete-function")
    assert result.returncode == 1
    out = result.stdout + result.stderr
    assert "jobmatch-api" in out and "jobmatch-cleanup" in out
    # Everything else was still deleted, and the state file is kept for a retry.
    world = remaining(sandbox.world())
    assert set(world) <= {"functions", "roles", "log_groups", "budgets"}
    assert sorted(world["budgets"]) == ["jobmatch-10", "jobmatch-25"]  # alerts kept
    assert sandbox.state_file.exists()
    retry = sandbox.run("teardown.sh", "--yes")
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert remaining(sandbox.world()) == {}


def test_budgets_kept_when_a_billable_resource_survives(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    sandbox.reset_logs()
    result = sandbox.run("teardown.sh", "--yes", FAKE_AWS_FAIL_ON="s3api delete-bucket")
    assert result.returncode == 1
    assert f"S3 bucket {BUCKET}" in result.stderr
    world = sandbox.world()
    assert sorted(world["budgets"]) == ["jobmatch-10", "jobmatch-25"]
    assert not call(sandbox.aws_calls(), "budgets", "delete-budget")
    assert "keeping the budget alerts" in result.stderr
    assert sandbox.state_file.exists()


def test_scripts_never_echo_secrets(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    for script in INFRA.glob("*.sh"):
        text = script.read_text()
        assert not re.search(r"set\s+-[a-wyz]*x|set\s+-o\s+xtrace|bash\s+-x", text), script
    deploy = deployed
    teardown = sandbox.run("teardown.sh", "--yes")
    outputs = deploy.stdout + deploy.stderr + teardown.stdout + teardown.stderr
    aws_log = (sandbox.root / "aws.log").read_text()
    docker_log = (sandbox.root / "docker.log").read_text()
    for blob in (outputs, aws_log, docker_log):
        assert PLANTED_SECRET not in blob
        assert "FAKE-ECR-PASSWORD-PLANTED" not in blob
    # The ECR password went to docker on stdin only.
    assert (sandbox.root / "docker.stdin").read_text().strip() == "FAKE-ECR-PASSWORD-PLANTED"
    # The budget email reaches AWS Budgets but is never stored or printed.
    assert BUDGET_EMAIL not in outputs
    assert BUDGET_EMAIL in aws_log


def test_state_file_holds_only_types_and_ids(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    raw = sandbox.state_file.read_text()
    assert BUDGET_EMAIL not in raw
    data = json.loads(raw)
    assert set(data) <= {"resources", "function_url"}
    assert all(set(r) == {"type", "id"} for r in data["resources"])
    assert len(data["resources"]) == len({(r["type"], r["id"]) for r in data["resources"]})


GLOBAL_FLAGS = {"--region", "--query", "--output", "--no-cli-pager"}
CLI_CUSTOMIZATIONS = {"wait", "get-login-password"}  # no API input shape to check against


@cache
def api_parameters(svc: str, cmd: str) -> frozenset[str]:
    """Input member names of the API operation behind `aws <svc> <cmd>`, from botocore's
    bundled service models (offline). Lower-cased, so they compare with de-hyphenated flags."""
    import botocore.session
    from botocore import xform_name

    model = botocore.session.get_session().get_service_model("s3" if svc == "s3api" else svc)
    ops = {xform_name(op, "-"): op for op in model.operation_names}
    assert cmd in ops, f"aws {svc} has no command {cmd}"
    shape = model.operation_model(ops[cmd]).input_shape
    return frozenset(m.lower() for m in (shape.members if shape else {}))


def test_every_cli_flag_exists_in_the_aws_api(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    """Catches misspelt commands and flags, which would otherwise only fail in a real deploy."""
    assert sandbox.run("teardown.sh", "--yes").returncode == 0
    seen: dict[tuple[str, str], set[str]] = {}
    for argv in sandbox.aws_calls():
        if argv[1] in CLI_CUSTOMIZATIONS or argv[0] == "s3":  # `aws s3` is all CLI-side
            continue
        names = {a for a in argv[2:] if a.startswith("--")} - GLOBAL_FLAGS
        seen.setdefault((argv[0], argv[1]), set()).update(names)
    assert len(seen) > 45
    for (svc, cmd), names in sorted(seen.items()):
        params = api_parameters(svc, cmd)
        for name in names:
            bare = name.removeprefix("--no-") if name.startswith("--no-") else name
            param = bare.removeprefix("--").replace("-", "")
            assert param in params, f"aws {svc} {cmd} has no {name}"


# ---- Phase 8: site bucket, CloudFront, ACM certificate and the jobmatch.ntrye.com records


def secret_json(world: dict[str, Any]) -> dict[str, str]:
    return dict(json.loads(world["secret_values"]["jobmatch/prod"]))


def the_distribution(world: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    (dist_id, dist), *more = world["cf_distributions"].items()
    assert not more
    return dist_id, dist


def validate_against_api(svc: str, operation: str, params: dict[str, Any]) -> None:
    """Raises if `params` does not match the API's input shape (botocore model, offline)."""
    import botocore.session
    from botocore.validate import validate_parameters

    model = botocore.session.get_session().get_service_model(svc)
    validate_parameters(params, model.operation_model(operation).input_shape)


def test_deploy_creates_site_distribution_certificate_and_alias(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    world = sandbox.world()
    dist_id, dist = the_distribution(world)
    (cert_arn, cert), *more_certs = world["certificates"].items()
    assert not more_certs and cert["domain"] == DOMAIN
    assert cert_arn.startswith("arn:aws:acm:us-east-1:")  # CloudFront needs us-east-1
    assert "Key=Project,Value=jobmatch-ai" in cert["tags"]
    assert {"Key": "Project", "Value": "jobmatch-ai"} in dist["tags"]["Items"]
    assert list(world["cf_oacs"]) and world["cf_functions"]["jobmatch-index-rewrite"]["published"]

    # Exactly three records were added to the author's zone, and nothing else changed.
    added = {(r["Name"], r["Type"]): r for r in added_records(world)}
    assert [r for r in zone_records(world) if r in NTRYE_RECORDS] == NTRYE_RECORDS
    validation = [k for k in added if k[1] == "CNAME"]
    assert len(added) == 3 and len(validation) == 1
    assert re.fullmatch(r"_[0-9a-f]+\.jobmatch\.ntrye\.com\.", validation[0][0])
    for rtype in ("A", "AAAA"):
        assert added[(FQDN, rtype)] == {
            "Name": FQDN,
            "Type": rtype,
            "AliasTarget": {
                "HostedZoneId": CLOUDFRONT_ZONE_ID,
                "DNSName": f"{dist_id.lower()}.cloudfront.net.",
                "EvaluateTargetHealth": False,
            },
        }
    # The private zone of the same name and the unrelated zone are untouched.
    assert world["hosted_zones"]["Z0FAKENTRYEPRIVATE"]["records"] == []
    assert world["hosted_zones"]["Z0FAKEOTHERZONE"]["records"] == []

    # Only this distribution may read the private site bucket.
    assert world["bucket_policies"][SITE_BUCKET] == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowCloudFrontReadOnly",
                "Effect": "Allow",
                "Principal": {"Service": "cloudfront.amazonaws.com"},
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{SITE_BUCKET}/*",
                "Condition": {
                    "StringEquals": {
                        "AWS:SourceArn": f"arn:aws:cloudfront::{ACCOUNT}:distribution/{dist_id}"
                    }
                },
            }
        ],
    }
    calls = sandbox.aws_calls()
    site_bucket_calls = [c for c in calls if c[0] == "s3api" and SITE_BUCKET in c]
    public_block = [c for c in site_bucket_calls if c[1] == "put-public-access-block"]
    assert public_block and "BlockPublicPolicy=true" in flag(
        public_block[0], "--public-access-block-configuration"
    )
    assert any(c[1] == "put-bucket-encryption" and "AES256" in str(c) for c in site_bucket_calls)
    assert any(c[1] == "put-bucket-tagging" for c in site_bucket_calls)

    # Browser uploads PUT straight to the uploads bucket from the site's origin only.
    assert world["bucket_cors"] == {
        BUCKET: {
            "CORSRules": [
                {
                    "AllowedOrigins": [f"https://{DOMAIN}"],
                    "AllowedMethods": ["PUT"],
                    "AllowedHeaders": ["content-type", "x-amz-server-side-encryption"],
                    "MaxAgeSeconds": 3000,
                }
            ]
        }
    }
    assert set(sandbox.state()) == created_resources(world)
    assert f"https://{DOMAIN}" in deployed.stdout


def test_distribution_config(sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]) -> None:
    world = sandbox.world()
    _, dist = the_distribution(world)
    cfg = dist["config"]
    validate_against_api(
        "cloudfront",
        "CreateDistributionWithTags",
        {"DistributionConfigWithTags": {"DistributionConfig": cfg, "Tags": dist["tags"]}},
    )
    assert cfg["Aliases"] == {"Quantity": 1, "Items": [DOMAIN]}
    assert cfg["Enabled"] is True
    assert cfg["PriceClass"] == "PriceClass_100"
    assert cfg["HttpVersion"] == "http2and3"
    assert cfg["IsIPV6Enabled"] is True
    assert cfg["DefaultRootObject"] == "index.html"
    (cert_arn,) = world["certificates"]
    assert cfg["ViewerCertificate"] == {
        "ACMCertificateArn": cert_arn,
        "SSLSupportMethod": "sni-only",
        "MinimumProtocolVersion": "TLSv1.2_2021",
    }
    origins = {o["Id"]: o for o in cfg["Origins"]["Items"]}
    assert set(origins) == {"site", "api"}
    (oac_id,) = world["cf_oacs"]
    assert origins["site"]["DomainName"] == f"{SITE_BUCKET}.s3.us-west-2.amazonaws.com"
    assert origins["site"]["OriginAccessControlId"] == oac_id
    assert origins["site"]["S3OriginConfig"] == {"OriginAccessIdentity": ""}
    api = origins["api"]
    assert api["DomainName"] == "abc123.lambda-url.us-west-2.on.aws"
    assert api["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
    assert api["CustomOriginConfig"]["OriginReadTimeout"] == 60
    assert api["CustomOriginConfig"]["OriginKeepaliveTimeout"] == 5
    assert api["CustomOriginConfig"]["OriginSslProtocols"] == {"Quantity": 1, "Items": ["TLSv1.2"]}
    token = secret_json(world)["ORIGIN_VERIFY_TOKEN"]
    assert api["CustomHeaders"] == {
        "Quantity": 1,
        "Items": [{"HeaderName": "X-Origin-Verify", "HeaderValue": token}],
    }

    default = cfg["DefaultCacheBehavior"]
    assert default["TargetOriginId"] == "site"
    assert default["ViewerProtocolPolicy"] == "redirect-to-https"
    assert default["CachePolicyId"] == CACHING_OPTIMIZED
    assert default["AllowedMethods"]["Items"] == ["GET", "HEAD"]
    (assoc,) = default["FunctionAssociations"]["Items"]
    assert assoc["EventType"] == "viewer-request"
    assert assoc["FunctionARN"].endswith(":function/jobmatch-index-rewrite")

    (api_behavior,) = cfg["CacheBehaviors"]["Items"]
    assert api_behavior["PathPattern"] == "/api/*"
    assert api_behavior["TargetOriginId"] == "api"
    assert api_behavior["CachePolicyId"] == CACHING_DISABLED
    # The managed policy forwards everything but Host (the Function URL needs its own Host)
    # and adds CloudFront's viewer-location headers, CloudFront-Viewer-Address among them.
    assert api_behavior["OriginRequestPolicyId"] == ALL_VIEWER_EXCEPT_HOST
    assert sorted(api_behavior["AllowedMethods"]["Items"]) == sorted(
        ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"]
    )
    assert api_behavior["AllowedMethods"]["CachedMethods"]["Items"] == ["GET", "HEAD"]
    assert api_behavior["ViewerProtocolPolicy"] == "https-only"
    assert "FunctionAssociations" not in api_behavior or not api_behavior[
        "FunctionAssociations"
    ].get("Items")


def test_other_cloudfront_and_dns_inputs_match_the_api(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    calls = sandbox.aws_calls()
    oac = call(calls, "cloudfront", "create-origin-access-control")[0]
    validate_against_api(
        "cloudfront",
        "CreateOriginAccessControl",
        {"OriginAccessControlConfig": json.loads(flag(oac, "--origin-access-control-config"))},
    )
    fn = call(calls, "cloudfront", "create-function")[0]
    fn_config = json.loads(flag(fn, "--function-config"))
    assert fn_config["Runtime"] == "cloudfront-js-2.0"
    validate_against_api(
        "cloudfront",
        "CreateFunction",
        {"Name": "x", "FunctionConfig": fn_config, "FunctionCode": b"x"},
    )
    cors = call(calls, "s3api", "put-bucket-cors")[0]
    validate_against_api(
        "s3",
        "PutBucketCors",
        {"Bucket": BUCKET, "CORSConfiguration": json.loads(flag(cors, "--cors-configuration"))},
    )


def test_index_rewrite_function_maps_directories_to_index_html(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    code = sandbox.world()["cf_functions"]["jobmatch-index-rewrite"]["code"]
    harness = (
        code
        + "\nfor (const uri of ['/', '/app/', '/app', '/app/index.txt', '/_next/static/a.js'])"
        + " console.log(uri + ' ' + handler({request: {uri}}).uri);\n"
    )
    out = subprocess.run(  # noqa: S603
        [node, "-e", harness], capture_output=True, text=True, check=True, timeout=30
    ).stdout.splitlines()
    assert out == [
        "/ /index.html",
        "/app/ /app/index.html",
        "/app /app/index.html",
        "/app/index.txt /app/index.txt",
        "/_next/static/a.js /_next/static/a.js",
    ]


def test_origin_token_is_merged_without_printing_it(
    sandbox: Sandbox,
    deploy_template: tuple[Path, subprocess.CompletedProcess[str]],
    deployed: subprocess.CompletedProcess[str],
) -> None:
    secret = secret_json(sandbox.world())
    token = secret.pop("ORIGIN_VERIFY_TOKEN")
    assert secret == EXISTING_SECRET  # every existing key kept as it was
    assert len(token) >= 64  # secrets.token_urlsafe(48)
    template_root, _ = deploy_template
    # The new JSON went to AWS as a file, never on a command line.
    put = call(sandbox.aws_calls(), "secretsmanager", "put-secret-value")
    assert len(put) == 1 and flag(put[0], "--secret-string").startswith("file://")
    distribution_calls = call(sandbox.aws_calls(), "cloudfront", "create-distribution-with-tags")
    assert flag(distribution_calls[0], "--distribution-config-with-tags").startswith("file://")
    for blob in (
        deployed.stdout + deployed.stderr,
        (sandbox.root / "aws.log").read_text(),
        sandbox.state_file.read_text(),
        (sandbox.root / "docker.log").read_text(),
    ):
        assert token not in blob
        assert PLANTED_SECRET not in blob
    assert "ORIGIN_VERIFY_TOKEN" in deployed.stdout  # the key name is reported, not the value
    # The temp files holding the secret and the distribution config are gone.
    assert list((template_root / "tmp").iterdir()) == []


def test_redeploy_keeps_the_token_and_updates_the_distribution(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    before = sandbox.world()
    sandbox.reset_logs()
    result = deploy_ok(sandbox)
    after = sandbox.world()
    calls = sandbox.aws_calls()
    assert secret_json(after) == secret_json(before)
    assert not call(calls, "secretsmanager", "put-secret-value")
    assert not call(calls, "acm", "request-certificate")
    for update in call(calls, "lambda", "update-function-configuration"):
        assert "CONFIG_REV" not in flag(update, "--environment")
    assert len(call(calls, "cloudfront", "update-distribution")) == 1
    assert len(call(calls, "cloudfront", "publish-function")) == 1
    assert after["cf_distributions"].keys() == before["cf_distributions"].keys()
    assert zone_records(after) == zone_records(before)
    token = secret_json(after)["ORIGIN_VERIFY_TOKEN"]
    assert token not in result.stdout + result.stderr + (sandbox.root / "aws.log").read_text()
    assert list((sandbox.root / "tmp").iterdir()) == []


def test_token_added_later_restarts_functions_and_updates_the_header(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    world = sandbox.world()
    old_token = secret_json(world)["ORIGIN_VERIFY_TOKEN"]
    world["secret_values"]["jobmatch/prod"] = json.dumps(EXISTING_SECRET)
    sandbox.world_file.write_text(json.dumps(world))
    sandbox.reset_logs()
    deploy_ok(sandbox)
    world = sandbox.world()
    token = secret_json(world)["ORIGIN_VERIFY_TOKEN"]
    assert token != old_token
    calls = sandbox.aws_calls()
    assert len(call(calls, "secretsmanager", "put-secret-value")) == 1
    # Lambda reads the secret at cold start: a config change makes it start fresh instances.
    updated = {
        flag(c, "--function-name"): json.loads(flag(c, "--environment"))["Variables"]
        for c in call(calls, "lambda", "update-function-configuration")
    }
    assert set(updated) == {"jobmatch-api", "jobmatch-cleanup"}
    assert all(env.get("CONFIG_REV") for env in updated.values())
    _, dist = the_distribution(world)
    api = next(o for o in dist["config"]["Origins"]["Items"] if o["Id"] == "api")
    assert api["CustomHeaders"]["Items"][0]["HeaderValue"] == token


def test_a_short_token_is_replaced(sandbox: Sandbox) -> None:
    world = sandbox.world()
    world["secret_values"]["jobmatch/prod"] = json.dumps(
        {**EXISTING_SECRET, "ORIGIN_VERIFY_TOKEN": ""}
    )
    sandbox.world_file.write_text(json.dumps(world))
    deploy_ok(sandbox)
    token = secret_json(sandbox.world())["ORIGIN_VERIFY_TOKEN"]
    assert len(token) >= 64


def test_unvalidated_certificate_fails_clearly_and_tears_down(sandbox: Sandbox) -> None:
    result = sandbox.run("deploy.sh", FAKE_AWS_ACM_PENDING="1", DEPLOY_ACM_TIMEOUT_S="0")
    assert result.returncode != 0
    assert "not validated within" in result.stderr
    assert "make teardown" in result.stderr
    world = sandbox.world()
    assert not world["cf_distributions"]
    types = {t for t, _ in sandbox.state()}
    assert {"acm_certificate", "route53_record"} <= types
    assert set(sandbox.state()) == created_resources(world)

    teardown = sandbox.run("teardown.sh", "--yes")
    assert teardown.returncode == 0, teardown.stdout + teardown.stderr
    assert remaining(sandbox.world()) == {}
    assert zone_records(sandbox.world()) == NTRYE_RECORDS


FOREIGN_A = {"Name": FQDN, "Type": "A", "TTL": 60, "ResourceRecords": [{"Value": "192.0.2.7"}]}


def test_deploy_refuses_to_overwrite_a_record_it_did_not_create(sandbox: Sandbox) -> None:
    world = sandbox.world()
    world["hosted_zones"][NTRYE_ZONE_ID]["records"].append(FOREIGN_A)
    sandbox.world_file.write_text(json.dumps(world))
    result = sandbox.run("deploy.sh")
    assert result.returncode != 0
    assert "refusing to replace" in result.stderr
    # Checked before anything is created.
    assert not [c for c in sandbox.aws_calls() if c[1].startswith("create-")]
    assert not call(sandbox.aws_calls(), "route53", "change-resource-record-sets")
    assert FOREIGN_A in zone_records(sandbox.world())

    # Teardown removes what deploy made and leaves the foreign record alone.
    teardown = sandbox.run("teardown.sh", "--yes")
    assert teardown.returncode == 0, teardown.stdout + teardown.stderr
    assert remaining(sandbox.world()) == {}
    assert zone_records(sandbox.world()) == [*NTRYE_RECORDS, FOREIGN_A]
    assert "left alone" in teardown.stdout + teardown.stderr


def test_dns_changes_are_limited_to_the_jobmatch_names(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    deploy_calls = sandbox.aws_calls()
    validation_name = next(n for t, n in sandbox.state() if t == "route53_record" and "CNAME" in n)
    allowed = {(FQDN, "A"), (FQDN, "AAAA"), tuple(validation_name.split("|"))}
    before = added_records(sandbox.world())
    sandbox.reset_logs()
    result = sandbox.run("teardown.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    teardown_calls = sandbox.aws_calls()

    all_calls = deploy_calls + teardown_calls
    route53 = {c[1] for c in all_calls if c[0] == "route53"}
    assert route53 == {
        "list-hosted-zones-by-name",
        "list-resource-record-sets",
        "change-resource-record-sets",
    }
    for argv in call(all_calls, "route53", "change-resource-record-sets"):
        assert flag(argv, "--hosted-zone-id") == NTRYE_ZONE_ID  # found by name, never made
    # The fake applied every change: the zone is back to exactly the author's records, and
    # teardown's DELETEs named exactly the records deploy added.
    assert zone_records(sandbox.world()) == NTRYE_RECORDS
    assert {(r["Name"], r["Type"]) for r in before} == allowed


def test_change_batches_only_name_jobmatch_records(sandbox: Sandbox, tmp_path: Path) -> None:
    """Keep the change-batch files (a copying aws shim) to inspect every change deploy and
    teardown sent to Route 53."""
    kept = tmp_path / "batches"
    kept.mkdir()
    shim = sandbox.root / "bin" / "aws"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in file://*/change-batch.*) '
        f'cp "${{a#file://}}" "{kept}/$(date +%s%N).json";; esac; done\n'
        f'exec "{sys.executable}" "{FAKE_AWS}" "$@"\n'
    )
    deploy_ok(sandbox)
    validation = next(n for t, n in sandbox.state() if t == "route53_record" and "CNAME" in n)
    allowed = {(FQDN, "A"), (FQDN, "AAAA"), tuple(validation.split("|"))}
    assert sandbox.run("teardown.sh", "--yes").returncode == 0
    actions: dict[tuple[str, str], list[str]] = {}
    files = sorted(kept.iterdir())
    assert len(files) == 6
    for path in files:
        batch = json.loads(path.read_text())
        validate_against_api(
            "route53",
            "ChangeResourceRecordSets",
            {"HostedZoneId": NTRYE_ZONE_ID, "ChangeBatch": batch},
        )
        for change in batch["Changes"]:
            rrs = change["ResourceRecordSet"]
            assert (rrs["Name"], rrs["Type"]) in allowed
            actions.setdefault((rrs["Name"], rrs["Type"]), []).append(change["Action"])
    assert actions == {name: ["UPSERT", "DELETE"] for name in allowed}
    assert zone_records(sandbox.world()) == NTRYE_RECORDS


def test_teardown_removes_cloudfront_in_a_safe_order(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    sandbox.reset_logs()
    result = sandbox.run("teardown.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = sandbox.aws_calls()

    def first(svc: str, cmd: str, pred: Any = None) -> int:
        return next(
            i for i, c in enumerate(calls) if c[:2] == [svc, cmd] and (pred is None or pred(c))
        )

    changes = [
        i for i, c in enumerate(calls) if c[:2] == ["route53", "change-resource-record-sets"]
    ]
    assert len(changes) == 3
    disable = first("cloudfront", "update-distribution")
    order = [
        changes[0],  # the alias records go first: the name stops pointing at CloudFront
        disable,
        first("cloudfront", "wait"),
        first("cloudfront", "delete-distribution"),
        first("cloudfront", "delete-function"),
        first("cloudfront", "delete-origin-access-control"),
        changes[2],  # the validation record, then the certificate it validated
        first("acm", "delete-certificate"),
        first("s3api", "delete-bucket", lambda c: SITE_BUCKET in c),
        first("budgets", "delete-budget"),
    ]
    assert order == sorted(order)
    assert calls[first("cloudfront", "wait")][2] == "distribution-deployed"
    assert "us-east-1" in calls[first("acm", "delete-certificate")]
    assert "15" in result.stdout  # warns that disabling takes a while
    assert not [
        c
        for c in calls
        if c[0] == "route53" and "hosted-zone" in c[1] and c[1] != "list-hosted-zones-by-name"
    ]


def test_failed_distribution_delete_keeps_budgets_and_retries(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    result = sandbox.run("teardown.sh", "--yes", FAKE_AWS_FAIL_ON="cloudfront delete-distribution")
    assert result.returncode == 1
    world = sandbox.world()
    assert world["cf_distributions"] and world["certificates"]  # in use, so kept too
    assert sorted(world["budgets"]) == ["jobmatch-10", "jobmatch-25"]
    assert "CloudFront distribution" in result.stderr
    retry = sandbox.run("teardown.sh", "--yes")
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert remaining(sandbox.world()) == {}
    assert zone_records(sandbox.world()) == NTRYE_RECORDS


def test_teardown_leaves_a_foreign_certificate_for_the_domain(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    foreign = f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/hand-made"
    world = sandbox.world()
    world["certificates"][foreign] = {"domain": DOMAIN, "tags": "", "described": 5}
    sandbox.world_file.write_text(json.dumps(world))
    sandbox.state_file.unlink()  # forces the by-name sweep
    result = sandbox.run("teardown.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert remaining(sandbox.world()) == {"certificates": {foreign: world["certificates"][foreign]}}
    assert zone_records(sandbox.world()) == NTRYE_RECORDS


# ---- make deploy-frontend


def run_deploy_frontend(sandbox: Sandbox) -> subprocess.CompletedProcess[str]:
    (sandbox.root / "frontend").mkdir(exist_ok=True)
    return sandbox.run("deploy-frontend.sh")


def test_deploy_frontend_needs_a_deployed_distribution(sandbox: Sandbox) -> None:
    result = run_deploy_frontend(sandbox)
    assert result.returncode != 0
    assert "make deploy" in result.stderr
    assert not [c for c in sandbox.aws_calls() if c[0] in ("s3", "cloudfront")]
    assert not (sandbox.root / "npm.log").exists()


def test_deploy_frontend_builds_syncs_and_invalidates(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    sandbox.reset_logs()
    result = run_deploy_frontend(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    npm = (sandbox.root / "npm.log").read_text().splitlines()
    assert [line.split(" (in ")[0] for line in npm] == ["npm ci", "npm run build"]
    assert all(line.endswith(f"(in {sandbox.root / 'frontend'})") for line in npm)

    syncs = call(sandbox.aws_calls(), "s3", "sync")
    assert len(syncs) == 2
    hashed, rest = syncs
    assert hashed[2].endswith("out/_next/static")
    assert hashed[3] == f"s3://{SITE_BUCKET}/_next/static"
    assert flag(hashed, "--cache-control") == "public, max-age=31536000, immutable"
    assert rest[2].endswith("/out") and rest[3] == f"s3://{SITE_BUCKET}"
    assert flag(rest, "--cache-control") == "no-cache"
    assert flag(rest, "--exclude") == "_next/static/*"
    assert "--delete" in hashed and "--delete" in rest
    # Stale objects are gone; the new build is there.
    assert sorted(sandbox.world()["buckets"][SITE_BUCKET]) == [
        "_next/static/chunks/main-1a2b3c.js",
        "app/index.html",
        "app/index.txt",
        "index.html",
    ]
    dist_id, _ = the_distribution(sandbox.world())
    (inv,) = call(sandbox.aws_calls(), "cloudfront", "create-invalidation")
    assert flag(inv, "--distribution-id") == dist_id
    assert flag(inv, "--paths") == "/*"
    assert f"https://{DOMAIN}" in result.stdout


def test_deploy_frontend_fails_when_the_build_has_no_output(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    npm = sandbox.root / "bin" / "npm"
    npm.write_text('#!/usr/bin/env bash\necho "npm $*" >> "$FAKE_NPM_LOG"\n')
    sandbox.reset_logs()
    result = run_deploy_frontend(sandbox)
    assert result.returncode != 0
    assert "index.html" in result.stderr
    assert not call(sandbox.aws_calls(), "s3", "sync")


# ---- infra/aws/origin_token.py


def run_origin_token(tmp_path: Path, current: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    src, out = tmp_path / "secret.json", tmp_path / "merged.json"
    src.write_text(current)
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(INFRA / "origin_token.py"), str(src), str(out)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return result, out


def test_origin_token_helper_adds_keeps_and_never_prints(tmp_path: Path) -> None:
    result, out = run_origin_token(tmp_path, json.dumps(EXISTING_SECRET))
    assert result.returncode == 0 and result.stdout.strip() == "added"
    merged = json.loads(out.read_text())
    assert out.stat().st_mode & 0o077 == 0
    token = merged.pop("ORIGIN_VERIFY_TOKEN")
    assert merged == EXISTING_SECRET and len(token) >= 64
    assert token not in result.stdout + result.stderr
    assert PLANTED_SECRET not in result.stdout + result.stderr

    out.unlink()
    kept, out = run_origin_token(
        tmp_path, json.dumps({**EXISTING_SECRET, "ORIGIN_VERIFY_TOKEN": token})
    )
    assert kept.returncode == 0 and kept.stdout.strip() == "present"
    assert not out.exists()


def test_origin_token_helper_rejects_bad_json_without_echoing(tmp_path: Path) -> None:
    for bad in (f"not json {PLANTED_SECRET}", json.dumps([PLANTED_SECRET])):
        result, out = run_origin_token(tmp_path, bad)
        assert result.returncode != 0
        assert PLANTED_SECRET not in result.stdout + result.stderr
        assert not out.exists()


def test_failed_cloudfront_listing_is_reported_and_keeps_budgets(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    sandbox.state_file.unlink()  # only the listing can find the distribution now
    result = sandbox.run("teardown.sh", "--yes", FAKE_AWS_FAIL_ON="cloudfront list-distributions")
    assert result.returncode == 1
    assert "CloudFront distributions (could not list)" in result.stderr
    assert sorted(sandbox.world()["budgets"]) == ["jobmatch-10", "jobmatch-25"]
    retry = sandbox.run("teardown.sh", "--yes")
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert remaining(sandbox.world()) == {}
    assert zone_records(sandbox.world()) == NTRYE_RECORDS


@pytest.mark.parametrize(
    ("name", "rtype", "allowed"),
    [
        (FQDN, "A", True),
        (FQDN, "AAAA", True),
        ("_0f3a9c.jobmatch.ntrye.com.", "CNAME", True),
        (FQDN, "CNAME", False),
        (FQDN, "TXT", False),
        ("ntrye.com.", "A", False),
        ("www.ntrye.com.", "CNAME", False),
        ("resend._domainkey.ntrye.com.", "TXT", False),
        ("_0f3a9c.ntrye.com.", "CNAME", False),
        ("_a.b.jobmatch.ntrye.com.", "CNAME", False),
        ("_0f3a9cxjobmatch.ntrye.com.", "CNAME", False),
        ("_0f3a9c.jobmatch.ntrye.com.evil.", "CNAME", False),
        ("_0f3a9c.jobmatch.ntrye.com.", "TXT", False),
        ("jobmatch.ntrye.com", "A", False),  # not fully qualified
    ],
)
def test_record_guard_allows_only_the_jobmatch_records(
    name: str, rtype: str, allowed: bool
) -> None:
    script = f'source "{INFRA}/config.sh"; source "{INFRA}/lib.sh"; record_allowed "$1" "$2"'
    result = subprocess.run(  # noqa: S603
        [BASH, "-c", script, "guard", name, rtype], capture_output=True, text=True, check=False
    )
    assert (result.returncode == 0) is allowed
