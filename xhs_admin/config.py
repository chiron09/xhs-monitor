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

# 每轮最多抓取的博主数（错峰限流）。用户要求加快抓取速度、可加大并发。
# 压测结论：本机单账号并发请求易触发风控（300011 账号异常），故默认取 6 而非更激进；
# 可用 XHS_ADMIN_MAX_PER_ROUND 覆盖。调度器已内置「风控自适应退避」，触发即暂停该账号 30 分钟。
MAX_PER_ROUND = int(os.environ.get("XHS_ADMIN_MAX_PER_ROUND") or 6)

# 密码加盐（本机后台，固定盐即可；可用环境变量覆盖）
SALT = os.environ.get("XHS_ADMIN_SALT") or "xhs-admin-local-2026"


def hash_password(pwd: str) -> str:
    import hashlib
    return hashlib.sha256((SALT + pwd).encode("utf-8")).hexdigest()
