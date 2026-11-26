"""PII guard: redacts emails, phones and addresses using the shared PII engine."""

import time

from app.core.pii import find_pii, redact_pii
from app.guardrails.base import GuardContext, GuardrailResult


class PiiGuard:
    name = "pii"
    provider = "self"

    async def check(self, ctx: GuardContext) -> GuardrailResult:
        start = time.perf_counter()
        kinds: set[str] = set()
        for text in ctx.texts:
            kinds.update(span.kind for span in find_pii(text))
        latency_ms = int((time.perf_counter() - start) * 1000)
        if not kinds:
            return GuardrailResult(self.name, self.provider, "allow", "", latency_ms)
        return GuardrailResult(
            self.name,
            self.provider,
            "redact",
            "pii:" + ",".join(sorted(kinds)),
            latency_ms,
            redacted=tuple(redact_pii(t) for t in ctx.texts),
        )
