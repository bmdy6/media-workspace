"""media/core/image_service.py - 生图底层能力核心 (Core)。

职责：
1. 接收标准化生图请求 (文生图 / 图生图)。
2. 调用风格包装器 (wrap_prompt)。
3. 按原画幅规范化参考图，去除 EXIF 方向差异并限制尺寸。
4. 调用底层 GPT-Image-2 中转服务。
5. 将生成结果转存到腾讯云 COS，获取稳定的公网 CDN URL。
6. 记录任务状态到数据库 (ImageTask)。
7. 绝不依赖任何积分扣费、充值流水或前端代码。
"""
from __future__ import annotations

import base64
import io
import json
import ipaddress
import re
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image, ImageOps

from ..database.session import session_scope
from ..database.models import ImageTask
from ..styles import wrap_prompt
from .config import get_config
from .cos_storage import cos_storage
from .errors import GenerationError, StorageError
from .image_stream import (
    BoundedImageStreamTransport, ImagePartialPreview, bounded_chunks, consume_image_stream,
    decode_stream_json, image_event_budget, stream_cancelled,
)
from .runtime_config import RuntimeConfigError, get_model, get_provider, get_runtime_config

# Compatibility export for integrations; catalog remains the source of truth.
RATIO_TO_SIZE: dict[str, str] = get_model(None, "image")["size_by_aspect_ratio"]

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _assert_safe_reference_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise GenerationError("参考图 URL 必须是安全的 HTTP(S) 地址")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, port)}
    except (OSError, ValueError) as exc:
        raise GenerationError("参考图地址无法解析") from exc
    if not addresses:
        raise GenerationError("参考图地址无法解析")
    try:
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise GenerationError("参考图 URL 不能指向本地或私有网络地址")
    except ValueError as exc:
        raise GenerationError("参考图地址无法解析") from exc


def _sanitize(text: str, limit: int) -> str:
    return _BAD_CHARS.sub("", text).strip()[:limit]


def _derive_name(prompt: str) -> str:
    first = re.split(r"[@#\n。，,;；]", prompt.strip())[0]
    return _sanitize(first, 12) or "image"


def _build_filename(name: str) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S")
    short = _sanitize(name, 20) or "image"
    return f"{ts}_{short}_{uuid.uuid4().hex[:6]}.png"


# Provider error values are untrusted. Only these exact upstream values are
# converted to fixed, low-sensitivity categories; raw codes/types/messages are
# never returned or logged.
_PROVIDER_DIAGNOSTIC_MAX_BYTES = 8 * 1024
_PROVIDER_DIAGNOSTIC_MAX_DEPTH = 8
_PROVIDER_ERROR_CODE_CATEGORIES = {
    "invalid_request_error": "invalid_request",
    "unsupported_parameter": "unsupported_parameter",
    "invalid_value": "invalid_value",
    "invalid_image_format": "invalid_image",
    "image_too_large": "image_too_large",
    "content_policy_violation": "content_policy",
    "moderation_blocked": "content_policy",
    "rate_limit_exceeded": "rate_limited",
    "insufficient_quota": "quota_exceeded",
    "invalid_api_key": "authentication",
    "server_error": "provider_error",
    "api_error": "provider_error",
}
_PROVIDER_ERROR_TYPE_CATEGORIES = {
    "invalid_request_error": "invalid_request",
    "image_generation_user_error": "request_rejected",
    "image_generation_server_error": "provider_error",
    "rate_limit_error": "rate_limited",
    "server_error": "provider_error",
    "api_error": "provider_error",
}
_PROVIDER_CATEGORY_MESSAGES = {
    "invalid_request": "request_rejected",
    "unsupported_parameter": "parameter_rejected",
    "invalid_value": "value_rejected",
    "invalid_image": "image_rejected",
    "image_too_large": "image_size_rejected",
    "content_policy": "content_policy_rejected",
    "rate_limited": "rate_limited",
    "quota_exceeded": "quota_unavailable",
    "authentication": "provider_authentication_failed",
    "request_rejected": "request_rejected",
    "provider_error": "provider_error",
}


def _json_within_depth(text: str, max_depth: int) -> bool:
    """Bound structural nesting before parsing an untrusted provider body."""
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif ord(character) == 92:
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > max_depth:
                return False
        elif character in "]}":
            depth = max(0, depth - 1)
    return True


