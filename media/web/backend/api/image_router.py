"""media/web/backend/api/image_router.py - 方案卡片确认生图、参考图上传与历史记录。"""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from media.core.image_service import image_core
from media.core import image_jobs
from media.core.errors import GenerationError
from media.database import session_scope, ImageTask, ChatMessage, User
from media.core.runtime_config import RuntimeConfigError, get_model, get_runtime_config
from media.references.search import load_references
from ..auth import get_current_user
from .chat_history import visual_context_turn_limit

router = APIRouter(prefix="/api/image", tags=["image"])


class ConfirmGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100000)
    final_prompt: Optional[str] = Field(default=None, max_length=100000)
    style: str = "default"
    aspect_ratio: Optional[str] = None
    quality: Optional[str] = None
    ref_image_url: Optional[str] = None
    parent_id: Optional[int] = None
    session_id: Optional[str] = Field(default=None, max_length=64)
    model_id: Optional[str] = None
    storage_policy: Optional[str] = None
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    proposal_card_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


class ImageJobRequest(ConfirmGenerateRequest):
    request_id: str = Field(min_length=1, max_length=128)


@router.post("/generate")
def confirm_and_generate(req: ConfirmGenerateRequest, user: User = Depends(get_current_user)):
    """用户点击方案卡片上的「确认生成」后，真正调用底层模型并转存 COS"""
    try:
        model = get_model(req.model_id, "image")
        runtime = get_runtime_config()
        quality = req.quality or runtime["defaults"]["quality"]
        aspect_ratio = req.aspect_ratio or runtime["defaults"]["aspect_ratio"]
        if quality not in model.get("qualities", []):
            raise RuntimeConfigError(f"Quality is not supported by {model['id']}: {quality}")
        if aspect_ratio not in model.get("aspect_ratios", []):
            raise RuntimeConfigError(f"Aspect ratio is not supported by {model['id']}: {aspect_ratio}")
        storage_policy = req.storage_policy or runtime["defaults"]["synchronous_storage_policy"]
        policy = get_runtime_config()["storage_policies"].get(storage_policy)
        if not policy or not policy.get("enabled"):
            raise RuntimeConfigError(f"Storage policy is not available: {storage_policy}")
        result = image_core.generate(
            prompt=req.prompt,
            style=req.style,
            aspect_ratio=aspect_ratio,
            quality=quality,
            ref_image=req.ref_image_url,
            user_id=user.id,
            parent_id=req.parent_id,
            model_id=model["id"],
            storage_policy=storage_policy,
            request_id=req.request_id,
            final_prompt=req.final_prompt,
        )
    except RuntimeConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 若关联了当前会话，将新生成的图片记录插入消息流
    if req.session_id and result.status == "success":
        with session_scope() as db:
            msg = ChatMessage(
                session_id=req.session_id,
                user_id=user.id,
                role="assistant",
                content=f"已为你生成作品《{req.prompt[:15]}...》",
                image_url=result.image_url,
            )
            db.add(msg)

    return {
        "task_id": result.task_id,
        "status": result.status,
        "image_url": result.image_url,
        "duration_ms": result.duration_ms,
        "final_prompt": result.final_prompt,
        "aspect_ratio": result.aspect_ratio,
        "style": result.style,
        "ref_image_url": result.ref_image_url,
        "parent_id": result.parent_id,
        "error_msg": result.error_msg,
        "model_id": result.model_id,
        "requested_model_id": result.requested_model_id,
        "fallback_from_model_id": result.fallback_from_model_id,
        "fallback_reason": result.fallback_reason,
        "storage_policy": result.storage_policy,
        "request_id": result.request_id,
    }


@router.post("/jobs", status_code=202)
def create_image_job(req: ImageJobRequest, user: User = Depends(get_current_user)):
    try:
        model = get_model(req.model_id, "image")
        runtime = get_runtime_config()
        max_prompt = runtime["image_jobs"]["max_prompt_chars"]
        if not req.prompt.strip() or len(req.prompt) > max_prompt:
            raise RuntimeConfigError("提示词不能为空或超出长度限制")
        if req.final_prompt is not None and (not req.final_prompt.strip() or len(req.final_prompt) > max_prompt):
            raise RuntimeConfigError("确认提示词不能为空或超出长度限制")
        if not req.request_id.strip() or (req.proposal_card_id is not None and not req.proposal_card_id.strip()):
            raise RuntimeConfigError("request_id/proposal_card_id 不能为空")
        if req.proposal_card_id and not (req.session_id and req.session_id.strip()):
            raise RuntimeConfigError("确认方案需要关联有效会话")
        quality = req.quality or runtime["defaults"]["quality"]
        ratio = req.aspect_ratio or runtime["defaults"]["aspect_ratio"]
        if quality not in model.get("qualities", []) or ratio not in model.get("aspect_ratios", []):
            raise RuntimeConfigError("所选模型不支持该画质或画幅")
        storage = req.storage_policy or runtime["defaults"]["storage_policy"]
        if storage not in ("background_cos", "direct", "cos"):
            raise RuntimeConfigError("不支持的图片存储策略")
        policy = runtime["storage_policies"].get(storage)
        if not policy or not policy.get("enabled"):
            raise RuntimeConfigError(f"图片存储策略不可用: {storage}")
        payload = {
            "prompt": req.prompt, "final_prompt": req.final_prompt, "style": req.style,
            "aspect_ratio": ratio, "quality": quality, "ref_image_url": req.ref_image_url,
            "parent_id": req.parent_id, "session_id": req.session_id, "model_id": model["id"],
            "storage_policy": storage, "request_id": req.request_id,
            "proposal_card_id": req.proposal_card_id,
        }
        result, replay = image_jobs.submit(user.id, payload)
        if replay:
            return result
        return result
    except RuntimeConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="关联图片或方案不存在于当前用户的会话") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/jobs/active")
