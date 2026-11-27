"""PII guard: redacts emails, phones and street addresses using the shared PII engine.

Bare locations (a city in "based in London?") are left alone: they are job-relevant.
"""

import asyncio
import time

from app.core.pii import find_pii, redact_pii
from app.guardrails.base import GuardContext, GuardrailResult


class PiiGuard:
    name = "pii"
    provider = "self"

    @staticmethod
    def _scan(texts: tuple[str, ...]) -> tuple[set[str], tuple[str, ...]]:
        kinds: set[str] = set()
        for text in texts:
            kinds.update(span.kind for span in find_pii(text, "street"))
        redacted = tuple(redact_pii(t, "street") for t in texts) if kinds else texts
        return kinds, redacted

    async def check(self, ctx: GuardContext) -> GuardrailResult:
        start = time.perf_counter()
        # Presidio is CPU-bound; keep it off the event loop.
        kinds, redacted = await asyncio.to_thread(self._scan, ctx.texts)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if not kinds:
            return GuardrailResult(self.name, self.provider, "allow", "", latency_ms)
        return GuardrailResult(
            self.name,
            self.provider,
            "redact",
            "pii:" + ",".join(sorted(kinds)),
            latency_ms,
            redacted=redacted,
        )
