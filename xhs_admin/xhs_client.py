# -*- coding: utf-8 -*-
"""封装 Spider_XHS 纯 SDK 的调用。"""
import base64
import io
import os
import sys
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager

from config import SDK_DIR

if SDK_DIR not in sys.path:
    sys.path.insert(0, SDK_DIR)

from apis.xhs_pc_apis import XHS_Apis, splice_str  # noqa: E402
from apis.xhs_pc_login_apis import XHSLoginApi  # noqa: E402
from xhs_utils.xhs_pc import XHSPcAuth  # noqa: E402


def _build_api(cookie: str):
    return XHS_Apis(XHSPcAuth.from_cookie(cookie)).bootstrap()


def check_cookie(cookie: str):
    """验证 cookie 有效性。返回 (ok, nickname, user_id, error)。

    注意：未登录时 get_user_me 仍会返回 success=true，但 data.guest 为 true
    （一个匿名 user_id）。必须额外判断 guest，否则会把游客态当成已登录。
    """
    if not cookie or not cookie.strip():
        return False, "", "", "cookie 为空"
    try:
        api = _build_api(cookie)
        success, msg, data = api.get_user_me()
        if success and isinstance(data, dict):
            d = data.get("data") or {}
            if d.get("guest"):
                return False, "", "", "未登录（游客状态）"
            return True, d.get("nickname", ""), d.get("user_id", ""), ""
        return False, "", "", str(msg or "登录态无效")
    except Exception as e:  # noqa: BLE001
        return False, "", "", str(e)


def extract_user_id(url: str) -> str:
    """从博主主页链接提取 24 位 user_id。"""
    try:
        path = urllib.parse.urlparse(url.strip()).path
        uid = path.rstrip("/").split("/")[-1]
        if len(uid) == 24 and all(c in "0123456789abcdef" for c in uid):
            return uid
        return uid if len(uid) == 24 else ""
    except Exception:  # noqa: BLE001
        return ""


def fetch_notes_page(cookie: str, url: str):
    """抓博主最新一页笔记。返回 (ok, notes_list, nickname, error)。"""
    uid = extract_user_id(url)
    if not uid:
        return False, [], "", "无法从链接解析 user_id（应为 24 位十六进制）"
    try:
        api = _build_api(cookie)
        parsed = urllib.parse.urlparse(url.strip())
        q = urllib.parse.parse_qs(parsed.query)
        xsec_token = q.get("xsec_token", [""])[0]
        xsec_source = q.get("xsec_source", ["pc_search"])[0]
        success, msg, data = api.get_user_note_info(uid, "", xsec_token, xsec_source)
        if not success:
            return False, [], "", str(msg)
        notes = []
        if isinstance(data, dict):
            notes = (data.get("data") or {}).get("notes") or []
        nickname = ""
        if notes and isinstance(notes[0], dict):
            user = notes[0].get("user") or {}
            nickname = user.get("nickname") or user.get("nick_name") or ""
        return True, notes, nickname, ""
    except Exception as e:  # noqa: BLE001
        return False, [], "", str(e)


def fetch_followings(cookie: str, user_id: str, cursor: str = ""):
    """获取账号关注列表一页。返回 (ok, users, cursor, has_more, error)。"""
    if not user_id:
        return False, [], "", False, "缺少 user_id"
    try:
        api = _build_api(cookie)
        a = "/api/sns/web/v1/user/followings"
        params = {"num": "30", "cursor": cursor, "user_id": user_id}
        sa = splice_str(a, params)
        headers, cks, _ = api._request_params(sa, "", "GET")
        resp = api.http.get(
            api.base_url + sa,
            headers=headers,
            cookies=cks,
            proxies=api._proxies(None),
            timeout=15,
        )
        body = resp.json()
        if not body.get("success"):
            return False, [], "", False, body.get("msg") or "获取关注列表失败"
        d = body.get("data") or {}
        users = d.get("users") or []
        return True, users, str(d.get("cursor") or ""), bool(d.get("has_more")), ""
    except Exception as e:  # noqa: BLE001
        return False, [], "", False, str(e)


