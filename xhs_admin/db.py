# -*- coding: utf-8 -*-
"""SQLAlchemy 数据模型 + 会话。"""
import os
from datetime import datetime

from sqlalchemy import BigInteger, Column, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from config import DATA_DIR, DB_PATH

os.makedirs(DATA_DIR, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Account(Base):
    """小红书账号（Cookie 导入）。"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), default="")
    cookie = Column(Text, default="")
    nickname = Column(String(128), default="")
    xhs_user_id = Column(String(64), default="")
    status = Column(String(16), default="unknown")  # active / expired / unknown
    last_checked_at = Column(String(32), default="")
    created_at = Column(String(32), default=_now)

    def to_dict(self, with_cookie: bool = False) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "nickname": self.nickname,
            "xhs_user_id": self.xhs_user_id,
            "status": self.status,
            "last_checked_at": self.last_checked_at,
            "created_at": self.created_at,
        }
        if with_cookie:
            d["cookie"] = self.cookie
        return d


class Blogger(Base):
    """博主监控目标。"""
    __tablename__ = "bloggers"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), default="")
    url = Column(Text, default="")
    xhs_user_id = Column(String(64), default="")
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    interval_minutes = Column(Integer, default=60)
    status = Column(String(16), default="active")  # active / paused
    # 监控时段（抓取时段）：'HH:MM' 字符串，闭区间 [monitor_start, monitor_end]。
    # 两端都为空 → 全天监控（兼容历史博主）；start > end 视为跨天（如 22:00-06:00）。
    monitor_start = Column(String(8), default="")
    monitor_end = Column(String(8), default="")
    baseline_note_ids = Column(Text, default="[]")  # JSON 列表
    # 监控起点（毫秒时间戳）：添加博主那一刻。
    # 只把此刻之后发布的笔记视为「新笔记」，避免把添加前的历史笔记推出去。
    monitor_since = Column(BigInteger, default=0)
    last_crawled_at = Column(String(32), default="")
    last_error = Column(Text, default="")
    created_at = Column(String(32), default=_now)

    def to_dict(self) -> dict:
        import json
        try:
            baseline = json.loads(self.baseline_note_ids or "[]")
        except Exception:
            baseline = []
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "xhs_user_id": self.xhs_user_id,
            "account_id": self.account_id,
            "interval_minutes": self.interval_minutes,
            "monitor_start": self.monitor_start or "",
            "monitor_end": self.monitor_end or "",
            "status": self.status,
            "baseline_count": len(baseline),
            "monitor_since": self.monitor_since or 0,
            "last_crawled_at": self.last_crawled_at,
            "last_error": self.last_error,
            "created_at": self.created_at,
        }


class Note(Base):
    """记录博主添加之后新发布的笔记。"""
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True)
    blogger_id = Column(Integer, ForeignKey("bloggers.id"), index=True)
    note_id = Column(String(64), default="", index=True)
    title = Column(String(512), default="")
    cover_url = Column(Text, default="")
    note_url = Column(Text, default="")
    publish_time = Column(BigInteger, default=0)  # 毫秒时间戳
    liked_count = Column(Integer, default=0)
    created_at = Column(String(32), default=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "blogger_id": self.blogger_id,
            "note_id": self.note_id,
            "title": self.title,
            "cover_url": self.cover_url,
            "note_url": self.note_url,
            "publish_time": self.publish_time,
            "liked_count": self.liked_count,
            "created_at": self.created_at,
        }


class Setting(Base):
    """键值设置（后台密码 hash 等）。"""
    __tablename__ = "settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, default="")


class AuthToken(Base):
    """登录 token（持久化，重启后台不掉登录）。

    expires_at 为空字符串表示永不过期；非空则为 'YYYY-MM-DD HH:MM:SS'，
    校验时与该时刻比较，过期即视为无效。
    """
    __tablename__ = "auth_tokens"

    token = Column(String(64), primary_key=True)
    created_at = Column(String(32), default=_now)
    expires_at = Column(String(32), default="")
    last_seen_at = Column(String(32), default="")
    user_agent = Column(String(256), default="")
    remote_addr = Column(String(64), default="")


Base.metadata.create_all(engine)


def _ensure_columns() -> None:
    """轻量迁移：为已存在的旧表补新增列（SQLite 不支持自动加列）。"""
    import sqlite3
    wanted = {
        "bloggers": [
            ("monitor_since", "BIGINT DEFAULT 0"),
            ("monitor_start", "VARCHAR(8) DEFAULT ''"),
            ("monitor_end", "VARCHAR(8) DEFAULT ''"),
        ],
    }
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            for table, cols in wanted.items():
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                for name, ddl in cols:
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001  # 迁移失败不应阻断启动
        pass


_ensure_columns()
