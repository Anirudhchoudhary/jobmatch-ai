import boto3
import pytest
from botocore.stub import Stubber

from app.core.secrets import SecretsError, load_secret


def _client_with(
    secret_string: str | None = None, error_code: str | None = None
) -> tuple[Stubber, object]:
    client = boto3.client(
        "secretsmanager",
        region_name="us-west-2",
        aws_access_key_id="fake",
        aws_secret_access_key="fake",
    )
    stubber = Stubber(client)
    if error_code:
        stubber.add_client_error("get_secret_value", service_error_code=error_code)
    else:
        stubber.add_response("get_secret_value", {"SecretString": secret_string})
    stubber.activate()
    return stubber, client


def test_load_secret_parses_json() -> None:
    _, client = _client_with('{"DATABASE_URL":"postgresql+psycopg://u:p@h/db","N":5}')
    result = load_secret("jobmatch/dev", region="us-west-2", client=client)  # type: ignore[arg-type]
    assert result == {"DATABASE_URL": "postgresql+psycopg://u:p@h/db", "N": "5"}


def test_load_secret_not_found_names_secret() -> None:
    _, client = _client_with(error_code="ResourceNotFoundException")
    with pytest.raises(SecretsError) as exc:
        load_secret("jobmatch/dev", region="us-west-2", client=client)  # type: ignore[arg-type]
    assert "jobmatch/dev" in str(exc.value)
    assert "ResourceNotFoundException" in str(exc.value)


def test_load_secret_invalid_json_hides_value() -> None:
    _, client = _client_with("not-json fake-value-do-not-leak")
    with pytest.raises(SecretsError) as exc:
        load_secret("jobmatch/dev", region="us-west-2", client=client)  # type: ignore[arg-type]
    assert "fake-value-do-not-leak" not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


def test_load_secret_non_object() -> None:
    _, client = _client_with('["a"]')
    with pytest.raises(SecretsError):
        load_secret("jobmatch/dev", region="us-west-2", client=client)  # type: ignore[arg-type]
