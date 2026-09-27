# -*- coding: utf-8 -*-
"""全局配置。"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Spider_XHS 纯 SDK 目录。仓库内默认相对路径（../Spider_XHS），
# 换机器/非标准布局可用环境变量 XHS_SDK_DIR 覆盖。
SDK_DIR = os.environ.get("XHS_SDK_DIR") or os.path.join(BASE_DIR, "..", "Spider_XHS")

DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "xhs_admin.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 后台默认密码（首次启动写入，可在系统设置里改）
DEFAULT_PASSWORD = "admin123"

# 登录 token 有效期（秒），0 表示不过期（直到服务重启）
TOKEN_TTL = 0

# 调度器检查间隔（秒）：每隔这么久扫一遍是否到点
SCHEDULER_INTERVAL = 20

# 密码加盐（本机后台，固定盐即可）
SALT = "xhs-admin-local-2026"


def hash_password(pwd: str) -> str:
    import hashlib
    return hashlib.sha256((SALT + pwd).encode("utf-8")).hexdigest()