def _safe_provider_error_diagnostic(response: Any) -> Optional[str]:
    """Return only normalized allowlisted categories from a bounded JSON body."""
    raw_body = getattr(response, "content", None)
    if not isinstance(raw_body, bytes) or len(raw_body) > _PROVIDER_DIAGNOSTIC_MAX_BYTES:
        return None
    try:
        body = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not _json_within_depth(body, _PROVIDER_DIAGNOSTIC_MAX_DEPTH):
        return None
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None

    raw_code = error.get("code")
    raw_type = error.get("type")
    code_category = (
        _PROVIDER_ERROR_CODE_CATEGORIES.get(raw_code)
        if isinstance(raw_code, str)
        else None
    )
    type_category = (
        _PROVIDER_ERROR_TYPE_CATEGORIES.get(raw_type)
        if isinstance(raw_type, str)
        else None
    )
    if not code_category and not type_category:
        return None

    # Expose normalized categories only. Never copy provider-controlled code,
    # type, message, nested body, headers, prompt, or image data into the error.
    details = []
    if code_category:
        details.append(f"provider_code={code_category}")
    if type_category:
        details.append(f"provider_type={type_category}")
    message_category = code_category or type_category
    safe_message = _PROVIDER_CATEGORY_MESSAGES.get(message_category)
    if safe_message:
        details.append(f"safe_message={safe_message}")
    return " [" + " ".join(details) + "]"


@dataclass
class ImageResult:
    task_id: str
    image_url: str
    status: str
    duration_ms: int
    prompt: str
    final_prompt: str
    aspect_ratio: str
    style: str
    quality: str
    ref_image_url: Optional[str] = None
    parent_id: Optional[int] = None
    error_msg: Optional[str] = None
    model_id: Optional[str] = None
    requested_model_id: Optional[str] = None
    fallback_from_model_id: Optional[str] = None
    fallback_reason: Optional[str] = None
    storage_policy: Optional[str] = None
    request_id: Optional[str] = None


@dataclass(frozen=True)
class ProviderImageResult:
    """One normalized upstream image; URL downloads happen only on request."""

    image_bytes: Optional[bytes]
    image_url: Optional[str]
    model_id: str


