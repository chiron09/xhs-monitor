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

服务模式（重要）：
    本平台注册为 Windows 服务（WinSW/XhsAdmin，SYSTEM 账户）后跑在
    Session 0（非交互会话），subprocess.Popen 启动的 Chrome 窗口用户根本
    看不见 —— 症状就是「点了浏览器登录，啥也没发生」。因此当前进程在
    Session 0 时，改走跨会话启动：WTSQueryUserToken 拿活动桌面会话的
    用户令牌 -> CreateEnvironmentBlock 构造用户环境 -> CreateProcessAsUserW
    在用户桌面（winsta0\\default）拉起 Chrome。前台手动运行时保持原 Popen。
"""
import json
import os
import subprocess
import time
import urllib.request

import ctypes
from ctypes import wintypes

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
PIDFILE = os.path.join(DATA_DIR, "browser_login.pid")

_state: dict = {"proc": None, "pid": None, "started_at": 0.0, "account": None}

# ---- 跨会话启动相关常量 ----
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_STARTF_USESHOWWINDOW = 0x00000001
_SW_SHOWNORMAL = 1
_MAXIMUM_ALLOWED = 0x02000000  # pywin32 里没有这个常量，这里用原始值
_SECURITY_IMPERSONATION = 2
_TOKEN_PRIMARY = 1
_WTS_ACTIVE = 0


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


# ================= 会话工具（ctypes 直调 WinAPI，避免 pywin32 签名歧义） =================

class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _WTS_SESSION_INFOW(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.DWORD),
        ("pWinStationName", wintypes.LPWSTR),
        ("State", wintypes.DWORD),
    ]


def _current_session_id() -> int:
    """当前进程所在的会话 ID。"""
    s = wintypes.DWORD(0)
    if ctypes.windll.kernel32.ProcessIdToSessionId(
            ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(s)):
        return s.value
    return -1


def _active_console_session_id() -> int:
    """物理控制台（接显示器那块）的活动会话 ID；无人登录时为 0/0xFFFFFFFF。"""
    try:
        val = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
        return int(val) & 0xFFFFFFFF
    except Exception:  # noqa: BLE001
        return 0


def _active_session_candidates() -> list[int]:
    """按优先级返回可尝试的交互会话：活动的桌面/RDP 会话在前，控制台兜底。

    用 WTSEnumerateSessions 而不是只看控制台，是为了覆盖「用户通过远程
    桌面登录」的情况 —— RDP 会话不是物理控制台会话。
    """
    ids: list[int] = []
    try:
        wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        arr = ctypes.POINTER(_WTS_SESSION_INFOW)()
        count = wintypes.DWORD()
        # hServer=None => 本机
        if wts.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(arr), ctypes.byref(count)):
            try:
                for i in range(count.value):
                    item = arr[i]
                    if item.State == _WTS_ACTIVE and item.SessionId not in ids:
                        ids.append(item.SessionId)
            finally:
                wts.WTSFreeMemory(arr)
    except Exception:  # noqa: BLE001
        pass
    console = _active_console_session_id()
    if console not in (0, 0xFFFFFFFF) and console not in ids:
        ids.append(console)
    return ids


def _launch_in_user_session(cmdline: str) -> int:
    """在活动桌面会话里拉起进程，返回新进程 PID。失败抛 OSError（含步骤前缀）。

    标准链路：WTSQueryUserToken（需 SYSTEM 的 SE_TCB 特权）->
    CreateEnvironmentBlock -> CreateProcessAsUserW(lpDesktop=winsta0\\default)。
    WTSQueryUserToken 返回的就是 primary token，可直接用于 CreateProcessAsUser；
    个别环境需要复制后使用，失败时用 DuplicateTokenEx 复制一份重试一次。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    wtsapi32.WTSQueryUserToken.argtypes = [wintypes.ULONG, ctypes.POINTER(wintypes.HANDLE)]
    userenv.CreateEnvironmentBlock.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL]
    userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    advapi32.DuplicateTokenEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_int, ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
        ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION)]

    def _fail(step: str) -> OSError:
        return OSError(f"[{step}] {ctypes.FormatError(ctypes.get_last_error())}")

    h_user = wintypes.HANDLE()
    got_token = False
    last_err = ""
    for session in _active_session_candidates():
        if wtsapi32.WTSQueryUserToken(session, ctypes.byref(h_user)):
            got_token = True
            break
        last_err = f"会话 {session}: {ctypes.FormatError(ctypes.get_last_error())}"
    if not got_token:
        raise OSError(
            f"[WTSQueryUserToken] 未能在任何交互会话取得用户令牌（{last_err}）。"
            "请确认已登录 Windows 桌面（远程桌面会话也可），且平台以系统服务运行。")

    env_ptr = ctypes.c_void_p()
    have_env = bool(userenv.CreateEnvironmentBlock(ctypes.byref(env_ptr), h_user, False))
    if not have_env:
        # 拿不到用户环境块就退化为继承服务环境（Chrome 主要读系统代理设置，影响不大）
        env_ptr = ctypes.c_void_p()

    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = "winsta0\\default"  # 必须显式指向用户桌面的默认桌面
    si.dwFlags = _STARTF_USESHOWWINDOW
    si.wShowWindow = _SW_SHOWNORMAL
    pi = _PROCESS_INFORMATION()
    cmd_buf = ctypes.create_unicode_buffer(cmdline)

    def _do_create(token) -> bool:
        return bool(advapi32.CreateProcessAsUserW(
            token, None, cmd_buf, None, None, False,
            _CREATE_UNICODE_ENVIRONMENT if have_env else 0,
            env_ptr if have_env else None, None,
            ctypes.byref(si), ctypes.byref(pi)))

    try:
        if not _do_create(h_user):
            err1 = ctypes.get_last_error()
            # 兜底：复制一份 primary token 再试
            h_dup = wintypes.HANDLE()
            if advapi32.DuplicateTokenEx(
                    h_user, _MAXIMUM_ALLOWED, None,
                    _SECURITY_IMPERSONATION, _TOKEN_PRIMARY, ctypes.byref(h_dup)):
                try:
                    if not _do_create(h_dup):
                        err1 = ctypes.get_last_error()
                    else:
                        return pi.dwProcessId
                finally:
                    kernel32.CloseHandle(h_dup)
            raise OSError(f"[CreateProcessAsUser] {ctypes.FormatError(err1)}")
        return pi.dwProcessId
    finally:
        if have_env and env_ptr.value:
            userenv.DestroyEnvironmentBlock(env_ptr)
        kernel32.CloseHandle(h_user)


