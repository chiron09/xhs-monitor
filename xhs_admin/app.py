# -*- coding: utf-8 -*-
"""小红书博主监控管理后台 —— FastAPI 主应用。"""
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException
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
from config import DEFAULT_PASSWORD, STATIC_DIR, hash_password
from db import Account, Blogger, Note, SessionLocal, Setting

# ---- 登录 token（内存态，重启失效） ----
_tokens: set = set()


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
    scheduler.start_scheduler()
    yield
    scheduler.stop_scheduler()


app = FastAPI(title="小红书博主监控后台", lifespan=lifespan)

# 封面图静态服务（抓取时已本地化到 data/covers/，避免 CDN 链接过期 403）
os.makedirs(covers.COVERS_DIR, exist_ok=True)
app.mount("/covers", StaticFiles(directory=covers.COVERS_DIR), name="covers")


def require_auth(authorization: str = Header(default="")):
    token = authorization.replace("Bearer", "").strip()
    if not token or token not in _tokens:
        raise HTTPException(status_code=401, detail="未登录")
    return token


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


class PhoneSendRequest(BaseModel):
    phone: str = Field(min_length=1)
    zone: str = "86"


class PhoneVerifyRequest(BaseModel):
    session_id: str = Field(min_length=1)
    phone: str = ""
    code: str = Field(min_length=1)
    zone: str = "86"
    name: str = ""


class BloggerCreate(BaseModel):
    name: str = ""
    url: str = Field(min_length=1)
    account_id: int | None = None
    interval_minutes: int = Field(default=60, ge=1, le=10080)


class BloggerUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    account_id: int | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=10080)
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
    interval_minutes: int = 60


class BatchUpdateRequest(BaseModel):
    ids: list[int]
    account_id: int | None = None
    interval_minutes: int = Field(default=60, ge=1, le=10080)


# ---------- 页面 ----------
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ---------- 认证 ----------
@app.post("/api/auth/login")
def login(req: LoginRequest):
    if hash_password(req.password) != _get_password_hash():
        raise HTTPException(status_code=401, detail="密码错误")
    token = secrets.token_hex(32)
    _tokens.add(token)
    return {"token": token}


@app.post("/api/auth/logout")
def logout(authorization: str = Header(default="")):
    _tokens.discard(authorization.replace("Bearer", "").strip())
    return {"ok": True}


@app.get("/api/auth/status")
def auth_status(authorization: str = Header(default="")):
    token = authorization.replace("Bearer", "").strip()
    return {"authed": bool(token and token in _tokens)}


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
    _tokens.clear()  # 改密后强制重新登录
    return {"ok": True}


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
    """内嵌模式：抓取登录页当前画面（JPEG dataURL）。"""
    ok, data, w, h = browser_login.snapshot()
    return {"ok": ok, "image": data, "width": w, "height": h}


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
            result["message"] = "已检测到登录，但账号校验未通过：" + (saved.get("error") or "未知原因") + "（可关闭浏览器后重试）"
            browser_login.stop()
    result.pop("cookie", None)
    return result


@app.post("/api/accounts/browser/stop")
def browser_login_stop(_=Depends(require_auth)):
    browser_login.stop()
    return {"ok": True}


# ---- 扫码登录 ----
def _persist_async(cookie: str, name: str, on_done) -> None:
    """后台线程校验入库（SDK 校验要 5-10 秒，不能堵轮询请求）。"""
    def _run():
        try:
            on_done(_persist_account(cookie, name))
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_run, daemon=True).start()


# 扫码/手机登录会话的入库结果缓存：session_id -> {status, message, account}
# （校验在后台线程跑，轮询接口立即返回；前端下一轮轮询时取到结果）
_persist_results: dict = {}
_persist_lock = threading.Lock()


def _persist_latched(session_id: str, kind: str, cookie: str, name: str, when_done=None):
    """确保同一登录会话只入库一次。立即返回；结果写进 _persist_results。"""
    with _persist_lock:
        if session_id in _persist_results:
            return _persist_results[session_id]
        _persist_results[session_id] = {"status": "pending", "message": "正在校验登录态…", "account": None}

    def _done(saved: dict):
        entry = {
            "status": "ok" if saved.get("ok") else "failed",
            "message": ("登录成功，账号已添加" if not saved.get("updated")
                        else "登录成功，已更新原账号的登录态") if saved.get("ok")
                       else "已登录，但账号校验未通过：" + (saved.get("error") or "未知原因"),
            "account": saved.get("item"),
            "error": saved.get("error", ""),
        }
        with _persist_lock:
            _persist_results[session_id] = entry
        if when_done:
            try:
                when_done(saved, entry)
            except Exception:  # noqa: BLE001
                pass

    _persist_async(cookie, name, _done)
    return _persist_results[session_id]


