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
BUDGET_EMAIL = "budget-alerts-PLANTED@example.com"
PLANTED_SECRET = "sk-ant-PLANTED-never-print-me"

BASH = shutil.which("bash") or "/bin/bash"
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None, reason="needs bash and jq"
)

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "docker $*" >> "$FAKE_DOCKER_LOG"
if [[ "$1" == login ]]; then cat > "$FAKE_DOCKER_STDIN"; fi
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
    for exe in (aws, docker):
        exe.chmod(0o755)
    # The secret exists already (created by `make secrets-set`).
    (tmp_path / "world.json").write_text(json.dumps({"secrets": ["jobmatch/prod"]}))
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_AWS_LOG": str(tmp_path / "aws.log"),
        "FAKE_AWS_WORLD": str(tmp_path / "world.json"),
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAKE_DOCKER_STDIN": str(tmp_path / "docker.stdin"),
        "STATE_FILE": str(tmp_path / "state" / "state.json"),
        "BUDGET_EMAIL": BUDGET_EMAIL,
        "DEPLOY_RETRY_SLEEP": "0",
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


def remaining(world: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in world.items() if k != "counters" and v}


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
    return out


def test_deploy_creates_the_stack_and_prints_url(
    sandbox: Sandbox, deployed: subprocess.CompletedProcess[str]
) -> None:
    result = deployed
    world = sandbox.world()
    assert world["ecr"] == ["jobmatch-api"]
    assert list(world["buckets"]) == [BUCKET]
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
    ],
)
def test_deploy_records_state_before_failure(sandbox: Sandbox, fail_on: str) -> None:
    result = sandbox.run("deploy.sh", FAKE_AWS_FAIL_ON=fail_on)
    assert result.returncode != 0
    world = sandbox.world()
    assert created_resources(world), "the failure point should come after some creates"
    assert set(sandbox.state()) == created_resources(world)

    # ...and teardown removes all of it.
    teardown = sandbox.run("teardown.sh", "--yes")
    assert teardown.returncode == 0, teardown.stdout + teardown.stderr
    assert remaining(sandbox.world()) == {}


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
    assert list(sandbox.world()["buckets"]) == [BUCKET]


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
    }
    for argv in deploy_calls:
        cmd = argv[1]
        if cmd.startswith("create-") or cmd in ("put-role-policy", "add-permission"):
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
        first("scheduler", "delete-schedule"),
        first("iam", "delete-role", "jobmatch-scheduler"),
        first("lambda", "delete-function", "jobmatch-cleanup"),
        first("lambda", "delete-function-url-config"),
        first("lambda", "remove-permission"),
        first("lambda", "delete-function", "jobmatch-api"),
        first("iam", "delete-role-policy", "jobmatch-lambda"),
        first("iam", "delete-role", "jobmatch-lambda"),
        first("logs", "delete-log-group"),
        first("s3api", "delete-objects"),
        first("s3api", "delete-bucket"),
        first("ecr", "delete-repository"),
        first("secretsmanager", "delete-secret"),
        first("budgets", "delete-budget"),  # last, once nothing billable is left
    ]
    assert expected_order == sorted(expected_order)
    assert first("iam", "delete-role-policy", "jobmatch-scheduler") < expected_order[1]


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
        if argv[1] in CLI_CUSTOMIZATIONS:
            continue
        names = {a for a in argv[2:] if a.startswith("--")} - GLOBAL_FLAGS
        seen.setdefault((argv[0], argv[1]), set()).update(names)
    assert len(seen) > 30
    for (svc, cmd), names in sorted(seen.items()):
        params = api_parameters(svc, cmd)
        for name in names:
            bare = name.removeprefix("--no-") if name.startswith("--no-") else name
            param = bare.removeprefix("--").replace("-", "")
            assert param in params, f"aws {svc} {cmd} has no {name}"
