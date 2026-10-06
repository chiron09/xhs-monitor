# -*- coding: utf-8 -*-
"""小红书博主监控管理后台 —— FastAPI 主应用。"""
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update

import browser_login
import covers
import notifier
import scheduler
import xhs_client
from config import DEFAULT_PASSWORD, MIN_INTERVAL_MINUTES, RATE_LIMIT_KEYWORDS, STATIC_DIR, TOKEN_TTL, get_tunables, hash_password, save_tunables
from db import Account, AuthToken, Blogger, Note, SessionLocal, Setting

# ---- 登录 token（持久化到 auth_tokens 表，重启后台不掉登录） ----
# TOKEN_TTL: 0 表示永不过期；>0 则登录后该秒数内有效。


def _token_expires_at() -> str:
    """按 TOKEN_TTL 计算过期时间；0 表示永不过期（空字符串）。"""
    if not TOKEN_TTL:
        return ""
    from datetime import timedelta
    return (datetime.now() + timedelta(seconds=TOKEN_TTL)).strftime("%Y-%m-%d %H:%M:%S")


def _is_expired(expires_at: str) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S") < datetime.now()
    except Exception:  # noqa: BLE001
        return True  # 时间格式异常一律视为过期，避免误放行


def _purge_expired_tokens(db) -> None:
    """清理已过期 token（启动与校验时顺带清理）。"""
    rows = db.query(AuthToken).filter(AuthToken.expires_at != "").all()
    dead = [r.token for r in rows if _is_expired(r.expires_at)]
    if dead:
        db.query(AuthToken).filter(AuthToken.token.in_(dead)).delete(synchronize_session=False)
        db.commit()


def _get_password_hash() -> str:
    db = SessionLocal()
    try:
        s = db.get(Setting, "admin_password")
        if s and s.value:
            return s.value
        h = hash_password(DEFAULT_PASSWORD)
        db.add(Setting(key="admin_password", value=h))
        db.commit()
        return h
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    _get_password_hash()  # 确保默认密码已写入
    db = SessionLocal()
    try:
        _purge_expired_tokens(db)  # 启动时清理过期 token
    finally:
        db.close()
    scheduler.start_scheduler()
    yield
    scheduler.stop_scheduler()


app = FastAPI(title="小红书博主监控后台", lifespan=lifespan)

# 封面图静态服务（抓取时已本地化到 data/covers/，避免 CDN 链接过期 403）
os.makedirs(covers.COVERS_DIR, exist_ok=True)
app.mount("/covers", StaticFiles(directory=covers.COVERS_DIR), name="covers")

# 前端依赖本地化（vendor/ 放 vue / element-plus 等），避免依赖公网 CDN。
# 挂载在 /vendor 下，与 index.html 里的引用路径一致。
_VENDOR_DIR = os.path.join(STATIC_DIR, "vendor")
os.makedirs(_VENDOR_DIR, exist_ok=True)
app.mount("/vendor", StaticFiles(directory=_VENDOR_DIR), name="vendor")


def require_auth(authorization: str = Header(default="")):
    token = authorization.replace("Bearer", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    db = SessionLocal()
    try:
        row = db.get(AuthToken, token)
        if not row:
            raise HTTPException(status_code=401, detail="未登录")
        if _is_expired(row.expires_at):
            db.delete(row)
            db.commit()
            raise HTTPException(status_code=401, detail="登录已过期")
        # 记录最近活跃时间（轻量写入，便于排查）
        row.last_seen_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.commit()
        return token
    finally:
        db.close()


# ---------- 请求模型 ----------
class LoginRequest(BaseModel):
    password: str


class PasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=4, max_length=64)


class AccountCreate(BaseModel):
    name: str = ""
    cookie: str = ""


class CookieUpdate(BaseModel):
    cookie: str = ""


class BloggerCreate(BaseModel):
    name: str = ""
    url: str = Field(min_length=1)
    account_id: int | None = None
    interval_minutes: int = Field(default=60, ge=MIN_INTERVAL_MINUTES, le=10080)
    monitor_start: str = ""
    monitor_end: str = ""


class BloggerUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    account_id: int | None = None
    interval_minutes: int | None = Field(default=None, ge=MIN_INTERVAL_MINUTES, le=10080)
    monitor_start: str | None = None
    monitor_end: str | None = None
    status: str | None = None


class NotesDelete(BaseModel):
    ids: list[int]


