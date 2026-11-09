"""Regression tests for authenticated chat history and context recovery."""
from __future__ import annotations

import os
import io
from PIL import Image
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.update({
    "OPENAI_API_KEY": "test-only",
    "OPENAI_BASE_URL": "https://example.invalid/v1",
    "DEEPSEEK_API_KEY": "test-only",
    "DEEPSEEK_BASE_URL": "https://example.invalid/v1",
    "SECRET_KEY": "test-only-chat-history-key",
    "TENCENT_CLOUD_SECRET_ID": "test-only",
    "TENCENT_CLOUD_SECRET_KEY": "test-only",
    "TENCENT_CLOUD_COS_REGION": "test-only",
    "TENCENT_CLOUD_COS_BUCKET": "test-only",
})

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from media.database import Base, ChatMessage, ImageTask, User, session_scope  # noqa: E402
import media.database.session as db_session  # noqa: E402
from media.web.backend.auth import get_current_user  # noqa: E402
from media.web.backend.main import app  # noqa: E402


_db_dir = tempfile.TemporaryDirectory(prefix="media-chat-history-")
_db_path = Path(_db_dir.name) / "chat-history.sqlite"
db_session._engine = create_engine(f"sqlite:///{_db_path}", connect_args={"check_same_thread": False})
db_session._SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=db_session._engine)
Base.metadata.create_all(bind=db_session._engine)

client = TestClient(app)


@pytest.fixture(autouse=True)
def restore_dependency_overrides():
    previous = app.dependency_overrides.copy()
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def _reset_db():
    Base.metadata.drop_all(bind=db_session._engine)
    Base.metadata.create_all(bind=db_session._engine)
    with session_scope() as db:
        first = User(id=1, username="history_a", password_hash="dummy")
        second = User(id=2, username="history_b", password_hash="dummy")
        db.add_all([first, second])
    return User(id=1, username="history_a", password_hash="dummy"), User(
        id=2, username="history_b", password_hash="dummy"
    )


def _as_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _message(user_id: int, session_id: str, role: str, content: str, **kwargs) -> ChatMessage:
    return ChatMessage(user_id=user_id, session_id=session_id, role=role, content=content, **kwargs)


def test_context_uses_latest_records_in_chronological_id_order(monkeypatch):
    user, _ = _reset_db()
    _as_user(user)
    monkeypatch.setenv("MEDIA_CHAT_HISTORY_LIMIT", "3")
    created_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with session_scope() as db:
        db.add_all([
            _message(1, "shared", "user", "earliest", created_at=created_at),
            _message(1, "shared", "assistant", "middle", created_at=created_at),
            _message(1, "shared", "user", "latest-1", created_at=created_at),
            _message(1, "shared", "assistant", "latest-2", created_at=created_at),
        ])

    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        from media.agent.card_schema import AgentChatResponse
        return AgentChatResponse(reply_text="ok")

    with patch("media.web.backend.api.chat_router.chat_agent.chat", side_effect=fake_chat):
        response = client.post("/api/chat/message", json={"session_id": "shared", "message": "new request"})
    assert response.status_code == 200
    contents = [message["content"] for message in captured["messages"]]
    assert contents == ["middle", "latest-1", "latest-2"]
    assert "earliest" not in contents


def test_sessions_and_history_are_scoped_and_restore_cards(monkeypatch):
    user_a, user_b = _reset_db()
    valid_card = '{"card_id":"c1","title":"海报","design_rationale":"留白","prompt":"蓝色玻璃建筑，标题写春日","style":"default","aspect_ratio":"1:1","quality":"low","is_modification":false,"base_image_url":null,"matched_reference":null}'
    with session_scope() as db:
        db.add_all([
            _message(1, "same-id", "user", "请做春日海报"),
            _message(1, "same-id", "assistant", "方案已就绪", card_data=valid_card),
            _message(1, "same-id", "assistant", "生成完成", image_url="https://img.example/a.png"),
            _message(1, "broken-card", "assistant", "坏卡片", card_data="{bad json"),
            _message(1, "broken-card", "user", "后续消息仍应显示"),
            _message(2, "same-id", "user", "私有消息"),
            _message(2, "only-b", "user", "另一个用户会话"),
        ])

    _as_user(user_a)
    listing = client.get("/api/chat/sessions").json()["sessions"]
    assert {item["session_id"] for item in listing} == {"same-id", "broken-card"}
    same = next(item for item in listing if item["session_id"] == "same-id")
    assert same["title"] == "请做春日海报"
    assert same["message_count"] == 3
    assert same["last_image_url"] == "https://img.example/a.png"
    assert same["updated_at"]

    history = client.get("/api/chat/history?session_id=same-id").json()["messages"]
    restored = next(message for message in history if message["card_data"])
    assert restored["card_data"]["prompt"] == "蓝色玻璃建筑，标题写春日"
    assert any(message["image_url"] == "https://img.example/a.png" for message in history)
    damaged = client.get("/api/chat/history?session_id=broken-card").json()["messages"]
    assert len(damaged) == 2
    assert next(message for message in damaged if message["id"])["id"]
    assert next(message for message in damaged if message["content"] == "坏卡片")["card_error"]
    assert client.get("/api/chat/history?session_id=missing").json() == {"messages": [], "visual_context": None}

    _as_user(user_b)
    assert [item["session_id"] for item in client.get("/api/chat/sessions").json()["sessions"]] == ["only-b", "same-id"]
    b_history = client.get("/api/chat/history?session_id=same-id").json()["messages"]
    assert [message["content"] for message in b_history] == ["私有消息"]


