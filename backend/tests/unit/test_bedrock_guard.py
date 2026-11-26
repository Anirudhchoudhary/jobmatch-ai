from typing import Any, Literal

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from app.guardrails.base import GuardContext
from app.guardrails.bedrock import BedrockGuard

GID = "gr-abc123"


def _client() -> Any:
    return boto3.client(
        "bedrock-runtime",
        region_name="us-west-2",
        aws_access_key_id="x",
        aws_secret_access_key="x",
        config=Config(retries={"max_attempts": 1}),
    )


def _ctx(*texts: str) -> GuardContext:
    return GuardContext(stage="chat_input", texts=texts, request_id="r", user_id=None)


def _params(text: str, source: str = "INPUT") -> dict[str, Any]:
    return {
        "guardrailIdentifier": GID,
        "guardrailVersion": "3",
        "source": source,
        "content": [{"text": {"text": text}}],
        "outputScope": "INTERVENTIONS",
    }


def _resp(action: str, assessments: list[dict[str, Any]] | None = None, out: str = "") -> Any:
    return {
        "usage": {
            "topicPolicyUnits": 0,
            "contentPolicyUnits": 1,
            "wordPolicyUnits": 0,
            "sensitiveInformationPolicyUnits": 0,
            "sensitiveInformationPolicyFreeUnits": 0,
            "contextualGroundingPolicyUnits": 0,
        },
        "action": action,
        "outputs": [{"text": out}] if out else [],
        "assessments": assessments or [],
    }


ATTACK = {
    "contentPolicy": {
        "filters": [
            {
                "type": "PROMPT_ATTACK",
                "confidence": "HIGH",
                "action": "BLOCKED",
                "detected": True,
            }
        ]
    }
}
PII = {
    "sensitiveInformationPolicy": {
        "regexes": [],
        "piiEntities": [
            {"match": "a@b.com", "type": "EMAIL", "action": "ANONYMIZED", "detected": True}
        ],
    }
}


def _guard(client: Any, source: Literal["INPUT", "OUTPUT"] = "INPUT") -> BedrockGuard:
    return BedrockGuard(client, GID, "3", source=source)


async def test_prompt_attack_blocks_item() -> None:
    client = _client()
    with Stubber(client) as stub:
        stub.add_response("apply_guardrail", _resp("NONE"), _params("hello"))
        stub.add_response(
            "apply_guardrail", _resp("GUARDRAIL_INTERVENED", [ATTACK], "blocked"), _params("evil")
        )
        res = await _guard(client).check(_ctx("hello", "evil"))
        stub.assert_no_pending_responses()
    assert (res.action, res.reason, res.items) == ("block", "bedrock:prompt_attack", (1,))
    assert (res.guardrail, res.provider) == ("bedrock_guardrails", "bedrock")
    assert res.redacted is None


async def test_pii_redacts_with_replacement_text() -> None:
    client = _client()
    with Stubber(client) as stub:
        stub.add_response("apply_guardrail", _resp("NONE"), _params("clean"))
        stub.add_response(
            "apply_guardrail",
            _resp("GUARDRAIL_INTERVENED", [PII], "mail {EMAIL}"),
            _params("mail a@b.com"),
        )
        res = await _guard(client).check(_ctx("clean", "mail a@b.com"))
    assert (res.action, res.reason, res.items) == ("redact", "bedrock:pii", (1,))
    assert res.redacted == ("clean", "mail {EMAIL}")


async def test_block_wins_over_redact() -> None:
    client = _client()
    with Stubber(client) as stub:
        stub.add_response(
            "apply_guardrail", _resp("GUARDRAIL_INTERVENED", [PII], "x {EMAIL}"), _params("a")
        )
        stub.add_response(
            "apply_guardrail", _resp("GUARDRAIL_INTERVENED", [ATTACK], "b"), _params("b")
        )
        res = await _guard(client).check(_ctx("a", "b"))
    assert (res.action, res.items, res.redacted) == ("block", (1,), None)


async def test_none_allows_and_sends_pinned_params() -> None:
    client = _client()
    with Stubber(client) as stub:
        stub.add_response("apply_guardrail", _resp("NONE"), _params("hi", "OUTPUT"))
        res = await _guard(client, "OUTPUT").check(_ctx("hi"))
    assert (res.action, res.reason) == ("allow", "")


async def test_undetected_entities_are_ignored() -> None:
    quiet = {
        "contentPolicy": {
            "filters": [
                {"type": "PROMPT_ATTACK", "confidence": "NONE", "action": "NONE", "detected": False}
            ]
        }
    }
    client = _client()
    with Stubber(client) as stub:
        stub.add_response("apply_guardrail", _resp("GUARDRAIL_INTERVENED", [quiet]), _params("hi"))
        res = await _guard(client).check(_ctx("hi"))
    assert res.action == "allow"


async def test_empty_texts_make_no_calls() -> None:
    client = _client()
    with Stubber(client) as stub:
        res = await _guard(client).check(_ctx())
        stub.assert_no_pending_responses()
    assert res.action == "allow"


async def test_client_error_propagates() -> None:
    client = _client()
    with Stubber(client) as stub:
        stub.add_client_error("apply_guardrail", "ThrottlingException")
        with pytest.raises(ClientError):
            await _guard(client).check(_ctx("hi"))
