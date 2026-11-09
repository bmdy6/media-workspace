"""media/tests/test_core.py - Core 核心与数据库单元测试"""
from __future__ import annotations

import io
from unittest.mock import patch, MagicMock
import pytest
from PIL import Image

from media.core.config import settings
from media.core.image_service import image_core, RATIO_TO_SIZE
from media.database import init_db, session_scope, ImageTask, User, InviteCode
from media.styles import wrap_prompt


def test_config_loading():
    """验证配置项加载正常"""
    assert settings.openai_base_url.startswith("http")
    assert settings.cos_bucket != ""
    assert settings.sqlite_db_path.name == "media.db"


def test_database_initialization_and_operations():
    """验证 SQLite 数据库初始化、用户及任务记录"""
    init_db()
    with session_scope() as db:
        # 清理历史测试数据
        db.query(ImageTask).filter(ImageTask.task_id.like("test-%")).delete()
        db.query(InviteCode).filter(InviteCode.code == "TESTINVITE123").delete()
        db.query(User).filter(User.username == "test_user").delete()

        # 创建测试用户
        user = User(username="test_user", password_hash="dummy_hash", is_admin=True)
        db.add(user)
        db.flush()

        # 创建测试邀请码
        invite = InviteCode(code="TESTINVITE123", created_by=user.id)
        db.add(invite)

        # 创建任务
        task = ImageTask(
            task_id="test-task-1",
            user_id=user.id,
            prompt="测试提示词",
            final_prompt="包装后测试提示词",
            style="tech_poster",
            aspect_ratio="16:9",
            status="pending",
        )
        db.add(task)

    # 验证读取
    with session_scope() as db:
        task_record = db.query(ImageTask).filter(ImageTask.task_id == "test-task-1").first()
        assert task_record is not None
        assert task_record.style == "tech_poster"
        assert task_record.aspect_ratio == "16:9"


def test_prompt_wrapping():
    """验证风格提示保持原文并以非强制提示补充"""
    wrapped_tech = wrap_prompt("tech_poster", "未来城市", "low")
    assert "未来城市" in wrapped_tech
    assert "信息层级" in wrapped_tech

    wrapped_real = wrap_prompt("real", "一位老人", "standard")
    assert "一位老人" in wrapped_real
    assert "真实摄影" in wrapped_real and "自然光线" in wrapped_real


