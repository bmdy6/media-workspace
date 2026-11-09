"""Explicit environment configuration; never inspect host secret stores."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Optional
from .errors import ConfigError

def get_config(key: str, default: Optional[str] = None, required: bool = False, project: Optional[str] = None) -> str:
    value = os.environ.get(key, "").strip()
    if value:
        return value
    if default is not None:
        return default
    if required:
        raise ConfigError(f"Missing required environment variable: {key}")
    return ""

class Settings:
    """集中式配置视图"""

    @property
    def openai_api_key(self) -> str:
        return get_config("OPENAI_API_KEY", required=True)

    @property
    def openai_base_url(self) -> str:
        return get_config("OPENAI_BASE_URL", default="https://api.openai.com/v1").rstrip("/")

    @property
    def deepseek_api_key(self) -> str:
        return get_config("DEEPSEEK_API_KEY", default="")

    @property
    def deepseek_base_url(self) -> str:
        return get_config("DEEPSEEK_BASE_URL", default="https://api.deepseek.com/v1").rstrip("/")

    @property
    def secret_key(self) -> str:
        return get_config("SECRET_KEY", required=True)

    # 腾讯云 COS
    @property
    def cos_secret_id(self) -> str:
        return get_config("TENCENT_CLOUD_SECRET_ID", default="")

    @property
    def cos_secret_key(self) -> str:
        return get_config("TENCENT_CLOUD_SECRET_KEY", default="")

    @property
    def cos_region(self) -> str:
        return get_config("TENCENT_CLOUD_COS_REGION", default="ap-guangzhou")

    @property
    def cos_bucket(self) -> str:
        return get_config("TENCENT_CLOUD_COS_BUCKET", default="")

    # 数据库连接
    @property
    def database_url(self) -> str:
        return get_config("DATABASE_URL", default="")

    @property
    def media_root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    @property
    def sqlite_db_path(self) -> Path:
        data_dir = Path(os.environ.get("MEDIA_DATA_DIR", str(self.media_root / "data")))
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "media.db"


settings = Settings()