def test_proposal_card_visual_input_persists_per_card_and_survives_history_restore():
    user_a, user_b = _reset_db()
    card = '{"card_id":"card-ref","title":"人物海报","design_rationale":"自然光","prompt":"人物街拍","style":"portrait","aspect_ratio":"3:4","quality":"low","is_modification":false,"base_image_url":null,"visual_input_url":null,"visual_input_role":null}'
    sibling = '{"card_id":"card-sibling","title":"另一张卡","design_rationale":"留白","prompt":"抽象海报","style":"minimal","aspect_ratio":"1:1","quality":"low","is_modification":false,"base_image_url":null,"visual_input_url":null,"visual_input_role":null}'
    with session_scope() as db:
        db.add_all([
            _message(1, "card-visual", "assistant", "人物方案", card_data=card),
            _message(1, "card-visual", "assistant", "另一方案", card_data=sibling),
            _message(1, "card-visual", "system", "参考图已上传", image_url="https://img.example/user-ref.png", image_role="reference"),
            _message(2, "card-visual", "system", "别人的参考图", image_url="https://img.example/foreign.png", image_role="reference"),
        ])

    _as_user(user_a)
    attached = client.patch("/api/chat/proposal-card/visual-input", json={
        "session_id": "card-visual",
        "card_id": "card-ref",
        "visual_input_url": "https://img.example/user-ref.png",
        "visual_input_role": "reference",
    })
    assert attached.status_code == 200
    assert attached.json()["card"]["visual_input_url"] == "https://img.example/user-ref.png"

    history = client.get("/api/chat/history?session_id=card-visual").json()["messages"]
    restored = next(item["card_data"] for item in history if item["card_data"] and item["card_data"]["card_id"] == "card-ref")
    sibling_restored = next(item["card_data"] for item in history if item["card_data"] and item["card_data"]["card_id"] == "card-sibling")
    assert restored["visual_input_url"] == "https://img.example/user-ref.png"
    assert restored["visual_input_role"] == "reference"
    assert sibling_restored["visual_input_url"] is None

    removed = client.patch("/api/chat/proposal-card/visual-input", json={
        "session_id": "card-visual", "card_id": "card-ref",
        "visual_input_url": None, "visual_input_role": None,
    })
    assert removed.status_code == 200
    history_after_remove = client.get("/api/chat/history?session_id=card-visual").json()
    restored_after_remove = next(item["card_data"] for item in history_after_remove["messages"] if item["card_data"] and item["card_data"]["card_id"] == "card-ref")
    assert restored_after_remove["visual_input_url"] is None
    assert restored_after_remove["visual_input_role"] is None
    assert history_after_remove["visual_context"]["url"] == "https://img.example/user-ref.png"

    from media.database.models import ImageTask
    with session_scope() as db:
        db.add(ImageTask(
            task_id="already-submitted", user_id=user_a.id, prompt="x", final_prompt="x",
            session_id="card-visual", proposal_card_id="card-ref",
        ))
    submitted = client.patch("/api/chat/proposal-card/visual-input", json={
        "session_id": "card-visual", "card_id": "card-ref",
        "visual_input_url": None, "visual_input_role": None,
    })
    assert submitted.status_code == 409

    foreign_ref = client.patch("/api/chat/proposal-card/visual-input", json={
        "session_id": "card-visual", "card_id": "card-ref",
        "visual_input_url": "https://img.example/foreign.png", "visual_input_role": "reference",
    })
    assert foreign_ref.status_code == 422

    _as_user(user_b)
    cannot_touch_other_users_card = client.patch("/api/chat/proposal-card/visual-input", json={
        "session_id": "card-visual", "card_id": "card-ref",
        "visual_input_url": None, "visual_input_role": None,
    })
    assert cannot_touch_other_users_card.status_code == 404