def test_reference_image_preserves_wide_frame():
    """参考图净化保留完整画幅和比例"""
    # 创建一张 1600x900 的矩形图片
    img = Image.new("RGBA", (1600, 900), color=(255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw_bytes = buf.getvalue()

    with patch("media.core.cos_storage.cos_storage.upload_bytes", return_value="https://cos.fake.com/ref.png"):
        ref_bytes, ref_url = image_core.prepare_reference_image(raw_bytes)
        assert ref_url == "https://cos.fake.com/ref.png"
        prepared_img = Image.open(io.BytesIO(ref_bytes))
        assert prepared_img.size == (1600, 900)


def test_reference_image_applies_exif_orientation_and_rejects_corrupt_bytes():
    img = Image.new("RGB", (40, 20), color="red")
    exif = Image.Exif()
    exif[274] = 6
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    with patch("media.core.cos_storage.cos_storage.upload_bytes", return_value="https://cos.fake.com/ref.png"):
        prepared, _ = image_core.prepare_reference_image(buf.getvalue())
    assert Image.open(io.BytesIO(prepared)).size == (20, 40)

    with patch("media.core.cos_storage.cos_storage.upload_bytes") as upload:
        try:
            image_core.prepare_reference_image(b"not an image")
        except Exception as exc:
            assert "有效" in str(exc)
        else:
            raise AssertionError("corrupt reference bytes must fail")
    upload.assert_not_called()


def test_runtime_aspect_sizes_are_exact_and_distinct():
    from media.core.runtime_config import get_runtime_config
    model = next(m for m in get_runtime_config()["models"]["image"] if m["id"] == "gpt-image-2")
    assert model["size_by_aspect_ratio"] == {
        "1:1": "1024x1024", "9:16": "864x1536", "2:3": "1024x1536",
        "3:4": "768x1024", "16:9": "1536x864", "3:2": "1536x1024", "4:3": "1024x768",
    }


def test_user_generation_rejects_local_reference_and_unowned_parent_before_network():
    from media.core.runtime_config import RuntimeConfigError
    from media.core.errors import GenerationError
    from unittest.mock import MagicMock

    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.first.return_value = None
    with patch("media.core.image_service.session_scope") as scope, \
         patch.object(image_core, "prepare_reference_image") as prepare, \
         patch.object(image_core, "_call_transit_api") as provider:
        scope.return_value.__enter__.return_value = fake_db
        with pytest.raises(RuntimeConfigError, match="does not belong"):
            image_core.generate("test", user_id=42, parent_id=7, ref_image="/etc/hosts")
    prepare.assert_not_called()
    provider.assert_not_called()

    with pytest.raises(GenerationError, match="不能读取服务器本地路径"):
        image_core.prepare_reference_image("/etc/hosts", allow_local_path=False)


def test_owned_cos_reference_uses_authenticated_storage_instead_of_dns_ssrf_check():
    image = Image.new("RGB", (8, 4), color="green")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    owned_url = __import__("media.core.cos_storage", fromlist=["cos_storage"]).cos_storage.get_public_url(
        "references/ref.png"
    )

    with patch("media.core.image_service._assert_safe_reference_url") as safe, \
         patch("media.core.image_service.requests.get") as get, \
         patch("media.core.image_service.cos_storage.download_bytes", return_value=buf.getvalue()) as download:
        prepared, ref_url = image_core.prepare_reference_image(owned_url)

    assert ref_url == owned_url
    assert Image.open(io.BytesIO(prepared)).size == (8, 4)
    safe.assert_not_called()
    get.assert_not_called()
    download.assert_called_once()


def test_owned_cos_url_matching_is_exact_and_rejects_spoofed_hosts():
    from media.core.cos_storage import cos_storage

    owned = cos_storage.get_public_url("references/ref.png")
    assert cos_storage.public_object_key(owned) == "references/ref.png"
    host = owned.split("/", 3)[2]
    assert cos_storage.public_object_key(f"https://{host}.evil.example/references/ref.png") is None
    assert cos_storage.public_object_key(owned + "?redirect=https://127.0.0.1") is None


def test_private_reference_urls_are_rejected_before_http():
    from media.core.image_service import GenerationError
    with patch("media.core.image_service.requests.get") as get:
        try:
            image_core.prepare_reference_image("http://127.0.0.1/private.png")
        except GenerationError as exc:
            assert "本地或私有网络" in str(exc)
        else:
            raise AssertionError("private reference URL must fail")
    get.assert_not_called()


def test_remote_reference_response_closes_after_success_http_error_and_byte_limit(monkeypatch):
    from copy import deepcopy
    from media.core.errors import GenerationError
    from media.core.runtime_config import get_runtime_config

    image = Image.new("RGB", (8, 4), color="blue")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    image_bytes = buf.getvalue()

    success = MagicMock(status_code=200, headers={}, iter_content=lambda **kwargs: iter([image_bytes]))
    with patch("media.core.image_service._assert_safe_reference_url"), \
         patch("media.core.image_service.requests.get", return_value=success), \
         patch("media.core.cos_storage.cos_storage.upload_bytes", return_value="https://cos.fake/ref.png"):
        image_core.prepare_reference_image("https://images.example/reference.png")
    success.close.assert_called_once()

    failed = MagicMock(status_code=503, headers={})
    with patch("media.core.image_service._assert_safe_reference_url"), \
         patch("media.core.image_service.requests.get", return_value=failed):
        with pytest.raises(GenerationError, match="HTTP 503"):
            image_core.prepare_reference_image("https://images.example/reference.png")
    failed.close.assert_called_once()

    tiny_limit_config = deepcopy(get_runtime_config())
    tiny_limit_config["request"]["reference_image_max_bytes"] = 8
    monkeypatch.setattr("media.core.image_service.get_runtime_config", lambda: tiny_limit_config)
    limited = MagicMock(status_code=200, headers={}, iter_content=lambda **kwargs: iter([b"x" * 9]))
    with patch("media.core.image_service._assert_safe_reference_url"), \
         patch("media.core.image_service.requests.get", return_value=limited):
        with pytest.raises(GenerationError, match="大小限制"):
            image_core.prepare_reference_image("https://images.example/reference.png")
    limited.close.assert_called_once()


def test_reference_url_with_empty_dns_result_fails_closed():
    from media.core.errors import GenerationError
    with patch("media.core.image_service.socket.getaddrinfo", return_value=[]), \
         patch("media.core.image_service.requests.get") as get:
        with pytest.raises(GenerationError, match="无法解析"):
            image_core.prepare_reference_image("https://images.example/reference.png")
    get.assert_not_called()


def test_image_core_mock_generate():
    """验证 Mock 底座 API 时的完整生图核心流程"""
    # This test persists an ImageTask, so it must create its schema itself.
    init_db()

    dummy_img = Image.new("RGBA", (100, 100), color=(0, 255, 0, 255))
    buf = io.BytesIO()
    dummy_img.save(buf, format="PNG")
    dummy_bytes = buf.getvalue()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": [{"url": "http://transit.fake/img.png"}]}

    with patch("requests.post", return_value=mock_resp), \
         patch("requests.get", return_value=MagicMock(status_code=200, content=dummy_bytes)), \
         patch.object(image_core, "_provider_api_key", return_value="test-only-ohmygpt-key"), \
         patch("media.core.cos_storage.cos_storage.upload_bytes", return_value="https://cos.myqcloud.com/creations/out.png"):

        result = image_core.generate(
            prompt="测试生成科技海报",
            style="tech_poster",
            aspect_ratio="3:4",
            quality="low",
        )

        assert result.status == "success", result.error_msg
        assert result.image_url == "https://cos.myqcloud.com/creations/out.png"
        assert result.aspect_ratio == "3:4"
        assert result.style == "tech_poster"

        # 检查持久化状态
        task_query = image_core.get_task(result.task_id)
        assert task_query is not None
        assert task_query.status == "success"
        assert task_query.image_url == "https://cos.myqcloud.com/creations/out.png"
