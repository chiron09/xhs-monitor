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
    baseline_note_ids = Column(Text, default="[]")  # JSON 列表
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
            "status": self.status,
            "baseline_count": len(baseline),
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


Base.metadata.create_all(engine)