def test_context_includes_previous_request_card_and_image_reference():
    user, _ = _reset_db()
    _as_user(user)
    with session_scope() as db:
        db.add_all([
            _message(1, "context", "user", "帮我做春日海报"),
            _message(1, "context", "assistant", "方案说明", card_data='{"prompt":"蓝色玻璃建筑","design_rationale":"春日柔光"}'),
            _message(1, "context", "user", "参考图已上传", image_url="https://img.example/ref.png"),
        ])
    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        from media.agent.card_schema import AgentChatResponse
        return AgentChatResponse(reply_text="ok")

    with patch("media.web.backend.api.chat_router.chat_agent.chat", side_effect=fake_chat):
        response = client.post("/api/chat/message", json={"session_id": "context", "message": "加上标题"})
    assert response.status_code == 200
    context = "\n".join(item["content"] for item in captured["messages"])
    assert "帮我做春日海报" in context
    assert "蓝色玻璃建筑" in context
    assert "春日柔光" in context
    assert "https://img.example/ref.png" in context


def test_reference_resume_message_is_persisted_as_hidden_system_context():
    user, _ = _reset_db()
    _as_user(user)
    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        from media.agent.card_schema import AgentChatResponse
        return AgentChatResponse(reply_text="ok")

    with patch("media.web.backend.api.chat_router.chat_agent.chat", side_effect=fake_chat):
        response = client.post("/api/chat/message", json={
            "session_id": "resume",
            "message": "继续刚才的需求，使用当前参考图。",
            "current_image_url": "https://img.example/ref.png",
            "current_image_role": "reference",
            "resume_after_reference": True,
        })
    assert response.status_code == 200
    with session_scope() as db:
        saved = db.query(ChatMessage).filter(ChatMessage.session_id == "resume").first()
        assert saved.role == "system"
        assert saved.image_role == "reference"


def test_reference_image_is_not_used_as_session_artwork_preview():
    user, _ = _reset_db()
    _as_user(user)
    with session_scope() as db:
        db.add_all([
            _message(1, "preview", "user", "用我的头像做海报"),
            _message(1, "preview", "system", "参考图已上传", image_url="https://img.example/ref.png", image_role="reference"),
            _message(1, "preview", "assistant", "作品完成", image_url="https://img.example/art.png", image_role="artwork"),
        ])
    session = client.get("/api/chat/sessions").json()["sessions"][0]
    assert session["last_image_url"] == "https://img.example/art.png"


def test_reference_upload_persists_optional_session_and_old_call_still_works():
    image = io.BytesIO()
    Image.new("RGB", (40, 80), "blue").save(image, format="PNG")
    image_bytes = image.getvalue()
    user, _ = _reset_db()
    _as_user(user)
    assert client.get("/api/chat/sessions").json() == {"sessions": []}
    with patch("media.core.image_service.cos_storage.upload_bytes", return_value="https://img.example/upload.png"):
        response = client.post(
            "/api/image/upload_ref",
            files={"file": ("ref.png", image_bytes, "image/png")},
            data={"session_id": "upload-session"},
        )
        assert response.status_code == 200
        assert response.json()["ref_url"] == "https://img.example/upload.png"
        assert response.json()["visual_context"] == {
            "url": "https://img.example/upload.png", "role": "reference",
            "image_task_id": None, "remaining_turns": 10, "turn_limit": 10,
        }
        history_response = client.get("/api/chat/history?session_id=upload-session").json()
        assert history_response["visual_context"]["remaining_turns"] == 10
        history = history_response["messages"]
        assert len(history) == 1
        assert history[0]["role"] == "system"
        assert history[0]["image_url"] == "https://img.example/upload.png"
        assert history[0]["image_role"] == "reference"

        old_call = client.post(
            "/api/image/upload_ref", files={"file": ("ref.png", image_bytes, "image/png")}
        )
    assert old_call.status_code == 200
    assert len(client.get("/api/chat/history?session_id=upload-session").json()["messages"]) == 1