class NotesClear(BaseModel):
    blogger_id: int | None = None


class FollowingsListRequest(BaseModel):
    account_id: int


class ImportFollowingsRequest(BaseModel):
    account_id: int
    items: list[dict]
    interval_minutes: int = Field(default=60, ge=MIN_INTERVAL_MINUTES, le=10080)
    monitor_start: str = ""
    monitor_end: str = ""


class BatchUpdateRequest(BaseModel):
    ids: list[int]
    account_id: int | None = None
    interval_minutes: int = Field(default=60, ge=MIN_INTERVAL_MINUTES, le=10080)
    # 只更新显式传入的字段：批量弹窗里没填的项保持原值不动
    monitor_start: str | None = None
    monitor_end: str | None = None
    clear_window: bool = False


_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _norm_hhmm(value: str | None) -> str:
    """把 'HH:MM' 规整成 'HH:MM'（补零）；空 → ''；非法 → 抛 400。"""
    v = (value or "").strip()
    if not v:
        return ""
    parts = v.split(":")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail=f"时间格式应为 HH:MM，收到「{value}」")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise HTTPException(status_code=400, detail=f"时间格式应为 HH:MM，收到「{value}」")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise HTTPException(status_code=400, detail=f"时间超出范围（00:00-23:59）：「{value}」")
    return f"{h:02d}:{m:02d}"


def _check_window(start: str, end: str) -> None:
    """时段合法性：单端可空；两端相同视为无意义配置，直接拒绝。"""
    if start and end and start == end:
        raise HTTPException(status_code=400, detail="监控时段的开始与结束时间不能相同（如需全天监控请都留空）")


# ---------- 页面 ----------
@app.api_route("/", methods=["GET", "HEAD"])
def index():
    # no-cache：HTML 每次都向服务器校验（ETag/Last-Modified），改版后浏览器不会
    # 拿旧缓存白屏或用旧样式。vendor 静态资源走 StaticFiles 自带的 ETag 校验。
    # 同时支持 HEAD（健康检查/浏览器预检不再 405）。
    return FileResponse(os.path.join(STATIC_DIR, "index.html"),
                        headers={"Cache-Control": "no-cache"})


# ---------- 认证 ----------
@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request):
    if hash_password(req.password) != _get_password_hash():
        raise HTTPException(status_code=401, detail="密码错误")
    token = secrets.token_hex(32)
    db = SessionLocal()
    try:
        db.add(AuthToken(
            token=token,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            expires_at=_token_expires_at(),
            last_seen_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            user_agent=(request.headers.get("user-agent") or "")[:256],
            remote_addr=(request.client.host if request.client else "")[:64],
        ))
        db.commit()
    finally:
        db.close()
    return {"token": token, "expires_in": TOKEN_TTL}


@app.post("/api/auth/logout")
def logout(authorization: str = Header(default="")):
    token = authorization.replace("Bearer", "").strip()
    if token:
        db = SessionLocal()
        try:
            row = db.get(AuthToken, token)
            if row:
                db.delete(row)
                db.commit()
        finally:
            db.close()
    return {"ok": True}


@app.get("/api/auth/status")
def auth_status(authorization: str = Header(default="")):
    token = authorization.replace("Bearer", "").strip()
    if not token:
        return {"authed": False}
    db = SessionLocal()
    try:
        row = db.get(AuthToken, token)
        if not row:
            return {"authed": False}
        if _is_expired(row.expires_at):
            db.delete(row)
            db.commit()
            return {"authed": False}
        return {"authed": True}
    finally:
        db.close()


@app.post("/api/auth/password")
def change_password(req: PasswordRequest, _=Depends(require_auth)):
    if hash_password(req.old_password) != _get_password_hash():
        raise HTTPException(status_code=401, detail="旧密码错误")
    db = SessionLocal()
    try:
        s = db.get(Setting, "admin_password")
        s.value = hash_password(req.new_password)
        db.commit()
    finally:
        db.close()
    # 改密后强制重新登录：清空所有 token
    db = SessionLocal()
    try:
        db.query(AuthToken).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    return {"ok": True}


# ---------- 监控参数（后台可调） ----------
@app.get("/api/settings/tunables")
def get_tunables_api(_=Depends(require_auth)):
    db = SessionLocal()
    try:
        return get_tunables(db)
    finally:
        db.close()


