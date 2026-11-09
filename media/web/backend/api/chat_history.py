"""Bounded, user-scoped chat history helpers shared by chat endpoints."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from media.database import ChatMessage


_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "chat_history.json"
_MAX_CONTEXT_LIMIT = 100
_MAX_VISUAL_CONTEXT_TURN_LIMIT = 50
_MAX_SESSION_LIST_LIMIT = 500


def _read_limits() -> dict[str, int]:
    try:
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        context_limit = int(raw["history_context_limit"])
        visual_context_turn_limit = int(raw["visual_context_turn_limit"])
        session_limit = int(raw["session_list_limit"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid chat history resource config: {_CONFIG_PATH}") from exc
    if not 1 <= context_limit <= _MAX_CONTEXT_LIMIT:
        raise RuntimeError("history_context_limit must be between 1 and 100")
    if not 1 <= visual_context_turn_limit <= _MAX_VISUAL_CONTEXT_TURN_LIMIT:
        raise RuntimeError("visual_context_turn_limit must be between 1 and 50")
    if not 1 <= session_limit <= _MAX_SESSION_LIST_LIMIT:
        raise RuntimeError("session_list_limit must be between 1 and 500")
    return {"context": context_limit, "visual_context": visual_context_turn_limit, "sessions": session_limit}


def history_context_limit() -> int:
    """Read context size from the resource file, with a validated env override."""
    configured = _read_limits()["context"]
    raw_override = os.environ.get("MEDIA_CHAT_HISTORY_LIMIT")
    if raw_override is None:
        return configured
    try:
        override = int(raw_override)
    except ValueError:
        return configured
    return override if 1 <= override <= _MAX_CONTEXT_LIMIT else configured


def visual_context_turn_limit() -> int:
    """Return how many user turns a reference/base remains active."""
    configured = _read_limits()["visual_context"]
    raw_override = os.environ.get("MEDIA_VISUAL_CONTEXT_TURN_LIMIT")
    if raw_override is None:
        return configured
    try:
        override = int(raw_override)
    except ValueError:
        return configured
    return override if 1 <= override <= _MAX_VISUAL_CONTEXT_TURN_LIMIT else configured


def visual_context_state(records: list[ChatMessage], *, turn_limit: int | None = None) -> dict[str, Any] | None:
    """Derive the active single-image visual context from chronological session records.

    Upload/system activations start with the full turn budget. A user message that
    explicitly activates a base/reference consumes the first turn immediately.
    Clear markers and exhausted budgets remove the context.
    """
    limit = turn_limit or visual_context_turn_limit()
    active: dict[str, Any] | None = None
    used_turns = 0
    for record in records:
        if record.image_role == "visual_input_cleared":
            active = None
            used_turns = 0
            continue
        if record.image_role in {"reference", "base"} and record.image_url:
            active = {
                "url": record.image_url,
                "role": record.image_role,
                "image_task_id": record.image_task_id,
            }
            used_turns = 1 if record.role == "user" else 0
            continue
        if active is not None and record.role == "user":
            used_turns += 1

    if active is None:
        return None
    remaining = limit - used_turns
    if remaining <= 0:
        return None
    return {
        **active,
        "remaining_turns": remaining,
        "turn_limit": limit,
    }


def session_list_limit() -> int:
    return _read_limits()["sessions"]


def context_message(record: ChatMessage) -> dict[str, str]:
    """Represent saved text, card requirements, and image URLs without visual claims."""
    content = record.content
    if record.card_data:
        try:
            card = json.loads(record.card_data)
            if isinstance(card, dict):
                card = card.get("card", card)
                if isinstance(card, dict):
                    details = []
                    for label, key in (("方案提示词", "prompt"), ("方案要求", "design_rationale")):
                        value = card.get(key)
                        if isinstance(value, str) and value.strip():
                            details.append(f"{label}：{value.strip()}")
                    if details:
                        content += "\n" + "\n".join(details)
        except (TypeError, json.JSONDecodeError):
            pass
    if record.image_url:
        if record.image_role == "reference":
            label = "用户参考图 URL"
        elif record.image_role == "base":
            label = "修改底图 URL"
        else:
            label = "参考图 URL" if record.role == "user" else "生成图片 URL"
        content += f"\n{label}：{record.image_url}（仅记录链接，未读取图像内容）"
    return {"role": record.role, "content": content}


def restored_card(card_data: str | None) -> tuple[Any | None, str | None]:
    if not card_data:
        return None, None
    try:
        card = json.loads(card_data)
    except (TypeError, json.JSONDecodeError):
        return None, "方案卡片数据格式无效"
    if not isinstance(card, dict):
        return None, "方案卡片数据格式无效"
    return card, None
