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
# 实测（单账号 37 博主，充分冷却后复测）：并发 1~20 全成，无风控；但并发↑单请求延迟线性上升
# （4→3.2s、8→6.2s、16→10.8s），吞吐在 16 见顶后 20 反降。延迟最优=4，吞吐峰值=16。
# 默认取 4（延迟/安全平衡点）；要更高吞吐可提 6~8。调度器另有「风控自适应退避」兜底。
MAX_PER_ROUND = int(os.environ.get("XHS_ADMIN_MAX_PER_ROUND") or 4)

# 密码加盐（本机后台，固定盐即可；可用环境变量覆盖）
SALT = os.environ.get("XHS_ADMIN_SALT") or "xhs-admin-local-2026"

# 风控关键词：抓笔记接口命中这些字样即视为账号被风控（300011「账号异常」等）。
# scheduler 的自适应退避、账号检测接口都引用同一份。
RATE_LIMIT_KEYWORDS = ("账号异常", "稍后重试", "300011", "风控", "操作频繁")

# 每次抓取拉取的最新笔记条数（增量抓取：首抓与后续轮询都用这个值，
# 只取最新 N 条，不再每次拉 30 条重复处理旧笔记）。可用 XHS_ADMIN_CRAWL_NUM 覆盖。
CRAWL_NUM = int(os.environ.get("XHS_ADMIN_CRAWL_NUM") or 5)

# 推送时效窗口（毫秒）：新笔记的发布时间距「当前抓取时刻」超过该时长就不推送。
# 用于避免把「监控停摆期间漏抓的旧笔记」当成新笔记补推。默认 30 分钟，可用
# XHS_ADMIN_PUSH_MAX_AGE_MS 覆盖（单位毫秒）。
PUSH_MAX_AGE_MS = int(os.environ.get("XHS_ADMIN_PUSH_MAX_AGE_MS") or 30 * 60 * 1000)


def hash_password(pwd: str) -> str:
    import hashlib
    return hashlib.sha256((SALT + pwd).encode("utf-8")).hexdigest()
