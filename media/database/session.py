"""media/database/session.py - 数据库连接与会话管理。

支持双模：
- 默认：轻量零运维 SQLite 文件数据库（位于 media/data/media.db，开箱即用、无需外部依赖）
- 可选：当设置 MEDIA_USE_MYSQL=1 或显式配置有效环境变量时，切换至 MySQL；若 MySQL 连接不可用则自动安全降级至 SQLite。
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session

from pathlib import Path
from .models import Base

_engine = None
_SessionFactory = None


def _get_sqlite_path() -> Path:
    p = Path(os.environ.get("MEDIA_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data")))
    p.mkdir(parents=True, exist_ok=True)
    return p / "media.db"


def _create_sqlite_engine():
    sqlite_path = _get_sqlite_path()
    db_url = f"sqlite:///{sqlite_path}"
    return create_engine(
        db_url,
        connect_args={"check_same_thread": False},
    )


def get_engine():
    global _engine
    if _engine is not None:
        return _engine

    # 仅在显式开启 MEDIA_USE_MYSQL 且配置了 DATABASE_URL 时尝试 MySQL
    use_mysql = os.environ.get("MEDIA_USE_MYSQL", "").lower() in ("1", "true", "yes")
    db_url = os.environ.get("DATABASE_URL", "") if use_mysql else ""

    if db_url and ("mysql" in db_url):
        if db_url.startswith("mysql://") and not db_url.startswith("mysql+pymysql://"):
            db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)
        try:
            temp_engine = create_engine(
                db_url,
                pool_recycle=3600,
                pool_pre_ping=True,
                connect_args={"connect_timeout": 3},
            )
            # 探测一次连接
            with temp_engine.connect() as conn:
                pass
            _engine = temp_engine
            return _engine
        except Exception as e:
            sys.stderr.write(f"[media/database] MySQL 连接失败 ({e})，安全降级至 SQLite\n")

    # 默认或降级：SQLite
    _engine = _create_sqlite_engine()
    return _engine


def get_session_factory():
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_engine(),
        )
    return _SessionFactory


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """数据库会话上下文管理器，支持自动提交与回滚"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """初始化建表"""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _additive_metadata_migration(engine)


def _additive_metadata_migration(engine) -> None:
    """Add nullable model metadata fields to databases created by older versions."""
    allowlisted_columns = {
        "image_tasks": {
            "model_id": "VARCHAR(128)",
            "requested_model_id": "VARCHAR(128)",
            "fallback_from_model_id": "VARCHAR(128)",
            "fallback_reason": "VARCHAR(128)",
            "storage_policy": "VARCHAR(64)",
            "request_id": "VARCHAR(128)",
            "phase": "VARCHAR(32)",
            "persistence_status": "VARCHAR(32)",
            "source_url": "TEXT",
            "result_path": "TEXT",
            "result_mime_type": "VARCHAR(64)",
            "result_expires_at": "DATETIME",
            "timings_json": "TEXT",
            "payload_hash": "VARCHAR(64)",
            "idempotency_key": "VARCHAR(64)",
            "session_id": "VARCHAR(64)",
            "proposal_card_id": "VARCHAR(128)",
        },
        "chat_messages": {
            "model_id": "VARCHAR(128)",
            "requested_model_id": "VARCHAR(128)",
            "fallback_from_model_id": "VARCHAR(128)",
            "fallback_reason": "VARCHAR(128)",
            "thinking_mode": "VARCHAR(32)",
            "reference_case_id": "VARCHAR(128)",
            "image_task_id": "INTEGER",
            "image_role": "VARCHAR(32)",
        },
    }
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table_name, columns in allowlisted_columns.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, sql_type in columns.items():
                if column_name not in existing_columns:
                    connection.execute(text(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {sql_type}"
                    ))
        if "image_tasks" in existing_tables:
            indexes = inspect(engine).get_indexes("image_tasks")
            unique_constraints = inspect(engine).get_unique_constraints("image_tasks")
            indexed = any(index.get("unique") and index.get("column_names") == ["idempotency_key"] for index in indexes)
            constrained = any(item.get("column_names") == ["idempotency_key"] for item in unique_constraints)
            if not indexed and not constrained:
                connection.execute(text(
                    "CREATE UNIQUE INDEX ix_image_tasks_idempotency_key ON image_tasks (idempotency_key)"
                ))
