# -*- coding: utf-8 -*-
"""博主监控轮巡调度：按间隔判断到期，抓首页笔记，识别新笔记入库。"""
import json
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

import covers
import notifier
from db import Account, Blogger, Note, SessionLocal
from xhs_client import fetch_notes_page, normalize_note

# 调度器每轮最多抓取的博主数（错峰限流，避免一次抓几十个触发风控）
MAX_PER_ROUND = 3

# 账号登录过期告警：同一账号 6 小时内最多提醒一次
_EXPIRY_ALERT_INTERVAL = 6 * 3600
_EXPIRY_KEYWORDS = ("登录已过期", "未登录", "登录态无效", "登录态失效", "登录信息")
_last_expiry_alert = {}  # account_id -> unix ts


def _alert_account_expiry(db, account, blogger_name: str) -> None:
    """账号 Cookie 失效时推送告警（跨渠道，带时间窗去重）。"""
    import time as _time
    now = _time.time()
    if now - _last_expiry_alert.get(account.id, 0) < _EXPIRY_ALERT_INTERVAL:
        return
    _last_expiry_alert[account.id] = now
    title = "⚠️ 小红书账号登录已过期，监控暂停"
    content = (
        f"账号「{account.name}」的 Cookie 已失效，该账号下的博主监控暂停。\n\n"
        f"最近受影响博主：{blogger_name}\n"
        f"恢复方法：打开监控后台 → 账号管理 → 点该账号的「更新」→ 浏览器登录一次。\n"
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    try:
        notifier.notify_custom(notifier.get_config(db), title, content)
    except Exception:  # noqa: BLE001
        pass


def _get_baseline(blogger: Blogger) -> list:
    try:
        return json.loads(blogger.baseline_note_ids or "[]")
    except Exception:  # noqa: BLE001
        return []


def _set_baseline(blogger: Blogger, note_ids: list) -> None:
    blogger.baseline_note_ids = json.dumps(note_ids, ensure_ascii=False)


def _day_start_ms(ts: float) -> int:
    """给定时间戳，返回其所在「当天 00:00:00」的毫秒时间戳（本地时区）。"""
    d = datetime.fromtimestamp(ts)
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _is_published_today(publish_time: int) -> bool:
    """publish_time（毫秒）是否落在今天 00:00 ~ 现在之间。

    publish_time 缺失（0 或非法）时无法判定，按「放行」处理，
    交由 _passes_new_filter 的「晚于监控起点」条件兜底，避免漏推。
    """
    try:
        pt = int(publish_time or 0)
    except (TypeError, ValueError):
        return True
    if pt <= 0:
        return True
    now = datetime.now()
    return _day_start_ms(now.timestamp()) <= pt <= int(now.timestamp() * 1000)


def _is_after_monitor_since(publish_time: int, monitor_since: int) -> bool:
    """publish_time 是否晚于「添加博主」时刻。

    monitor_since 未设置（0）时不做此过滤（兼容历史博主），
    再交给「当天」条件约束。
    """
    if not monitor_since:
        return True
    try:
        pt = int(publish_time or 0)
    except (TypeError, ValueError):
        return True
    if pt <= 0:
        # 发布时间缺失：无法证明是「监控起点后」发布，保守判定为不属于新笔记
        return False
    return pt > int(monitor_since)


def _passes_new_filter(note: dict, monitor_since: int) -> bool:
    """双重过滤：必须是「今天发布」且「晚于监控起点」的笔记。"""
    pt = note.get("publish_time", 0)
    return _is_published_today(pt) and _is_after_monitor_since(pt, monitor_since)


def refresh_blogger(db, blogger_id: int, *, establish_baseline: bool = False) -> dict:
    """抓取一个博主，识别新笔记并入库。

    establish_baseline=True 时只建立基线不入库（用于添加博主那一刻）。
    入库条件（同时满足）：① 不在基线中；② 今天发布；③ 晚于监控起点。
    """
    blogger = db.get(Blogger, blogger_id)
    if not blogger:
        return {"ok": False, "error": "博主不存在"}

    account = db.get(Account, blogger.account_id) if blogger.account_id else None
    if not account or not account.cookie:
        blogger.last_error = "未指定有效账号"
        db.commit()
        return {"ok": False, "error": "未指定有效账号"}

    # 首次抓取且未设监控起点：以当前时刻为起点（添加博主那一刻）
    if not blogger.monitor_since:
        blogger.monitor_since = int(datetime.now().timestamp() * 1000)

    ok, notes, nickname, error = fetch_notes_page(account.cookie, blogger.url)
    blogger.last_crawled_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not ok:
        blogger.last_error = error or "抓取失败"
        db.commit()
        # 登录过期类错误：推送告警（去重），提醒用户及时续命
        if account and any(k in (error or "") for k in _EXPIRY_KEYWORDS):
            _alert_account_expiry(db, account, blogger.name)
        return {"ok": False, "error": error or "抓取失败"}

    normalized = [
        normalize_note(n) for n in notes
        if isinstance(n, dict) and n.get("note_id")
    ]
    current_ids = [n["note_id"] for n in normalized]
    baseline = _get_baseline(blogger)

    if establish_baseline or not baseline:
        # 基线为空（首次成功抓取，或此前基线未建成）：先补基线，
        # 绝不把博主添加之前的旧笔记当成"新笔记"入库。
        candidates = []
    else:
        known = set(baseline)
        candidates = [n for n in normalized if n["note_id"] not in known]

    # 双重过滤：只保留「今天发布」且「晚于监控起点」的笔记
    monitor_since = blogger.monitor_since or 0
    new_notes = [n for n in candidates if _passes_new_filter(n, monitor_since)]
    skipped = len(candidates) - len(new_notes)

    for n in new_notes:
        # 封面本地化：CDN 链接带时效签名会过期，入库时立即下载（失败保留原链接）
        local_cover = None
        try:
            local_cover = covers.download_cover(n["note_id"], n["cover_url"])
        except Exception:  # noqa: BLE001
            local_cover = None
        db.add(Note(
            blogger_id=blogger.id,
            note_id=n["note_id"],
            title=n["title"],
            cover_url=local_cover or n["cover_url"],
            note_url=n["note_url"],
            publish_time=n["publish_time"],
            liked_count=n["liked_count"],
        ))

    _set_baseline(blogger, current_ids)
    blogger.last_error = ""

    # 顺带回填：出现在本次列表里、但封面尚未本地化的存量笔记
    # （旧入库时未本地化，CDN 链接过期后 403；趁还有新鲜链接时补下载）
    try:
        existing_rows = {
            row.note_id: row
            for row in db.query(Note).filter(Note.blogger_id == blogger.id).all()
            if not (row.cover_url or "").startswith("/covers/")
        }
        for n in normalized:
            row = existing_rows.get(n["note_id"])
            if row is None or not n.get("cover_url"):
                continue
            local = None
            try:
                local = covers.download_cover(n["note_id"], n["cover_url"])
            except Exception:  # noqa: BLE001
                local = None
            if local:
                row.cover_url = local
    except Exception:  # noqa: BLE001  # 回填失败不影响主流程
        pass

    db.commit()

    # 新笔记推送（失败不影响抓取结果）
    notify_result = None
    if new_notes:
        try:
            notify_result = notifier.notify_new_notes(
                notifier.get_config(db), blogger.name, new_notes
            )
        except Exception:  # noqa: BLE001
            notify_result = None

    return {
        "ok": True,
        "crawled": len(normalized),
        "new_count": len(new_notes),
        "skipped_count": skipped,
        "baseline_count": len(current_ids),
        "nickname": nickname,
        "notify": notify_result,
    }


def check_and_crawl() -> dict:
    """扫描所有 active 博主，按各自间隔判断是否到期并抓取（每轮限流错峰）。"""
    db = SessionLocal()
    results = []
    try:
        now = datetime.now()
        bloggers = db.query(Blogger).filter(Blogger.status == "active").order_by(Blogger.id.asc()).all()
        crawled = 0
        for b in bloggers:
            if crawled >= MAX_PER_ROUND:
                break
            due = False
            if not b.last_crawled_at:
                due = True
            else:
                try:
                    last = datetime.strptime(b.last_crawled_at, "%Y-%m-%d %H:%M:%S")
                    due = (now - last).total_seconds() >= b.interval_minutes * 60
                except Exception:  # noqa: BLE001
                    due = True
            if due:
                r = refresh_blogger(db, b.id)
                results.append({"blogger_id": b.id, **r})
                crawled += 1
        db.commit()
    finally:
        db.close()
    return {"checked": len(results), "results": results}


_scheduler = None
_lock = threading.Lock()


def start_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return
        from config import SCHEDULER_INTERVAL
        _scheduler = BackgroundScheduler()
        _scheduler.add_job(
            check_and_crawl,
            "interval",
            seconds=SCHEDULER_INTERVAL,
            id="blogger_crawl",
        )
        _scheduler.start()


def stop_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None
