"""media/tests/test_agent.py - DeepSeek Agent 意图与卡片协议单元测试"""
from __future__ import annotations

from unittest.mock import patch
from media.agent.chat_agent import chat_agent
from media.agent.card_schema import ProposalCard


def test_parse_proposal_card_success():
    """验证从模型输出中准确剥离自然语言与方案卡片"""
    sample_llm_output = """好的！为你构思了一个极简深色科技海报。突出神经网络的光影质感，留出顶部标题空间。

```json
{
  "card": {
    "title": "AI Agent 科技感海报",
    "design_rationale": "深色科技背景，青蓝色荧光节点网络，信息留白",
    "prompt": "深色背景，发光神经网络芯片与微光数据流，极简高级，8K",
    "style": "tech_poster",
    "aspect_ratio": "3:4",
    "reference_case_id": "ref-03",
    "is_modification": false
  }
}
```
"""
    clean_text, card = chat_agent.parse_proposal_card(
        text=sample_llm_output,
        candidates=[{"id": "ref-03", "title": "极简海报", "style": "tech_poster", "tags": ["科技"]}],
    )

    assert "为你构思了一个极简深色科技海报" in clean_text
    assert card is not None
    assert card.title == "AI Agent 科技感海报"
    assert card.style == "tech_poster"
    assert card.aspect_ratio == "3:4"
    assert card.final_prompt.startswith("深色背景，发光神经网络芯片与微光数据流，极简高级，8K")
    assert "视觉重心" in card.final_prompt and "信息层级" in card.final_prompt
    assert "主体、文案、颜色和背景遵循用户明确描述" in card.final_prompt
    assert card.matched_reference is not None
    assert card.matched_reference.id == "ref-03"


def test_continuous_modification_linking():
    """验证连续修改时正确继承上一张底图作为参考"""
    sample_mod_output = """明白，我将整体背景调亮，并去掉多余的人物杂质。

```json
{
  "card": {
    "title": "调亮修改版",
    "design_rationale": "环境光整体提升，强化纯净质感",
    "prompt": "背景更加明亮通透，无人物，纯净科技界面",
    "style": "tech_poster",
    "aspect_ratio": "3:4",
    "is_modification": true
  }
}
```
"""
    clean_text, card = chat_agent.parse_proposal_card(
        text=sample_mod_output,
        candidates=[],
        current_image_url="https://cos.myqcloud.com/creations/prev_gen.png",
    )

    assert card is not None
    assert card.is_modification is True
    assert card.base_image_url == "https://cos.myqcloud.com/creations/prev_gen.png"


def test_reference_visual_input_is_preserved_without_modification():
    output = """参考你的头像，我会保留人物身份，同时做自然街拍构图。

```json
{"card":{"title":"自然街拍头像","design_rationale":"保留人物身份，使用自然光街拍","prompt":"保持参考人物身份与面部特征，自然街拍","style":"portrait","aspect_ratio":"3:4","is_modification":false}}
```
"""
    _, card = chat_agent.parse_proposal_card(
        text=output,
        candidates=[],
        current_image_url="https://images.example/me.png",
        current_image_role="reference",
    )
    assert card is not None
    assert card.is_modification is False
    assert card.visual_input_url == "https://images.example/me.png"
    assert card.visual_input_role == "reference"
    assert card.base_image_url is None


def test_personal_identity_request_without_reference_continues_to_model():
    output = '''可以先做近似的人物街拍方案；如果你补一张本人照片，会更容易保留身份特征，但不上传也能继续生成。\n\n```json\n{"card":{"title":"自然街拍头像","design_rationale":"自然光街拍","prompt":"自然街拍人物，真实肤质","style":"portrait","aspect_ratio":"3:4","reference_recommended":true,"reference_reason":"补一张本人照片可以提高身份特征的一致性","is_modification":false}}\n```'''
    with patch.object(chat_agent, "_call_deepseek_api", return_value=output) as provider:
        response = chat_agent.chat(messages=[], user_query="用我的头像做一张自然街拍照片")
    provider.assert_called_once()
    assert response.needs_clarification is False
    assert response.required_input is None
    assert response.proposal_card is not None
    assert response.proposal_card.reference_recommended is True
    assert "身份特征" in (response.proposal_card.reference_reason or "")


