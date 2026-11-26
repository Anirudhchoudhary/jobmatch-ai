"""Versioned prompt files loaded from ``<backend root>/prompts/<name>.md``."""

import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

# app/llm/prompts.py -> backend root (``/app`` in the image, next to ``app/``).
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str
    version: str  # first 8 hex chars of sha256(text)


@cache
def load_prompt(name: str) -> Prompt:
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return Prompt(name, text, hashlib.sha256(text.encode()).hexdigest()[:8])