def get_active_image_jobs(session_id: Optional[str] = Query(default=None), user: User = Depends(get_current_user)):
    return {"jobs": image_jobs.active(user.id, session_id)}


@router.get("/result/{task_id}")
def get_image_result(task_id: str, user: User = Depends(get_current_user)):
    try:
        result = image_jobs.result_file(task_id, user.id)
    except TimeoutError as exc:
        raise HTTPException(status_code=410, detail="图片结果已过期") from exc
    if not result:
        raise HTTPException(status_code=404, detail="图片结果不存在")
    path, media_type = result
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})


@router.post("/upload_ref")
async def upload_reference_image(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(default=None),
    activate_context: bool = Form(default=True),
    user: User = Depends(get_current_user),
):
    """手机端从相册上传参考图，转存至 COS 返回公网 URL"""
    try:
        max_bytes = get_runtime_config()["request"]["reference_image_max_bytes"]
        content = await file.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail="参考图超过大小限制")
        _, cos_url = await run_in_threadpool(image_core.prepare_reference_image, content)
        if not cos_url:
            raise HTTPException(status_code=502, detail="参考图存储失败")
        if session_id and session_id.strip():
            with session_scope() as db:
                db.add(ChatMessage(
                    session_id=session_id.strip(),
                    user_id=user.id,
                    role="system",
                    content="参考图已上传" if activate_context else "方案卡参考图已上传",
                    image_url=cos_url,
                    image_role="reference" if activate_context else "reference_candidate",
                ))
        limit = visual_context_turn_limit()
        return {
            "ref_url": cos_url,
            "visual_context": {
                "url": cos_url,
                "role": "reference",
                "image_task_id": None,
                "remaining_turns": limit,
                "turn_limit": limit,
            } if session_id and session_id.strip() and activate_context else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="参考图无效或无法存储") from e


@router.post("/visual-input/clear")
def clear_visual_input(
    session_id: str = Form(...),
    user: User = Depends(get_current_user),
):
    """Persist an explicit visual-input clear marker for session restore."""
    normalized = session_id.strip()
    if not normalized or len(normalized) > 64:
        raise HTTPException(status_code=422, detail="session_id must contain 1 to 64 characters")
    with session_scope() as db:
        db.add(ChatMessage(
            session_id=normalized,
            user_id=user.id,
            role="system",
            content="已取消参考图",
            image_role="visual_input_cleared",
        ))
    return {"cleared": True, "visual_context": None}


@router.get("/task/{task_id}")
def get_task_status(task_id: str, user: User = Depends(get_current_user)):
    """查询任务生成状态"""
    managed = image_jobs.get_status(task_id, user.id)
    if managed:
        return managed
    with session_scope() as db:
        task = (
            db.query(ImageTask)
            .filter(ImageTask.task_id == task_id, ImageTask.user_id == user.id)
            .first()
        )
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {
            "task_id": task.task_id,
            "status": task.status,
            "image_url": task.image_url,
            "duration_ms": task.duration_ms,
            "aspect_ratio": task.aspect_ratio,
            "style": task.style,
            "error_msg": task.error_msg,
            "model_id": task.model_id,
            "requested_model_id": task.requested_model_id,
            "fallback_from_model_id": task.fallback_from_model_id,
            "fallback_reason": task.fallback_reason,
            "storage_policy": task.storage_policy,
            "request_id": task.request_id,
            "id": task.id,
            "parent_id": task.parent_id,
            "session_id": getattr(task, "session_id", None),
            "phase": getattr(task, "phase", None),
            "persistence_status": getattr(task, "persistence_status", None),
            "result_url": None,
            "is_temporary": False,
            "expires_at": None,
            "timings": {},
            "proposal_card_id": getattr(task, "proposal_card_id", None),
            "final_prompt": task.final_prompt,
            "quality": task.quality,
        }


@router.get("/history")
def get_image_history(user: User = Depends(get_current_user)):
    """获取当前用户的生图历史列表"""
    with session_scope() as db:
        tasks = (
            db.query(ImageTask)
            .filter(ImageTask.user_id == user.id, ImageTask.status == "success")
            .order_by(ImageTask.created_at.desc())
            .limit(50)
            .all()
        )
        results = []
        for t in tasks:
            results.append({
                "id": t.id,
                "image_task_id": t.id,
                "session_id": t.session_id,
                "proposal_card_id": t.proposal_card_id,
                "final_prompt": t.final_prompt,
                "quality": t.quality,
                "task_id": t.task_id,
                "prompt": t.prompt,
                "style": t.style,
                "aspect_ratio": t.aspect_ratio,
                "image_url": t.image_url,
                "ref_image_url": t.ref_image_url,
                "model_id": t.model_id,
                "requested_model_id": t.requested_model_id,
                "fallback_from_model_id": t.fallback_from_model_id,
                "fallback_reason": t.fallback_reason,
                "storage_policy": t.storage_policy,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            })
        return {"images": results}


@router.get("/references")
def get_references():
    """获取官方 Showcase 案例库"""
    return {"references": load_references()}
