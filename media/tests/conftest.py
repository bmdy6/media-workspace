"""Keep media tests isolated from the developer's database and credentials."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.update({
    "MEDIA_CHAT_API_KEY": "test-chat-key",
    "MEDIA_CHAT_BASE_URL": "https://chat.invalid/v1",
    "MEDIA_IMAGE_API_KEY": "test-image-key",
    "MEDIA_IMAGE_BASE_URL": "https://images.invalid",
    "OPENAI_API_KEY": "test-openai-key",
    "OPENAI_BASE_URL": "https://openai.invalid/v1",
    "DEEPSEEK_API_KEY": "test-deepseek-key",
    "DEEPSEEK_BASE_URL": "https://deepseek.invalid/v1",
    "MIMO_TOKEN_PLAN_CN_API_KEY": "test-mimo-key",
    "SECRET_KEY": "test-only-signing-key-with-at-least-32-characters",
    "TENCENT_CLOUD_SECRET_ID": "test-cos-id",
    "TENCENT_CLOUD_SECRET_KEY": "test-cos-secret",
    "TENCENT_CLOUD_COS_REGION": "test-region",
    "TENCENT_CLOUD_COS_BUCKET": "test-bucket",
    "MEDIA_USE_MYSQL": "0",
})

from media.database import session as database_session  # noqa: E402

_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="media-tests-"))
database_session._get_sqlite_path = lambda: _TEST_DB_DIR / "media-test.db"


import pytest

@pytest.fixture(autouse=True)
def standalone_test_signing_key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "synthetic-test-signing-key-with-at-least-32-characters")


@pytest.fixture
def optional_provider_catalog(tmp_path, monkeypatch):
    """Explicitly enable optional protocol fixtures, never production defaults."""
    import json
    from media.core.runtime_config import get_runtime_config, load_runtime_config
    catalog = load_runtime_config()
    for model in catalog["models"]["chat"]:
        if model["provider"] == "commandcode":
            model.update(enabled=True, available=True)
    path = tmp_path / "optional-provider-fixture.json"
    path.write_text(json.dumps(catalog))
    monkeypatch.setenv("MEDIA_RUNTIME_CONFIG", str(path))
    get_runtime_config.cache_clear()
    yield
    get_runtime_config.cache_clear()
