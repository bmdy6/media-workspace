"""media/tests/test_web_api.py - Web 后端接口端到端单元测试"""
from __future__ import annotations

from unittest.mock import patch
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from media.database import session as database_session
from media.database.models import Base

from media.database import session_scope, User, InviteCode
from media.web.backend.main import app
from media.core.image_service import ImageResult

@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'web-api.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database_session, "_engine", engine)
    monkeypatch.setattr(database_session, "_SessionFactory", sessionmaker(bind=engine))
    with TestClient(app) as api:
        yield api
    engine.dispose()


def test_auth_and_invite_flow(client):
    """验证邀请码注册、登录与个人信息获取"""
    # 1. 准备测试邀请码
    with session_scope() as db:
        db.query(InviteCode).filter(InviteCode.code == "FLOWTEST123").delete()
        db.query(User).filter(User.username == "flow_user").delete()

        inv = InviteCode(code="FLOWTEST123", status="active")
        db.add(inv)

    # 2. 错误邀请码注册 -> 失败
    bad_reg = client.post("/api/auth/register", json={
        "username": "flow_user",
        "password": "password123",
        "invite_code": "INVALID_CODE",
    })
    assert bad_reg.status_code == 400

    # 3. 正确邀请码注册 -> 成功
    ok_reg = client.post("/api/auth/register", json={
        "username": "flow_user",
        "password": "password123",
        "invite_code": "FLOWTEST123",
    })
    assert ok_reg.status_code == 200
    token = ok_reg.json()["access_token"]
    assert token != ""

    # 4. 同一邀请码再次使用 -> 失败 (已使用)
    reuse_reg = client.post("/api/auth/register", json={
        "username": "flow_user_2",
        "password": "password123",
        "invite_code": "FLOWTEST123",
    })
    assert reuse_reg.status_code == 400

    # 5. 登录接口 -> 成功
    login_resp = client.post("/api/auth/login", json={
        "username": "flow_user",
        "password": "password123",
    })
    assert login_resp.status_code == 200

    # 6. /api/auth/me 获取个人信息
    me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["username"] == "flow_user"


def test_chat_and_confirm_generation_flow(client):
    """验证完整的两阶段流程：对话获取方案卡片 -> 确认生成图片"""
    # 登录预置的 admin
    login_resp = client.post("/api/auth/login", json={
        "username": "admin",
        "password": "password123",
    })
    if login_resp.status_code != 200:
        # 若密码不同，使用通用注册用户
        with session_scope() as db:
            db.query(InviteCode).filter(InviteCode.code == "ADMIN_TEST_INV").delete()
            db.query(User).filter(User.username == "api_tester").delete()
            db.add(InviteCode(code="ADMIN_TEST_INV", status="active"))
        reg = client.post("/api/auth/register", json={
            "username": "api_tester",
            "password": "password123",
            "invite_code": "ADMIN_TEST_INV",
        })
        token = reg.json()["access_token"]
    else:
        token = login_resp.json()["access_token"]

    headers = {"Authorization": f"Bearer {token}"}

    # 阶段 1: 发送对话，获取方案卡片
    mock_agent_reply = """已为你设计好科技海报方案。
```json
{
  "card": {
    "title": "测试科技海报",
    "design_rationale": "深色背景与荧光矩阵",
    "prompt": "深色背景，发光神经网络芯片，8K",
    "style": "tech_poster",
    "aspect_ratio": "3:4",
    "is_modification": false
  }
}
```"""
    with patch("media.agent.chat_agent.chat_agent._call_deepseek_api", return_value=mock_agent_reply):
        chat_resp = client.post("/api/chat/message", json={
            "session_id": "sess-test-01",
            "message": "帮我做一张科技感海报",
        }, headers=headers)

        assert chat_resp.status_code == 200
        chat_data = chat_resp.json()
        assert chat_data["proposal_card"] is not None
        assert chat_data["proposal_card"]["style"] == "tech_poster"

    # 阶段 2: 确认生成卡片
    mock_img_result = ImageResult(
        task_id="task-test-confirm-1",
        image_url="https://cos.myqcloud.com/creations/confirm_out.png",
        status="success",
        duration_ms=1800,
        prompt="深色背景，发光神经网络芯片，8K",
        final_prompt="final prompt",
        aspect_ratio="3:4",
        style="tech_poster",
        quality="low",
    )

    with patch("media.web.backend.api.image_router.image_core.generate", return_value=mock_img_result):
        gen_resp = client.post("/api/image/generate", json={
            "prompt": "深色背景，发光神经网络芯片，8K",
            "style": "tech_poster",
            "aspect_ratio": "3:4",
            "session_id": "sess-test-01",
        }, headers=headers)

        assert gen_resp.status_code == 200
        gen_data = gen_resp.json()
        assert gen_data["status"] == "success"
        assert gen_data["image_url"] == "https://cos.myqcloud.com/creations/confirm_out.png"

    # 检查会话记录中已包含生成图片
    hist_resp = client.get("/api/chat/history?session_id=sess-test-01", headers=headers)
    assert hist_resp.status_code == 200
    msgs = hist_resp.json()["messages"]
    assert any(m.get("image_url") == "https://cos.myqcloud.com/creations/confirm_out.png" for m in msgs)
