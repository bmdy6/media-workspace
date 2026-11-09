"""media/agent/card_schema.py - 方案确认卡片与会话协议数据契约。"""
from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class ReferenceCaseItem(BaseModel):
    id: str
    title: str
    style: str
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    recommended_ratio: str = "1:1"
    preview_url: str = ""


class ProposalCard(BaseModel):
    """AI 生成后呈现给用户的交互式方案确认卡片。

    用户在 Web 界面看到此卡片，确认无误后点击「确认生成」才触发底层生图。
    """
    card_id: str
    title: str
    design_rationale: str  # 设计构思与审美阐述
    prompt: str            # 提炼优化后的具体生图提示词
    final_prompt: Optional[str] = None # 已展示给用户确认、将原样提交给生图服务的完整提示词；兼容旧卡片
    style: str = "default" # 推荐风格预设
    aspect_ratio: str = "1:1" # 推荐画幅比例
    quality: str = "low"
    is_modification: bool = False # 是否基于上一代图修改 (图生图)
    base_image_url: Optional[str] = None # 兼容旧客户端：仅修改底图时填充
    visual_input_url: Optional[str] = None # 本轮真正携带给生图模型的用户图片
    visual_input_role: Optional[Literal["reference", "base"]] = None
    reference_recommended: bool = False # AI 可建议用户补参考图，但不得作为生成前置条件
    reference_reason: Optional[str] = None # 面向用户的简短建议原因
    matched_reference: Optional[ReferenceCaseItem] = None # 推荐的系统灵感案例


class VisualContextState(BaseModel):
    url: str
    role: Literal["reference", "base"]
    image_task_id: Optional[int] = None
    remaining_turns: int
    turn_limit: int


class AgentChatResponse(BaseModel):
    """Agent 对话回合的完整响应"""
    reply_text: str  # 自然语言阐述与回复
    proposal_card: Optional[ProposalCard] = None # 若准备就绪，附带确认方案卡片
    needs_clarification: bool = False # 是否处于关键提问状态
    required_input: Optional[Literal["reference_image"]] = None
    model_id: Optional[str] = None
    requested_model_id: Optional[str] = None
    fallback_from_model_id: Optional[str] = None
    fallback_reason: Optional[str] = None
    thinking_mode: Optional[str] = None
    reference_case_id: Optional[str] = None
    image_model_id: Optional[str] = None
    visual_context: Optional[VisualContextState] = None
