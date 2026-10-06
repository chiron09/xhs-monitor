# -*- coding: utf-8 -*-
"""博主监控轮巡调度：按间隔判断到期，抓首页笔记，识别新笔记入库并推送。"""
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

import notifier
from config import CRAWL_NUM, DATA_DIR, MAX_PER_ROUND, MIN_INTERVAL_MINUTES, RATE_LIMIT_KEYWORDS
from db import Account, Blogger, Note, SessionLocal, Setting
from xhs_client import fetch_notes_page, is_pinned, normalize_note

# 账号异常告警：同一账号 6 小时内最多提醒一次（登录过期 / 风控共用去重）
_ACCOUNT_ALERT_INTERVAL = 6 * 3600
_EXPIRY_KEYWORDS = ("登录已过期", "未登录", "登录态无效", "登录态失效", "登录信息")
_ACCOUNT_COOLDOWN_SECONDS = 30 * 60  # 风控后暂停该账号 30 分钟

# 推送补偿：失败笔记最多重试次数、重试冷却（秒）
_PUSH_MAX_ATTEMPTS = 5
_PUSH_RETRY_COOLDOWN = 300

_LOG = logging.getLogger("xhs_monitor.scheduler")


def _setup_logging() -> None:
    """结构化日志落到 data/monitor.log（滚动），便于排查「某博主为何没抓到」。"""
    if _LOG.handlers:
        return
    try:
        from logging.handlers import RotatingFileHandler
        os.makedirs(DATA_DIR, exist_ok=True)
        h = RotatingFileHandler(
            os.path.join(DATA_DIR, "monitor.log"),
            maxBytes=1_000_000, backupCount=3, encoding="utf-8",
        )
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _LOG.addHandler(h)
        _LOG.setLevel(logging.INFO)
    except Exception:  # noqa: BLE001
        pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _notify_account_alert(db, account, blogger_name: str, title: str, content: str,
                          dedup_key: str = "acct_alert_ts") -> None:
    """账号异常告警（跨渠道），带时间窗去重，去重状态落库（重启不丢、多实例共享）。

    dedup_key 区分告警类型：登录过期与风控各自独立去重，互不压制。
    """
    now = time.time()
    key = dedup_key
    s = db.get(Setting, key)
    try:
        data = json.loads(s.value) if s and s.value else {}
    except Exception:  # noqa: BLE001
        data = {}
    if now - float(data.get(str(account.id), 0) or 0) < _ACCOUNT_ALERT_INTERVAL:
        return
    data[str(account.id)] = now
    if s:
        s.value = json.dumps(data)
    else:
        db.add(Setting(key=key, value=json.dumps(data)))
    db.commit()
    try:
        notifier.notify_custom(notifier.get_config(db), title, content)
    except Exception:  # noqa: BLE001
        pass


def _alert_account_expiry(db, account, blogger_name: str) -> None:
    """账号 Cookie 失效告警。"""
    _notify_account_alert(
        db, account, blogger_name,
        "⚠️ 小红书账号登录已过期，监控暂停",
        f"账号「{account.name}」的 Cookie 已失效，该账号下的博主监控暂停。\n\n"
        f"最近受影响博主：{blogger_name}\n"
        f"恢复方法：打开监控后台 → 账号管理 → 点该账号的「更新」→ 浏览器登录一次。\n"
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        dedup_key="acct_expiry_ts",
    )


def _alert_account_ratelimit(db, account, blogger_name: str, error: str = "") -> None:
    """账号触发风控（300011 账号异常）告警。"""
    code = "300011" if "300011" in (error or "") else ""
    hint = f"接口返回：{error}" if error else "接口返回 300011（账号异常）"
    _notify_account_alert(
        db, account, blogger_name,
        "⚠️ 小红书账号触发风控（300011），已暂停抓取",
        f"账号「{account.name}」被风控，已自动暂停 {_ACCOUNT_COOLDOWN_SECONDS // 60} 分钟，"
        f"期间该账号下所有博主暂停抓取，避免持续请求加重风控。\n\n"
        f"错误码：{code or '300011'}\n"
        f"{hint}\n"
        f"最近受影响博主：{blogger_name}\n"
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        dedup_key="acct_ratelimit_ts",
    )


