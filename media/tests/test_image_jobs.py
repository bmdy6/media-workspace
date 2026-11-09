from __future__ import annotations

import io
import json
import os
import time
from threading import Event, Thread

os.environ.setdefault("SECRET_KEY", "test-only-image-jobs-secret")
os.environ.setdefault("MEDIA_USE_MYSQL", "0")

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import media.database.session as database_session
from media.database.models import Base, ImageTask, User
from media.web.backend.auth import create_access_token
from media.core.image_service import ProviderImageResult


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (1024, 1024), "red").save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def jobs_api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'image-jobs.sqlite'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database_session, "_engine", engine)
    monkeypatch.setattr(database_session, "_SessionFactory", sessionmaker(bind=engine))
    from media.database import session_scope
    from media.web.backend.main import app
    with session_scope() as db:
        a, b = User(username="job_a", password_hash="x"), User(username="job_b", password_hash="x")
        db.add_all([a, b])
        db.flush()
        ids = (a.id, b.id)
    tokens = [create_access_token({"sub": name}) for name in ("job_a", "job_b")]
    with TestClient(app) as client:
        yield client, tokens, ids, session_scope
    engine.dispose()


def _submit(client, token, **extra):
    payload = {"prompt": "test image", "request_id": "r1", "storage_policy": "background_cos", **extra}
    return client.post("/api/image/jobs", json=payload, headers={"Authorization": f"Bearer {token}"})


def _wait_status(client, token, task_id, status="success"):
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(100):
        response = client.get(f"/api/image/task/{task_id}", headers=headers)
        assert response.status_code == 200
        data = response.json()
        if data["status"] == status and (status != "success" or data["phase"] in {"result_ready", "persisting", "completed"}):
            return data
        time.sleep(.01)
    raise AssertionError(f"image job did not finish in time: {data}")



def test_disabled_flare_rejects_before_any_paid_call(monkeypatch, jobs_api):
    client, tokens, _, _ = jobs_api
    import media.core.image_jobs as jobs

    monkeypatch.setattr(
        jobs.image_core,
        "_call_transit_api",
        lambda **kwargs: pytest.fail("disabled model must be rejected before provider call"),
    )
    response = _submit(
        client, tokens[0], request_id="reject-high", model_id="gpt-image-2.5-flare", quality="high", storage_policy="direct"
    )

    assert response.status_code == 422
    assert "not available" in response.json()["detail"]