# ================= PID 记录（跨服务重启也能找到并关闭我们的 Chrome） =================

def _write_pid(pid: int) -> None:
    try:
        with open(PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(pid))
    except OSError:
        pass


def _read_pid() -> int | None:
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:  # noqa: BLE001
        return None


def _pid_is_chrome(pid: int) -> bool:
    """确认该 PID 确实是 chrome.exe（防止 pid 被系统复用后误杀无辜进程）。"""
    try:
        out = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, check=False)
        line = (out.stdout or "").strip()
        return bool(line) and "chrome.exe" in line.lower()
    except Exception:  # noqa: BLE001
        return False


def _kill_tree(pid: int) -> None:
    subprocess.run(  # noqa: S603
        ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


# ================= 登录浏览器主逻辑 =================

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
        if _state.get("pid") is None:
            pid = _read_pid()
            if pid and _pid_is_chrome(pid):
                _state["pid"] = pid
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

    cur_session = _current_session_id()
    if cur_session > 0:
        # 前台手动运行（当前进程就在用户会话里）→ 普通启动即可
        try:
            _state["proc"] = subprocess.Popen(  # noqa: S603
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            _state["pid"] = _state["proc"].pid
        except Exception as e:  # noqa: BLE001
            return False, f"启动浏览器失败：{e}"
    else:
        # 服务模式（Session 0，窗口不可见）→ 跨会话拉到用户桌面
        _state["proc"] = None
        try:
            _state["pid"] = _launch_in_user_session(subprocess.list2cmdline(args))
        except OSError as e:
            return False, f"启动浏览器失败：{e}"
        except Exception as e:  # noqa: BLE001
            return False, f"启动浏览器失败（跨会话）：{e}"

    _write_pid(_state["pid"])
    _state["started_at"] = time.time()
    for _ in range(60):  # 最多等 30 秒（首次初始化 profile 较慢）
        if is_running():
            return True, "浏览器已打开，请扫码或使用手机验证码登录"
        time.sleep(0.5)
    return False, "浏览器启动超时，请重试"


def stop() -> None:
    """关闭我们启动的浏览器实例（用进程树，不动用户自己的 Chrome）。"""
    candidates: list[int] = []
    proc = _state.get("proc")
    if proc is not None and proc.poll() is None:
        candidates.append(proc.pid)
    for pid in (_state.get("pid"), _read_pid()):
        if pid and pid not in candidates:
            candidates.append(pid)
    for pid in candidates:
        if _pid_is_chrome(pid):
            _kill_tree(pid)
    _state["proc"] = None
    _state["pid"] = None
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


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
    # 已检测到登录，但本地校验失败：如实反馈（不能谎报"等待登录中…"，
    # 否则用户已登录成功却看到页面毫无进展）。
    return {"running": True, "logged_in": False, "nickname": "", "user_id": "",
            "cookie": "", "account": None,
            "message": "已检测到浏览器登录，但校验未通过：" + (err or "未知原因")}


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