def _account_cooldown_until(db, account_id) -> float:
    """账号风控冷却到期时间（epoch 秒）；无冷却返回 0。"""
    if not account_id:
        return 0.0
    s = db.get(Setting, f"acct_cooldown:{account_id}")
    try:
        return float(s.value) if s and s.value else 0.0
    except (ValueError, TypeError):
        return 0.0


def _set_account_cooldown(db, account_id, seconds: int) -> None:
    """设置账号风控冷却（落库，独立 commit，不依赖告警是否去重）。"""
    if not account_id:
        return
    key = f"acct_cooldown:{account_id}"
    s = db.get(Setting, key)
    val = str(time.time() + seconds)
    if s:
        s.value = val
    else:
        db.add(Setting(key=key, value=val))
    db.commit()


def _is_rate_limited(error) -> bool:
    return any(k in (error or "") for k in RATE_LIMIT_KEYWORDS)


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
    """publish_time（epoch 毫秒，绝对时间）是否落在「今天 00:00 ~ 现在」。

    「今天」按服务器本地时区定义（部署机为 GMT+8，即用户所在时区）。
    缺失 / 非法 / 非正值一律判 False（无法确认是今天，保守不推）。
    """
    try:
        pt = int(publish_time or 0)
    except (TypeError, ValueError):
        return False
    if pt <= 0:
        return False
    now = datetime.now()
    return _day_start_ms(now.timestamp()) <= pt <= int(now.timestamp() * 1000)


def _is_after_monitor_since(publish_time: int, monitor_since: int) -> bool:
    """publish_time 是否晚于「添加博主」时刻。

    monitor_since 未设置（0）时不做此过滤（兼容历史博主）。
    发布时间缺失/非法 → 保守判 False（无法证明是监控起点后发布）。
    """
    if not monitor_since:
        return True
    try:
        pt = int(publish_time or 0)
    except (TypeError, ValueError):
        return False
    if pt <= 0:
        return False
    return pt > int(monitor_since)


def _passes_new_filter(note: dict, monitor_since: int) -> bool:
    """双重过滤：必须「今天发布」且「晚于监控起点」。

    发布时间缺失/非法 → 两条都不满足 → 不推（保守，避免误报旧笔记）。
    """
    pt = note.get("publish_time", 0)
    return _is_published_today(pt) and _is_after_monitor_since(pt, monitor_since)


def _parse_hhmm(value: str):
    """把 'HH:MM' 解析为当天的分钟数（0~1439）；格式非法返回 None。"""
    if not value:
        return None
    try:
        hh, mm = str(value).strip().split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h * 60 + m
    return None


def in_monitor_window(blogger, now: datetime | None = None) -> bool:
    """当前是否落在博主的监控时段内。

    两端为空 → 全天放行（兼容历史博主）。
    只填一端 → 视为从该时刻开始 / 到该时刻结束。
    start > end → 跨天时段（如 22:00-06:00）。
    """
    start_raw = (getattr(blogger, "monitor_start", "") or "").strip()
    end_raw = (getattr(blogger, "monitor_end", "") or "").strip()
    if not start_raw and not end_raw:
        return True

    start = _parse_hhmm(start_raw)
    end = _parse_hhmm(end_raw)
    if start is None and end is None:
        return True  # 配置非法，不阻断监控
    if start is None:
        start = 0
    if end is None:
        end = 24 * 60 - 1

    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    if start <= end:
        return start <= cur <= end
    # 跨天：22:00-06:00 → [22:00, 24:00) ∪ [00:00, 06:00]
    return cur >= start or cur <= end


def _effective_interval_minutes(b: Blogger) -> int:
    """有效轮巡间隔 = max(下限, 配置值) × 2^连续失败（封顶 8 倍，即失败退避）。"""
    base = max(MIN_INTERVAL_MINUTES, b.interval_minutes or MIN_INTERVAL_MINUTES)
    fails = min(b.fail_count or 0, 3)
    return base * (1 << fails)