def test_card_scoped_reference_upload_does_not_activate_session_context():
    image = io.BytesIO()
    Image.new("RGB", (40, 80), "green").save(image, format="PNG")
    image_bytes = image.getvalue()
    user, _ = _reset_db()
    _as_user(user)
    with patch("media.core.image_service.cos_storage.upload_bytes", return_value="https://img.example/card-only.png"):
        response = client.post(
            "/api/image/upload_ref",
            files={"file": ("ref.png", image_bytes, "image/png")},
            data={"session_id": "card-upload", "activate_context": "false"},
        )
    assert response.status_code == 200
    assert response.json()["ref_url"] == "https://img.example/card-only.png"
    assert response.json()["visual_context"] is None
    history = client.get("/api/chat/history?session_id=card-upload").json()
    assert history["visual_context"] is None
    assert len(history["messages"]) == 1
    assert history["messages"][0]["image_url"] == "https://img.example/card-only.png"
    assert history["messages"][0]["image_role"] == "reference_candidate"


def test_task_status_is_hidden_from_other_users():
    user_a, user_b = _reset_db()
    with session_scope() as db:
        db.add(ImageTask(
            task_id="private-task",
            user_id=user_a.id,
            prompt="private",
            final_prompt="private",
            status="success",
        ))
    _as_user(user_b)
    response = client.get("/api/image/task/private-task")
    assert response.status_code == 404
    _as_user(user_a)
    own_response = client.get("/api/image/task/private-task")
    assert own_response.status_code == 200
    assert own_response.json()["task_id"] == "private-task"


def test_visual_context_restores_without_explicit_image_and_expires_by_user_turn(monkeypatch):
    user, _ = _reset_db()
    _as_user(user)
    monkeypatch.setenv("MEDIA_VISUAL_CONTEXT_TURN_LIMIT", "2")
    with session_scope() as db:
        db.add(_message(
            1, "visual-ttl", "system", "参考图已上传",
            image_url="https://img.example/ref.png", image_role="reference",
        ))

    seen = []

    def fake_chat(**kwargs):
        seen.append((kwargs["current_image_url"], kwargs["current_image_role"]))
        from media.agent.card_schema import AgentChatResponse
        return AgentChatResponse(reply_text="ok")

    with patch("media.web.backend.api.chat_router.chat_agent.chat", side_effect=fake_chat):
        first = client.post("/api/chat/message", json={"session_id": "visual-ttl", "message": "第一轮"})
        second = client.post("/api/chat/message", json={"session_id": "visual-ttl", "message": "第二轮"})
        third = client.post("/api/chat/message", json={"session_id": "visual-ttl", "message": "第三轮"})

    assert seen == [
        ("https://img.example/ref.png", "reference"),
        ("https://img.example/ref.png", "reference"),
        (None, None),
    ]
    assert first.json()["visual_context"]["remaining_turns"] == 1
    assert second.json()["visual_context"] is None
    assert third.json()["visual_context"] is None


def test_visual_context_replacement_resets_budget_and_clear_stops_reuse(monkeypatch):
    user, _ = _reset_db()
    _as_user(user)
    monkeypatch.setenv("MEDIA_VISUAL_CONTEXT_TURN_LIMIT", "3")
    with session_scope() as db:
        db.add(ImageTask(id=42, task_id="owned-base", user_id=1, prompt="synthetic", final_prompt="synthetic",
                         image_url="https://img.example/base.png", status="success"))
        db.add(_message(
            1, "visual-replace", "system", "参考图已上传",
            image_url="https://img.example/ref.png", image_role="reference",
        ))

    seen = []

    def fake_chat(**kwargs):
        seen.append((kwargs["current_image_url"], kwargs["current_image_role"]))
        from media.agent.card_schema import AgentChatResponse
        return AgentChatResponse(reply_text="ok")

    with patch("media.web.backend.api.chat_router.chat_agent.chat", side_effect=fake_chat):
        replaced = client.post("/api/chat/message", json={
            "session_id": "visual-replace", "message": "基于作品继续",
            "current_image_url": "https://img.example/base.png",
            "current_image_role": "base",
            "current_image_task_id": 42,
        })
    assert seen[-1] == ("https://img.example/base.png", "base")
    assert replaced.json()["visual_context"] == {
        "url": "https://img.example/base.png", "role": "base", "image_task_id": 42,
        "remaining_turns": 2, "turn_limit": 3,
    }

    cleared = client.post("/api/image/visual-input/clear", data={"session_id": "visual-replace"})
    assert cleared.status_code == 200
    assert cleared.json()["visual_context"] is None
    seen.clear()
    with patch("media.web.backend.api.chat_router.chat_agent.chat", side_effect=fake_chat):
        client.post("/api/chat/message", json={"session_id": "visual-replace", "message": "新话题"})
    assert seen == [(None, None)]
    history = client.get("/api/chat/history?session_id=visual-replace").json()
    assert history["visual_context"] is None
