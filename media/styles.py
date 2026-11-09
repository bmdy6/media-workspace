"""Prompt composition using optional style hints from media resources."""
from __future__ import annotations

import json
from pathlib import Path


STYLE_HINTS_PATH = Path(__file__).resolve().parent / "prompts" / "style_hints.json"
STYLE_HINTS: dict[str, str] = json.loads(STYLE_HINTS_PATH.read_text(encoding="utf-8"))
QUALITY_HINTS_PATH = Path(__file__).resolve().parent / "prompts" / "quality_hints.json"
QUALITY_HINTS: dict[str, str] = json.loads(QUALITY_HINTS_PATH.read_text(encoding="utf-8"))


def wrap_prompt(style_id: str, raw_prompt: str, quality: str) -> str:
    """Keep the complete user prompt intact and append nonbinding hints."""
    parts = [raw_prompt]
    style_hint = STYLE_HINTS.get(style_id)
    if style_hint:
        parts.append(style_hint)

    quality_hint = QUALITY_HINTS.get(quality)
    if quality_hint:
        parts.append(quality_hint)

    return "\n\n".join(parts)
