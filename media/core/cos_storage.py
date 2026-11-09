"""media/core/cos_storage.py - 腾讯云 COS 对象存储转存与管理。

用于将生成的图片和上传的参考图转存到腾讯云 COS，返回稳定的公网 CDN 访问链接。
"""
from __future__ import annotations

import base64
import io
from typing import Optional
from urllib.parse import unquote, urlsplit
from qcloud_cos import CosConfig, CosS3Client
from PIL import Image
import requests

from .config import settings
from .errors import StorageError


class CosStorage:
    def __init__(self):
        self._client: Optional[CosS3Client] = None
        self._client_timeout: Optional[int] = None
        self._clients: dict[Optional[int], CosS3Client] = {}

    def _get_client(self, timeout_seconds: Optional[int] = None) -> CosS3Client:
        if self._client is not None and self._client_timeout == timeout_seconds:
            return self._client
        if timeout_seconds in self._clients:
            return self._clients[timeout_seconds]

        secret_id = settings.cos_secret_id
        secret_key = settings.cos_secret_key
        region = settings.cos_region
        bucket = settings.cos_bucket

        if not all([secret_id, secret_key, region, bucket]):
            raise StorageError("腾讯云 COS 配置缺失 (需 SecretId, SecretKey, Region, Bucket)")

        try:
            kwargs = {"Region": region, "SecretId": secret_id, "SecretKey": secret_key}
            if timeout_seconds is not None:
                kwargs["Timeout"] = timeout_seconds
            config = CosConfig(**kwargs)
            self._client = CosS3Client(config)
            self._client_timeout = timeout_seconds
            self._clients[timeout_seconds] = self._client
            return self._client
        except Exception as e:
            raise StorageError(f"初始化 COS 客户端失败: {e}")

    def get_public_url(self, key: str) -> str:
        bucket = settings.cos_bucket
        region = settings.cos_region
        return f"https://{bucket}.cos.{region}.myqcloud.com/{key.lstrip('/')}"

    def public_object_key(self, image_url: str) -> Optional[str]:
        """Return the object key only for this configured bucket's own HTTPS URL.

        Production COS DNS can resolve to a Tencent link-local address. Treating that
        as an arbitrary user URL makes the SSRF guard reject our own uploaded
        references. Exact bucket ownership is a stronger boundary than DNS here: an
        owned URL is read through the authenticated COS client instead of HTTP.
        """
        bucket = settings.cos_bucket
        region = settings.cos_region
        if not bucket or not region:
            return None
        try:
            parsed = urlsplit(image_url)
            expected_host = f"{bucket}.cos.{region}.myqcloud.com".lower()
            if (
                parsed.scheme != "https"
                or (parsed.hostname or "").lower() != expected_host
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or parsed.query
                or parsed.fragment
            ):
                return None
        except ValueError:
            return None
        key = unquote(parsed.path.lstrip("/"))
        if not key or "\x00" in key:
            return None
        return key

    def download_bytes(
        self, key: str, *, max_bytes: int, timeout_seconds: Optional[int] = None,
    ) -> bytes:
        """Read one object from the configured bucket with a strict byte cap."""
        if not key:
            raise StorageError("COS 对象 key 不能为空")
        body = None
        raw_stream = None
        try:
            response = self._get_client(timeout_seconds=timeout_seconds).get_object(
                Bucket=settings.cos_bucket, Key=key,
            )
            declared_length = response.get("Content-Length")
            if declared_length and int(declared_length) > max_bytes:
                raise StorageError(f"COS 对象超过大小限制 ({max_bytes} bytes)")
            body = response.get("Body")
            if body is None:
                raise StorageError("COS 下载响应缺少 Body")
            raw_stream = body.get_raw_stream() if hasattr(body, "get_raw_stream") else None
            chunks = bytearray()
            for chunk in body.get_stream(chunk_size=64 * 1024):
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise StorageError(f"COS 对象超过大小限制 ({max_bytes} bytes)")
            return bytes(chunks)
        except StorageError:
            raise
        except Exception as e:
            raise StorageError("读取 COS 对象失败") from e
        finally:
            if raw_stream is not None:
                try:
                    raw_stream.close()
                except Exception:
                    pass

    def delete_object(self, key: str, timeout_seconds: Optional[int] = None) -> None:
        """Delete one object by key from the configured bucket."""
        if not key:
            raise StorageError("COS 对象 key 不能为空")
        try:
            self._get_client(timeout_seconds=timeout_seconds).delete_object(
                Bucket=settings.cos_bucket, Key=key,
            )
        except Exception as e:
            raise StorageError("删除 COS 对象失败") from e

    def upload_bytes(
        self,
        data: bytes,
        filename: str,
        content_type: str = "image/png",
        prefix: str = "creations",
        timeout_seconds: Optional[int] = None,
    ) -> str:
        """上传图片二进制到 COS，返回公网 URL"""
        client = self._get_client(timeout_seconds=timeout_seconds)
        bucket = settings.cos_bucket
        key = f"{prefix.strip('/')}/{filename.lstrip('/')}"

        try:
            client.put_object(
                Bucket=bucket,
                Body=data,
                Key=key,
                ContentType=content_type,
            )
            return self.get_public_url(key)
        except Exception as e:
            raise StorageError(f"上传数据到 COS 失败: {e}")

    def upload_url(self, image_url: str, filename: str, prefix: str = "creations") -> str:
        """从外部 URL 下载图片并转存到 COS"""
        try:
            resp = requests.get(image_url, timeout=60)
            resp.raise_for_status()
            data = resp.content
        except Exception as e:
            raise StorageError(f"从外部 URL 下载图片失败 ({image_url}): {e}")

        return self.upload_bytes(data, filename, content_type="image/png", prefix=prefix)

    def upload_base64(
        self,
        base64_data: str,
        filename: str,
        prefix: str = "creations",
        sanitize_square: bool = True,
    ) -> str:
        """解码 Base64 并上传至 COS。支持参考图 1024x1024 正方形净化。"""
        try:
            if "," in base64_data:
                _, encoded = base64_data.split(",", 1)
            else:
                encoded = base64_data
            raw_bytes = base64.b64decode(encoded)
        except Exception as e:
            raise StorageError(f"解析 Base64 数据失败: {e}")

        # 参考图正方形净化 (gpt-image-2 edits 严格要求正方形)
        if sanitize_square:
            try:
                img = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
                w, h = img.size
                min_side = min(w, h)
                left = (w - min_side) // 2
                top = (h - min_side) // 2
                img = img.crop((left, top, left + min_side, top + min_side))
                img = img.resize((1024, 1024), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                data_to_upload = buf.getvalue()
            except Exception:
                data_to_upload = raw_bytes
        else:
            data_to_upload = raw_bytes

        return self.upload_bytes(data_to_upload, filename, content_type="image/png", prefix=prefix)


cos_storage = CosStorage()
