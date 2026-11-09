"""Standalone identity and configuration boundaries, using synthetic SQLite data."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from media.database import session as database_session
from media.database.models import Base, InviteCode, User
from media.web.backend.main import app


@pytest.fixture
def standalone_api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'standalone.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database_session, "_engine", engine)
    monkeypatch.setattr(database_session, "_SessionFactory", sessionmaker(bind=engine))
    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def test_no_host_identity_exchange_or_unrelated_routes(standalone_api):
    client, _ = standalone_api
    assert client.get("/api/auth/me", headers={"X-Workbench-User": "admin"}).status_code == 401
    paths = set(app.openapi()["paths"])
    assert not any("workbench" in path or "feedback" in path or "worklog" in path for path in paths)
    assert client.post("/api/auth/workbench-token", json={}).status_code in {404, 405}


def test_invite_is_consumed_once_under_concurrent_registration(standalone_api):
    client, engine = standalone_api
    with database_session.session_scope() as db:
        db.add(InviteCode(code="SYNTHETIC-ONCE", status="active"))
    selected = Barrier(2)

    def synchronize_existing_invite_reads(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "invite_codes" in statement and "SYNTHETIC-ONCE" in str(parameters):
            selected.wait(timeout=5)

    event.listen(engine, "after_cursor_execute", synchronize_existing_invite_reads)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda name: client.post("/api/auth/register", json={
                "username": name, "password": "synthetic-password", "invite_code": "SYNTHETIC-ONCE",
            }), ("racing_one", "racing_two")))
    finally:
        event.remove(engine, "after_cursor_execute", synchronize_existing_invite_reads)
    assert sorted(response.status_code for response in results) == [200, 400]
    with database_session.session_scope() as db:
        assert db.query(User).count() == 1
        invite = db.query(InviteCode).one()
        assert invite.status == "used" and invite.used_by == db.query(User).one().id


def test_new_registration_rejects_passwords_that_bcrypt_would_truncate(standalone_api):
    client, _ = standalone_api
    with database_session.session_scope() as db:
        db.add(InviteCode(code="SYNTHETIC-LENGTH", status="active"))
    response = client.post("/api/auth/register", json={
        "username": "too_long", "password": "字" * 25, "invite_code": "SYNTHETIC-LENGTH",
    })
    assert response.status_code == 400
    with database_session.session_scope() as db:
        assert db.query(User).count() == 0
        assert db.query(InviteCode).one().status == "active"


def test_missing_environment_configuration_never_reads_host_files(monkeypatch):
    from media.core.config import ConfigError, get_config
    monkeypatch.delenv("SYNTHETIC_CONFIG_KEY", raising=False)
    monkeypatch.setattr("pathlib.Path.read_text", lambda *a, **k: pytest.fail("must not read a host configuration file"))
    with pytest.raises(ConfigError, match="SYNTHETIC_CONFIG_KEY"):
        get_config("SYNTHETIC_CONFIG_KEY", required=True)


def test_startup_rejects_short_signing_key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "short")
    with pytest.raises(RuntimeError, match="at least 32"):
        with TestClient(app):
            pass


@pytest.mark.parametrize("legacy_secret", [None, "synthetic-old-host-secret"])
def test_text_requests_use_only_explicit_provider_configuration(monkeypatch, legacy_secret):
    from media.core.text_completion import _chat_request
    if legacy_secret:
        monkeypatch.setenv("OPENAI_RELAY_SHARED_SECRET", legacy_secret)
    else:
        monkeypatch.delenv("OPENAI_RELAY_SHARED_SECRET", raising=False)
    result = _chat_request([{"role": "user", "content": "synthetic"}],
                           model_id="gpt-6-luna", thinking_mode="low", stream=False)
    headers = result[5]
    assert "X-Relay-Key" not in headers
    assert result[3] == "https://chat.invalid/v1/chat/completions"


def test_failed_user_creation_rolls_back_invite_claim(standalone_api, monkeypatch):
    client, _ = standalone_api
    with database_session.session_scope() as db:
        db.add(InviteCode(code="SYNTHETIC-ROLLBACK", status="active"))
    def fail(_password):
        raise ValueError("synthetic hash failure")
    monkeypatch.setattr("media.web.backend.api.auth_router.hash_password", fail)
    with pytest.raises(ValueError, match="synthetic hash failure"):
        client.post("/api/auth/register", json={"username":"rolled_back", "password":"synthetic-password", "invite_code":"SYNTHETIC-ROLLBACK"})
    with database_session.session_scope() as db:
        assert db.query(User).count() == 0
        assert db.query(InviteCode).one().status == "active"


@pytest.mark.parametrize("stream", [False, True])
def test_chat_sends_no_host_relay_secret(monkeypatch, stream):
    import httpx
    from media.agent.chat_agent import chat_agent
    monkeypatch.setenv("OPENAI_RELAY_SHARED_SECRET", "synthetic-host-secret-not-to-send")
    real_client = httpx.Client
    captured = []
    def response(request):
        captured.append(request)
        if stream:
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"synthetic"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={"choices":[{"message":{"content":"synthetic"}}]})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs))
    if stream:
        list(chat_agent._iter_deepseek_stream([{"role":"user","content":"synthetic"}], model_id="gpt-6-luna", thinking_mode="low"))
    else:
        chat_agent._call_deepseek_api([{"role":"user","content":"synthetic"}], model_id="gpt-6-luna", thinking_mode="low")
    assert len(captured) == 1
    assert str(captured[0].url) == "https://chat.invalid/v1/chat/completions"
    assert "x-relay-key" not in captured[0].headers
