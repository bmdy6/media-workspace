"""media/references/search.py - 参考案例轻量检索。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LIBRARY_PATH = Path(__file__).resolve().parent / "library.json"


def load_references() -> list[dict[str, Any]]:
    if not LIBRARY_PATH.exists():
        return []
    try:
        return json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def get_reference_by_id(reference_id: str) -> dict[str, Any] | None:
    """Resolve a selected reference against the authoritative library."""
    if not reference_id:
        return None
    return next(
        (item for item in load_references() if item.get("id") == reference_id),
        None,
    )


def search_references(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """根据自然语言 query 检索最相关的参考案例。"""
    items = load_references()
    if not items:
        return []

    if not query or not query.strip():
        return []

    # Exact reference IDs are explicit selections, even when no descriptive term matches.
    selected_id = query.strip()
    exact_match = next((item for item in items if item.get("id") == selected_id), None)
    if exact_match:
        return [exact_match]

    q_lower = query.lower()
    scored_items: list[tuple[float, dict[str, Any]]] = []

    for item in items:
        score = 0.0
        # 标签匹配 (权重最高)
        for tag in item.get("tags", []):
            if tag.lower() in q_lower or q_lower in tag.lower():
                score += 3.0

        # 标题匹配
        title = item.get("title", "").lower()
        if any(term in title for term in q_lower.split()):
            score += 2.0

        # 描述匹配
        desc = item.get("description", "").lower()
        if any(term in desc for term in q_lower.split()):
            score += 1.0

        # 风格 ID 匹配
        style = item.get("style", "").lower()
        if style in q_lower:
            score += 2.5

        if score > 0:
            scored_items.append((score, item))

    # 按相关度从高到低排序
    scored_items.sort(key=lambda x: x[0], reverse=True)
    results = [item for _, item in scored_items]

    if not results:
        return []

    return results[:limit]