@app.post("/api/settings/tunables")
def save_tunables_api(req: dict, _=Depends(require_auth)):
    db = SessionLocal()
    try:
        cfg = save_tunables(db, req or {})
        return {"ok": True, "config": cfg}
    finally:
        db.close()


# ---------- 账号管理 ----------
@app.get("/api/accounts")
def list_accounts(_=Depends(require_auth)):
    db = SessionLocal()
    try:
        items = [a.to_dict() for a in db.query(Account).order_by(Account.id.asc()).all()]
        return {"items": items}
    finally:
        db.close()


def _persist_account(cookie: str, name: str = "") -> dict:
    """校验 cookie 并落库为账号；同一小红书号（xhs_user_id）或同名账号已存在时，更新其 Cookie 而非新建。

    校验失败时一律不写库（不新建垃圾账号，也不覆盖原有登录态）。
    """
    ok, nickname, uid, err = xhs_client.check_cookie(cookie)
    if not ok:
        return {"ok": False, "error": err or "Cookie 校验未通过", "item": None}
    db = SessionLocal()
    try:
        acc = None
        if uid:
            acc = db.query(Account).filter(Account.xhs_user_id == uid).first()
        if acc is None and name:
            acc = db.query(Account).filter(Account.name == name).first()
        if acc is None:
            acc = Account(
                name=name or nickname or "未命名账号",
                cookie=cookie,
                nickname=nickname,
                xhs_user_id=uid,
                status="active",
                last_checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            db.add(acc)
            updated = False
        else:
            acc.cookie = cookie
            acc.nickname = nickname or acc.nickname
            acc.xhs_user_id = uid or acc.xhs_user_id
            acc.status = "active"
            acc.last_checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if name:
                acc.name = name
            updated = True
        db.commit()
        db.refresh(acc)
        return {"ok": True, "error": "", "item": acc.to_dict(), "updated": updated}
    finally:
        db.close()


@app.post("/api/accounts")
def create_account(req: AccountCreate, _=Depends(require_auth)):
    return _persist_account(req.cookie, req.name)


@app.put("/api/accounts/{account_id}")
def update_account_cookie(account_id: int, req: CookieUpdate, _=Depends(require_auth)):
    """更新已有账号的 Cookie：校验通过才替换，失败不动原值。"""
    db = SessionLocal()
    try:
        acc = db.get(Account, account_id)
        if not acc:
            raise HTTPException(status_code=404, detail="账号不存在")
        ok, nickname, uid, err = xhs_client.check_cookie(req.cookie)
        if not ok:
            return {"ok": False, "error": err or "Cookie 校验未通过，未更新原登录态"}
        acc.cookie = req.cookie
        acc.nickname = nickname or acc.nickname
        acc.xhs_user_id = uid or acc.xhs_user_id
        acc.status = "active"
        acc.last_checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.commit()
        db.refresh(acc)
        return {"ok": True, "item": acc.to_dict()}
    finally:
        db.close()


# ---- 浏览器登录（推荐：登录类接口服务器直连会被风控，交给真实浏览器）----
@app.post("/api/accounts/browser/start")
def browser_login_start(_=Depends(require_auth)):
    ok, message = browser_login.start()
    return {"ok": ok, "message": message, "embedded": bool(browser_login.HEADLESS)}


@app.get("/api/accounts/browser/frame")
def browser_login_frame(_=Depends(require_auth)):
    """内嵌模式：抓取登录页当前画面（JPEG dataURL，2 倍分辨率）。"""
    ok, data, w, h = browser_login.snapshot()
    return {"ok": ok, "image": data, "width": w, "height": h}


@app.get("/api/accounts/browser/qrcode")
def browser_login_qrcode(_=Depends(require_auth)):
    """二维码特写：只截登录页里的二维码，PNG 无损 2 倍，方便手机扫码。"""
    ok, data, w, h, err = browser_login.qrcode_snapshot()
    return {"ok": ok, "image": data, "width": w, "height": h, "message": err}


class BrowserInputRequest(BaseModel):
    events: list[dict] = Field(default_factory=list)


@app.post("/api/accounts/browser/input")
def browser_login_input(req: BrowserInputRequest, _=Depends(require_auth)):
    """内嵌模式：把网页上采集的输入事件注入无头浏览器。"""
    return browser_login.inject_input(req.events[:200])


@app.get("/api/accounts/browser/status")
def browser_login_status(_=Depends(require_auth)):
    # 轻量查询：只回 running，供前端刷新按钮状态，不触发校验
    result = browser_login.status()
    return result


@app.post("/api/accounts/browser/check")
def browser_login_check(name: str = "", _=Depends(require_auth)):
    # 手动触发登录态校验（同步，前端用 loading 覆盖 5-10 秒）
    result = browser_login.check()
    if result.get("logged_in") and result.get("cookie") and not result.get("account"):
        cookie = result.pop("cookie")
        saved = _persist_account(cookie, name)
        if saved.get("ok") and saved.get("item"):
            browser_login.set_account(saved["item"])
            result["account"] = saved["item"]
            result["nickname"] = saved["item"].get("nickname", "")
            browser_login.stop()
            result["message"] = "登录成功，已更新原账号的登录态" if saved.get("updated") else "登录成功，账号已添加"
        else:
            err = saved.get("error") or "未知原因"
            if "游客" in err:
                # 点「完成登录」时还没真正登录（只是游客态 web_session）
                result["logged_in"] = False
                result["message"] = "还未登录成功（游客状态），请在页面内扫码或输入验证码后再点「完成登录」"
            else:
                result["message"] = "已检测到登录，但账号校验未通过：" + err + "（可关闭浏览器后重试）"
                browser_login.stop()
    result.pop("cookie", None)
    return result


@app.post("/api/accounts/browser/stop")
def browser_login_stop(_=Depends(require_auth)):
    browser_login.stop()
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int, _=Depends(require_auth)):
    db = SessionLocal()
    try:
        db.execute(sa_update(Blogger).where(Blogger.account_id == account_id).values(account_id=None))
        acc = db.get(Account, account_id)
        if acc:
            db.delete(acc)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/accounts/{account_id}/check")