def _is_due(b: Blogger, now_ms: int) -> bool:
    """是否到期：从未抓过 → 到期；否则距上次「尝试」超过有效间隔。"""
    if not b.last_attempt_at:
        return True
    return (now_ms - b.last_attempt_at) >= _effective_interval_minutes(b) * 60 * 1000


def _note_to_dict(note: Note) -> dict:
    """把 Note 行还原成推送所需字段（失败重推用）。"""
    return {
        "note_id": note.note_id,
        "title": note.title,
        "cover_url": note.cover_url,
        "note_url": note.note_url,
        "publish_time": note.publish_time,
        "liked_count": note.liked_count,
    }


def retry_failed_pushes(db) -> dict:
    """推送补偿：重推 push_status=failed 且未超过重试上限的笔记（按博主分组）。"""
    now = time.time()
    s = db.get(Setting, "push_retry_at")
    try:
        last = float(s.value) if s and s.value else 0.0
    except (ValueError, TypeError):
        last = 0.0
    if now - last < _PUSH_RETRY_COOLDOWN:
        return {"retried": 0}
    # 无论有无失败都记录本次检查时间，避免每 20 秒全表扫一次
    if s:
        s.value = str(now)
    else:
        db.add(Setting(key="push_retry_at", value=str(now)))
    db.commit()

    failed = (db.query(Note)
              .filter(Note.push_status == "failed", Note.push_attempts < _PUSH_MAX_ATTEMPTS)
              .all())
    if not failed:
        return {"retried": 0}

    cfg = notifier.get_config(db)
    if not cfg.get("enabled"):
        # 推送未启用：无需重试，标记为已处理，避免永远挂在 failed
        for n in failed:
            n.push_status = "sent"
        db.commit()
        return {"retried": 0}

    by_blogger: dict[int, list] = {}
    for n in failed:
        by_blogger.setdefault(n.blogger_id, []).append(n)

    retried = 0
    for bid, notes in by_blogger.items():
        blogger = db.get(Blogger, bid)
        if not blogger:
            continue
        note_dicts = [_note_to_dict(n) for n in notes]
        try:
            res = notifier.notify_new_notes(cfg, blogger.name, note_dicts)
            ok = (res or {}).get("ok_count", 0) > 0
        except Exception:  # noqa: BLE001
            ok = False
        for n in notes:
            if ok:
                n.push_status = "sent"
            else:
                n.push_attempts = (n.push_attempts or 0) + 1
        if ok:
            retried += len(notes)
            _LOG.info("推送补偿成功 博主#%s %s 共%d条", bid, blogger.name, len(notes))
        db.commit()
    return {"retried": retried}