def test_local_artwork_chat_and_card_confirmation_keep_owned_parent(monkeypatch, jobs_api):
    client, tokens, _, session_scope = jobs_api
    import media.core.image_jobs as jobs
    from media.agent.chat_agent import chat_agent
    generated_refs = []
    def image(**kwargs):
        generated_refs.append(kwargs.get("ref_bytes"))
        return _png()
    monkeypatch.setattr(jobs.image_core, "_call_transit_api", image)
    first = _submit(client, tokens[0], storage_policy="direct", session_id="local-edit").json()
    _wait_status(client, tokens[0], first["task_id"])
    captured = []
    def reply(messages, **kwargs):
        captured.extend(messages)
        yield {"kind":"content", "text":'修改方案\n```json\n{"card":{"title":"局部改色","prompt":"改为橙色，保留构图","style":"default","aspect_ratio":"1:1","is_modification":true}}\n```'}
        yield {"kind":"complete", "model_id":"gpt-6-luna"}
    monkeypatch.setattr(chat_agent, "_iter_deepseek_stream", reply)
    headers = {"Authorization": f"Bearer {tokens[0]}"}
    response = client.post("/api/chat/stream", headers=headers, json={
        "session_id":"local-edit", "message":"改为橙色", "current_image_role":"base", "current_image_task_id":first["id"],
    })
    assert response.status_code == 200
    done = next(json.loads(chunk.split("data: ",1)[1]) for chunk in response.text.split("\n\n") if chunk.startswith("event: done"))
    canonical = "/api/image/result/" + first["task_id"]
    card = done["proposal_card"]
    assert card["visual_input_url"] == canonical and card["visual_input_role"] == "base"
    assert done["visual_context"]["image_task_id"] == first["id"]
    vision = captured[-1]["content"]
    assert vision[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "data:image" not in vision[0]["text"]
    history = client.get("/api/chat/history?session_id=local-edit", headers=headers).text
    assert "data:image" not in history
    payload = {"session_id":"local-edit", "request_id":"local-edit-confirm", "prompt":card["prompt"],
               "style":"default", "storage_policy":"direct", "parent_id":first["id"],
               "proposal_card_id":card["card_id"], "ref_image_url":canonical}
    wrong = client.post("/api/image/jobs",headers=headers,json={**payload,"parent_id":None})
    assert wrong.status_code == 409
    accepted = client.post("/api/image/jobs",headers=headers,json=payload)
    assert accepted.status_code == 202
    _wait_status(client,tokens[0],accepted.json()["task_id"])
    assert len(generated_refs) == 2 and generated_refs[1] is not None


@pytest.mark.parametrize("case", ["other-user", "expired"])
def test_local_chat_base_rejects_unowned_or_expired_task_before_model(monkeypatch, jobs_api, case):
    client, tokens, _, session_scope = jobs_api
    import media.core.image_jobs as jobs
    from media.agent.chat_agent import chat_agent
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(jobs.image_core,"_call_transit_api",lambda **kwargs:_png())
    first = _submit(client,tokens[0],storage_policy="direct").json()
    _wait_status(client,tokens[0],first["task_id"])
    if case == "expired":
        with session_scope() as db:
            db.query(ImageTask).filter(ImageTask.id == first["id"]).one().result_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    monkeypatch.setattr(chat_agent,"chat",lambda **kwargs:pytest.fail("invalid base must not reach provider"))
    token = tokens[1] if case == "other-user" else tokens[0]
    result=client.post("/api/chat/message",headers={"Authorization":f"Bearer {token}"},json={
        "session_id":"invalid-local-base", "message":"改色", "current_image_role":"base", "current_image_task_id":first["id"],
    })
    assert result.status_code in (404,410)

def test_background_job_publishes_first_image_before_cos_and_updates_single_history(monkeypatch, jobs_api):
    client, tokens, _, session_scope = jobs_api
    from media.core import image_jobs
    import media.core.image_jobs as jobs
    monkeypatch.setattr(jobs.image_core, "_call_transit_api", lambda **kw: _png())
    def slow_upload(*args, **kwargs):
        time.sleep(1)
        return "https://cdn.example/image.png"
    monkeypatch.setattr(jobs.cos_storage, "upload_bytes", slow_upload)
    response = _submit(client, tokens[0], session_id="s1")
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    data = _wait_status(client, tokens[0], task_id)
    assert data["result_url"] == f"/api/image/result/{task_id}"
    assert data["phase"] in {"result_ready", "persisting"}
    blob = client.get(data["result_url"], headers={"Authorization": f"Bearer {tokens[0]}"})
    assert blob.status_code == 200 and blob.headers["content-type"] == "image/png"
    time.sleep(1.05)
    final = client.get(f"/api/image/task/{task_id}", headers={"Authorization": f"Bearer {tokens[0]}"}).json()
    assert final["persistence_status"] == "stored"
    history = client.get("/api/chat/history?session_id=s1", headers={"Authorization": f"Bearer {tokens[0]}"}).json()["messages"]
    assert len(history) == 1 and history[0]["image_task_id"] == response.json()["id"]
    assert history[0]["image_duration_ms"] >= 0
    assert history[0]["image_model_id"] == "gpt-image-2"


@pytest.mark.parametrize("storage_fails", [False, True], ids=["stored", "storage-failed"])
def test_managed_job_api_exposes_real_phase_contract_and_keeps_result_during_persistence(
    monkeypatch, jobs_api, storage_fails
):
    client, tokens, _, _ = jobs_api
    import media.core.image_jobs as jobs

    reference_entered = Event()
    continue_reference = Event()
    provider_entered = Event()
    continue_provider = Event()
    storage_scheduled = Event()
    storage_entered = Event()
    continue_storage = Event()
    scheduled_calls = []
    persistence_thread = None
    original_storage_pool = jobs._storage_pool

    def prepare_reference(*args, **kwargs):
        reference_entered.set()
        if not continue_reference.wait(10):
            raise RuntimeError("test did not release reference preparation")
        return _png(), "https://references.example/processed.png"

    def generate_image(**kwargs):
        provider_entered.set()
        if not continue_provider.wait(10):
            raise RuntimeError("test did not release image generation")
        return _png()

    def upload_image(*args, **kwargs):
        storage_entered.set()
        if not continue_storage.wait(10):
            raise RuntimeError("test did not release image persistence")
        if storage_fails:
            raise RuntimeError("synthetic storage failure")
        return "https://cdn.example/persisted.png"

    class DeferredStoragePool:
        """Hold the real persistence callable so result_ready is observable."""

        def submit(self, function, *args, **kwargs):
            scheduled_calls.append((function, args, kwargs))
            storage_scheduled.set()

        def shutdown(self, wait=True):
            # Preserve TestClient's ordinary lifecycle cleanup.
            original_storage_pool.shutdown(wait=wait)

    monkeypatch.setattr(jobs.image_core, "prepare_reference_image", prepare_reference)
    monkeypatch.setattr(jobs.image_core, "_call_transit_api", generate_image)
    monkeypatch.setattr(jobs.cos_storage, "upload_bytes", upload_image)
    monkeypatch.setattr(jobs, "_storage_pool", DeferredStoragePool())

    def status_for(task_id):
        response = client.get(
            f"/api/image/task/{task_id}",
            headers={"Authorization": f"Bearer {tokens[0]}"},
        )
        assert response.status_code == 200
        return response.json()

    def release_storage_task():
        nonlocal persistence_thread
        if scheduled_calls and persistence_thread is None:
            function, args, kwargs = scheduled_calls[0]
            persistence_thread = Thread(target=function, args=args, kwargs=kwargs, daemon=True)
            persistence_thread.start()

    try:
        accepted = _submit(
            client,
            tokens[0],
            request_id=f"phase-contract-{storage_fails}",
            ref_image_url="https://references.example/input.png",
        )
        assert accepted.status_code == 202
        assert accepted.json()["phase"] == "queued"
        task_id = accepted.json()["task_id"]

        assert reference_entered.wait(10)
        preparing = status_for(task_id)
        assert preparing["phase"] == "preparing"

        continue_reference.set()
        assert provider_entered.wait(10)
        generating = status_for(task_id)
        assert generating["phase"] == "generating"

        continue_provider.set()
        assert storage_scheduled.wait(10)
        result_ready = status_for(task_id)
        assert result_ready["status"] == "success"
        assert result_ready["phase"] == "result_ready"
        assert result_ready["persistence_status"] == "pending"
        assert result_ready["result_url"] == f"/api/image/result/{task_id}"
        result_url = result_ready["result_url"]
        image = client.get(result_url, headers={"Authorization": f"Bearer {tokens[0]}"})
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"

        release_storage_task()
        assert storage_entered.wait(10)
        persisting = status_for(task_id)
        assert persisting["phase"] == "persisting"
        still_available = client.get(result_url, headers={"Authorization": f"Bearer {tokens[0]}"})
        assert still_available.status_code == 200

        continue_storage.set()
        persistence_thread.join(timeout=10)
        assert not persistence_thread.is_alive()

        completed = status_for(task_id)
        assert completed["status"] == "success"
        assert completed["phase"] == "completed"
        assert completed["persistence_status"] == ("failed" if storage_fails else "stored")
        final_result = client.get(result_url, headers={"Authorization": f"Bearer {tokens[0]}"})
        assert final_result.status_code == 200
        assert completed["is_temporary"] is storage_fails
    finally:
        continue_reference.set()
        continue_provider.set()
        continue_storage.set()
        release_storage_task()
        if persistence_thread is not None:
            persistence_thread.join(timeout=10)



def test_active_jobs_restore_latest_failed_attempt_for_each_proposal(jobs_api):
    client, tokens, ids, session_scope = jobs_api
    owner_id = ids[0]
    with session_scope() as db:
        db.add_all([
            ImageTask(
                task_id="failed-old", user_id=owner_id, prompt="old", final_prompt="old",
                status="failed", phase="failed", error_msg="old failure",
                session_id="failed-session", proposal_card_id="card-failed",
            ),
            ImageTask(
                task_id="failed-new", user_id=owner_id, prompt="new", final_prompt="new",
                status="failed", phase="failed", error_msg="new failure",
                session_id="failed-session", proposal_card_id="card-failed",
            ),
            ImageTask(
                task_id="still-running", user_id=owner_id, prompt="running", final_prompt="running",
                status="generating", phase="generating",
                session_id="failed-session", proposal_card_id="card-running",
            ),
        ])

    response = client.get(
        "/api/image/jobs/active?session_id=failed-session",
        headers={"Authorization": f"Bearer {tokens[0]}"},
    )

    assert response.status_code == 200
    jobs = response.json()["jobs"]
    by_card = {job["proposal_card_id"]: job for job in jobs}
    assert by_card["card-failed"]["task_id"] == "failed-new"
    assert by_card["card-failed"]["status"] == "failed"
    assert by_card["card-failed"]["error_msg"] == "new failure"
    assert by_card["card-running"]["status"] == "generating"
    assert [job["proposal_card_id"] for job in jobs].count("card-failed") == 1

def test_direct_url_never_downloads_and_request_id_is_idempotent(monkeypatch, jobs_api):
    client, tokens, _, _ = jobs_api
    import media.core.image_jobs as jobs
    calls = []
    monkeypatch.setattr(jobs.image_core, "_call_transit_api", lambda **kw: (calls.append(kw) or ProviderImageResult(None, "https://images.example/one.png", "gpt-image-2")))
    monkeypatch.setattr(jobs.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected download")))
    first = _submit(client, tokens[0], storage_policy="direct")
    assert first.status_code == 202
    _wait_status(client, tokens[0], first.json()["task_id"])
    direct_status = client.get(f"/api/image/task/{first.json()['task_id']}", headers={"Authorization": f"Bearer {tokens[0]}"}).json()
    assert direct_status["is_temporary"] is True and direct_status["expires_at"] is None
    replay = _submit(client, tokens[0], storage_policy="direct")
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    assert len(calls) == 1
    conflict = _submit(client, tokens[0], prompt="different", storage_policy="direct")
    assert conflict.status_code == 409
    assert client.get(f"/api/image/task/{first.json()['task_id']}", headers={"Authorization": f"Bearer {tokens[1]}"}).status_code == 404


def test_base64_parent_edit_uses_owned_bytes_and_protected_result(monkeypatch, jobs_api):
    client, tokens, _, _ = jobs_api
    import media.core.image_jobs as jobs
    seen = []
    def fake_call(**kwargs):
        seen.append(kwargs.get("ref_bytes"))
        return _png()
    monkeypatch.setattr(jobs.image_core, "_call_transit_api", fake_call)
    monkeypatch.setattr(jobs.cos_storage, "upload_bytes", lambda *a, **k: "https://cdn.example/persisted.png")
    parent_response = _submit(client, tokens[0], storage_policy="direct")
    assert parent_response.status_code == 202
    parent = _wait_status(client, tokens[0], parent_response.json()["task_id"])
    assert client.get(parent["result_url"]).status_code == 401
    child = _submit(client, tokens[0], request_id="r2", parent_id=parent["id"], storage_policy="direct")
    assert child.status_code == 202
    _wait_status(client, tokens[0], child.json()["task_id"])
    with Image.open(io.BytesIO(seen[1])) as parent_image:
        assert parent_image.size == (1024, 1024)
    foreign = _submit(client, tokens[1], request_id="b1", parent_id=parent["id"], storage_policy="direct")
    assert foreign.status_code == 404


def test_storage_failure_keeps_generated_image_success(monkeypatch, jobs_api):
    client, tokens, _, _ = jobs_api
    import media.core.image_jobs as jobs
    monkeypatch.setattr(jobs.image_core, "_call_transit_api", lambda **kw: _png())
    monkeypatch.setattr(jobs.cos_storage, "upload_bytes", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("private failure")))
    response = _submit(client, tokens[0])
    assert response.status_code == 202
    task = _wait_status(client, tokens[0], response.json()["task_id"])
    for _ in range(100):
        task = client.get(f"/api/image/task/{task['task_id']}", headers={"Authorization": f"Bearer {tokens[0]}"}).json()
        if task["persistence_status"] == "failed":
            break
        time.sleep(.01)
    assert task["status"] == "success" and task["persistence_status"] == "failed"
    assert client.get(task["result_url"], headers={"Authorization": f"Bearer {tokens[0]}"}).status_code == 200


def test_expired_blob_returns_410_without_removing_result(monkeypatch, jobs_api, tmp_path):
    client, tokens, _, session_scope = jobs_api
    import media.core.image_jobs as jobs
    monkeypatch.setattr(jobs, "_result_dir", tmp_path / "owned-results")
    monkeypatch.setattr(jobs.image_core, "_call_transit_api", lambda **kw: _png())
    response = _submit(client, tokens[0], storage_policy="direct")
    task_data = _wait_status(client, tokens[0], response.json()["task_id"])
    from media.database.models import ImageTask
    from datetime import datetime, timedelta, timezone
    with session_scope() as db:
        task = db.query(ImageTask).filter(ImageTask.id == task_data["id"]).first()
        result_path = task.result_path
        task.result_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired = client.get(task_data["result_url"], headers={"Authorization": f"Bearer {tokens[0]}"})
    assert expired.status_code == 410
    assert __import__("pathlib").Path(result_path).is_file()


def test_startup_interrupts_only_unrecoverable_jobs_and_resumes_provider_polling(monkeypatch, jobs_api):
    client, tokens, (owner_id, _), session_scope = jobs_api
    import media.core.image_jobs as jobs
    from media.database.models import ImageTask
    jobs.stop()
    polled = []
    monkeypatch.setattr(jobs.image_core, "_poll_async_image_task", lambda **kwargs: (polled.append(kwargs) or _png()))
    monkeypatch.setattr(jobs.image_core, "_call_transit_api", lambda **kwargs: pytest.fail("resume must not submit a second paid generation"))
    with session_scope() as db:
        legacy = ImageTask(task_id="legacy-still-running", user_id=owner_id, prompt="old", final_prompt="old", status="generating")
        managed = ImageTask(task_id="managed-old", user_id=owner_id, prompt="new", final_prompt="new", status="generating", phase="generating")
        recoverable = ImageTask(
            task_id="managed-recover", user_id=owner_id, prompt="recover", final_prompt="recover",
            status="generating", phase="generating", persistence_status="not_requested",
            requested_model_id="gpt-image-2", storage_policy="direct", quality="low", aspect_ratio="1:1",
            timings_json=json.dumps({
                "provider_task_id": "upstream-1",
                "provider_pull_url": "/api/v1/ai/openai/v1/images/generations/omg-task-pull?taskId=upstream-1",
                "provider_ttl_seconds": 43200,
                "provider_submitted_at_ms": int(time.time() * 1000),
            }),
        )
        persisting = ImageTask(task_id="managed-persist", user_id=owner_id, prompt="new", final_prompt="new", status="success", phase="result_ready", persistence_status="pending")
        db.add_all([legacy, managed, recoverable, persisting])
    assert jobs.start()
    for _ in range(100):
        with session_scope() as db:
            recovered = db.query(ImageTask).filter(ImageTask.task_id == "managed-recover").first()
            if recovered.status == "success":
                break
        time.sleep(.01)
    with session_scope() as db:
        assert db.query(ImageTask).filter(ImageTask.task_id == "legacy-still-running").first().status == "generating"
        interrupted = db.query(ImageTask).filter(ImageTask.task_id == "managed-old").first()
        assert interrupted.status == "failed" and interrupted.phase == "interrupted"
        recovered = db.query(ImageTask).filter(ImageTask.task_id == "managed-recover").first()
        assert recovered.status == "success" and recovered.phase == "completed"
        persisted = db.query(ImageTask).filter(ImageTask.task_id == "managed-persist").first()
        assert persisted.status == "success" and persisted.persistence_status == "failed"
    assert len(polled) == 1
    assert polled[0]["provider_task_id"] == "upstream-1"


def test_proposal_confirmation_snapshot_survives_chat_history_and_blocks_new_request(monkeypatch, jobs_api):
    client, tokens, (owner_id, _), session_scope = jobs_api
    from media.database.models import ChatMessage
    import media.core.image_jobs as jobs
    provider_calls = []
    monkeypatch.setattr(jobs.image_core, "_call_transit_api", lambda **kw: (provider_calls.append(kw) or _png()))
    with session_scope() as db:
        db.add(ChatMessage(session_id="cards", user_id=owner_id, role="assistant", content="idea", card_data=json.dumps({
            "card_id": "proposal-A", "title": "Keep title", "design_rationale": "Keep rationale",
            "prompt": "old suggestion", "final_prompt": "old final", "aspect_ratio": "1:1", "quality": "low",
        })))
    payload = {"prompt": "confirmed full prompt", "final_prompt": "confirmed full prompt with edits", "request_id": "card-req-1",
        "proposal_card_id": "proposal-A", "session_id": "cards", "aspect_ratio": "16:9", "quality": "low", "storage_policy": "direct"}
    headers = {"Authorization": f"Bearer {tokens[0]}"}
    first = client.post("/api/image/jobs", json=payload, headers=headers)
    assert first.status_code == 202
    assert first.json()["proposal_card_id"] == "proposal-A"
    assert first.json()["session_id"] == "cards"
    status = _wait_status(client, tokens[0], first.json()["task_id"])
    assert status["proposal_card_id"] == "proposal-A"
    assert status["final_prompt"] == "confirmed full prompt with edits"
    history = client.get("/api/chat/history?session_id=cards", headers=headers).json()["messages"]
    card = history[0]["card_data"]
    assert card["card_id"] == "proposal-A" and card["title"] == "Keep title"
    assert card["design_rationale"] == "Keep rationale" and card["image_task_id"] == first.json()["id"]
    assert card["prompt"] == payload["final_prompt"] and card["aspect_ratio"] == "16:9" and card["quality"] == "low"
    replay = client.post("/api/image/jobs", json={**payload, "request_id": "new-after-refresh"}, headers=headers)
    assert replay.status_code == 202 and replay.json()["id"] == first.json()["id"]
    assert len(provider_calls) == 1
    images = client.get("/api/image/history", headers=headers).json()["images"]
    assert images[0]["session_id"] == "cards" and images[0]["proposal_card_id"] == "proposal-A"
    assert images[0]["final_prompt"] == payload["final_prompt"] and images[0]["quality"] == "low"


def test_proposal_reference_mismatch_is_rejected_before_paid_call(monkeypatch, jobs_api):
    client, tokens, (owner_id, _), session_scope = jobs_api
    from media.database.models import ChatMessage
    import media.core.image_jobs as jobs
    calls = []
    monkeypatch.setattr(jobs.image_core, "_call_transit_api", lambda **kw: (calls.append(kw) or _png()))
    with session_scope() as db:
        db.add(ChatMessage(session_id="card-ref-check", user_id=owner_id, role="assistant", content="idea", card_data=json.dumps({
            "card_id": "proposal-ref", "prompt": "portrait", "visual_input_url": "https://img.example/expected.png",
            "visual_input_role": "reference",
        })))
    denied = _submit(
        client, tokens[0], request_id="ref-mismatch", session_id="card-ref-check",
        proposal_card_id="proposal-ref", ref_image_url="https://img.example/stale.png", storage_policy="direct",
    )
    assert denied.status_code == 409
    assert "reference changed" in denied.json()["detail"]
    assert calls == []


def test_old_or_missing_card_is_resolved_before_any_paid_call(monkeypatch, jobs_api):
    client, tokens, (owner_id, foreign_id), session_scope = jobs_api
    from media.database.models import ChatMessage
    import media.core.image_jobs as jobs
    calls = []
    monkeypatch.setattr(jobs.image_core, "_call_transit_api", lambda **kw: (calls.append(kw) or _png()))
    old_card_id = "old_%_card"
    with session_scope() as db:
        db.add(ChatMessage(session_id="long-session", user_id=owner_id, role="assistant", content="old", card_data=json.dumps({"card_id": old_card_id, "prompt": "before"})))
        for index in range(120):
            db.add(ChatMessage(session_id="long-session", user_id=owner_id, role="assistant", content="new", card_data=json.dumps({"card_id": f"new-{index}", "prompt": "other"})))
        db.add(ChatMessage(session_id="long-session", user_id=foreign_id, role="assistant", content="private", card_data=json.dumps({"card_id": "foreign-only"})))
    accepted = _submit(client, tokens[0], request_id="old-card", session_id="long-session", proposal_card_id=old_card_id, final_prompt="exact confirmed text", storage_policy="direct")
    assert accepted.status_code == 202
    _wait_status(client, tokens[0], accepted.json()["task_id"])
    restored = client.get("/api/chat/history?session_id=long-session", headers={"Authorization": f"Bearer {tokens[0]}"}).json()["messages"]
    assert restored[0]["card_data"]["final_prompt"] == "exact confirmed text"
    assert restored[0]["card_data"]["image_task_id"] == accepted.json()["id"]
    for card_id in ("missing", "foreign-only", "old-X-card"):
        denied = _submit(client, tokens[0], request_id=f"deny-{card_id}", session_id="long-session", proposal_card_id=card_id, storage_policy="direct")
        assert denied.status_code == 404
    assert len(calls) == 1


def test_cos_sdk_timeout_uses_job_resource(monkeypatch):
    from media.core.cos_storage import CosStorage
    from media.core.config import settings
    import importlib
    cos_module = importlib.import_module("media.core.cos_storage")
    monkeypatch.setenv("TENCENT_CLOUD_SECRET_ID", "test-id")
    monkeypatch.setenv("TENCENT_CLOUD_SECRET_KEY", "test-key")
    monkeypatch.setenv("TENCENT_CLOUD_COS_REGION", "test-region")
    monkeypatch.setenv("TENCENT_CLOUD_COS_BUCKET", "test-bucket")
    seen = {}
    class FakeConfig:
        def __init__(self, **kwargs): seen.update(kwargs)
    class FakeClient:
        def __init__(self, config): pass
    monkeypatch.setattr(cos_module, "CosConfig", FakeConfig)
    monkeypatch.setattr(cos_module, "CosS3Client", FakeClient)
    storage = CosStorage()
    storage._get_client(timeout_seconds=47)
    assert seen["Timeout"] == 47


def test_sync_entry_rejects_a_policy_it_cannot_honor_before_provider_call(monkeypatch):
    from media.core.image_service import image_core
    from media.core.runtime_config import RuntimeConfigError, get_runtime_config
    assert get_runtime_config()["defaults"]["storage_policy"] == "direct"
    monkeypatch.setattr(image_core, "_call_transit_api", lambda **kwargs: pytest.fail("unexpected paid call"))
    for policy in ("direct", "background_cos"):
        with pytest.raises(RuntimeConfigError, match="synchronous entry point"):
            image_core.generate("test", storage_policy=policy)


def test_job_rejects_blank_or_oversized_inputs_before_acceptance(jobs_api):
    client, tokens, _, _ = jobs_api
    headers = {"Authorization": f"Bearer {tokens[0]}"}
    base = {"prompt": "ok", "request_id": "input-check", "storage_policy": "direct"}
    assert client.post("/api/image/jobs", json={**base, "prompt": "   "}, headers=headers).status_code == 422
    assert client.post("/api/image/jobs", json={**base, "request_id": " "}, headers=headers).status_code == 422
    assert client.post("/api/image/jobs", json={**base, "final_prompt": " "}, headers=headers).status_code == 422
    assert client.post("/api/image/jobs", json={**base, "proposal_card_id": " "}, headers=headers).status_code == 422
    assert client.post("/api/image/jobs", json={**base, "session_id": "s" * 65}, headers=headers).status_code == 422
    assert client.post("/api/image/jobs", json={**base, "prompt": "x" * 8001}, headers=headers).status_code == 422