def check_account(account_id: int, _=Depends(require_auth)):
    db = SessionLocal()
    try:
        acc = db.get(Account, account_id)
        if not acc:
            raise HTTPException(status_code=404, detail="账号不存在")
        ok, nickname, uid, err = xhs_client.check_cookie(acc.cookie)
        rate_limited = False
        rate_limit_msg = ""
        if ok:
            acc.nickname = nickname or acc.nickname
            acc.xhs_user_id = uid or acc.xhs_user_id
            # 风控探测：get_user_me 被风控也返回成功，只有抓笔记接口会报 300011
            probe_uid = uid or acc.xhs_user_id
            if probe_uid:
                c_ok, c_err = xhs_client.check_crawl(acc.cookie, probe_uid)
                if not c_ok and any(k in (c_err or "") for k in RATE_LIMIT_KEYWORDS):
                    rate_limited = True
                    rate_limit_msg = c_err
        if not ok:
            acc.status = "expired"
        elif rate_limited:
            acc.status = "rate_limited"
        else:
            acc.status = "active"
        acc.last_checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.commit()
        return {
            "ok": ok,
            "error": err,
            "rate_limited": rate_limited,
            "rate_limit_msg": rate_limit_msg,
            "item": acc.to_dict(),
        }
    finally:
        db.close()


# ---------- 博主监控 ----------
@app.get("/api/bloggers")
def list_bloggers(_=Depends(require_auth)):
    db = SessionLocal()
    try:
        items = []
        for b in db.query(Blogger).order_by(Blogger.id.asc()).all():
            d = b.to_dict()
            acc = db.get(Account, b.account_id) if b.account_id else None
            d["account_name"] = acc.name if acc else ""
            items.append(d)
        return {"items": items}
    finally:
        db.close()


@app.post("/api/bloggers")
def create_blogger(req: BloggerCreate, _=Depends(require_auth)):
    uid = xhs_client.extract_user_id(req.url)
    if not uid:
        raise HTTPException(status_code=400, detail="链接无效：无法解析 user_id（应为 24 位十六进制）")
    db = SessionLocal()
    try:
        m_start = _norm_hhmm(req.monitor_start)
        m_end = _norm_hhmm(req.monitor_end)
        _check_window(m_start, m_end)
        b = Blogger(
            name=req.name or uid,
            url=req.url.strip(),
            xhs_user_id=uid,
            account_id=req.account_id,
            interval_minutes=req.interval_minutes,
            monitor_start=m_start,
            monitor_end=m_end,
            status="active",
            monitor_since=int(datetime.now().timestamp() * 1000),
        )
        db.add(b)
        db.commit()
        db.refresh(b)
        r = scheduler.refresh_blogger(db, b.id, establish_baseline=True)
        db.refresh(b)
        # 博主名自动获取：优先用抓取到的昵称，兜底 user_id
        nickname = r.get("nickname") or ""
        if nickname:
            b.name = nickname
        elif not req.name:
            b.name = uid
        db.commit()
        db.refresh(b)
        return {"ok": r["ok"], "error": r.get("error", ""), "item": b.to_dict()}
    finally:
        db.close()


