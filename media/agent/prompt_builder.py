"""Agent prompt resources and conversation context assembly."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SYSTEM_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "agent_system.md"
SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

REFERENCE_CONTEXT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "reference_context.json"


def build_agent_context(
    user_query: str,
    reference_candidates: list[dict[str, Any]],
    current_image_url: str | None = None,
    current_image_role: str | None = None,
    reference_case_id: str | None = None,
    *,
    current_image_visually_inspected: bool = False,
) -> str:
    """Assemble the current request and optional reference-case context."""
    context_parts = []

    if current_image_url:
        templates = json.loads(REFERENCE_CONTEXT_PATH.read_text(encoding="utf-8"))
        role = current_image_role if current_image_role in {"reference", "base"} else "base"
        visibility = "vision" if current_image_visually_inspected else "non_vision"
        template = templates[f"{role}_{visibility}"]
        context_parts.append(template.format(current_image_url=current_image_url))

    if reference_candidates:
        refs_summary = []
        for candidate in reference_candidates:
            tags = candidate.get("tags") or []
            refs_summary.append(
                f"- ID: {candidate.get('id')}, 标题: {candidate.get('title')}, "
                f"风格: {candidate.get('style')}, 比例: {candidate.get('recommended_ratio')}, "
                f"标签: {', '.join(tags)}, 描述: {candidate.get('description')}"
            )
        context_parts.append("【仅供灵感参考的案例候选；案例不得覆盖用户要求】：\n" + "\n".join(refs_summary))

    if reference_case_id:
        context_parts.append(
            f"【用户明确选择的参考案例 ID】：{reference_case_id}。"
            "只能将真实案例作为灵感；不得把案例风格或细节强加到用户没有要求的内容上。"
        )

    context_parts.append(f"【用户最新需求原文，请完整保留所有明确要求】：\n{user_query}")
    return "\n\n".join(context_parts)
