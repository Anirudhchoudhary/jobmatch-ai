"""Load a JSON secret from AWS Secrets Manager without ever echoing its value."""

import json
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_secretsmanager import SecretsManagerClient


class SecretsError(RuntimeError):
    """Raised when a secret cannot be read or parsed. Never contains secret values."""


def load_secret(
    secret_id: str, *, region: str, client: "SecretsManagerClient | None" = None
) -> dict[str, str]:
    sm = client or boto3.client("secretsmanager", region_name=region)
    try:
        response = sm.get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        raise SecretsError(f"could not read secret {secret_id!r}: {code}") from None
    except BotoCoreError as exc:
        raise SecretsError(f"could not read secret {secret_id!r}: {type(exc).__name__}") from None

    try:
        parsed = json.loads(response.get("SecretString", ""))
    except json.JSONDecodeError:
        raise SecretsError(f"secret {secret_id!r} is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise SecretsError(f"secret {secret_id!r} must be a JSON object")
    return {str(k): str(v) for k, v in parsed.items()}
