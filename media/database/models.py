from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Text,
    JSON,
    UniqueConstraint,
)

from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=get_utc_now)

    image_tasks = relationship("ImageTask", back_populates="user")
    created_invites = relationship("InviteCode", foreign_keys="InviteCode.created_by", back_populates="creator")

class InviteCode(Base):
    __tablename__ = "invite_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(32), unique=True, index=True, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    used_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    used_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="active")  # active, used, revoked
    created_at = Column(DateTime, default=get_utc_now)

    creator = relationship("User", foreign_keys=[created_by], back_populates="created_invites")
    consumer = relationship("User", foreign_keys=[used_by])

class ImageTask(Base):
    __tablename__ = "image_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String(64), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    prompt = Column(Text, nullable=False)
    final_prompt = Column(Text, nullable=False)
    style = Column(String(50), default="default")
    aspect_ratio = Column(String(20), default="1:1")
    quality = Column(String(20), default="low")
    ref_image_url = Column(Text, nullable=True)
    parent_id = Column(Integer, nullable=True, index=True)  # 版本衍生树
    image_url = Column(String(500), nullable=True)          # COS 链接
    status = Column(String(20), default="pending", index=True)  # pending, generating, success, failed
    error_msg = Column(Text, nullable=True)
    duration_ms = Column(Integer, default=0)
    model_id = Column(String(128), nullable=True)  # 实际使用的生图模型
    requested_model_id = Column(String(128), nullable=True)
    fallback_from_model_id = Column(String(128), nullable=True)
    fallback_reason = Column(String(128), nullable=True)
    storage_policy = Column(String(64), nullable=True)
    request_id = Column(String(128), nullable=True)
    phase = Column(String(32), nullable=True)
    persistence_status = Column(String(32), nullable=True)
    source_url = Column(Text, nullable=True)
    result_path = Column(Text, nullable=True)
    result_mime_type = Column(String(64), nullable=True)
    result_expires_at = Column(DateTime, nullable=True)
    timings_json = Column(Text, nullable=True)
    payload_hash = Column(String(64), nullable=True)
    idempotency_key = Column(String(64), nullable=True, unique=True)
    session_id = Column(String(64), nullable=True, index=True)
    proposal_card_id = Column(String(128), nullable=True, index=True)
    created_at = Column(DateTime, default=get_utc_now, index=True)

    user = relationship("User", back_populates="image_tasks")

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(64), index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    role = Column(String(20), nullable=False)  # user, assistant, system
    content = Column(Text, nullable=False)
    card_data = Column(Text, nullable=True)    # JSON 方案卡片数据
    image_url = Column(String(500), nullable=True)
    image_role = Column(String(32), nullable=True)  # reference, base, artwork, visual_input_cleared
    image_task_id = Column(Integer, ForeignKey("image_tasks.id"), nullable=True, index=True)
    model_id = Column(String(128), nullable=True)  # 实际使用的构思模型
    requested_model_id = Column(String(128), nullable=True)
    fallback_from_model_id = Column(String(128), nullable=True)
    fallback_reason = Column(String(128), nullable=True)
    thinking_mode = Column(String(32), nullable=True)
    reference_case_id = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=get_utc_now, index=True)
