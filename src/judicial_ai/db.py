from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

if _settings.is_sqlite:
    Path(_settings.data_dir).mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        _settings.database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )
else:
    engine = create_engine(
        _settings.database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=_settings.db_pool_size,
        max_overflow=_settings.db_max_overflow,
        pool_timeout=_settings.db_pool_timeout_seconds,
        future=True,
    )

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    # 导入模型以注册到 Base.metadata
    from . import models  # noqa: F401

    if get_settings().auto_create_schema:
        Base.metadata.create_all(bind=engine)