def refresh_blogger(db, blogger_id: int, *, establish_baseline: bool = False,
                    force: bool = False) -> dict:
    """抓取一个博主，识别新笔记并入库、推送。

    establish_baseline=True 时只建立基线不入库（用于添加博主那一刻）。
    force=True 时忽略监控时段（用户手动点「抓取」时使用）。
    入库条件（同时满足）：① 不在基线中；② 不在已入库去重集；③ 今天发布；④ 晚于监控起点。
    """
    blogger = db.get(Blogger, blogger_id)
    if not blogger:
        return {"ok": False, "error": "博主不存在"}

    if not force and not establish_baseline and not in_monitor_window(blogger):
        return {"ok": False, "error": "当前不在监控时段", "skipped_by_window": True}

    account = db.get(Account, blogger.account_id) if blogger.account_id else None
    if not account or not account.cookie:
        blogger.last_error = "未指定有效账号"
        db.commit()
        return {"ok": False, "error": "未指定有效账号"}

    # 首次抓取且未设监控起点：以当前时刻为起点（添加博主那一刻）
    if not blogger.monitor_since:
        blogger.monitor_since = _now_ms()

    # 记录本次尝试时间（失败退避用）；成功时间单独记 last_crawled_at
    blogger.last_attempt_at = _now_ms()

    ok, notes, nickname, error = fetch_notes_page(
        account.cookie, blogger.url,
        num=1 if establish_baseline else CRAWL_NUM,
    )

    if not ok:
        # 失败：不推进 last_crawled_at，累加 fail_count 做退避
        blogger.fail_count = (blogger.fail_count or 0) + 1
        blogger.last_error = error or "抓取失败"
        db.commit()
        _LOG.warning("博主#%s %s 抓取失败: %s", blogger.id, blogger.name, error)
        if account and _is_rate_limited(error):
            # 风控：暂停该账号一段时间，避免持续请求加重风控
            _set_account_cooldown(db, account.id, _ACCOUNT_COOLDOWN_SECONDS)
            _alert_account_ratelimit(db, account, blogger.name, error)
        elif account and any(k in (error or "") for k in _EXPIRY_KEYWORDS):
            _alert_account_expiry(db, account, blogger.name)
        return {"ok": False, "error": error or "抓取失败"}

    # 成功：复位失败计数、记成功时间
    blogger.fail_count = 0
    blogger.last_crawled_at = _now_ms()
    blogger.last_error = ""

    # 归一化：跳过置顶笔记，再做早停（列表按时间倒序，遇到「非今天」的旧笔记即可停止）。
    # 置顶笔记永远排最前，必须 continue 跳过而非 break；publish_time 缺失(0)不触发早停。
    normalized = []
    for n in notes:
        if not (isinstance(n, dict) and n.get("note_id")):
            continue
        if is_pinned(n):
            continue
        norm = normalize_note(n)
        pt = norm.get("publish_time") or 0
        if pt > 0 and not _is_published_today(pt):
            break
        normalized.append(norm)
    current_ids = [n["note_id"] for n in normalized]
    baseline = _get_baseline(blogger)

    if establish_baseline:
        # 添加博主那一刻：只建基线，不识别新笔记（绝不把添加前的旧笔记当新笔记）。
        candidates = []
    else:
        known = set(baseline)
        candidates = [n for n in normalized if n["note_id"] not in known]

    # 双重过滤：只保留「今天发布」且「晚于监控起点」的笔记
    monitor_since = blogger.monitor_since or 0
    new_notes = [n for n in candidates if _passes_new_filter(n, monitor_since)]
    skipped = len(candidates) - len(new_notes)

    # 去重：已入库的 note 不再重复插入（DB 唯一索引兜底并发）
    existing_ids = {
        row.note_id for row in db.query(Note).filter(Note.blogger_id == blogger.id).all()
    }
    new_notes = [n for n in new_notes if n["note_id"] not in existing_ids]

    # 封面直接使用原 URL（不下载本地化）
    note_objs = []
    for n in new_notes:
        note_objs.append(Note(
            blogger_id=blogger.id,
            note_id=n["note_id"],
            title=n["title"],
            cover_url=n["cover_url"],
            note_url=n["note_url"],
            publish_time=n["publish_time"],
            liked_count=n["liked_count"],
            push_status="pending",
            push_attempts=0,
        ))
    if note_objs:
        db.add_all(note_objs)

    # 基线更新：抓到空列表时不动基线（避免旧笔记被误判为新笔记、造成重复推送）
    if current_ids:
        _set_baseline(blogger, current_ids)

    db.commit()

    # 新笔记推送（失败不影响抓取结果；失败标记进补偿队列）
    # 时段只约束「定时轮巡」：时段外调度器不抓取，自然不会推送。
    # 用户手动点「抓取」（force=True）是主动行为，抓到新笔记照常推送。
    notify_result = None
    pushed = False
    if note_objs:
        cfg = notifier.get_config(db)
        try:
            if cfg.get("enabled"):
                notify_result = notifier.notify_new_notes(cfg, blogger.name, new_notes)
                ok_push = (notify_result or {}).get("ok_count", 0) > 0
            else:
                # 推送未启用：视为无需重试
                ok_push = True
                notify_result = {"ok_count": 0, "failures": []}
            pushed = bool(ok_push and cfg.get("enabled"))
            for no in note_objs:
                no.push_status = "sent" if ok_push else "failed"
                if not ok_push:
                    no.push_attempts = 1
        except Exception:  # noqa: BLE001
            notify_result = None
            for no in note_objs:
                no.push_status = "failed"
                no.push_attempts = 1
        db.commit()

    _LOG.info("博主#%s %s 抓取成功 抓取=%d 新=%d 推送=%s",
              blogger.id, blogger.name, len(normalized), len(new_notes), pushed)

    return {
        "ok": True,
        "crawled": len(normalized),
        "new_count": len(new_notes),
        "pushed": pushed,
        "skipped_count": skipped,
        "baseline_count": len(current_ids),
        "nickname": nickname,
        "notify": notify_result,
    }


