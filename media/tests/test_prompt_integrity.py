"""Regression tests for prompt preservation and reference-case handling."""
from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Keep imports inert if implementation code later starts resolving settings at import.
os.environ.setdefault("OPENAI_API_KEY", "test-only-no-network")
os.environ.setdefault("TENCENT_CLOUD_COS_BUCKET", "test-only-bucket")
os.environ.setdefault("MEDIA_USE_MYSQL", "false")

from media.agent.chat_agent import ChatAgent
from media.agent.prompt_builder import SYSTEM_PROMPT, build_agent_context
from media.core.image_service import ImageCore, ImageResult
from media.references.search import search_references
from media.styles import wrap_prompt
from media.web.backend.api.image_router import ConfirmGenerateRequest, confirm_and_generate


def test_style_wrapper_preserves_full_prompt_and_does_not_force_conflicting_template():
    raw_prompt = "主体：一只红色机器人，白色背景；标题：明天见\n保留这行括号（不要裁掉）。"

    final_prompt = wrap_prompt("tech_poster", raw_prompt, "low")

    assert raw_prompt in final_prompt
    assert "白色背景" in final_prompt
    assert "明天见" in final_prompt
    assert "（不要裁掉）。" in final_prompt
    assert "深色调" not in final_prompt


def test_unmatched_reference_query_returns_no_cases():
    with patch("media.references.search.load_references", return_value=[
        {"id": "ref-01", "title": "无关案例", "style": "real", "tags": [], "description": "无关"}
    ]):
        assert search_references("红色机器人，白色背景", limit=2) == []


def test_card_uses_only_model_selected_valid_reference_and_exposes_final_prompt():
    agent = ChatAgent()
    candidates = [
        {"id": "unselected", "title": "不应自动套用", "style": "real"},
        {"id": "ref-03", "title": "极简海报：重定义视觉美学", "style": "tech_poster", "tags": ["科技"]},
    ]
    output = '''
```json
    {"card":{"title":"机器人海报","design_rationale":"按要求设计","prompt":"主体：一只红色机器人，白色背景；标题：明天见","style":"tech_poster","quality":"low","reference_case_id":"ref-03"}}
```
'''

    _, card = agent.parse_proposal_card(output, candidates)

    assert card is not None
    assert card.matched_reference is not None
    assert card.matched_reference.id == "ref-03"
    expected_final_prompt = wrap_prompt(card.style, card.prompt, card.quality)
    assert card.final_prompt == expected_final_prompt
    assert card.prompt in card.final_prompt


def test_card_without_selected_reference_does_not_claim_a_match():
    agent = ChatAgent()
    output = '''```json
{"card":{"title":"方案","design_rationale":"说明","prompt":"完整提示词","style":"default"}}
```'''

    _, card = agent.parse_proposal_card(
        output,
        [{"id": "candidate", "title": "候选", "style": "real"}],
    )

    assert card is not None
    assert card.matched_reference is None


def test_agent_prompt_prefers_inference_over_unnecessary_questions():
    assert "缺失信息可以合理推断" in SYSTEM_PROMPT
    assert "每轮优先只问 **1 个**最关键问题" in SYSTEM_PROMPT
    assert "技术参数（模型、画质档位、具体像素、内部保存策略）永远不向用户追问" in SYSTEM_PROMPT
    assert "你决定" in SYSTEM_PROMPT and "不要追问风格、颜色、画幅或模型参数" in SYSTEM_PROMPT


def test_agent_prompt_contains_automatic_visual_strategy_layer():
    assert "场景策略：用户不选，AI 自动选" in SYSTEM_PROMPT
    assert "高拟真 / 摄影感" in SYSTEM_PROMPT
    assert "产品摄影" in SYSTEM_PROMPT
    assert "人物 / 街拍 / 纪实" in SYSTEM_PROMPT
    assert "海报 / 活动视觉" in SYSTEM_PROMPT
    assert "极简视觉" in SYSTEM_PROMPT
    assert "已有图片修改" in SYSTEM_PROMPT
    assert "用户明确要求 > 当前图片不可变信息 > 场景策略" in SYSTEM_PROMPT
    assert "8K / masterpiece" in SYSTEM_PROMPT