def normalize_note(n: dict) -> dict:
    """把一条原始笔记转成标准字段。"""
    note_id = n.get("note_id", "")
    title = n.get("display_title") or n.get("title") or ""
    cover = n.get("cover") or {}
    cover_url = cover.get("url_default") or cover.get("url_pre") or ""
    xsec = n.get("xsec_token", "")
    note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec:
        note_url += f"?xsec_token={xsec}&xsec_source=pc_user"
    liked = (n.get("interact_info") or {}).get("liked_count", 0)
    try:
        liked = int(liked)
    except (ValueError, TypeError):
        liked = 0
    return {
        "note_id": note_id,
        "title": title,
        "cover_url": cover_url,
        "note_url": note_url,
        "publish_time": int(n.get("time") or 0),
        "liked_count": liked,
    }


# ---------------------------------------------------------------------------
# 登录流程（扫码 / 手机验证码）
#
# 登录是多步有状态流程：先建匿名设备 cookie，再拿二维码或发短信，
# 之后轮询/校验换回正式 web_session。会话保存在内存里（服务重启即失效）。
# ---------------------------------------------------------------------------

_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_LOGIN_SESSIONS: dict = {}
_SESSION_LOCK = threading.Lock()
SESSION_TTL_SECONDS = 600

_QRCODE_STATUS_TEXT = {
    "请扫描二维码": ("pending", "等待扫码…"),
    "请确认登录": ("scanned", "已扫码，请在手机上点击确认"),
    "二维码已过期": ("expired", "二维码已过期，请重新生成"),
}


@contextmanager
def _direct_env():
    """请求期间清除代理环境变量（curl_cffi/libcurl 会自动读取它们）。"""
    saved = {k: os.environ.get(k) for k in _PROXY_KEYS + ("NO_PROXY", "no_proxy")}
    try:
        for key in _PROXY_KEYS:
            os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _safe_close(api) -> None:
    try:
        api.close()
    except Exception:  # noqa: BLE001
        pass


def _prune_sessions() -> None:
    now = time.time()
    with _SESSION_LOCK:
        dead = [sid for sid, s in _LOGIN_SESSIONS.items() if now - s.get("created", 0) > SESSION_TTL_SECONDS]
        for sid in dead:
            _safe_close(_LOGIN_SESSIONS.pop(sid).get("api"))


def _session_get(session_id: str, kind: str):
    if not session_id:
        return None
    with _SESSION_LOCK:
        sess = _LOGIN_SESSIONS.get(session_id)
    if sess is None or sess.get("kind") != kind:
        return None
    if time.time() - sess.get("created", 0) > SESSION_TTL_SECONDS:
        with _SESSION_LOCK:
            _LOGIN_SESSIONS.pop(session_id, None)
        _safe_close(sess.get("api"))
        return None
    return sess


def _qr_data_url(url: str) -> str:
    import qrcode

    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def start_qrcode_login():
    """生成登录二维码。返回 (ok, session_id, qr_image_data_url, error)。"""
    _prune_sessions()
    api = None
    try:
        with _direct_env():
            api = XHSLoginApi()
            cookies = api.generate_init_cookies()
            ok, msg, data = api.generate_qrcode(cookies)
        if not ok or not data:
            _safe_close(api)
            return False, "", "", str(msg or "获取二维码失败")
        sid = uuid.uuid4().hex
        with _SESSION_LOCK:
            _LOGIN_SESSIONS[sid] = {
                "kind": "qrcode",
                "api": api,
                "cookies": data["cookies"],
                "qr_id": data["qr_id"],
                "code": data["code"],
                "created": time.time(),
                "status": "pending",
                "cookie_str": "",
                "account": None,
            }
        return True, sid, _qr_data_url(data["qr_url"]), ""
    except Exception as e:  # noqa: BLE001
        if api is not None:
            _safe_close(api)
        return False, "", "", str(e)


