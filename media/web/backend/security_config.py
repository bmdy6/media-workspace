"""Authentication policy settings kept separate from provider configuration."""
from __future__ import annotations

import os
import json
from pathlib import Path

SECURITY_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "security.json"


def _configured_default_expire_days() -> int:
    try:
        config = json.loads(SECURITY_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to load media security policy: {SECURITY_CONFIG_PATH}") from exc
    days = config.get("access_token_expire_days") if isinstance(config, dict) else None
    if type(days) is not int or days <= 0:
        raise RuntimeError("security.json access_token_expire_days must be a positive integer")
    return days


def access_token_expire_days() -> int:
    """Return configured token lifetime, with an environment override."""
    value = os.environ.get("MEDIA_AUTH_ACCESS_TOKEN_EXPIRE_DAYS")
    if value is None or not value.strip():
        return _configured_default_expire_days()
    try:
        days = int(value)
    except ValueError as exc:
        raise ValueError("MEDIA_AUTH_ACCESS_TOKEN_EXPIRE_DAYS must be a positive integer") from exc
    if days <= 0:
        raise ValueError("MEDIA_AUTH_ACCESS_TOKEN_EXPIRE_DAYS must be a positive integer")
    return days