def test_agent_prompt_marks_cases_as_optional_inspiration_and_preserves_constraints():
    assert "仅供灵感参考" in SYSTEM_PROMPT
    assert "不能和用户要求冲突" in SYSTEM_PROMPT
    assert '"reference_case_id"' in SYSTEM_PROMPT
    query = "主体：红色机器人；背景：白色\n标题（原样）：明天见"
    context = build_agent_context(query, [])
    assert query in context


def test_selected_reference_case_is_resolved_and_unknown_selection_is_recoverable():
    agent = ChatAgent()
    output = '''```json
{"card":{"title":"方案","design_rationale":"说明","prompt":"用户需求","style":"default"}}
```'''

    _, valid_card = agent.parse_proposal_card(output, [], reference_case_id="ref-03")
    error_text, invalid_card = agent.parse_proposal_card(output, [], reference_case_id="ref-999")

    assert valid_card is not None
    assert valid_card.matched_reference.id == "ref-03"
    assert invalid_card is None
    assert "案例不存在" in error_text


def test_invalid_modification_flag_returns_recoverable_error():
    agent = ChatAgent()
    output = '''```json
{"card":{"title":"方案","design_rationale":"说明","prompt":"用户需求","style":"default","is_modification":"false"}}
```'''

    error_text, card = agent.parse_proposal_card(output, [])

    assert card is None
    assert "连续修改标记格式无效" in error_text


def test_chat_includes_explicit_reference_and_unknown_id_stops_before_model_call():
    agent = ChatAgent()
    model_output = '''```json
{"card":{"title":"方案","design_rationale":"说明","prompt":"主体：红色机器人；背景：白色","style":"default"}}
```'''
    with patch("media.agent.chat_agent.search_references", return_value=[]), \
         patch.object(agent, "_call_deepseek_api", return_value=model_output) as call_model:
        response = agent.chat([], "设计机器人海报", reference_case_id="ref-03")
        invalid_response = agent.chat([], "设计机器人海报", reference_case_id="ref-999")

    assert response.proposal_card.matched_reference.id == "ref-03"
    assert "用户明确选择的参考案例 ID】：ref-03" in call_model.call_args.args[0][-1]["content"]
    assert invalid_response.proposal_card is None
    assert invalid_response.needs_clarification is True
    assert "案例不存在" in invalid_response.reply_text
    assert call_model.call_count == 1


def test_confirmed_final_prompt_is_sent_without_rewrapping():
    core = ImageCore()
    db = MagicMock()
    task_record = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = task_record

    @contextmanager
    def isolated_session():
        yield db

    with patch("media.core.image_service.session_scope", isolated_session), \
         patch.object(core, "_call_transit_api", return_value=b"image-bytes") as call_api, \
         patch("media.core.image_service.cos_storage.upload_bytes", return_value="https://example.invalid/image.png"):
        result = core.generate(
            prompt="主体：红色机器人；背景：白色",
            final_prompt="主体：红色机器人；背景：白色\n用户确认的最终提示词",
            style="tech_poster",
            quality="low",
        )

    assert result.status == "success"
    assert call_api.call_args.kwargs["final_prompt"] == "主体：红色机器人；背景：白色\n用户确认的最终提示词"
    assert result.final_prompt == call_api.call_args.kwargs["final_prompt"]


def test_confirmation_route_forwards_confirmed_prompt():
    request = ConfirmGenerateRequest(
        prompt="主体：红色机器人；背景：白色",
        final_prompt="主体：红色机器人；背景：白色\n用户确认的最终提示词",
    )
    result = ImageResult(
        prompt=request.prompt,
        quality="low",
        task_id="task-test",
        status="failed",
        image_url="",
        duration_ms=0,
        final_prompt=request.final_prompt,
        aspect_ratio="1:1",
        style="tech_poster",
        ref_image_url=None,
        parent_id=None,
        error_msg="test only",
    )

    with patch("media.web.backend.api.image_router.image_core.generate", return_value=result) as generate:
        confirm_and_generate(request, user=SimpleNamespace(id=7))

    assert generate.call_args.kwargs["final_prompt"] == request.final_prompt


def test_agent_prompt_preserves_user_intent_without_unsolicited_redirection():
    assert "准确理解并保留用户原意" in SYSTEM_PROMPT
    assert "不要未经用户同意就把它改写成另一张图" in SYSTEM_PROMPT
    assert "用户只是讨论或提问" in SYSTEM_PROMPT
    assert "不强制输出方案卡" in SYSTEM_PROMPT
    assert "不允许绕过底层模型或 Provider 自身的安全规则" in SYSTEM_PROMPT