@app.put("/api/bloggers/{blogger_id}")
def update_blogger(blogger_id: int, req: BloggerUpdate, _=Depends(require_auth)):
    db = SessionLocal()
    try:
        b = db.get(Blogger, blogger_id)
        if not b:
            raise HTTPException(status_code=404, detail="博主不存在")
        if req.name is not None:
            b.name = req.name
        if req.url is not None:
            uid = xhs_client.extract_user_id(req.url)
            if not uid:
                raise HTTPException(status_code=400, detail="链接无效：无法解析 user_id")
            b.url = req.url.strip()
            b.xhs_user_id = uid
        if req.account_id is not None:
            b.account_id = req.account_id
        if req.interval_minutes is not None:
            b.interval_minutes = req.interval_minutes
        if req.monitor_start is not None or req.monitor_end is not None:
            m_start = _norm_hhmm(req.monitor_start if req.monitor_start is not None else b.monitor_start)
            m_end = _norm_hhmm(req.monitor_end if req.monitor_end is not None else b.monitor_end)
            _check_window(m_start, m_end)
            b.monitor_start = m_start
            b.monitor_end = m_end
        if req.status is not None:
            b.status = req.status
        db.commit()
        db.refresh(b)
        return {"ok": True, "item": b.to_dict()}
    finally:
        db.close()


@app.delete("/api/bloggers/{blogger_id}")
def delete_blogger(blogger_id: int, _=Depends(require_auth)):
    db = SessionLocal()
    try:
        db.execute(sa_delete(Note).where(Note.blogger_id == blogger_id))
        b = db.get(Blogger, blogger_id)
        if b:
            db.delete(b)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/bloggers/{blogger_id}/refresh")
def refresh_blogger_api(blogger_id: int, force: bool = False, _=Depends(require_auth)):
    """手动抓取：用户主动触发，不受监控时段限制（force=true），抓到新笔记照常推送。"""
    db = SessionLocal()
    try:
        r = scheduler.refresh_blogger(db, blogger_id, force=force)
        b = db.get(Blogger, blogger_id)
        return {
            "ok": r["ok"],
            "error": r.get("error", ""),
            "skipped_by_window": r.get("skipped_by_window", False),
            "new_count": r.get("new_count", 0),
            "pushed": r.get("pushed", False),
            "item": b.to_dict() if b else None,
        }
    finally:
        db.close()


# ---------- 关注列表导入 ----------
@app.post("/api/followings/list")
def list_followings(req: FollowingsListRequest, _=Depends(require_auth)):
    db = SessionLocal()
    try:
        acc = db.get(Account, req.account_id)
        if not acc or not acc.cookie:
            raise HTTPException(status_code=400, detail="账号不存在或未配置 Cookie")
        uid = acc.xhs_user_id
        if not uid:
            ok, _nick, uid, err = xhs_client.check_cookie(acc.cookie)
            if not ok:
                raise HTTPException(status_code=400, detail="账号登录态无效，请先检测账号")
            acc.xhs_user_id = uid
            db.commit()
        all_users = []
        cursor = ""
        for _ in range(100):  # 最多翻 100 页
            ok, users, cursor, has_more, err = xhs_client.fetch_followings(acc.cookie, uid, cursor)
            if not ok:
                raise HTTPException(status_code=400, detail="获取关注列表失败：" + err)
            all_users.extend(users)
            if not has_more or not cursor:
                break
        existing = {b.xhs_user_id for b in db.query(Blogger).all()}
        items = []
        for u in all_users:
            uid2 = str(u.get("user_id") or "")
            if not uid2:
                continue
            items.append({
                "user_id": uid2,
                "nickname": u.get("nickname") or u.get("nick_name") or uid2,
                "url": f"https://www.xiaohongshu.com/user/profile/{uid2}",
                "already": uid2 in existing,
            })
        return {"items": items, "total": len(items), "account_name": acc.name or acc.nickname}
    finally:
        db.close()