def check_and_crawl() -> dict:
    """扫描所有 active 博主，按间隔判断到期，并发抓取（每轮限流错峰）。

    - 时段外整轮跳过、不占名额；
    - 到期排序按「最久未抓取优先」（LRU），避免 id 靠后的博主长期饿死；
    - 抓取并发执行（每博主独立会话），缩短整轮耗时、避免 misfire。
    """
    _setup_logging()
    db = SessionLocal()
    due_ids = []
    skipped_window = 0
    skipped_cooldown = 0
    try:
        bloggers = (db.query(Blogger)
                    .filter(Blogger.status == "active")
                    .order_by(Blogger.id.asc())
                    .all())
        now_ms = _now_ms()
        now = time.time()
        in_window = []
        for b in bloggers:
            if not in_monitor_window(b):
                skipped_window += 1
                continue
            # 账号风控冷却中：整轮跳过，不消耗名额、也不触发请求
            if b.account_id and _account_cooldown_until(db, b.account_id) > now:
                skipped_cooldown += 1
                continue
            in_window.append(b)
        due = [b for b in in_window if _is_due(b, now_ms)]
        due.sort(key=lambda b: b.last_attempt_at or 0)  # 最久未抓取优先
        due_ids = [b.id for b in due[:MAX_PER_ROUND]]
    finally:
        db.close()

    results = []

    def _run(bid: int) -> dict:
        d = SessionLocal()
        try:
            return {"blogger_id": bid, **refresh_blogger(d, bid)}
        finally:
            d.close()

    if due_ids:
        with ThreadPoolExecutor(max_workers=MAX_PER_ROUND) as ex:
            futures = [ex.submit(_run, bid) for bid in due_ids]
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as e:  # noqa: BLE001
                    results.append({"blogger_id": None, "ok": False, "error": str(e)})

    # 推送补偿重试（有冷却，不会每轮都全表扫）
    retried = 0
    db2 = SessionLocal()
    try:
        retried = (retry_failed_pushes(db2) or {}).get("retried", 0)
    finally:
        db2.close()

    _LOG.info("轮巡结束 抓取=%d 时段外=%d 风控冷却=%d 补偿重推=%d",
              len(results), skipped_window, skipped_cooldown, retried)
    for r in results:
        _LOG.info("  - 博主#%s ok=%s new=%d pushed=%s err=%s",
                  r.get("blogger_id"), r.get("ok"), r.get("new_count", 0),
                  r.get("pushed"), r.get("error", ""))

    return {
        "checked": len(results),
        "skipped_window": skipped_window,
        "skipped_cooldown": skipped_cooldown,
        "retried": retried,
        "results": results,
    }


_scheduler = None
_lock = threading.Lock()


def start_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return
        from config import SCHEDULER_INTERVAL
        _setup_logging()
        _scheduler = BackgroundScheduler()
        _scheduler.add_job(
            check_and_crawl,
            "interval",
            seconds=SCHEDULER_INTERVAL,
            id="blogger_crawl",
            coalesce=True,                     # 积压时只跑最新一轮
            misfire_grace_time=SCHEDULER_INTERVAL,  # 一轮略超时仍补跑，避免整体错位
        )
        _scheduler.start()
        _LOG.info("调度器启动 间隔=%ds 每轮上限=%d 下限=%d分钟",
                  SCHEDULER_INTERVAL, MAX_PER_ROUND, MIN_INTERVAL_MINUTES)


def stop_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None
