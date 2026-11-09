import base64
from unittest.mock import MagicMock

import pytest

from media.core import image_service
from media.core.runtime_config import get_model


def test_default_catalog_only_enables_verified_integration_targets():
    from media.core.runtime_config import load_runtime_config
    config = load_runtime_config()
    for kind, expected in (("chat", "gpt-6-luna"), ("image", "gpt-image-2")):
        assert [model["id"] for model in config["models"][kind] if model["enabled"] and model["available"]] == [expected]


@pytest.mark.parametrize("reference", [None, b"synthetic-reference"])
def test_default_image_protocol_matches_official_endpoint(monkeypatch, reference):
    response = MagicMock(status_code=200)
    response.json.return_value = {"data":[{"b64_json":base64.b64encode(b"synthetic-result").decode()}]}
    post = MagicMock(return_value=response)
    monkeypatch.setattr(image_service.requests, "post", post)
    model = get_model(None, "image")
    assert model["api_protocol"] == "openai_images_v1"
    result = image_service.image_core._call_transit_api(final_prompt="synthetic", size="1024x1024", quality="low",
                                                       ref_bytes=reference, model_id=model["id"], defer_download=True)
    assert result.image_bytes == b"synthetic-result"
    assert post.call_count == 1
    assert post.call_args.args[0] == "https://images.invalid/images/" + ("edits" if reference else "generations")
    payload = post.call_args.kwargs["data" if reference else "json"]
    assert payload["quality"] == "low" and "omg" not in payload
    if reference:
        assert post.call_args.kwargs["files"]["image"][1] == reference
