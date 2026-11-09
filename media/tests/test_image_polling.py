import base64
from unittest.mock import MagicMock
from pathlib import Path

import pytest
import requests

from media.core import image_service
from media.core.errors import GenerationError
from media.core.runtime_config import load_runtime_config


class Response:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data


def _model():
    return next(item for item in load_runtime_config(Path(__file__).parents[1] / "config" / "runtime-ohmygpt.json")["models"]["image"] if item["id"] == "gpt-image-2")


def test_ohmygpt_polling_submits_once_persists_metadata_and_maps_standard_quality(monkeypatch):
    model = _model()
    monkeypatch.setattr(image_service, "get_model", lambda model_id, kind: model)
    monkeypatch.setattr(image_service, "get_config", lambda key, **kwargs: "https://images.example/v1" if key.endswith("BASE_URL") else "test-key")
    monkeypatch.setattr(image_service.time, "sleep", lambda _: None)
    post = MagicMock(return_value=Response({
        "object": "omg.async_task",
        "async_method": "pulling",
        "status": "pending",
        "task_id": "provider-task-1",
        "ttl_seconds": 43200,
        "pull_url": "/api/v1/ai/openai/v1/images/generations/omg-task-pull?taskId=provider-task-1",
    }))
    image = base64.b64encode(b"image-bytes").decode("ascii")
    get = MagicMock(side_effect=[
        Response({"status": "pending"}),
        Response({"data": [{"b64_json": image}], "quality": "medium", "size": "1024x1024"}),
    ])
    monkeypatch.setattr(image_service.requests, "post", post)
    monkeypatch.setattr(image_service.requests, "get", get)
    metadata = []

    result = image_service.image_core._call_transit_api(
        final_prompt="test", size="1024x1024", quality="standard",
        model_id="gpt-image-2", defer_download=True, on_async_task=metadata.append,
    )

    assert result.image_bytes == b"image-bytes"
    assert post.call_count == 1
    payload = post.call_args.kwargs["json"]
    assert payload["model"] == "gpt-image-2"
    assert payload["quality"] == "medium"
    assert payload["omg"] == {"async_method": "pulling"}
    assert metadata[0]["provider_task_id"] == "provider-task-1"
    assert metadata[0]["provider_ttl_seconds"] == 43200
    assert metadata[0]["provider_submitted_at_ms"] > 0
    assert get.call_count == 2
    assert get.call_args.args[0].startswith("https://images.example/")


def test_ohmygpt_submit_timeout_is_not_retried_to_avoid_duplicate_paid_generation(monkeypatch):
    model = _model()
    monkeypatch.setattr(image_service, "get_model", lambda model_id, kind: model)
    monkeypatch.setattr(image_service, "get_config", lambda key, **kwargs: "https://images.example/v1" if key.endswith("BASE_URL") else "test-key")
    post = MagicMock(side_effect=requests.ConnectTimeout("ambiguous"))
    monkeypatch.setattr(image_service.requests, "post", post)

    with pytest.raises(GenerationError, match="避免重复扣费"):
        image_service.image_core._call_transit_api(
            final_prompt="test", size="1024x1024", quality="low", model_id="gpt-image-2"
        )
    assert post.call_count == 1


def test_ohmygpt_poll_transport_error_is_safe_to_retry(monkeypatch):
    model = _model()
    monkeypatch.setattr(image_service, "get_model", lambda model_id, kind: model)
    monkeypatch.setattr(image_service, "get_config", lambda key, **kwargs: "https://images.example/v1" if key.endswith("BASE_URL") else "test-key")
    monkeypatch.setattr(image_service.time, "sleep", lambda _: None)
    image = base64.b64encode(b"recovered").decode("ascii")
    get = MagicMock(side_effect=[requests.ConnectTimeout("temporary"), Response({"data": [{"b64_json": image}]})])
    monkeypatch.setattr(image_service.requests, "get", get)

    result = image_service.image_core._poll_async_image_task(
        model_id="gpt-image-2", provider_task_id="p2",
        pull_url="/api/v1/ai/openai/v1/images/generations/omg-task-pull?taskId=p2",
        ttl_seconds=43200, provider_submitted_at_ms=None,
    )
    assert result.image_bytes == b"recovered"
    assert get.call_count == 2


def test_ohmygpt_rejects_cross_host_pull_url(monkeypatch):
    model = _model()
    monkeypatch.setattr(image_service, "get_model", lambda model_id, kind: model)
    monkeypatch.setattr(image_service, "get_config", lambda key, **kwargs: "https://images.example/v1" if key.endswith("BASE_URL") else "test-key")
    with pytest.raises(GenerationError, match="无效的图片任务地址"):
        image_service.image_core._poll_async_image_task(
            model_id="gpt-image-2", provider_task_id="p3",
            pull_url="https://evil.example/task/p3", ttl_seconds=100,
        )
