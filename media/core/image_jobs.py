"""Bounded in-process execution for user-visible asynchronous image jobs."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image
import io
import requests

from media.core.cos_storage import cos_storage
from media.core.errors import GenerationError
from media.core.image_service import image_core, ProviderImageResult, _build_filename, _derive_name, _assert_safe_reference_url
from media.core.runtime_config import get_model, get_runtime_config
from media.database import ChatMessage, ImageTask, session_scope

ROOT = Path(__file__).resolve().parents[1]
_data_dir = Path(os.environ.get("MEDIA_DATA_DIR", str(ROOT / "data")))
_result_dir = _data_dir / "image-results"
_lock_path = _data_dir / "image-jobs.lock"
_generation_pool: ThreadPoolExecutor | None = None
_storage_pool: ThreadPoolExecutor | None = None
_generation_slots: threading.BoundedSemaphore | None = None
_storage_slots: threading.BoundedSemaphore | None = None
_lock_handle = None
_ready = False
_lock = threading.RLock()
_submission_lock = threading.Lock()


def _config() -> dict[str, Any]:
    return get_runtime_config().get("image_jobs", {})


def start() -> bool:
    """Own the managed-job lane and resume accepted provider polling tasks after restart."""
    global _generation_pool, _storage_pool, _generation_slots, _storage_slots, _lock_handle, _ready
    with _lock:
        if _ready:
            return True
        cfg = _config()
        _data_dir.mkdir(parents=True, exist_ok=True)
        handle = _lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        _lock_handle = handle
        _generation_pool = ThreadPoolExecutor(max_workers=cfg.get("workers", 2), thread_name_prefix="image-job")
        _storage_pool = ThreadPoolExecutor(max_workers=cfg.get("storage_workers", 1), thread_name_prefix="image-store")
        _generation_slots = threading.BoundedSemaphore(cfg.get("workers", 2) + cfg.get("queue_capacity", 4))
        _storage_slots = threading.BoundedSemaphore(cfg.get("storage_workers", 1) + cfg.get("storage_queue_capacity", 4))
        recoveries: list[tuple[int, dict[str, Any], str, dict[str, Any]]] = []
        with session_scope() as db:
            pending = db.query(ImageTask).filter(
                ImageTask.phase.in_(["queued", "preparing", "generating"])
            ).all()
            for task in pending:
                resume = _provider_resume_metadata(task)
                policy = task.storage_policy or get_runtime_config()["defaults"]["storage_policy"]
                can_resume = task.phase == "generating" and resume is not None and bool(task.requested_model_id or task.model_id)
                if can_resume and _generation_slots.acquire(blocking=False):
                    storage_acquired = False
                    if policy in ("background_cos", "cos"):
                        storage_acquired = _storage_slots.acquire(blocking=False)
                        if not storage_acquired:
                            _generation_slots.release()
                            can_resume = False
                    if can_resume:
                        recoveries.append((task.id, _payload_from_task(task), policy, resume))
                        task.status = "generating"
                        task.phase = "generating"
                        task.error_msg = None
                        continue
                task.status = "failed"
                task.phase = "interrupted"
                task.error_msg = "服务重启后任务已中断；未发现可恢复的上游任务，请重新生成"
                task.duration_ms = task.duration_ms or 0
            interrupted_storage = db.query(ImageTask).filter(
                ImageTask.phase.in_(["result_ready", "persisting"]),
                ImageTask.persistence_status == "pending",
            ).all()
            for task in interrupted_storage:
                task.phase = "completed"
                task.persistence_status = "failed"
        _ready = True
        for task_pk, payload, policy, resume in recoveries:
            _generation_pool.submit(_run_generation, task_pk, payload, policy, resume)
        return True


def stop() -> None:
    global _generation_pool, _storage_pool, _lock_handle, _ready
    with _lock:
        if not _ready:
            return
        # Drain accepted work before releasing the process guard.
        _generation_pool.shutdown(wait=True)
        _storage_pool.shutdown(wait=True)
        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
        _lock_handle.close()
        _generation_pool = _storage_pool = _lock_handle = None
        _ready = False


def available() -> bool:
    return _ready


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(value: datetime | None) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= _now()


def _status(task: ImageTask) -> dict[str, Any]:
    result_url = None
    expires = task.result_expires_at
    if task.result_path and Path(task.result_path).is_file() and not _is_expired(expires):
        result_url = f"/api/image/result/{task.task_id}"
    return {
        "id": task.id, "task_id": task.task_id, "parent_id": task.parent_id,
        "session_id": task.session_id, "status": task.status, "phase": task.phase,
        "image_url": task.image_url or task.source_url, "result_url": result_url,
        "persistence_status": task.persistence_status,
        "is_temporary": bool((task.storage_policy == "direct" and task.source_url)
                              or ((task.source_url or task.result_path) and not task.image_url)),
        "expires_at": expires.isoformat() if expires else None,
        "duration_ms": task.duration_ms, "timings": json.loads(task.timings_json or "{}"),
        "model_id": task.model_id,
        "requested_model_id": task.requested_model_id, "storage_policy": task.storage_policy,
        "request_id": task.request_id, "error_msg": task.error_msg,
        "created_at": ((task.created_at.replace(tzinfo=timezone.utc) if task.created_at.tzinfo is None else task.created_at).isoformat() if task.created_at else None),
        "proposal_card_id": task.proposal_card_id, "final_prompt": task.final_prompt,
        "aspect_ratio": task.aspect_ratio, "quality": task.quality,
        "poll_interval_ms": get_runtime_config()["request"]["task_poll_interval_ms"],
        "poll_timeout_ms": _config()["poll_timeout_seconds"] * 1000,
    }



def _provider_resume_metadata(task: ImageTask) -> dict[str, Any] | None:
    try:
        data = json.loads(task.timings_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    task_id = data.get("provider_task_id")
    pull_url = data.get("provider_pull_url")
    submitted_at = data.get("provider_submitted_at_ms")
    if not isinstance(task_id, str) or not task_id or not isinstance(pull_url, str) or not pull_url:
        return None
    if not isinstance(submitted_at, int) or submitted_at <= 0:
        return None
    ttl = data.get("provider_ttl_seconds")
    if ttl is not None and (not isinstance(ttl, int) or ttl <= 0):
        ttl = None
    return {
        "provider_task_id": task_id,
        "provider_pull_url": pull_url,
        "provider_submitted_at_ms": submitted_at,
        "provider_ttl_seconds": ttl,
    }


def _payload_from_task(task: ImageTask) -> dict[str, Any]:
    runtime = get_runtime_config()
    return {
        "prompt": task.prompt,
        "final_prompt": task.final_prompt,
        "style": task.style or "default",
        "aspect_ratio": task.aspect_ratio or runtime["defaults"]["aspect_ratio"],
        "quality": task.quality or runtime["defaults"]["quality"],
        "ref_image_url": task.ref_image_url,
        "parent_id": task.parent_id,
        "session_id": task.session_id,
        "model_id": task.requested_model_id or task.model_id or runtime["defaults"]["image_model"],
        "storage_policy": task.storage_policy or runtime["defaults"]["storage_policy"],
        "request_id": task.request_id or task.task_id,
        "proposal_card_id": task.proposal_card_id,
    }


def _associate_proposal_card(db, user_id: int, task: ImageTask, payload: dict[str, Any]) -> None:
    card_id = payload.get("proposal_card_id")
    session_id = payload.get("session_id")
    if not card_id or not session_id:
        return
    messages = db.query(ChatMessage).filter(
        ChatMessage.user_id == user_id, ChatMessage.session_id == session_id,
        ChatMessage.role == "assistant", ChatMessage.card_data.is_not(None),
        ChatMessage.card_data.contains(card_id, autoescape=True),
    ).order_by(ChatMessage.id.desc()).all()
    for message in messages:
        try:
            card = json.loads(message.card_data)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(card, dict) or card.get("card_id") != card_id:
            continue
        expected_ref = card.get("visual_input_url")
        if expected_ref is None and card.get("visual_input_role") == "base":
            expected_ref = card.get("base_image_url")
        if expected_ref != payload.get("ref_image_url"):
            raise ValueError("proposal card reference changed; reload the card before generating")
        if isinstance(expected_ref, str) and expected_ref.startswith("/api/image/result/"):
            expected_task_id = expected_ref.removeprefix("/api/image/result/")
            parent = db.query(ImageTask).filter(
                ImageTask.task_id == expected_task_id, ImageTask.user_id == user_id,
                ImageTask.id == payload.get("parent_id"),
            ).first()
            if parent is None:
                raise ValueError("proposal card base image changed; reload the card before generating")
            try:
                readable = result_file(expected_task_id, user_id)
            except TimeoutError:
                raise ValueError("proposal card base image expired") from None
            if readable is None:
                raise ValueError("proposal card base image is unavailable")
        confirmed_prompt = payload.get("final_prompt") or payload["prompt"]
        card.update({
            "prompt": confirmed_prompt,
            "final_prompt": confirmed_prompt,
            "aspect_ratio": payload["aspect_ratio"],
            "quality": payload["quality"],
            "image_task_id": task.id,
        })
        message.card_data = json.dumps(card, ensure_ascii=False)
        return
    raise LookupError("proposal card not found in the current user's session")


def _validate_image_bytes(data: bytes, model: dict[str, Any]) -> str:
    try:
        with Image.open(io.BytesIO(data)) as im:
            mime = Image.MIME.get(im.format, "")
            if mime not in {"image/png", "image/jpeg", "image/webp"}:
                raise GenerationError("图片结果格式无效")
            width, height = im.size
            im.verify()
    except GenerationError:
        raise
    except Exception as exc:
        raise GenerationError("图片结果格式无效") from exc
    constraints = model.get("size_constraints", {})
    multiple = constraints.get("multiple_of", 1)
    pixels = width * height
    max_edge = constraints.get("max_edge", max(width, height))
    inclusive = constraints.get("max_edge_inclusive", True)
    max_aspect = constraints.get("max_aspect_ratio")
    aspect_too_wide = False
    if isinstance(max_aspect, str) and ":" in max_aspect:
        left, right = max_aspect.split(":", 1)
        try:
            limit = float(left) / float(right)
            aspect_too_wide = max(width / height, height / width) > limit
        except (ValueError, ZeroDivisionError):
            raise GenerationError("模型图片宽高比约束无效")
    if (width % multiple or height % multiple or pixels < constraints.get("min_pixels", 1)
            or pixels > constraints.get("max_pixels", pixels)
            or (max(width, height) > max_edge if inclusive else max(width, height) >= max_edge)
            or aspect_too_wide):
        raise GenerationError("上游图片尺寸超出模型约束")
    return mime


def submit(user_id: int, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    with _submission_lock:
        return _submit_locked(user_id, payload)


def _submit_locked(user_id: int, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if not _ready:
        raise RuntimeError("managed image jobs unavailable: another worker owns the job lock")
    request_id = payload["request_id"]
    key = hashlib.sha256(f"{user_id}\0{request_id}".encode()).hexdigest()
    effective = {k: payload.get(k) for k in (
        "prompt", "final_prompt", "style", "aspect_ratio", "quality", "ref_image_url",
        "parent_id", "session_id", "model_id", "storage_policy", "proposal_card_id",
    )}
    payload_hash = hashlib.sha256(json.dumps(effective, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    acquired_generation = False
    acquired_storage = False
    task_pk = task_id = None
    policy = payload["storage_policy"]
    try:
        with session_scope() as db:
            if payload.get("proposal_card_id") and payload.get("session_id"):
                prior_card = db.query(ImageTask).filter(
                    ImageTask.user_id == user_id,
                    ImageTask.session_id == payload["session_id"],
                    ImageTask.proposal_card_id == payload["proposal_card_id"],
                ).first()
                if prior_card:
                    if prior_card.payload_hash != payload_hash:
                        raise ValueError("proposal card was already confirmed with a different payload")
                    return _status(prior_card), True
            prior = db.query(ImageTask).filter(ImageTask.idempotency_key == key).first()
            if prior:
                if prior.payload_hash != payload_hash:
                    raise ValueError("request_id was already used with a different payload")
                return _status(prior), True
            if payload.get("parent_id") is not None:
                parent = db.query(ImageTask).filter(ImageTask.id == payload["parent_id"], ImageTask.user_id == user_id).first()
                if parent is None:
                    raise LookupError("parent image task not found")
            if not _generation_slots.acquire(blocking=False):
                raise OverflowError("image job queue is full")
            acquired_generation = True
            if policy in ("background_cos", "cos"):
                if not _storage_slots.acquire(blocking=False):
                    raise OverflowError("image storage queue is full")
                acquired_storage = True
            task = ImageTask(
                task_id=str(uuid.uuid4()), user_id=user_id, prompt=payload["prompt"],
                final_prompt=payload.get("final_prompt") or payload["prompt"], style=payload["style"],
                aspect_ratio=payload["aspect_ratio"], quality=payload["quality"],
                ref_image_url=payload.get("ref_image_url"), parent_id=payload.get("parent_id"),
                status="pending", phase="queued", persistence_status="pending" if policy in ("background_cos", "cos") else "not_requested",
                requested_model_id=payload["model_id"], storage_policy=policy, request_id=request_id,
                session_id=payload.get("session_id"), proposal_card_id=payload.get("proposal_card_id"),
                payload_hash=payload_hash, idempotency_key=key, timings_json="{}",
            )
            db.add(task)
            db.flush()
            _associate_proposal_card(db, user_id, task, payload)
            task_pk = task.id
            task_id = task.task_id
            accepted_created_at = ((task.created_at.replace(tzinfo=timezone.utc) if task.created_at.tzinfo is None else task.created_at).isoformat() if task.created_at else None)
        _generation_pool.submit(_run_generation, task_pk, payload, policy)
        acquired_generation = acquired_storage = False
    except Exception:
        if acquired_storage:
            _storage_slots.release()
        if acquired_generation:
            _generation_slots.release()
        if task_pk is not None:
            with session_scope() as db:
                accepted = db.query(ImageTask).filter(ImageTask.id == task_pk).first()
                if accepted and accepted.phase == "queued":
                    accepted.status = "failed"
                    accepted.phase = "failed"
                    accepted.error_msg = "图片任务未能进入执行队列"
        raise
    return {"id": task_pk, "task_id": task_id, "status": "pending", "phase": "queued",
            "created_at": accepted_created_at,
            "session_id": payload.get("session_id"),
            "proposal_card_id": payload.get("proposal_card_id"),
            "poll_interval_ms": get_runtime_config()["request"]["task_poll_interval_ms"],
            "poll_timeout_ms": _config()["poll_timeout_seconds"] * 1000}, False


def _run_generation(
    task_pk: int, payload: dict[str, Any], policy: str, resume_provider: dict[str, Any] | None = None
) -> None:
    started = time.monotonic()
    task_id = None
    actual_model = payload["model_id"]
    timings: dict[str, Any] = {}
    generation_released = False
    try:
        with session_scope() as db:
            task = db.query(ImageTask).filter(ImageTask.id == task_pk).first()
            if task is None:
                return
            task_id = task.task_id
            owner_id = task.user_id
            enqueued_at = task.created_at
            try:
                prior_timings = json.loads(task.timings_json or "{}")
                if isinstance(prior_timings, dict):
                    timings.update(prior_timings)
            except (TypeError, json.JSONDecodeError):
                pass
            task.status = "generating"
            task.phase = "generating" if resume_provider is not None else "preparing"
        budget = get_runtime_config()["request"]["total_budget_seconds"]
        queued_at = enqueued_at.replace(tzinfo=timezone.utc) if enqueued_at and enqueued_at.tzinfo is None else enqueued_at
        if queued_at:
            timings["queue_ms"] = max(0, int((_now() - queued_at).total_seconds() * 1000))
        if queued_at and (_now() - queued_at).total_seconds() > budget:
            raise GenerationError("图片任务排队超出总时限")
        model = get_model(actual_model, "image")
        size = model["size_by_aspect_ratio"][payload["aspect_ratio"]]
        ref: bytes | None = None
        ref_url = payload.get("ref_image_url")
        reference_started = time.monotonic()
        parent_id = payload.get("parent_id")
        if parent_id is not None:
            with session_scope() as db:
                parent = db.query(ImageTask).filter(ImageTask.id == parent_id, ImageTask.user_id == owner_id).first()
                if not parent:
                    raise LookupError("parent image task not found")
                parent_path = parent.result_path
                parent_url = parent.image_url or parent.source_url
                parent_expired = _is_expired(parent.result_expires_at)
            if parent_path and not parent_expired and Path(parent_path).is_file() and Path(parent_path).parent.resolve() == _result_dir.resolve():
                ref, _ = image_core.prepare_reference_image(Path(parent_path).read_bytes(), allow_local_path=False, upload_reference=False)
            else:
                if parent_path and parent_expired:
                    raise LookupError("parent image result expired")
                ref_url = parent_url
                if not ref_url:
                    raise LookupError("parent image result is unavailable")
        if ref_url and ref is None:
            ref, _ = image_core.prepare_reference_image(ref_url, allow_local_path=False)
        if ref is not None:
            timings["reference_ms"] = int((time.monotonic() - reference_started) * 1000)
        provider_started = time.monotonic()
        if resume_provider is None:
            with session_scope() as db:
                current = db.query(ImageTask).filter(ImageTask.id == task_pk).first()
                if current:
                    current.status = "generating"
                    current.phase = "generating"
        if resume_provider is not None:
            result = image_core._poll_async_image_task(
                model_id=actual_model,
                provider_task_id=resume_provider["provider_task_id"],
                pull_url=resume_provider["provider_pull_url"],
                ttl_seconds=resume_provider.get("provider_ttl_seconds"),
                provider_submitted_at_ms=resume_provider.get("provider_submitted_at_ms"),
            )
        else:
            def record_async_task(metadata: dict[str, Any]) -> None:
                timings.update(metadata)
                with session_scope() as db:
                    current = db.query(ImageTask).filter(ImageTask.id == task_pk).first()
                    if current:
                        current.status = "generating"
                        current.phase = "generating"
                        current.timings_json = json.dumps(timings)
                        current.error_msg = None

            result = image_core._call_transit_api(
                final_prompt=payload.get("final_prompt") or payload["prompt"], size=size,
                quality=payload["quality"], ref_bytes=ref, model_id=actual_model, defer_download=True,
                max_retries_override=0, on_async_task=record_async_task,
            )
        submitted_at_ms = timings.get("provider_submitted_at_ms")
        if isinstance(submitted_at_ms, int) and submitted_at_ms > 0:
            timings["provider_ms"] = max(0, int(time.time() * 1000) - submitted_at_ms)
        else:
            timings["provider_ms"] = int((time.monotonic() - provider_started) * 1000)
        if isinstance(result, tuple):
            image_bytes, actual_model = result
            result = ProviderImageResult(image_bytes=image_bytes, image_url=None, model_id=actual_model)
        elif isinstance(result, bytes):
            result = ProviderImageResult(image_bytes=result, image_url=None, model_id=actual_model)
        source_url = result.image_url
        mime = "image/png"
        result_path = None
        if result.image_bytes:
            max_bytes = _config().get("result_max_bytes", 20 * 1024 * 1024)
            if len(result.image_bytes) > max_bytes:
                raise GenerationError("图片结果超过大小限制")
            mime = _validate_image_bytes(result.image_bytes, model)
            _result_dir.mkdir(parents=True, exist_ok=True)
            result_path = _result_dir / f"{task_id}.img"
            temp = result_path.with_suffix(".tmp")
            temp.write_bytes(result.image_bytes)
            os.replace(temp, result_path)
        elif not source_url:
            raise GenerationError("上游没有返回可用图片")
        ready_time = time.monotonic()
        timings["first_result_ms"] = int((_now() - queued_at).total_seconds() * 1000) if queued_at else int((ready_time - started) * 1000)
        timings["total_ms"] = timings["first_result_ms"]
        expires = _now() + timedelta(seconds=_config().get("result_retention_seconds", 86400)) if result_path else None
        with session_scope() as db:
            task = db.query(ImageTask).filter(ImageTask.id == task_pk).first()
            task.source_url = source_url
            task.result_path = str(result_path) if result_path else None
            task.result_mime_type = mime if result_path else None
            task.result_expires_at = expires
            task.model_id = result.model_id
            task.status = "success"
            task.phase = "result_ready" if policy in ("background_cos", "cos") else "completed"
            task.persistence_status = "pending" if policy in ("background_cos", "cos") else "not_requested"
            task.duration_ms = timings["first_result_ms"]
            task.timings_json = json.dumps(timings)
            task.image_url = source_url if policy == "direct" else None
            task.error_msg = None
            if payload.get("session_id"):
                msg = db.query(ChatMessage).filter(ChatMessage.image_task_id == task.id).first()
                if msg is None:
                    db.add(ChatMessage(session_id=payload["session_id"], user_id=owner_id, role="assistant",
                        content=f"已为你生成作品《{payload['prompt'][:15]}...》", image_url=source_url or f"/api/image/result/{task_id}",
                        image_role="artwork", image_task_id=task.id))
        if policy in ("background_cos", "cos"):
            _generation_slots.release()
            generation_released = True
            _storage_pool.submit(_persist_cos, task_pk, task_id, payload, result_path, source_url, result.image_bytes)
        else:
            _generation_slots.release()
            generation_released = True
    except Exception as exc:
        try:
            with session_scope() as db:
                task = db.query(ImageTask).filter(ImageTask.id == task_pk).first()
                if task:
                    task.status = "failed"
                    task.phase = "failed"
                    task.error_msg = str(exc)[:300] if isinstance(exc, (GenerationError, LookupError)) else f"图片任务失败 ({type(exc).__name__})"
                    task.duration_ms = int((time.monotonic() - started) * 1000)
                    task.timings_json = json.dumps(timings)
        finally:
            if policy in ("background_cos", "cos"):
                _storage_slots.release()
            if not generation_released:
                _generation_slots.release()


def _persist_cos(task_pk: int, task_id: str, payload: dict[str, Any], path: Path | None, source_url: str | None, raw: bytes | None) -> None:
    started = time.monotonic()
    try:
        with session_scope() as db:
            task = db.query(ImageTask).filter(ImageTask.id == task_pk).first()
            if task:
                task.phase = "persisting"
                task.persistence_status = "pending"
        if raw is None and source_url:
            _assert_safe_reference_url(source_url)
            with requests.get(source_url, timeout=_config().get("storage_timeout_seconds", 60), stream=True, allow_redirects=False) as response:
                response.raise_for_status()
                chunks = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    chunks.extend(chunk)
                    if len(chunks) > _config().get("result_max_bytes", 20 * 1024 * 1024):
                        raise GenerationError("图片结果超过大小限制")
                raw = bytes(chunks)
        model = get_model(payload["model_id"], "image", require_available=False)
        mime = _validate_image_bytes(raw or (path.read_bytes() if path else b""), model)
        storage_config = get_runtime_config()["storage_policies"].get(payload["storage_policy"], {})
        url = cos_storage.upload_bytes(
            raw or (path.read_bytes() if path else b""), _build_filename(_derive_name(payload["prompt"])),
            content_type=mime, prefix=storage_config.get("result_prefix", "creations"),
            timeout_seconds=_config().get("storage_timeout_seconds", 60),
        )
        with session_scope() as db:
            task = db.query(ImageTask).filter(ImageTask.id == task_pk).first()
            if task:
                task.image_url = url
                task.phase = "completed"
                task.persistence_status = "stored"
                times = json.loads(task.timings_json or "{}")
                times["storage_ms"] = int((time.monotonic() - started) * 1000)
                times["total_ms"] = int((_now() - (task.created_at.replace(tzinfo=timezone.utc) if task.created_at.tzinfo is None else task.created_at)).total_seconds() * 1000)
                task.timings_json = json.dumps(times)
                msg = db.query(ChatMessage).filter(ChatMessage.image_task_id == task.id).first()
                if msg:
                    msg.image_url = url
    except Exception:
        with session_scope() as db:
            task = db.query(ImageTask).filter(ImageTask.id == task_pk).first()
            if task:
                task.phase = "completed"
                task.persistence_status = "failed"
                times = json.loads(task.timings_json or "{}")
                times["storage_ms"] = int((time.monotonic() - started) * 1000)
                times["total_ms"] = int((_now() - (task.created_at.replace(tzinfo=timezone.utc) if task.created_at.tzinfo is None else task.created_at)).total_seconds() * 1000)
                task.timings_json = json.dumps(times)
    finally:
        _storage_slots.release()


def get_status(task_id: str, user_id: int) -> dict[str, Any] | None:
    with session_scope() as db:
        task = db.query(ImageTask).filter(ImageTask.task_id == task_id, ImageTask.user_id == user_id).first()
        return _status(task) if task else None


def active(user_id: int, session_id: str | None) -> list[dict[str, Any]]:
    """Return jobs the UI still needs to render or recover.

    Failed/interrupted jobs stay visible as terminal feedback instead of silently
    disappearing.  Only the newest job for a proposal card is returned so a
    retry replaces the previous failed attempt.
    """
    with session_scope() as db:
        query = db.query(ImageTask).filter(
            ImageTask.user_id == user_id,
            ImageTask.phase.in_(["queued", "preparing", "generating", "result_ready", "persisting", "failed", "interrupted"]),
        )
        if session_id:
            query = query.filter(ImageTask.session_id == session_id)
        rows = query.order_by(ImageTask.created_at.desc(), ImageTask.id.desc()).limit(_config().get("active_jobs_limit", 100)).all()
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for task in rows:
            key = f"card:{task.proposal_card_id}" if task.proposal_card_id else f"task:{task.task_id}"
            if key in seen:
                continue
            seen.add(key)
            result.append(_status(task))
        return result


def result_file(task_id: str, user_id: int) -> tuple[Path, str] | None:
    with session_scope() as db:
        task = db.query(ImageTask).filter(ImageTask.task_id == task_id, ImageTask.user_id == user_id).first()
        if not task or not task.result_path:
            return None
        if _is_expired(task.result_expires_at):
            raise TimeoutError("result expired")
        path = Path(task.result_path)
        if not path.is_file() or path.parent != _result_dir:
            return None
        return path, task.result_mime_type or "application/octet-stream"
