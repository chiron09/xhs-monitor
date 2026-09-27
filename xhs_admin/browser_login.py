# -*- coding: utf-8 -*-
"""用本机 Chrome（CDP）完成小红书登录。

为什么需要它：
    本服务用的是纯 SDK 直连（curl_cffi 模拟 Chrome），小红书对登录类接口
    的风控比数据接口严得多 —— 扫码最后一步换 session 会失败、手机验证码
    接口虽返回成功但短信被静默丢弃。真实浏览器能通过这些校验，所以把
    「登录」这一步交给浏览器，登录成功后再把 cookie 交回平台入库。

实现方式：
    启动一个独立的 Chrome 实例（专属 user-data-dir，不影响用户自己的浏览器），
    打开小红书登录页让用户自行扫码或输入验证码；平台通过 CDP 轮询浏览器
    cookie，并用 SDK 的 get_user_me 验证是否真的登录成功。
"""
import json
import os
import subprocess
import time
import urllib.request

import websocket  # websocket-client

from config import DATA_DIR

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

DEBUG_PORT = 9334
LOGIN_URL = "https://www.xiaohongshu.com/login"
PROFILE_DIR = os.path.join(DATA_DIR, "chrome_profile")

_state: dict = {"proc": None, "started_at": 0.0, "account": None}


def _chrome_path() -> str:
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    return ""


def _http_json(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _debug_url(path: str) -> str:
    return f"http://127.0.0.1:{DEBUG_PORT}{path}"


def is_running() -> bool:
    """调试端口是否可用（即我们启动的 Chrome 还活着）。"""
    try:
        _http_json(_debug_url("/json/version"), timeout=2.0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _pick_page_target():
    try:
        targets = _http_json(_debug_url("/json/list"), timeout=3.0)
    except Exception:  # noqa: BLE001
        return None
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    for t in pages:
        if "xiaohongshu.com" in (t.get("url") or ""):
            return t
    return pages[0]


def _cdp_connect(ws_url: str):
    """建立 CDP 连接。

    websocket-client 默认会带 Origin 头，Chrome 93+ 对 CDP 端口会校验并拒绝，
    因此必须 suppress_origin（启动参数里也加了 --remote-allow-origins 兜底）。
    """
    return websocket.create_connection(ws_url, timeout=15, suppress_origin=True)


def _cdp(ws, method: str, params: dict | None = None, msg_id: int = 1):
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    deadline = time.time() + 12
    while time.time() < deadline:
        raw = ws.recv()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if msg.get("id") == msg_id:
            return msg
    raise TimeoutError(f"CDP 命令超时: {method}")


def read_cookies() -> dict:
    """读取浏览器里所有 xiaohongshu.com 的 cookie。"""
    target = _pick_page_target()
    if not target:
        return {}
    ws = None
    try:
        ws = _cdp_connect(target["webSocketDebuggerUrl"])
        _cdp(ws, "Network.enable")
        result = _cdp(ws, "Network.getAllCookies")
        cookies = (result.get("result") or {}).get("cookies") or []
    except Exception:  # noqa: BLE001
        return {}
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
    picked = {}
    for c in cookies:
        domain = str(c.get("domain") or "")
        if "xiaohongshu.com" not in domain:
            continue
        name = c.get("name")
        if name:
            picked[name] = c.get("value", "")
    return picked


def cookies_to_str(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def start():
    """启动（或复用）登录用浏览器。返回 (ok, message)。"""
    if is_running():
        return True, "浏览器窗口已打开，请在其中完成登录"
    chrome = _chrome_path()
    if not chrome:
        return False, "未找到 Chrome，请先安装 Google Chrome"
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return False, f"创建浏览器数据目录失败：{e}"

    _state["account"] = None
    args = [
        chrome,
        f"--remote-debugging-port={DEBUG_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,OptimizationHints",
        "--window-size=1120,800",
        LOGIN_URL,
    ]
    try:
        _state["proc"] = subprocess.Popen(  # noqa: S603
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"启动浏览器失败：{e}"

    _state["started_at"] = time.time()
    for _ in range(60):  # 最多等 30 秒（首次初始化 profile 较慢）
        if is_running():
            return True, "浏览器已打开，请扫码或使用手机验证码登录"
        time.sleep(0.5)
    return False, "浏览器启动超时，请重试"


def stop() -> None:
    """关闭我们启动的浏览器实例（用进程树，不动用户自己的 Chrome）。"""
    proc = _state.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            subprocess.run(  # noqa: S603
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:  # noqa: BLE001
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
    _state["proc"] = None


def set_account(account: dict) -> None:
    """记录已入库的账号，避免轮询重复建号。"""
    _state["account"] = account


def status() -> dict:
    """检查浏览器登录状态。

    返回 {running, logged_in, nickname, user_id, cookie, account, message}
    cookie 仅在首次检测到已登录时返回一次（由调用方入库后即不再返回）。
    """
    if _state.get("account"):
        return {"running": is_running(), "logged_in": True, "nickname": "", "user_id": "",
                "cookie": "", "account": _state["account"], "message": "登录成功"}

    if not is_running():
        return {"running": False, "logged_in": False, "nickname": "", "user_id": "",
                "cookie": "", "account": None, "message": "浏览器未打开"}

    cookies = read_cookies()
    if not cookies or not cookies.get("web_session"):
        return {"running": True, "logged_in": False, "nickname": "", "user_id": "",
                "cookie": "", "account": None, "message": "等待登录中…"}

    cookie_str = cookies_to_str(cookies)
    import xhs_client  # 延迟导入，避免模块循环

    ok, nickname, uid, err = xhs_client.check_cookie(cookie_str)
    if ok:
        return {"running": True, "logged_in": True, "nickname": nickname, "user_id": uid,
                "cookie": cookie_str, "account": None, "message": "登录成功"}
    return {"running": True, "logged_in": False, "nickname": "", "user_id": "",
            "cookie": "", "account": None, "message": "等待登录中…"}


def cleanup_profile() -> bool:
    """删除浏览器数据目录（用户主动清理登录痕迹时用）。"""
    stop()
    if not os.path.isdir(PROFILE_DIR):
        return True
    import shutil

    try:
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        return not os.path.isdir(PROFILE_DIR)
    except Exception:  # noqa: BLE001
        return False
