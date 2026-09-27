# -*- coding: utf-8 -*-
"""博主监控轮巡调度：按间隔判断到期，抓首页笔记，识别新笔记入库。"""
import json
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

import notifier
from db import Account, Blogger, Note, SessionLocal
from xhs_client import fetch_notes_page, normalize_note

# 调度器每轮最多抓取的博主数（错峰限流，避免一次抓几十个触发风控）
MAX_PER_ROUND = 3


def _get_baseline(blogger: Blogger) -> list:
    try:
        return json.loads(blogger.baseline_note_ids or "[]")
    except Exception:  # noqa: BLE001
        return []


def _set_baseline(blogger: Blogger, note_ids: list) -> None:
    blogger.baseline_note_ids = json.dumps(note_ids, ensure_ascii=False)


def refresh_blogger(db, blogger_id: int, *, establish_baseline: bool = False) -> dict:
    """抓取一个博主，识别新笔记并入库。

    establish_baseline=True 时只建立基线不入库（用于添加博主那一刻）。
    """
    blogger = db.get(Blogger, blogger_id)
    if not blogger:
        return {"ok": False, "error": "博主不存在"}

    account = db.get(Account, blogger.account_id) if blogger.account_id else None
    if not account or not account.cookie:
        blogger.last_error = "未指定有效账号"
        db.commit()
        return {"ok": False, "error": "未指定有效账号"}

    ok, notes, nickname, error = fetch_notes_page(account.cookie, blogger.url)
    blogger.last_crawled_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not ok:
        blogger.last_error = error or "抓取失败"
        db.commit()
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
        new_notes = []
    else:
        known = set(baseline)
        new_notes = [n for n in normalized if n["note_id"] not in known]

    for n in new_notes:
        db.add(Note(
            blogger_id=blogger.id,
            note_id=n["note_id"],
            title=n["title"],
            cover_url=n["cover_url"],
            note_url=n["note_url"],
            publish_time=n["publish_time"],
            liked_count=n["liked_count"],
        ))

    _set_baseline(blogger, current_ids)
    blogger.last_error = ""
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
