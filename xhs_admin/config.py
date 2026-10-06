# -*- coding: utf-8 -*-
"""全局配置。"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Spider_XHS 纯 SDK 目录（默认本机路径，换机器可用环境变量 XHS_SDK_DIR 覆盖）
SDK_DIR = os.environ.get("XHS_SDK_DIR") or r"C:\Workbuddy\Spider_XHS"

DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "xhs_admin.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 后台默认密码（首次启动写入，可在系统设置里改；可用环境变量覆盖）
DEFAULT_PASSWORD = os.environ.get("XHS_ADMIN_PASSWORD") or "admin123"

# 登录 token 有效期（秒），0 表示不过期（直到服务重启）
TOKEN_TTL = int(os.environ.get("XHS_ADMIN_TOKEN_TTL") or 0)

# 调度器检查间隔（秒）：每隔这么久扫一遍是否到点
SCHEDULER_INTERVAL = int(os.environ.get("XHS_ADMIN_SCHEDULER_INTERVAL") or 20)

# 轮巡间隔下限（分钟）：每轮最多抓 MAX_PER_ROUND 个博主，间隔设太短也达不到，
# 反而让调度器长期处于「积压」状态。统一在此收敛下限（后端校验 + 前端输入都引用）。
MIN_INTERVAL_MINUTES = int(os.environ.get("XHS_ADMIN_MIN_INTERVAL") or 10)

# 每轮最多抓取的博主数（错峰限流）。可用 XHS_ADMIN_MAX_PER_ROUND 覆盖。
# 实测（单账号 37 博主）：并发 4 全成、延迟 4.5s；并发 8 触发风控(300011)。
# 故默认取 4 稳值；6 临界、8 易锁号。调度器另有「风控自适应退避」兜底。
MAX_PER_ROUND = int(os.environ.get("XHS_ADMIN_MAX_PER_ROUND") or 4)

# 密码加盐（本机后台，固定盐即可；可用环境变量覆盖）
SALT = os.environ.get("XHS_ADMIN_SALT") or "xhs-admin-local-2026"

# 风控关键词：抓笔记接口命中这些字样即视为账号被风控（300011「账号异常」等）。
# scheduler 的自适应退避、账号检测接口都引用同一份。
RATE_LIMIT_KEYWORDS = ("账号异常", "稍后重试", "300011", "风控", "操作频繁")


def hash_password(pwd: str) -> str:
    import hashlib
    return hashlib.sha256((SALT + pwd).encode("utf-8")).hexdigest()