@app.post("/api/bloggers/import-followings")
def import_followings(req: ImportFollowingsRequest, _=Depends(require_auth)):
    db = SessionLocal()
    try:
        m_start = _norm_hhmm(req.monitor_start)
        m_end = _norm_hhmm(req.monitor_end)
        _check_window(m_start, m_end)
        existing = {b.xhs_user_id for b in db.query(Blogger).all()}
        imported = 0
        skipped = 0
        for it in req.items:
            uid = str(it.get("user_id") or "").strip()
            if not uid or uid in existing:
                skipped += 1
                continue
            db.add(Blogger(
                name=it.get("nickname") or uid,
                url=it.get("url") or f"https://www.xiaohongshu.com/user/profile/{uid}",
                xhs_user_id=uid,
                account_id=req.account_id,
                interval_minutes=req.interval_minutes or 60,
                monitor_start=m_start,
                monitor_end=m_end,
                status="active",
                # 监控起点 = 导入这一刻，之后的笔记才算新笔记
                monitor_since=int(datetime.now().timestamp() * 1000),
                # last_crawled_at 留空 → 调度器判定到期，按限流错峰抓取建基线
            ))
            existing.add(uid)
            imported += 1
        db.commit()
        return {"ok": True, "imported": imported, "skipped": skipped}
    finally:
        db.close()


@app.post("/api/bloggers/batch-update")
def batch_update_bloggers(req: BatchUpdateRequest, _=Depends(require_auth)):
    db = SessionLocal()
    try:
        if not req.ids:
            raise HTTPException(status_code=400, detail="未选择博主")
        values = {"interval_minutes": req.interval_minutes}
        if req.clear_window:
            values["monitor_start"] = ""
            values["monitor_end"] = ""
        elif req.monitor_start is not None or req.monitor_end is not None:
            m_start = _norm_hhmm(req.monitor_start)
            m_end = _norm_hhmm(req.monitor_end)
            _check_window(m_start, m_end)
            values["monitor_start"] = m_start
            values["monitor_end"] = m_end
        # account_id 显式传 null 时才清空账号
        if "account_id" in req.model_fields_set:
            values["account_id"] = req.account_id
        result = db.execute(
            sa_update(Blogger).where(Blogger.id.in_(req.ids)).values(**values)
        )
        db.commit()
        return {"ok": True, "updated": result.rowcount}
    finally:
        db.close()


# ---------- 笔记记录 ----------
@app.get("/api/notes")
def list_notes(
    blogger_id: int | None = None,
    limit: int = 500,
    offset: int = 0,
    _=Depends(require_auth),
):
    db = SessionLocal()
    try:
        q = db.query(Note)
        if blogger_id:
            q = q.filter(Note.blogger_id == blogger_id)
        total = q.count()
        rows = q.order_by(Note.publish_time.desc()).offset(offset).limit(limit).all()
        names = {b.id: b.name for b in db.query(Blogger).all()}
        items = []
        for n in rows:
            d = n.to_dict()
            d["blogger_name"] = names.get(n.blogger_id, "")
            items.append(d)
        return {"total": total, "items": items}
    finally:
        db.close()


@app.delete("/api/notes/clear")
def clear_notes(req: NotesClear, _=Depends(require_auth)):
    db = SessionLocal()
    try:
        stmt = sa_delete(Note)
        if req.blogger_id is not None:
            stmt = stmt.where(Note.blogger_id == req.blogger_id)
        result = db.execute(stmt)
        db.commit()
        return {"ok": True, "deleted": result.rowcount}
    finally:
        db.close()


@app.post("/api/notes/delete")
def delete_notes(req: NotesDelete, _=Depends(require_auth)):
    db = SessionLocal()
    try:
        result = db.execute(sa_delete(Note).where(Note.id.in_(req.ids)))
        db.commit()
        return {"ok": True, "deleted": result.rowcount}
    finally:
        db.close()


# ---------- 新笔记推送 ----------
@app.get("/api/notify/config")
def get_notify_config(_=Depends(require_auth)):
    db = SessionLocal()
    try:
        return notifier.get_config(db)
    finally:
        db.close()


@app.post("/api/notify/config")
def save_notify_config(cfg: dict, _=Depends(require_auth)):
    db = SessionLocal()
    try:
        notifier.save_config(db, cfg)
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/notify/test")
def test_notify(cfg: dict, _=Depends(require_auth)):
    return notifier.send_test(cfg)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