def test_reference_recommendation_fields_are_parsed_and_false_clears_reason():
    recommended = '''```json\n{"card":{"title":"修复旧照","design_rationale":"保持原貌","prompt":"真实旧照修复","style":"real","reference_recommended":true,"reference_reason":"上传原图才能提高真实细节的忠实度"}}\n```'''
    ordinary = '''```json\n{"card":{"title":"普通海报","design_rationale":"简洁","prompt":"蓝色抽象海报","style":"minimal","reference_recommended":false,"reference_reason":"不应保留"}}\n```'''

    _, recommended_card = chat_agent.parse_proposal_card(recommended, [])
    _, ordinary_card = chat_agent.parse_proposal_card(ordinary, [])

    assert recommended_card is not None
    assert recommended_card.reference_recommended is True
    assert recommended_card.reference_reason == "上传原图才能提高真实细节的忠实度"
    assert ordinary_card is not None
    assert ordinary_card.reference_recommended is False
    assert ordinary_card.reference_reason is None


def test_chat_agent_clarification():
    """验证缺少信息时进入提问澄清状态"""
    mock_reply = "你想制作真人写实风格的头像，还是插画动漫风格的？"
    with patch.object(chat_agent, "_call_deepseek_api", return_value=mock_reply):
        resp = chat_agent.chat(messages=[], user_query="做一个头像")
        assert resp.needs_clarification is True
        assert resp.proposal_card is None
        assert "真人写实" in resp.reply_text


def test_vision_chat_sends_reference_image_to_deepseek_and_uses_vision_context():
    seen_messages = []

    def capture(messages, **kwargs):
        seen_messages.extend(messages)
        return "我看到了这张参考图，可以基于画面继续判断。"

    with patch.object(chat_agent, "_call_deepseek_api", side_effect=capture):
        chat_agent.chat(
            messages=[],
            user_query="这个图片适合怎么二改？",
            current_image_url="https://images.example/reference.png",
            current_image_role="reference",
        )

    content = seen_messages[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "【用户参考图】" in content[0]["text"]
    assert "本轮模型没有看到图片内容" not in content[0]["text"]
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "https://images.example/reference.png"},
    }


def test_vision_chat_marks_selected_artwork_as_base_image():
    seen_messages = []

    def capture(messages, **kwargs):
        seen_messages.extend(messages)
        return "我会基于当前作品做局部调整。"

    with patch.object(chat_agent, "_call_deepseek_api", side_effect=capture):
        chat_agent.chat(
            messages=[],
            user_query="把背景调亮一些",
            current_image_url="https://images.example/artwork.png",
            current_image_role="base",
        )

    content = seen_messages[-1]["content"]
    assert isinstance(content, list)
    assert "【修改底图】" in content[0]["text"]
    assert content[1]["image_url"]["url"] == "https://images.example/artwork.png"


def test_chat_without_current_image_stays_text_only():
    seen_messages = []

    def capture(messages, **kwargs):
        seen_messages.extend(messages)
        return "可以，我先给你一个构图方向。"

    with patch.object(chat_agent, "_call_deepseek_api", side_effect=capture):
        chat_agent.chat(messages=[], user_query="做一个极简产品海报")

    content = seen_messages[-1]["content"]
    assert isinstance(content, str)
    assert "做一个极简产品海报" in content


def test_streaming_chat_with_image_announces_visual_inspection_status():
    chunks = iter([
        {"kind": "content", "text": "我已经看过这张图。"},
        {"kind": "complete", "model_id": "deepseek/deepseek-v4.1-flash"},
    ])
    with patch.object(chat_agent, "_iter_deepseek_stream", return_value=chunks):
        events = list(chat_agent.stream_chat(
            [],
            "这张图适合怎么改？",
            current_image_url="https://images.example/reference.png",
            current_image_role="reference",
        ))

    statuses = [payload for event, payload in events if event == "status"]
    assert statuses[0] == {"phase": "context", "label": "正在看参考图"}
    assert statuses[1] == {"phase": "provider", "label": "正在看参考图并构思方案"}