def poll_qrcode_login(session_id: str) -> dict:
    """轮询二维码状态。返回 {status, message, account}。

    status: pending / scanned / confirmed / expired / error
    """
    sess = _session_get(session_id, "qrcode")
    if sess is None:
        return {"status": "expired", "message": "会话不存在或已过期，请重新生成二维码", "account": None}
    if sess.get("cookie_str"):
        return {"status": "confirmed", "message": "登录成功", "account": sess.get("account")}

    try:
        with _direct_env():
            ok, msg, cookies = sess["api"].check_qrcode_status(sess["qr_id"], sess["code"], sess["cookies"])
        sess["cookies"] = cookies
    except Exception as e:  # noqa: BLE001
        sess["status"] = "error"
        return {"status": "error", "message": str(e), "account": None}

    if ok:
        sess["cookie_str"] = XHSLoginApi.cookies_to_str(cookies)
        sess["status"] = "confirmed"
        _safe_close(sess["api"])
        return {"status": "confirmed", "message": "登录成功", "account": sess.get("account")}

    status, label = _QRCODE_STATUS_TEXT.get(str(msg), ("pending", str(msg)))
    sess["status"] = status
    return {"status": status, "message": label, "account": None}


def session_cookie(session_id: str, kind: str = "qrcode") -> str:
    """取登录成功后换回的 cookie 串（供落库用，不返回给前端）。"""
    sess = _session_get(session_id, kind)
    return (sess or {}).get("cookie_str", "")


def attach_session_account(session_id: str, account: dict, kind: str = "qrcode") -> None:
    """记录该会话已落库的账号，避免轮询期间重复创建。"""
    sess = _session_get(session_id, kind)
    if sess is not None:
        sess["account"] = account


def drop_session(session_id: str) -> None:
    with _SESSION_LOCK:
        sess = _LOGIN_SESSIONS.pop(session_id or "", None)
    if sess:
        _safe_close(sess.get("api"))


def start_phone_login(phone: str, zone: str = "86"):
    """发送手机验证码。返回 (ok, session_id, message)。"""
    phone = (phone or "").strip()
    if not phone:
        return False, "", "请输入手机号"
    _prune_sessions()
    api = None
    try:
        with _direct_env():
            api = XHSLoginApi()
            cookies = api.generate_init_cookies()
            ok, msg, _res = api.send_phone_code(phone, cookies, zone)
        if not ok:
            _safe_close(api)
            return False, "", str(msg or "验证码发送失败")
        sid = uuid.uuid4().hex
        with _SESSION_LOCK:
            _LOGIN_SESSIONS[sid] = {
                "kind": "phone",
                "api": api,
                "cookies": cookies,
                "phone": phone,
                "zone": zone,
                "created": time.time(),
                "cookie_str": "",
                "account": None,
            }
        return True, sid, "验证码已发送"
    except Exception as e:  # noqa: BLE001
        if api is not None:
            _safe_close(api)
        return False, "", str(e)


def submit_phone_login(session_id: str, phone: str, code: str, zone: str = "86"):
    """提交验证码完成登录。返回 (ok, cookie_str, message)。"""
    phone = (phone or "").strip()
    code = (code or "").strip()
    if not code:
        return False, "", "请输入验证码"
    sess = _session_get(session_id, "phone")
    if sess is None:
        return False, "", "会话不存在或已过期，请重新发送验证码"
    if sess.get("cookie_str"):
        return True, sess["cookie_str"], "登录成功"
    try:
        with _direct_env():
            ok, msg, data = sess["api"].login_by_phone(phone, code, sess["cookies"], zone)
    except Exception as e:  # noqa: BLE001
        return False, "", str(e)
    if not ok or not data:
        return False, "", str(msg or "登录失败")
    cookie_str = XHSLoginApi.cookies_to_str(data["cookies"])
    sess["cookie_str"] = cookie_str
    _safe_close(sess["api"])
    return True, cookie_str, "登录成功"