class ImageCore:
    def __init__(self, *, stream_transport: Optional[BoundedImageStreamTransport] = None):
        self._stream_transport = stream_transport

    def prepare_reference_image(
        self, ref_image: str | bytes, storage_policy: Optional[str] = None, *, allow_local_path: bool = True,
        upload_reference: bool = True,
    ) -> tuple[bytes, str]:
        """Decode, orient, bound, and encode a reference without changing its frame.

        返回: (ref_bytes, ref_cos_url)
        """
        raw_bytes: bytes
        ref_url: str = ""

        if isinstance(ref_image, bytes):
            raw_bytes = ref_image
        elif ref_image.startswith(("http://", "https://")):
            ref_url = ref_image
            request_config = get_runtime_config()["request"]
            max_bytes = request_config["reference_image_max_bytes"]
            timeout_seconds = request_config["reference_image_timeout_seconds"]
            owned_key = cos_storage.public_object_key(ref_image)
            if owned_key is not None:
                try:
                    raw_bytes = cos_storage.download_bytes(
                        owned_key, max_bytes=max_bytes, timeout_seconds=timeout_seconds,
                    )
                except StorageError as e:
                    raise GenerationError("读取已上传参考图失败") from e
            else:
                _assert_safe_reference_url(ref_image)
                resp = None
                try:
                    resp = requests.get(
                        ref_image,
                        timeout=timeout_seconds,
                        stream=True,
                        allow_redirects=False,
                    )
                    if resp.status_code != 200:
                        raise GenerationError(f"读取远程参考图失败: HTTP {resp.status_code}")
                    resp.raise_for_status()
                    declared_length = resp.headers.get("Content-Length")
                    if declared_length and int(declared_length) > max_bytes:
                        raise GenerationError(f"参考图超过大小限制 ({max_bytes} bytes)")
                    chunks = bytearray()
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        chunks.extend(chunk)
                        if len(chunks) > max_bytes:
                            raise GenerationError(f"参考图超过大小限制 ({max_bytes} bytes)")
                    raw_bytes = bytes(chunks)
                except Exception as e:
                    if isinstance(e, GenerationError):
                        raise
                    raise GenerationError("读取远程参考图失败") from e
                finally:
                    if resp is not None:
                        resp.close()
        elif ref_image.startswith("data:image"):
            try:
                _, encoded = ref_image.split(",", 1)
                if len(encoded) > (get_runtime_config()["request"]["reference_image_max_bytes"] * 4 // 3) + 8:
                    raise GenerationError("参考图超过大小限制")
                raw_bytes = base64.b64decode(encoded, validate=True)
            except Exception as e:
                raise GenerationError("参考图 data URL 无效") from e
        else:
            if not allow_local_path:
                raise GenerationError("HTTP 用户参考图必须使用已上传的图片 URL，不能读取服务器本地路径")
            # 本地文件路径
            p = Path(ref_image)
            if not p.exists():
                raise GenerationError(f"参考图文件不存在: {ref_image}")
            max_bytes = get_runtime_config()["request"]["reference_image_max_bytes"]
            with p.open("rb") as source:
                raw_bytes = source.read(max_bytes + 1)
            if len(raw_bytes) > max_bytes:
                raise GenerationError(f"参考图超过大小限制 ({max_bytes} bytes)")

        max_bytes = get_runtime_config()["request"]["reference_image_max_bytes"]
        if len(raw_bytes) > max_bytes:
            raise GenerationError(f"参考图超过大小限制 ({max_bytes} bytes)")

        try:
            with Image.open(io.BytesIO(raw_bytes)) as opened:
                width, height = opened.size
                request = get_runtime_config()["request"]
                if width * height > request["reference_image_max_pixels"]:
                    raise GenerationError("参考图解码像素超过限制")
                if max(width, height) > request["reference_image_max_edge"]:
                    raise GenerationError("参考图边长超过限制")
                img = ImageOps.exif_transpose(opened).convert("RGBA")
            max_edge = get_runtime_config()["reference_image"]["max_edge"]
            if max(img.size) > max_edge:
                img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            prepared = buf.getvalue()
            if len(prepared) > max_bytes:
                raise GenerationError("规范化后的参考图超过大小限制")
        except GenerationError:
            raise
        except Exception as e:
            raise GenerationError("参考图不是有效且受支持的图片") from e

        # 若原图没有远程公网 URL，将其上传至 COS
        if not ref_url and upload_reference:
            runtime = get_runtime_config()
            reference_prefix = runtime["storage_policies"][
                storage_policy or runtime["defaults"]["storage_policy"]
            ].get("reference_prefix", "references")
            filename = f"ref_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
            try:
                ref_url = cos_storage.upload_bytes(prepared, filename, prefix=reference_prefix)
            except Exception as e:
                # 若 COS 上传失败，仍可使用字节生图
                ref_url = ""

        return prepared, ref_url

    def _normalize_image_response(self, resp_data: dict, requested_model_id: str) -> ProviderImageResult:
        if not isinstance(resp_data, dict):
            raise GenerationError("接口返回的图片响应格式无效")
        items = resp_data.get("data") or resp_data.get("images") or []
        if not isinstance(items, list) or not items:
            raise GenerationError("响应中未找到图片数据")
        item = items[0]
        reported_model_id = resp_data.get("model")
        model_id = reported_model_id if isinstance(reported_model_id, str) and reported_model_id else requested_model_id

        if isinstance(item, dict):
            if "b64_json" in item:
                encoded = item["b64_json"]
                if not isinstance(encoded, str):
                    raise GenerationError("接口返回的 base64 图片数据格式无效")
                try:
                    image_bytes = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as exc:
                    raise GenerationError("接口返回的 base64 图片数据无效") from exc
                if not image_bytes:
                    raise GenerationError("接口返回了空的 base64 图片数据")
                return ProviderImageResult(image_bytes, None, model_id)
            if "url" in item:
                url = item["url"]
                if not isinstance(url, str):
                    raise GenerationError("接口返回的图片地址格式无效")
                if url.startswith("data:image/"):
                    return self._normalize_image_data_url(url, model_id)
                return ProviderImageResult(None, self._validated_image_url(url), model_id)
        elif isinstance(item, str):
            if item.startswith("data:image/"):
                return self._normalize_image_data_url(item, model_id)
            return ProviderImageResult(None, self._validated_image_url(item), model_id)

        raise GenerationError("无法从接口响应提取图片")

    @staticmethod
    def _normalize_image_data_url(data_url: str, model_id: str) -> ProviderImageResult:
        header, separator, encoded = data_url.partition(",")
        if not separator or ";base64" not in header.lower():
            raise GenerationError("接口返回了不支持的图片 data URL")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise GenerationError("接口返回的 base64 图片数据无效") from exc
        if not image_bytes:
            raise GenerationError("接口返回了空的 base64 图片数据")
        return ProviderImageResult(image_bytes, None, model_id)

    @staticmethod
    def _validated_image_url(url: str) -> str:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            if (
                parsed.scheme.lower() not in ("http", "https")
                or not hostname
                or parsed.username is not None
                or parsed.password is not None
                or any(character.isspace() or ord(character) < 32 for character in url)
            ):
                raise ValueError("invalid URL")
            parsed.port  # Accessing this property validates port syntax and range.
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                ascii_hostname = hostname.encode("idna").decode("ascii")
                labels = ascii_hostname.rstrip(".").split(".")
                if len(ascii_hostname.rstrip(".")) > 253 or any(
                    not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                    for label in labels
                ):
                    raise ValueError("invalid hostname")
        except (UnicodeError, ValueError) as exc:
            raise GenerationError("接口返回了无效或含凭据的上游 HTTP(S) 图片地址") from exc
        return url

    def _materialize_provider_image(self, result: ProviderImageResult) -> bytes:
        if result.image_bytes is not None:
            return result.image_bytes
        if result.image_url is not None:
            response = requests.get(
                result.image_url,
                timeout=get_runtime_config()["request"]["timeout_seconds"],
            )
            response.raise_for_status()
            return response.content
        raise GenerationError("规范化的上游图片结果缺少图片数据")

    def _extract_image_bytes(self, resp_data: dict) -> bytes:
        """Compatibility helper for callers that need immediate image bytes."""
        result = self._normalize_image_response(resp_data, resp_data.get("model") or "unknown")
        return self._materialize_provider_image(result)

    @staticmethod
    def _provider_api_key(provider: dict[str, Any]) -> str:
        env_key = provider["api_key_env"]
        project = provider.get("api_key_vault_project")
        value = get_config(env_key, default="", project=project)
        if value:
            return value
        vault_key = provider.get("api_key_vault_key")
        if vault_key:
            return get_config(vault_key, required=True, project=project)
        return get_config(env_key, required=True, project=project)

    @staticmethod
    def _provider_base_url(provider: dict[str, Any]) -> str:
        base_url = provider.get("base_url")
        if provider.get("base_url_env"):
            base_url = get_config(
                provider["base_url_env"],
                default=provider.get("default_base_url"),
            )
        if not base_url:
            raise GenerationError("图片 Provider 地址未配置")
        return str(base_url).rstrip("/")

    @staticmethod
    def _resolve_provider_pull_url(base_url: str, pull_url: str) -> str:
        if not isinstance(pull_url, str) or not pull_url.strip():
            raise GenerationError("上游未返回可恢复的图片任务地址")
        resolved = urljoin(f"{base_url}/", pull_url.strip())
        base = urlsplit(base_url)
        target = urlsplit(resolved)
        if target.scheme not in {"http", "https"} or target.hostname != base.hostname:
            raise GenerationError("上游返回了无效的图片任务地址")
        return resolved

    def _poll_async_image_task(
        self,
        *,
        model_id: str,
        provider_task_id: str,
        pull_url: str,
        ttl_seconds: Optional[int] = None,
        provider_submitted_at_ms: Optional[int] = None,
    ) -> ProviderImageResult:
        model = get_model(model_id, "image")
        if model.get("api_protocol") != "ohmygpt_images_polling":
            raise RuntimeConfigError(f"Image model {model_id} does not support provider polling")
        provider = get_provider(model["provider"])
        api_key = self._provider_api_key(provider)
        base_url = self._provider_base_url(provider)
        resolved_pull_url = self._resolve_provider_pull_url(base_url, pull_url)
        request_config = get_runtime_config()["request"]
        total_budget = float(request_config["total_budget_seconds"])
        if provider_submitted_at_ms:
            elapsed = max(0.0, time.time() - (provider_submitted_at_ms / 1000.0))
            total_budget = max(0.0, total_budget - elapsed)
        if ttl_seconds is not None:
            total_budget = min(total_budget, float(ttl_seconds))
        if total_budget <= 0:
            raise GenerationError("上游图片任务已超过可恢复时限")
        deadline = time.monotonic() + total_budget
        interval = float(request_config.get("image_provider_poll_interval_seconds", 2))
        headers = {"Authorization": f"Bearer {api_key}"}
        consecutive_errors = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GenerationError("上游图片仍未完成，已超过等待时限")
            try:
                response = requests.get(
                    resolved_pull_url,
                    headers=headers,
                    timeout=min(30.0, remaining),
                )
            except requests.RequestException as exc:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    raise GenerationError("查询上游图片任务失败，任务可稍后继续恢复") from exc
                time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
                continue
            if response.status_code != 200:
                if response.status_code in request_config["retry_http_statuses"] and consecutive_errors < 2:
                    consecutive_errors += 1
                    time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
                    continue
                raise GenerationError(f"查询上游图片任务失败 HTTP {response.status_code}")
            consecutive_errors = 0
            try:
                payload = response.json()
            except ValueError as exc:
                raise GenerationError("上游图片任务返回格式无效") from exc
            if not isinstance(payload, dict):
                raise GenerationError("上游图片任务返回格式无效")
            if payload.get("error"):
                raise GenerationError("上游图片生成失败")
            if payload.get("data") or payload.get("images"):
                return self._normalize_image_response(payload, model_id)
            status = str(payload.get("status") or "").lower()
            if status in {"failed", "error", "cancelled", "canceled", "expired"}:
                raise GenerationError("上游图片生成失败")
            if status in {"pending", "queued", "running", "processing", "generating", ""}:
                time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
                continue
            raise GenerationError(f"未知的上游图片任务状态: {status[:40]}")

    def _call_image_stream(
        self, *, url: str, headers: dict, common: dict, request_parameters: dict,
        ref_bytes: Optional[bytes], partial_images: int,
        on_partial_image: Callable[[ImagePartialPreview], None],
        is_cancelled: Optional[Callable[[], bool]],
    ) -> ProviderImageResult:
        """One request only: never retry a possibly accepted image generation."""
        runtime = get_runtime_config()
        deadline = time.monotonic() + runtime["request"]["total_budget_seconds"]
        max_image_bytes = runtime["image_jobs"]["result_max_bytes"]

        def cancelled() -> bool:
            return stream_cancelled(is_cancelled)

        def check_active() -> None:
            if cancelled():
                raise GenerationError("图片流已取消；上游是否完成或计费未知，未自动重试")
            if time.monotonic() >= deadline:
                raise GenerationError("图片流超出总时限；未自动重试")

        check_active()
        body = {**request_parameters, **common, "stream": True, "partial_images": partial_images}
        timeout = min(runtime["request"]["timeout_seconds"], max(0.001, deadline - time.monotonic()))
        transport = self._stream_transport
        if transport is None:
            raise GenerationError("图片流尚未配置有界可取消传输；未提交上游")
        response = None
        try:
            try:
                if ref_bytes:
                    response = transport(url, deadline=deadline, is_cancelled=cancelled,
                        headers={**headers, "Accept": "text/event-stream"},
                        data={**body, "stream": "true"},
                        files={"image": ("ref.png", ref_bytes, "image/png")}, timeout=timeout, stream=True)
                else:
                    response = transport(url, deadline=deadline, is_cancelled=cancelled,
                        headers={**headers, "Accept": "text/event-stream", "Content-Type": "application/json"},
                        json=body, timeout=timeout, stream=True)
            except Exception:
                raise GenerationError("图片流传输中断；上游接单状态未知，未自动重试") from None
            check_active()
            try:
                status = response.status_code
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if type(status) is not int or not 100 <= status <= 599:
                    raise ValueError()
            except Exception:
                raise GenerationError("底层生图流响应格式无效；未自动重试") from None
            if status != 200:
                # Do not buffer an arbitrary streaming error body or echo it.
                raise GenerationError(f"底层生图流异常 HTTP {status}；未自动重试")

            def response_chunks():
                # Defer adapter invocation into bounded_chunks' safe boundary.
                yield from response.iter_content(chunk_size=8192)

            chunks = response_chunks()
            if content_type == "text/event-stream":
                image_bytes = consume_image_stream(chunks,
                    operation="image_edit" if ref_bytes else "image_generation", partial_images=partial_images,
                    max_image_bytes=max_image_bytes, on_partial=on_partial_image, check_active=check_active)
                return ProviderImageResult(image_bytes, None, common["model"])
            if content_type == "application/json":
                # Some proxies ignore stream=true. Reuse their completed response,
                # rather than issuing a second potentially paid generation.
                raw = b"".join(bounded_chunks(chunks, image_event_budget(max_image_bytes), check_active))
                normalized = self._normalize_image_response(decode_stream_json(raw), common["model"])
                if normalized.image_bytes is not None and len(normalized.image_bytes) > max_image_bytes:
                    raise GenerationError("图片结果超过大小限制")
                return normalized
            raise GenerationError("底层生图流响应格式无效；未自动重试")
        except GenerationError:
            raise
        except Exception:
            raise GenerationError("图片流传输中断；上游接单状态未知，未自动重试") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    raise GenerationError("图片流传输关闭失败；未自动重试") from None

    def _call_transit_api(
        self,
        final_prompt: str,
        size: str,
        quality: str,
        ref_bytes: Optional[bytes] = None,
        model_id: Optional[str] = None,
        defer_download: bool = False,
        max_retries_override: Optional[int] = None,
        on_async_task: Optional[Callable[[dict[str, Any]], None]] = None,
        on_partial_image: Optional[Callable[[ImagePartialPreview], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> tuple[bytes, str] | ProviderImageResult:
        config = get_runtime_config()
        model = get_model(model_id, "image")
        stream_config = model.get("partial_streaming", {})
        use_stream = on_partial_image is not None and stream_config.get("enabled") is True
        if use_stream:
            if (model["id"] != "gpt-image-2.5-flare" or model.get("api_protocol") != "openai_images_v1"
                or not all(model.get(flag) is True for flag in ("enabled", "available", "verified"))
                or quality != "low"):
                raise RuntimeConfigError("Partial streaming requires verified Flare / OpenAI Images / low")
            # requests.iter_content alone cannot enforce a wall-clock deadline
            # against a slow-drip peer. Never fall back to that transport.
            if self._stream_transport is None:
                raise GenerationError("图片流尚未配置有界可取消传输；未提交上游")
            if stream_cancelled(is_cancelled):
                raise GenerationError("图片流已取消；未提交上游")
        provider = get_provider(model["provider"])
        api_key = self._provider_api_key(provider)
        base_url = self._provider_base_url(provider)

        api_protocol = model.get("api_protocol")
        if api_protocol not in ("transit_images_legacy", "openai_images_v1", "ohmygpt_images_polling"):
            raise RuntimeConfigError(f"Unsupported image API protocol for {model['id']}: {api_protocol}")

        endpoint = "/images/edits" if ref_bytes else "/images/generations"
        url = f"{base_url}{endpoint}"
        headers = {"Authorization": f"Bearer {api_key}"}
        provider_quality = model.get("quality_map", {}).get(quality, quality)
        if use_stream and provider_quality != "low":
            raise RuntimeConfigError("Partial streaming provider quality must remain low")
        common = {
            "model": model["id"],
            "prompt": final_prompt,
            "n": 1,
            "quality": provider_quality,
            "size": size,
        }
        request_parameters = model.get("request_parameters", {}).get(
            "edits" if ref_bytes else "generations", {}
        )
        if api_protocol == "openai_images_v1" and {"response_format", "input_fidelity"}.intersection(request_parameters):
            raise RuntimeConfigError(
                f"Image model {model['id']} has parameters unsupported by the OpenAI Images API"
            )

        if use_stream:
            normalized = self._call_image_stream(url=url, headers=headers, common=common,
                request_parameters=request_parameters, ref_bytes=ref_bytes,
                partial_images=stream_config["partial_images"], on_partial_image=on_partial_image,
                is_cancelled=is_cancelled)
            if defer_download:
                return normalized
            return self._materialize_provider_image(normalized), normalized.model_id

        request_config = config["request"]
        if api_protocol == "ohmygpt_images_polling":
            submitted_at_ms = int(time.time() * 1000)
            try:
                if ref_bytes:
                    response = requests.post(
                        url,
                        headers=headers,
                        data={**request_parameters, **common, "omg": json.dumps({"async_method": "pulling"})},
                        files={"image": ("ref.png", ref_bytes, "image/png")},
                        timeout=min(30, request_config["timeout_seconds"]),
                    )
                else:
                    response = requests.post(
                        url,
                        headers={**headers, "Content-Type": "application/json"},
                        json={**request_parameters, **common, "omg": {"async_method": "pulling"}},
                        timeout=min(30, request_config["timeout_seconds"]),
                    )
            except requests.RequestException as exc:
                raise GenerationError(
                    f"生图任务提交状态不明确 ({type(exc).__name__})；为避免重复扣费未自动重试"
                ) from exc
            if not 200 <= response.status_code < 300:
                raise GenerationError(f"生图任务提交失败 HTTP {response.status_code}；未自动重试")
            try:
                response_data = response.json()
            except ValueError as exc:
                raise GenerationError("生图任务提交响应格式无效") from exc
            if isinstance(response_data, dict) and (response_data.get("data") or response_data.get("images")):
                normalized = self._normalize_image_response(response_data, model["id"])
                if defer_download:
                    return normalized
                return self._materialize_provider_image(normalized), normalized.model_id
            if not isinstance(response_data, dict):
                raise GenerationError("生图任务提交响应格式无效")
            provider_task_id = response_data.get("task_id")
            pull_url = response_data.get("pull_url")
            if not isinstance(provider_task_id, str) or not provider_task_id or not isinstance(pull_url, str):
                raise GenerationError("上游未返回可恢复的图片任务 ID")
            ttl_seconds = response_data.get("ttl_seconds")
            if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
                ttl_seconds = None
            metadata = {
                "provider_task_id": provider_task_id,
                "provider_pull_url": pull_url,
                "provider_ttl_seconds": ttl_seconds,
                "provider_submitted_at_ms": submitted_at_ms,
            }
            if on_async_task is not None:
                on_async_task(metadata)
            normalized = self._poll_async_image_task(
                model_id=model["id"],
                provider_task_id=provider_task_id,
                pull_url=pull_url,
                ttl_seconds=ttl_seconds,
                provider_submitted_at_ms=submitted_at_ms,
            )
            if defer_download:
                return normalized
            return self._materialize_provider_image(normalized), normalized.model_id

        last_err = "未知错误"
        max_retries = request_config["max_retries"] if max_retries_override is None else max_retries_override
        deadline = time.monotonic() + request_config["total_budget_seconds"]
        for attempt in range(max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GenerationError(f"生图请求超出总时限: {request_config['total_budget_seconds']} 秒")
            timeout = min(request_config["timeout_seconds"], remaining)
            try:
                if ref_bytes:
                    resp = requests.post(
                        url,
                        headers=headers,
                        data={**request_parameters, **common},
                        files={"image": ("ref.png", ref_bytes, "image/png")},
                        timeout=timeout,
                    )
                else:
                    resp = requests.post(
                        url,
                        headers={**headers, "Content-Type": "application/json"},
                        json={**request_parameters, **common},
                        timeout=timeout,
                    )
            except requests.RequestException as e:
                last_err = type(e).__name__
                if request_config.get("retry_transport_errors") and attempt < max_retries:
                    delay = request_config["retry_backoff_seconds"][attempt]
                    time.sleep(min(delay, max(0, deadline - time.monotonic())))
                    continue
                raise GenerationError(f"生图网络超时或连接失败: {last_err}")

            if resp.status_code == 200:
                try:
                    response_data = resp.json()
                except ValueError:
                    # A proxy can return HTML, an unsupported SSE stream, or
                    # truncated JSON with HTTP 200. Keep this distinguishable
                    # from storage failures without echoing the provider body
                    # or retrying an already accepted, potentially paid request.
                    raise GenerationError("底层生图服务响应格式无效 (HTTP 200)") from None
                normalized = self._normalize_image_response(response_data, model["id"])
                if defer_download:
                    return normalized
                return self._materialize_provider_image(normalized), normalized.model_id

            if resp.status_code in request_config["retry_http_statuses"]:
                last_err = f"HTTP {resp.status_code}"
                if attempt < max_retries:
                    delay = request_config["retry_backoff_seconds"][attempt]
                    time.sleep(min(delay, max(0, deadline - time.monotonic())))
                    continue

            diagnostic = (
                _safe_provider_error_diagnostic(resp)
                if 400 <= resp.status_code < 500
                else None
            ) or ""
            raise GenerationError(f"底层生图服务异常 HTTP {resp.status_code}{diagnostic}")

        raise GenerationError(f"生图请求失败（已尝试 {max_retries + 1} 次）: {last_err}")

    def generate(
        self,
        prompt: str,
        style: str = "default",
        aspect_ratio: Optional[str] = None,
        quality: Optional[str] = None,
        ref_image: Optional[str | bytes] = None,
        user_id: Optional[int] = None,
        parent_id: Optional[int] = None,
        model_id: Optional[str] = None,
        storage_policy: Optional[str] = None,
        request_id: Optional[str] = None,
        final_prompt: Optional[str] = None,
    ) -> ImageResult:
        """同步执行完整生图任务并转存到 COS"""
        runtime = get_runtime_config()
        model = get_model(model_id, "image")
        selected_model_id = model["id"]
        selected_quality = quality or runtime["defaults"]["quality"]
        selected_ratio = aspect_ratio or runtime["defaults"]["aspect_ratio"]
        if selected_quality not in model.get("qualities", []):
            raise RuntimeConfigError(f"Quality is not supported by {selected_model_id}: {selected_quality}")
        if selected_ratio not in model.get("aspect_ratios", []):
            raise RuntimeConfigError(f"Aspect ratio is not supported by {selected_model_id}: {selected_ratio}")
        selected_storage = storage_policy or runtime["defaults"]["synchronous_storage_policy"]
        if selected_storage != runtime["defaults"]["synchronous_storage_policy"]:
            raise RuntimeConfigError("This synchronous entry point supports COS storage; use /api/image/jobs for direct or background results")
        storage_config = runtime["storage_policies"].get(selected_storage)
        if not storage_config or not storage_config.get("enabled"):
            raise RuntimeConfigError(f"Storage policy is not available: {selected_storage}")
        if request_id is not None and (not request_id.strip() or len(request_id) > 128):
            raise RuntimeConfigError("request_id must contain 1 to 128 characters")

        start_time = time.time()
        task_id = str(uuid.uuid4())
        size = model["size_by_aspect_ratio"][selected_ratio]
        try:
            width, height = (int(part) for part in size.split("x"))
            ratio_w, ratio_h = (int(part) for part in selected_ratio.split(":"))
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigError(f"Invalid size for aspect ratio {selected_ratio}") from exc
        if width * ratio_h != height * ratio_w:
            raise RuntimeConfigError(f"Configured size does not match aspect ratio {selected_ratio}")
        if parent_id is not None and user_id is not None:
            with session_scope() as db:
                parent = db.query(ImageTask).filter(ImageTask.id == parent_id, ImageTask.user_id == user_id).first()
                if parent is None:
                    raise RuntimeConfigError("Parent image task does not belong to this user")

        # 1. 包装提示词
        final_prompt = final_prompt if final_prompt is not None else wrap_prompt(style, prompt, selected_quality)

        # 2. 处理参考图
        ref_bytes: Optional[bytes] = None
        ref_cos_url: Optional[str] = None
        if ref_image:
            ref_bytes, ref_cos_url = self.prepare_reference_image(
                ref_image, selected_storage, allow_local_path=user_id is None
            )

        # 3. 初始数据库任务状态 (pending -> generating)
        with session_scope() as db:
            task_record = ImageTask(
                task_id=task_id,
                user_id=user_id,
                prompt=prompt,
                final_prompt=final_prompt,
                style=style,
                aspect_ratio=selected_ratio,
                quality=selected_quality,
                model_id=None,
                requested_model_id=selected_model_id,
                storage_policy=selected_storage,
                request_id=request_id,
                ref_image_url=ref_cos_url,
                parent_id=parent_id,
                status="generating",
            )
            db.add(task_record)

        # 4. 执行生成与转存
        actual_model_id: Optional[str] = None
        try:
            api_result = self._call_transit_api(
                final_prompt=final_prompt,
                size=size,
                quality=selected_quality,
                ref_bytes=ref_bytes,
                model_id=selected_model_id,
            )
            if isinstance(api_result, tuple):
                img_bytes, actual_model_id = api_result
            else:  # Keep compatibility with existing integrations overriding the call.
                img_bytes, actual_model_id = api_result, selected_model_id

            # 上传到 COS
            filename = _build_filename(_derive_name(prompt))
            cos_url = cos_storage.upload_bytes(
                img_bytes, filename, prefix=storage_config.get("result_prefix", "creations")
            )

            duration_ms = int((time.time() - start_time) * 1000)

            # 更新任务为成功
            with session_scope() as db:
                record = db.query(ImageTask).filter(ImageTask.task_id == task_id).first()
                if record:
                    record.image_url = cos_url
                    record.status = "success"
                    record.duration_ms = duration_ms
                    record.model_id = actual_model_id

            return ImageResult(
                task_id=task_id,
                image_url=cos_url,
                status="success",
                duration_ms=duration_ms,
                prompt=prompt,
                final_prompt=final_prompt,
                aspect_ratio=selected_ratio,
                style=style,
                quality=selected_quality,
                ref_image_url=ref_cos_url,
                parent_id=parent_id,
                model_id=actual_model_id,
                requested_model_id=selected_model_id,
                storage_policy=selected_storage,
                request_id=request_id,
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            err_msg = str(e) if isinstance(e, (GenerationError, RuntimeConfigError)) else "生图或存储失败，请检查服务配置或稍后重试"
            with session_scope() as db:
                record = db.query(ImageTask).filter(ImageTask.task_id == task_id).first()
                if record:
                    record.status = "failed"
                    record.error_msg = err_msg
                    record.duration_ms = duration_ms
                    record.model_id = actual_model_id

            return ImageResult(
                task_id=task_id,
                image_url="",
                status="failed",
                duration_ms=duration_ms,
                prompt=prompt,
                final_prompt=final_prompt,
                aspect_ratio=selected_ratio,
                style=style,
                quality=selected_quality,
                ref_image_url=ref_cos_url,
                parent_id=parent_id,
                error_msg=err_msg,
                model_id=actual_model_id,
                requested_model_id=selected_model_id,
                storage_policy=selected_storage,
                request_id=request_id,
            )

    def get_task(self, task_id: str) -> Optional[ImageResult]:
        """查询任务状态"""
        with session_scope() as db:
            record = db.query(ImageTask).filter(ImageTask.task_id == task_id).first()
            if not record:
                return None
            return ImageResult(
                task_id=record.task_id,
                image_url=record.image_url or "",
                status=record.status,
                duration_ms=record.duration_ms,
                prompt=record.prompt,
                final_prompt=record.final_prompt,
                aspect_ratio=record.aspect_ratio,
                style=record.style,
                quality=record.quality,
                ref_image_url=record.ref_image_url,
                parent_id=record.parent_id,
                error_msg=record.error_msg,
                model_id=record.model_id,
                requested_model_id=record.requested_model_id,
                fallback_from_model_id=record.fallback_from_model_id,
                fallback_reason=record.fallback_reason,
                storage_policy=record.storage_policy,
                request_id=record.request_id,
            )


image_core = ImageCore()