def test_streaming_chat_suppresses_card_fence_and_reasoning_and_emits_card_once():
    from media.agent.card_schema import AgentChatResponse

    card_json = '{"card":{"title":"海报","prompt":"蓝色玻璃花瓶","aspect_ratio":"1:1"}}'
    chunks = [
        {"kind": "reasoning"},
        {"kind": "content", "text": "为你设计一张海报。`"},
        {"kind": "content", "text": "``json\n" + card_json + "\n```"},
        {"kind": "complete", "model_id": "deepseek-flash"},
    ]
    with patch.object(chat_agent, "_iter_deepseek_stream", return_value=iter(chunks)):
        events = list(chat_agent.stream_chat([], "设计海报", image_model_id="gpt-image-2"))

    deltas = [payload["text"] for event, payload in events if event == "delta"]
    done = [payload for event, payload in events if event == "done"]
    statuses = [payload["phase"] for event, payload in events if event == "status"]
    assert "".join(deltas) == "为你设计一张海报。"
    assert "thinking" in statuses
    assert len(done) == 1 and isinstance(done[0], AgentChatResponse)
    assert done[0].proposal_card.prompt == "蓝色玻璃花瓶"
    assert done[0].image_model_id == "gpt-image-2"
    assert all("reasoning" not in str(payload) and "secret" not in str(payload) for _, payload in events)


def test_deepseek_stream_uses_configured_thinking_params_and_closes_on_disconnect(monkeypatch, optional_provider_catalog):
    import importlib
    import json
    import media.core.config as core_config
    agent_module = importlib.import_module("media.agent.chat_agent")

    class Response:
        status_code = 200

        def __init__(self, body):
            self.body = body
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def iter_bytes(self, chunk_size=8192):
            midpoint = len(self.body) // 2
            yield self.body[:midpoint]
            yield self.body[midpoint:]

    reasoning = b'data: {"choices":[{"delta":{"reasoning_content":"hidden"}}]}\n\n'
    content = 'data: {"model":"deepseek/deepseek-v4.1-flash","choices":[{"delta":{"content":"\u4f60\u597d"}}]}\n\n'.encode()
    response = Response(reasoning + content + b"data: [DONE]\n\n")
    captured = {}

    class Client:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            captured.update(kwargs)
            return response

    monkeypatch.setattr(agent_module.httpx, "Client", Client)
    monkeypatch.setattr(core_config, "get_config", lambda *args, **kwargs: "test-value")
    events = list(chat_agent._iter_deepseek_stream([], model_id="deepseek/deepseek-v4.1-flash", thinking_mode="low"))

    assert captured["json"]["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert captured["json"]["reasoning_effort"] == "low"
    assert captured["json"]["max_tokens"] == 4096
    assert [event for event in events if event["kind"] == "reasoning"] == [{"kind": "reasoning"}]
    assert "hidden" not in json.dumps(events)
    assert events[-1] == {"kind": "complete", "model_id": "deepseek/deepseek-v4.1-flash"}
    assert response.closed

    response.body = reasoning + content + b"data: [DONE]\n\n"
    response.closed = False
    partial = chat_agent._iter_deepseek_stream([], model_id="deepseek/deepseek-v4.1-flash", thinking_mode="low")
    next(partial)
    partial.close()
    assert response.closed
    assert captured["json"]["thinking"] == {"type": "enabled", "budget_tokens": 1024}


def test_sse_parser_handles_split_utf8_and_bounds_event_size():
    import importlib
    import pytest
    from media.core.errors import MediaError

    parser = importlib.import_module("media.agent.chat_agent")._iter_sse_data

    class Response:
        def __init__(self, chunks):
            self.chunks = chunks

        def iter_bytes(self, chunk_size=8192):
            yield from self.chunks

    encoded = 'data: {"text":"你好"}\n\ndata: [DONE]\n\n'.encode("utf-8")
    split = encoded.index("好".encode("utf-8")) + 1
    assert list(parser(Response([encoded[:split], encoded[split:]]), 128)) == ['{"text":"你好"}', "[DONE]"]
    with pytest.raises(MediaError, match="大小限制"):
        list(parser(Response([b"data: " + b"x" * 40]), 16))


    class DelayedResponse:
        def __init__(self):
            self.eof_reached = False

        def iter_bytes(self):
            yield b"data: {\"delta\":\"first\"}\n\n"
            self.eof_reached = True
            yield b"data: [DONE]\n\n"

    delayed = DelayedResponse()
    events = parser(delayed, 128)
    assert next(events) == '{"delta":"first"}'
    assert delayed.eof_reached is False
    assert next(events) == "[DONE]"
    assert delayed.eof_reached is True
