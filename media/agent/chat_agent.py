"""media/agent/chat_agent.py - DeepSeek AI 生图创意助手引擎。

负责自然语言意图理解、视觉参考案例智能匹配、方案卡片组装与连续修改判定。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Generator, Optional
import httpx
from pydantic import ValidationError

from ..core.errors import ConfigError, MediaError
from ..core.runtime_config import RuntimeConfigError, get_model, get_provider, get_runtime_config
from ..references.search import get_reference_by_id, search_references
from ..styles import wrap_prompt
from .card_schema import AgentChatResponse, ProposalCard, ReferenceCaseItem
from .prompt_builder import SYSTEM_PROMPT, build_agent_context


JSON_BLOCK_REGEX = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.DOTALL)
RUNTIME_CAPABILITIES_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "runtime_capabilities.md"


def _iter_sse_data(
    response: httpx.Response, max_event_bytes: int, *, deadline: Optional[float] = None
) -> Generator[str, None, None]:
    buffer = bytearray()
    data_lines: list[bytes] = []
    event_bytes = 0

    def consume_line(raw_line: bytes) -> Optional[str]:
        nonlocal event_bytes
        line = raw_line.rstrip(b"\r")
        if not line:
            if not data_lines:
                event_bytes = 0
                return None
            payload = b"\n".join(data_lines).decode("utf-8", errors="strict")
            data_lines.clear()
            event_bytes = 0
            return payload
        if line.startswith(b":"):
            return None
        field, separator, value = line.partition(b":")
        if field == b"data":
            if separator and value.startswith(b" "):
                value = value[1:]
            data_lines.append(value)
            event_bytes += len(value) + 1
            if event_bytes > max_event_bytes:
                raise MediaError("模型流事件超过大小限制")
        return None

    for chunk in response.iter_bytes():
        if deadline is not None and time.monotonic() > deadline:
            raise MediaError("模型响应超过时间限制")
        buffer.extend(chunk)
        while True:
            line_end = buffer.find(b"\n")
            if line_end < 0:
                if len(buffer) > max_event_bytes:
                    raise MediaError("模型流事件超过大小限制")
                break
            if line_end > max_event_bytes:
                raise MediaError("模型流事件超过大小限制")
            line = bytes(buffer[:line_end])
            del buffer[:line_end + 1]
            payload = consume_line(line)
            if payload is not None:
                yield payload
    if buffer:
        payload = consume_line(bytes(buffer))
        if payload is not None:
            yield payload
    if data_lines:
        payload = b"\n".join(data_lines).decode("utf-8", errors="strict")
        if len(payload.encode("utf-8")) > max_event_bytes:
            raise MediaError("模型流事件超过大小限制")
        yield payload


@dataclass
class ChatModelResult:
    content: str
    model_id: str
    requested_model_id: str
    fallback_from_model_id: Optional[str] = None
    fallback_reason: Optional[str] = None


@dataclass
class ChatPreparation:
    messages: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    chat_model: dict[str, Any]
    image_model: dict[str, Any]
    thinking_mode: str


class ChatAgent:
    def __init__(self):
        pass

    def _call_deepseek_api(
        self,
        messages: list[dict[str, Any]],
        stream: bool = False,
        model_id: Optional[str] = None,
        thinking_mode: Optional[str] = None,
    ) -> Any:
        runtime = get_runtime_config()
        model = get_model(model_id, "chat")
        selected_id = model["id"]
        selected_thinking = thinking_mode if thinking_mode is not None else runtime["defaults"].get("thinking_mode")
        if selected_thinking not in model.get("thinking_modes", []):
            raise RuntimeConfigError(f"Thinking mode is not supported by {selected_id}: {selected_thinking}")

        def request_for(selected_model: dict[str, Any], active_client: httpx.Client):
            provider = get_provider(selected_model["provider"])
            from ..core.config import get_config
            api_key = get_config(
                provider["api_key_env"], required=True,
                project=provider.get("api_key_vault_project"),
            )
            base_url = provider.get("base_url")
            if provider.get("base_url_env"):
                base_url = get_config(
                    provider["base_url_env"],
                    default=provider.get("default_base_url"),
                )
            if not base_url:
                raise ConfigError(f"Provider base URL is not configured: {selected_model['provider']}")
            url = f"{base_url.rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {
                **selected_model.get("request_parameters", {}),
                **selected_model.get("thinking_mode_parameters", {}).get(selected_thinking, {}),
                "model": selected_model["id"],
                "messages": messages,
                "stream": stream,
            }
            return active_client.post(url, headers=headers, json=payload)

        request_timeout = get_runtime_config()["request"]["timeout_seconds"]
        client_options = {"trust_env": False} if model["provider"] == "openai_official" else {}
        with httpx.Client(timeout=request_timeout, **client_options) as client:
            resp = request_for(model, client)
            if resp.status_code == 200:
                data = resp.json()
                return ChatModelResult(
                    content=data["choices"][0]["message"]["content"],
                    model_id=data.get("model") or selected_id,
                    requested_model_id=selected_id,
                )
            raise MediaError(f"AI API 请求失败 (HTTP {resp.status_code})")

    def _iter_deepseek_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: Optional[str],
        thinking_mode: Optional[str],
    ) -> Generator[dict[str, Any], None, str]:
        runtime = get_runtime_config()
        model = get_model(model_id, "chat")
        selected_id = model["id"]
        selected_thinking = thinking_mode if thinking_mode is not None else runtime["defaults"].get("thinking_mode")
        if selected_thinking not in model.get("thinking_modes", []):
            raise RuntimeConfigError(f"Thinking mode is not supported by {selected_id}: {selected_thinking}")

        provider = get_provider(model["provider"])
        from ..core.config import get_config
        api_key = get_config(provider["api_key_env"], required=True, project=provider.get("api_key_vault_project"))
        base_url = provider.get("base_url")
        if provider.get("base_url_env"):
            base_url = get_config(provider["base_url_env"], default=provider.get("default_base_url"))
        if not base_url:
            raise ConfigError(f"Provider base URL is not configured: {model['provider']}")
        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            **model.get("request_parameters", {}),
            **model.get("thinking_mode_parameters", {}).get(selected_thinking, {}),
            "model": selected_id,
            "messages": messages,
            "stream": True,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        request = runtime["request"]
        timeout = min(request["chat_timeout_seconds"], request["chat_total_budget_seconds"])
        deadline = time.monotonic() + request["chat_total_budget_seconds"]
        max_chars = request["chat_max_output_chars"]
        emitted_chars = 0
        actual_model_id = selected_id
        stream_complete = False

        client_options = {"trust_env": False} if model["provider"] == "openai_official" else {}
        with httpx.Client(timeout=timeout, **client_options) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    raise MediaError(f"AI API 请求失败 (HTTP {response.status_code})")
                for data in _iter_sse_data(
                    response, request["chat_max_sse_event_bytes"], deadline=deadline
                ):
                    if time.monotonic() > deadline:
                        raise MediaError("模型响应超过时间限制")
                    if data == "[DONE]":
                        stream_complete = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise MediaError("模型流返回格式无效") from exc
                    if isinstance(chunk.get("model"), str):
                        actual_model_id = chunk["model"]
                    choices = chunk.get("choices") or []
                    if not choices or not isinstance(choices[0], dict):
                        continue
                    delta = choices[0].get("delta") or {}
                    reasoning = delta.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        emitted_chars += len(reasoning)
                        if emitted_chars > max_chars:
                            raise MediaError("模型响应超过长度限制")
                        yield {"kind": "reasoning"}
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        emitted_chars += len(content)
                        if emitted_chars > max_chars:
                            raise MediaError("模型响应超过长度限制")
                        yield {"kind": "content", "text": content}

        if not stream_complete:
            raise MediaError("模型响应流意外结束")
        yield {"kind": "complete", "model_id": actual_model_id}

    def parse_proposal_card(
        self,
        text: str,
        candidates: list[dict[str, Any]],
        current_image_url: Optional[str] = None,
        current_image_role: Optional[str] = None,
        *,
        reference_case_id: Optional[str] = None,
    ) -> tuple[str, Optional[ProposalCard]]:
        """从模型回复中提取并剥离结构化卡片 JSON"""
        match = JSON_BLOCK_REGEX.search(text)
        if not match:
            return text.strip(), None

        clean_text = text[:match.start()].strip()
        json_str = match.group(1)

        try:
            data = json.loads(json_str)
            if not isinstance(data, dict):
                return "方案卡片格式无效，请重试或补充需求。", None
            card_dict = data.get("card", data)
            if not isinstance(card_dict, dict):
                return "方案卡片格式无效，请重试或补充需求。", None
            card_id = f"card_{uuid.uuid4().hex[:8]}"

            # An explicit user selection wins; otherwise require a valid model-selected candidate.
            matched_ref = None
            if reference_case_id and not get_reference_by_id(reference_case_id):
                return "指定的参考案例不存在，请检查案例 ID 后重试。", None
            candidate_ids = {candidate.get("id") for candidate in candidates}
            model_reference_id = card_dict.get("reference_case_id")
            if not reference_case_id and model_reference_id is not None:
                if not isinstance(model_reference_id, str) or model_reference_id not in candidate_ids:
                    return "方案卡片引用了无效案例，请重试或移除案例选择。", None
                if not get_reference_by_id(model_reference_id):
                    return "方案卡片引用的案例不存在，请重试或移除案例选择。", None
            selected_reference_id = reference_case_id or model_reference_id
            selected = (
                get_reference_by_id(selected_reference_id)
                if isinstance(selected_reference_id, str)
                and (reference_case_id is not None or selected_reference_id in candidate_ids)
                else None
            )
            if selected:
                matched_ref = ReferenceCaseItem(
                    id=selected.get("id", ""),
                    title=selected.get("title", ""),
                    style=selected.get("style", "default"),
                    tags=selected.get("tags", []),
                    description=selected.get("description", ""),
                    recommended_ratio=selected.get("recommended_ratio", "1:1"),
                    preview_url=selected.get("preview_url", ""),
                )

            prompt = card_dict.get("prompt", "")
            if not isinstance(prompt, str) or not prompt.strip():
                return "方案卡片缺少有效提示词，请重试或补充需求。", None
            style = card_dict.get("style", "default")
            quality = "low"
            if not isinstance(style, str):
                return "方案卡片中的风格格式无效，请重试。", None

            is_mod = card_dict.get("is_modification", False)
            if not isinstance(is_mod, bool):
                return "方案卡片中的连续修改标记格式无效，请重试。", None
            reference_recommended = card_dict.get("reference_recommended", False)
            if not isinstance(reference_recommended, bool):
                return "方案卡片中的参考图建议格式无效，请重试。", None
            reference_reason = card_dict.get("reference_reason")
            if reference_reason is not None and not isinstance(reference_reason, str):
                return "方案卡片中的参考图建议说明格式无效，请重试。", None
            if not reference_recommended:
                reference_reason = None
            effective_image_role = (
                current_image_role
                if current_image_url and current_image_role in {"reference", "base"}
                else "base" if current_image_url and is_mod else None
            )
            card = ProposalCard(
                card_id=card_id,
                title=card_dict.get("title", "创意生图方案"),
                design_rationale=card_dict.get("design_rationale", "已为你规划构图与光影方案。"),
                prompt=prompt,
                final_prompt=wrap_prompt(style, prompt, quality),
                style=style,
                aspect_ratio=card_dict.get("aspect_ratio", "1:1"),
                quality=quality,
                is_modification=is_mod,
                base_image_url=current_image_url if effective_image_role == "base" else None,
                visual_input_url=current_image_url if effective_image_role else None,
                visual_input_role=effective_image_role,
                reference_recommended=reference_recommended,
                reference_reason=reference_reason.strip() if reference_reason else None,
                matched_reference=matched_ref,
            )
            return clean_text, card
        except (json.JSONDecodeError, TypeError, ValueError, KeyError, ValidationError):
            return "方案卡片格式无效，请重试或补充需求。", None

    def _prepare_chat(
        self,
        messages: list[dict[str, Any]],
        user_query: str,
        current_image_url: Optional[str],
        current_image_role: Optional[str],
        *,
        model_id: Optional[str],
        image_model_id: Optional[str],
        thinking_mode: Optional[str],
        reference_case_id: Optional[str],
        provider_image_url: Optional[str] = None,
    ) -> ChatPreparation:
        chat_model = get_model(model_id, "chat")
        image_model = get_model(image_model_id, "image")
        selected_thinking = (
            thinking_mode if thinking_mode is not None else get_runtime_config()["defaults"].get("thinking_mode")
        )
        if selected_thinking not in chat_model.get("thinking_modes", []):
            raise RuntimeConfigError(f"Thinking mode is not supported by {chat_model['id']}: {selected_thinking}")

        requested_reference = get_reference_by_id(reference_case_id) if reference_case_id else None
        if reference_case_id and requested_reference is None:
            raise RuntimeConfigError("指定的参考案例不存在，请检查案例 ID 后重试。")
        candidates = search_references(user_query, limit=2)
        if requested_reference and all(item.get("id") != requested_reference.get("id") for item in candidates):
            candidates = [requested_reference, *candidates][:2]

        enriched_user_prompt = build_agent_context(
            user_query=user_query,
            reference_candidates=candidates,
            current_image_url=current_image_url,
            current_image_role=current_image_role,
            reference_case_id=reference_case_id,
            current_image_visually_inspected=bool(chat_model.get("supports_vision") and current_image_url),
        )
        full_messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        runtime_context = {
            "image_model_id": image_model["id"],
            "qualities": image_model.get("qualities", []),
            "aspect_ratios": image_model.get("aspect_ratios", []),
            "size_by_aspect_ratio": image_model.get("size_by_aspect_ratio", {}),
        }
        capability_template = Template(RUNTIME_CAPABILITIES_PROMPT.read_text(encoding="utf-8"))
        full_messages.append({
            "role": "system",
            "content": capability_template.substitute(
                runtime_data=json.dumps(runtime_context, ensure_ascii=False, separators=(",", ":"))
            ),
        })
        full_messages.extend(messages)
        user_content: Any = enriched_user_prompt
        if chat_model.get("supports_vision") and current_image_url:
            user_content = [
                {"type": "text", "text": enriched_user_prompt},
                {"type": "image_url", "image_url": {"url": provider_image_url or current_image_url}},
            ]
        full_messages.append({"role": "user", "content": user_content})
        return ChatPreparation(full_messages, candidates, chat_model, image_model, selected_thinking)

    def _response_from_content(
        self,
        content: str,
        prep: ChatPreparation,
        *,
        current_image_url: Optional[str],
        current_image_role: Optional[str],
        requested_model_id: str,
        actual_model_id: str,
        reference_case_id: Optional[str],
        fallback_from_model_id: Optional[str] = None,
        fallback_reason: Optional[str] = None,
    ) -> AgentChatResponse:
        clean_text, card = self.parse_proposal_card(
            text=content,
            candidates=prep.candidates,
            current_image_url=current_image_url,
            current_image_role=current_image_role,
            reference_case_id=reference_case_id,
        )
        return AgentChatResponse(
            reply_text=clean_text or "我已经为你规划了以下视觉设计方案：",
            proposal_card=card,
            needs_clarification=card is None,
            model_id=actual_model_id,
            requested_model_id=requested_model_id,
            fallback_from_model_id=fallback_from_model_id,
            fallback_reason=fallback_reason,
            thinking_mode=prep.thinking_mode,
            reference_case_id=reference_case_id,
            image_model_id=prep.image_model["id"],
        )

    def chat(
        self,
        messages: list[dict[str, str]],
        user_query: str,
        current_image_url: Optional[str] = None,
        current_image_role: Optional[str] = None,
        *,
        model_id: Optional[str] = None,
        image_model_id: Optional[str] = None,
        thinking_mode: Optional[str] = None,
        reference_case_id: Optional[str] = None,
        provider_image_url: Optional[str] = None,
    ) -> AgentChatResponse:
        """同步完整执行一轮对话交互"""
        if reference_case_id:
            if get_reference_by_id(reference_case_id) is None:
                return AgentChatResponse(
                    reply_text="指定的参考案例不存在，请检查案例 ID 后重试。",
                    proposal_card=None,
                    needs_clarification=True,
                )

        prep = self._prepare_chat(
            messages, user_query, current_image_url, current_image_role, model_id=model_id,
            image_model_id=image_model_id, thinking_mode=thinking_mode,
            reference_case_id=reference_case_id,
            provider_image_url=provider_image_url,
        )

        # 3. 调 DeepSeek API
        model_result = self._call_deepseek_api(
            prep.messages,
            stream=False,
            model_id=prep.chat_model["id"],
            thinking_mode=prep.thinking_mode,
        )
        if isinstance(model_result, ChatModelResult):
            raw_response = model_result.content
            actual_model_id = model_result.model_id
            requested_model_id = model_result.requested_model_id
            fallback_from_model_id = model_result.fallback_from_model_id
            fallback_reason = model_result.fallback_reason
        else:  # Existing mock integrations may still return plain text.
            raw_response = model_result
            model = prep.chat_model
            actual_model_id = requested_model_id = model["id"]
            fallback_from_model_id = fallback_reason = None

        return self._response_from_content(
            raw_response, prep, current_image_url=current_image_url, current_image_role=current_image_role,
            requested_model_id=requested_model_id, actual_model_id=actual_model_id,
            reference_case_id=reference_case_id, fallback_from_model_id=fallback_from_model_id,
            fallback_reason=fallback_reason,
        )

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        user_query: str,
        current_image_url: Optional[str] = None,
        current_image_role: Optional[str] = None,
        *,
        model_id: Optional[str] = None,
        image_model_id: Optional[str] = None,
        thinking_mode: Optional[str] = None,
        reference_case_id: Optional[str] = None,
        provider_image_url: Optional[str] = None,
    ) -> Generator[tuple[str, Any], None, None]:
        context_label = "正在看参考图" if current_image_url else "正在理解你的需求"
        yield "status", {"phase": "context", "label": context_label}
        prep = self._prepare_chat(
            messages, user_query, current_image_url, current_image_role, model_id=model_id,
            image_model_id=image_model_id, thinking_mode=thinking_mode,
            reference_case_id=reference_case_id,
            provider_image_url=provider_image_url,
        )
        provider_label = "正在看参考图并构思方案" if current_image_url else "正在构思画面"
        yield "status", {"phase": "provider", "label": provider_label}
        raw_response = ""
        pending_visible = ""
        hiding_card = False
        thinking_announced = False
        actual_model_id = prep.chat_model["id"]
        for part in self._iter_deepseek_stream(
            prep.messages, model_id=prep.chat_model["id"], thinking_mode=prep.thinking_mode
        ):
            if part["kind"] == "reasoning":
                if not thinking_announced:
                    thinking_label = "正在结合参考图构思" if current_image_url else "正在构思画面"
                    yield "status", {"phase": "thinking", "label": thinking_label}
                    thinking_announced = True
            elif part["kind"] == "complete":
                actual_model_id = part["model_id"]
            elif part["kind"] == "content":
                text = part["text"]
                raw_response += text
                if hiding_card:
                    continue
                pending_visible += text
                fence_at = pending_visible.find("```")
                if fence_at >= 0:
                    visible = pending_visible[:fence_at]
                    pending_visible = ""
                    hiding_card = True
                    if visible:
                        yield "delta", {"text": visible}
                elif len(pending_visible) > 2:
                    safe_end = len(pending_visible) - 2
                    visible = pending_visible[:safe_end]
                    pending_visible = pending_visible[safe_end:]
                    if visible:
                        yield "delta", {"text": visible}

        if not hiding_card and pending_visible:
            yield "delta", {"text": pending_visible}
        yield "status", {"phase": "parse", "label": "正在整理生成方案"}
        response = self._response_from_content(
            raw_response, prep, current_image_url=current_image_url, current_image_role=current_image_role,
            requested_model_id=prep.chat_model["id"], actual_model_id=actual_model_id,
            reference_case_id=reference_case_id,
        )
        yield "done", response


chat_agent = ChatAgent()
