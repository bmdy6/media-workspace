"""media/web/backend/api/chat_router.py - 对话意图识别与方案卡片生成。"""
from __future__ import annotations

import json
import base64
from typing import Literal, Optional
from sqlalchemy import func, select
from sqlalchemy.orm import aliased
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from media.agent.chat_agent import chat_agent
from media.agent.card_schema import AgentChatResponse
from media.database import session_scope, ChatMessage, ImageTask, User
from media.core.runtime_config import RuntimeConfigError, get_model, get_runtime_config
from media.core import image_jobs
from media.references.search import get_reference_by_id
from ..auth import get_current_user
from .chat_history import (
    context_message, history_context_limit, restored_card, session_list_limit,
    visual_context_state, visual_context_turn_limit,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatInput(BaseModel):
    session_id: str
    message: str
    current_image_url: Optional[str] = None
    current_image_role: Optional[Literal["reference", "base"]] = None
    current_image_task_id: Optional[int] = None
    resume_after_reference: bool = False
    model_id: Optional[str] = None
    image_model_id: Optional[str] = None
    thinking_mode: Optional[str] = None
    reference_case_id: Optional[str] = Field(default=None, max_length=128)


class ProposalCardVisualInputUpdate(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    card_id: str = Field(min_length=1, max_length=128)
    visual_input_url: Optional[str] = Field(default=None, max_length=2048)
    visual_input_role: Optional[Literal["reference"]] = None


def _validate_chat_input(req: ChatInput) -> tuple[str, str, str, str, str]:
    session_id = req.session_id.strip()
    user_query = req.message.strip()
    limits = get_runtime_config()["request"]
    if not session_id or len(session_id) > 64:
        raise HTTPException(status_code=422, detail="session_id must contain 1 to 64 characters")
    if not user_query or len(user_query) > limits["chat_max_input_chars"]:
        raise HTTPException(status_code=422, detail="message is empty or exceeds the configured length limit")
    if req.model_id is not None and not req.model_id.strip():
        raise HTTPException(status_code=422, detail="model_id must not be empty")
    if req.image_model_id is not None and not req.image_model_id.strip():
        raise HTTPException(status_code=422, detail="image_model_id must not be empty")
    try:
        chat_model = get_model(req.model_id, "chat")
        image_model = get_model(req.image_model_id, "image")
        thinking_mode = (
            req.thinking_mode if req.thinking_mode is not None
            else get_runtime_config()["defaults"].get("thinking_mode")
        )
        if thinking_mode not in chat_model.get("thinking_modes", []):
            raise RuntimeConfigError(f"Thinking mode is not supported by {chat_model['id']}: {thinking_mode}")
    except RuntimeConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if req.reference_case_id and get_reference_by_id(req.reference_case_id) is None:
        raise HTTPException(status_code=422, detail="指定的参考案例不存在，请检查案例 ID 后重试。")
    return session_id, user_query, chat_model["id"], image_model["id"], thinking_mode


def _load_history_and_save_user(
    session_id: str, user_query: str, req: ChatInput, user: User,
) -> tuple[list[dict[str, str]], dict | None]:
    with session_scope() as db:
        all_records = (
            db.query(ChatMessage)
            .filter(ChatMessage.session_id == session_id, ChatMessage.user_id == user.id)
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            .all()
        )
        history_records = all_records[-history_context_limit():]
        history_messages = [context_message(record) for record in history_records]
        existing_context = visual_context_state(all_records)
        explicit_context = None
        explicit_url = req.current_image_url
        if req.current_image_role == "base" and req.current_image_task_id is not None:
            owned = db.query(ImageTask).filter(ImageTask.id == req.current_image_task_id, ImageTask.user_id == user.id).first()
            if not owned:
                raise HTTPException(404, "修改底图不属于当前用户")
            explicit_url = owned.image_url or f"/api/image/result/{owned.task_id}"
        if explicit_url and req.current_image_role in {"reference", "base"}:
            explicit_context = {
                "url": explicit_url,
                "role": req.current_image_role,
                "image_task_id": req.current_image_task_id,
                "remaining_turns": visual_context_turn_limit(),
                "turn_limit": visual_context_turn_limit(),
            }
        effective_context = explicit_context or existing_context
        if effective_context and effective_context["url"].startswith("/api/image/result/"):
            # Resolve only the current user's protected result. The data URL is
            # used for this provider request, never persisted in cards/history.
            task_id = effective_context["url"].removeprefix("/api/image/result/")
            try:
                result = image_jobs.result_file(task_id, user.id)
            except TimeoutError:
                raise HTTPException(410, "修改底图已过期，请选择仍可用的作品") from None
            if result is None:
                raise HTTPException(404, "修改底图暂不可读取")
            path, mime = result
            limit = get_runtime_config()["request"]["reference_image_max_bytes"]
            with path.open("rb") as source:
                content = source.read(limit + 1)
            if len(content) > limit or mime not in {"image/png", "image/jpeg", "image/webp"}:
                raise HTTPException(422, "修改底图超过限制或格式不受支持")
            effective_context = {**effective_context, "_provider_image_url": f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"}
        is_new_activation = bool(explicit_context and (
            existing_context is None
            or existing_context["url"] != explicit_context["url"]
            or existing_context["role"] != explicit_context["role"]
        ))
        db.add(ChatMessage(
            session_id=session_id,
            user_id=user.id,
            role="system" if req.resume_after_reference else "user",
            content=user_query,
            image_url=explicit_context["url"] if is_new_activation else None,
            image_role=explicit_context["role"] if is_new_activation else None,
            image_task_id=req.current_image_task_id if is_new_activation else None,
        ))
    return history_messages, effective_context


def _current_visual_context(session_id: str, user: User) -> dict | None:
    with session_scope() as db:
        records = (
            db.query(ChatMessage)
            .filter(ChatMessage.session_id == session_id, ChatMessage.user_id == user.id)
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            .all()
        )
        return visual_context_state(records)


def _persist_assistant_response(session_id: str, req: ChatInput, response: AgentChatResponse, user: User) -> dict | None:
    card_json_str = response.proposal_card.model_dump_json() if response.proposal_card else None
    with session_scope() as db:
        db.add(ChatMessage(
            session_id=session_id,
            user_id=user.id,
            role="assistant",
            content=response.reply_text,
            card_data=card_json_str,
            model_id=response.model_id,
            requested_model_id=response.requested_model_id,
            fallback_from_model_id=response.fallback_from_model_id,
            fallback_reason=response.fallback_reason,
            thinking_mode=response.thinking_mode,
            reference_case_id=req.reference_case_id,
        ))
    return _current_visual_context(session_id, user)


@router.post("/message", response_model=AgentChatResponse)
def send_chat_message(req: ChatInput, user: User = Depends(get_current_user)):
    """用户向 AI 发送生图需求，AI 构思并返回方案卡片"""
    session_id, user_query, chat_model_id, image_model_id, thinking_mode = _validate_chat_input(req)
    history_messages, visual_context = _load_history_and_save_user(session_id, user_query, req, user)

    # 2. 调 Agent 思考生成
    response = chat_agent.chat(
        messages=history_messages,
        user_query=user_query,
        current_image_url=visual_context["url"] if visual_context else None,
        current_image_role=visual_context["role"] if visual_context else None,
        provider_image_url=visual_context.get("_provider_image_url") if visual_context else None,
        model_id=chat_model_id,
        image_model_id=image_model_id,
        thinking_mode=thinking_mode,
        reference_case_id=req.reference_case_id,
    )

    current_context = _persist_assistant_response(session_id, req, response, user)
    return AgentChatResponse.model_validate({**response.model_dump(), "visual_context": current_context})


@router.post("/stream")
def stream_chat_message(req: ChatInput, user: User = Depends(get_current_user)):
    """Stream visible assistant text and emit the parsed card exactly once at completion."""
    session_id, user_query, chat_model_id, image_model_id, thinking_mode = _validate_chat_input(req)
    history_messages, visual_context = _load_history_and_save_user(session_id, user_query, req, user)
    agent_events = chat_agent.stream_chat(
        messages=history_messages,
        user_query=user_query,
        current_image_url=visual_context["url"] if visual_context else None,
        current_image_role=visual_context["role"] if visual_context else None,
        provider_image_url=visual_context.get("_provider_image_url") if visual_context else None,
        model_id=chat_model_id,
        image_model_id=image_model_id,
        thinking_mode=thinking_mode,
        reference_case_id=req.reference_case_id,
    )

    async def event_stream():
        try:
            async for event, payload in iterate_in_threadpool(agent_events):
                if event == "done":
                    current_context = _persist_assistant_response(session_id, req, payload, user)
                    payload = payload.model_dump(mode="json")
                    payload["visual_context"] = current_context
                data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                yield f"event: {event}\ndata: {data}\n\n"
        except Exception:
            data = json.dumps({"message": "回复生成失败，请稍后重试。"}, ensure_ascii=False)
            yield f"event: error\ndata: {data}\n\n"
        finally:
            close = getattr(agent_events, "close", None)
            if close:
                await run_in_threadpool(close)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.patch("/proposal-card/visual-input")
def update_proposal_card_visual_input(
    req: ProposalCardVisualInputUpdate,
    user: User = Depends(get_current_user),
):
    """Persist a card-scoped optional user reference without clearing session-wide context."""
    session_id = req.session_id.strip()
    card_id = req.card_id.strip()
    if bool(req.visual_input_url) != bool(req.visual_input_role):
        raise HTTPException(status_code=422, detail="visual_input_url and visual_input_role must be set or cleared together")

    with session_scope() as db:
        if req.visual_input_url:
            owned_reference = (
                db.query(ChatMessage.id)
                .filter(
                    ChatMessage.session_id == session_id,
                    ChatMessage.user_id == user.id,
                    ChatMessage.image_role.in_(["reference", "reference_candidate"]),
                    ChatMessage.image_url == req.visual_input_url,
                )
                .first()
            )
            if not owned_reference:
                raise HTTPException(status_code=422, detail="参考图不属于当前会话")

        candidates = (
            db.query(ChatMessage)
            .filter(
                ChatMessage.session_id == session_id,
                ChatMessage.user_id == user.id,
                ChatMessage.role == "assistant",
                ChatMessage.card_data.is_not(None),
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .all()
        )
        target = None
        target_card = None
        for record in candidates:
            card, error = restored_card(record.card_data)
            if not error and card and card.get("card_id") == card_id:
                target = record
                target_card = card
                break
        if target is None or target_card is None:
            raise HTTPException(status_code=404, detail="方案卡不存在")
        submitted_task = db.query(ImageTask.id).filter(
            ImageTask.user_id == user.id,
            ImageTask.session_id == session_id,
            ImageTask.proposal_card_id == card_id,
        ).first()
        if target_card.get("image_task_id") is not None or submitted_task is not None:
            raise HTTPException(status_code=409, detail="任务已提交，方案卡参考图不可修改")

        target_card["visual_input_url"] = req.visual_input_url
        target_card["visual_input_role"] = req.visual_input_role
        target_card["base_image_url"] = None
        target.card_data = json.dumps(target_card, ensure_ascii=False, separators=(",", ":"))
        db.add(target)

    return {"card": target_card}


@router.get("/history")
def get_chat_history(session_id: str, user: User = Depends(get_current_user)):
    """获取指定会话的历史聊天记录"""
    with session_scope() as db:
        records = (
            db.query(ChatMessage)
            .filter(ChatMessage.session_id == session_id, ChatMessage.user_id == user.id)
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            .all()
        )
        task_ids = {r.image_task_id for r in records if r.image_task_id is not None}
        image_tasks = {}
        if task_ids:
            image_tasks = {
                task.id: task
                for task in db.query(ImageTask).filter(
                    ImageTask.id.in_(task_ids), ImageTask.user_id == user.id
                ).all()
            }
        results = []
        for r in records:
            card_obj, card_error = restored_card(r.card_data)
            image_task = image_tasks.get(r.image_task_id)
            message = {
                "id": r.id,
                "role": r.role,
                "content": r.content,
                "card_data": card_obj,
                "image_url": r.image_url,
                "image_role": r.image_role,
                "image_task_id": r.image_task_id,
                "model_id": r.model_id,
                "requested_model_id": r.requested_model_id,
                "fallback_from_model_id": r.fallback_from_model_id,
                "fallback_reason": r.fallback_reason,
                "thinking_mode": r.thinking_mode,
                "reference_case_id": r.reference_case_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "image_duration_ms": image_task.duration_ms if image_task else None,
                "image_model_id": image_task.model_id if image_task else None,
                "image_aspect_ratio": image_task.aspect_ratio if image_task else None,
                "image_quality": image_task.quality if image_task else None,
                "persistence_status": image_task.persistence_status if image_task else None,
            }
            if card_error:
                message["card_error"] = card_error
            results.append(message)
        return {"messages": results, "visual_context": visual_context_state(records)}


@router.get("/sessions")
def get_chat_sessions(user: User = Depends(get_current_user)):
    """List the current user's latest sessions, derived from persisted messages."""
    first_user_message = aliased(ChatMessage)
    last_image_message = aliased(ChatMessage)
    title_query = (
        select(first_user_message.content)
        .where(
            first_user_message.user_id == user.id,
            first_user_message.session_id == ChatMessage.session_id,
            first_user_message.role == "user",
        )
        .order_by(first_user_message.created_at.asc(), first_user_message.id.asc())
        .limit(1)
        .scalar_subquery()
    )
    image_query = (
        select(last_image_message.image_url)
        .where(
            last_image_message.user_id == user.id,
            last_image_message.session_id == ChatMessage.session_id,
            last_image_message.role == "assistant",
            last_image_message.image_url.is_not(None),
        )
        .order_by(last_image_message.created_at.desc(), last_image_message.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    with session_scope() as db:
        rows = (
            db.query(
                ChatMessage.session_id,
                func.max(ChatMessage.created_at).label("updated_at"),
                func.count(ChatMessage.id).label("message_count"),
                title_query.label("title"),
                image_query.label("last_image_url"),
            )
            .filter(ChatMessage.user_id == user.id)
            .group_by(ChatMessage.session_id)
            .order_by(func.max(ChatMessage.created_at).desc(), ChatMessage.session_id.asc())
            .limit(session_list_limit())
            .all()
        )
        return {
            "sessions": [
                {
                    "session_id": row.session_id,
                    "title": (row.title or "新会话")[:80],
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    "message_count": row.message_count,
                    "last_image_url": row.last_image_url,
                }
                for row in rows
            ]
        }