@app.post("/api/accounts/qrcode/start")
def start_qrcode_login(_=Depends(require_auth)):
    ok, session_id, qr_image, err = xhs_client.start_qrcode_login()
    return {"ok": ok, "session_id": session_id, "qr_image": qr_image, "error": err}


@app.get("/api/accounts/qrcode/poll")
def poll_qrcode_login(session_id: str, name: str = "", _=Depends(require_auth)):
    result = xhs_client.poll_qrcode_login(session_id)
    if result["status"] == "confirmed" and not result.get("account"):
        # 入库不阻塞轮询：cookie 从会话取出后丢给后台线程，结果下轮轮询取
        with _persist_lock:
            entry = _persist_results.get(session_id)
        if entry is None:
            cookie = xhs_client.session_cookie(session_id, "qrcode")
            if cookie:
                def _attach(saved: dict, _entry: dict):
                    if saved.get("item"):
                        xhs_client.attach_session_account(session_id, saved["item"], "qrcode")
                _persist_latched(session_id, "qrcode", cookie, name, _attach)
                result["message"] = "登录成功，正在校验登录态…"
                result["persisting"] = True
        else:
            result["persisting"] = True
            result["message"] = entry.get("message") or result["message"]
            if entry.get("status") == "ok" and entry.get("account"):
                result["account"] = entry["account"]
                result["persist_done"] = True
            elif entry.get("status") == "failed":
                result["persist_failed"] = True
    return result


# ---- 手机验证码登录 ----
@app.post("/api/accounts/phone/send")
def send_phone_code(req: PhoneSendRequest, _=Depends(require_auth)):
    ok, session_id, message = xhs_client.start_phone_login(req.phone, req.zone)
    return {"ok": ok, "session_id": session_id, "message": message}


@app.post("/api/accounts/phone/verify")
def verify_phone_code(req: PhoneVerifyRequest, _=Depends(require_auth)):
    ok, cookie, message = xhs_client.submit_phone_login(req.session_id, req.phone, req.code, req.zone)
    if not ok:
        return {"ok": False, "message": message, "account": None, "persisting": False}
    entry = _persist_latched(req.session_id, "phone", cookie, req.name)
    if entry.get("status") == "pending":
        return {"ok": True, "message": "登录成功，正在校验登录态…", "account": None, "persisting": True}
    return {
        "ok": entry.get("status") == "ok",
        "message": entry.get("message") or "登录成功",
        "error": entry.get("error", ""),
        "account": entry.get("account"),
        "persisting": False,
    }


@app.get("/api/accounts/phone/verify/result")
def phone_verify_result(session_id: str, _=Depends(require_auth)):
    """前端轮询取异步入库结果。"""
    with _persist_lock:
        entry = _persist_results.get(session_id)
    if entry is None:
        return {"status": "pending", "message": "正在校验登录态…", "account": None}
    return entry


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
        acc.status = "active" if ok else "expired"
        if ok:
            acc.nickname = nickname or acc.nickname
            acc.xhs_user_id = uid or acc.xhs_user_id
        acc.last_checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.commit()
        return {"ok": ok, "error": err, "item": acc.to_dict()}
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
        b = Blogger(
            name=req.name or uid,
            url=req.url.strip(),
            xhs_user_id=uid,
            account_id=req.account_id,
            interval_minutes=req.interval_minutes,
            status="active",
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
def refresh_blogger_api(blogger_id: int, _=Depends(require_auth)):
    db = SessionLocal()
    try:
        r = scheduler.refresh_blogger(db, blogger_id)
        b = db.get(Blogger, blogger_id)
        return {
            "ok": r["ok"],
            "error": r.get("error", ""),
            "new_count": r.get("new_count", 0),
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
                status="active",
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
        result = db.execute(
            sa_update(Blogger)
            .where(Blogger.id.in_(req.ids))
            .values(account_id=req.account_id, interval_minutes=req.interval_minutes)
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
