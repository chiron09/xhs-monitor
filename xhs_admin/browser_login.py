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
import base64
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

# 内嵌模式（默认开）：Chrome 无头运行，画面经 CDP 截图流式投到网页上，
# 点击/滚动/文字输入经 CDP 注回。环境变量 XHS_LOGIN_HEADLESS=0 可退回
# 「弹本地窗口」的旧行为（服务模式下走跨会话启动）。
HEADLESS = os.environ.get("XHS_LOGIN_HEADLESS", "1") != "0"
# 视口越大，登录页里二维码/输入框渲染得越大，投到网页上才够扫
VIEWPORT = (int(os.environ.get("XHS_LOGIN_VW", "1600")),
            int(os.environ.get("XHS_LOGIN_VH", "1000")))
# 截图像素密度：CSS 尺寸不变（坐标换算照旧），但图片像素翻 N 倍，
# 前端把画面缩着看时二维码才不会糊（XHS_LOGIN_DSF=1 可关掉）
# 注意：dsf 太高（2.0 = 3150x1700）会让前端每帧解码大图，把渲染主线程压得
# 连页面脚本都跑不动；1.5 倍（2400x1500）清晰度与流畅度平衡最好。
SHOT_DSF = float(os.environ.get("XHS_LOGIN_DSF", "1.5"))
SHOT_QUALITY = int(os.environ.get("XHS_LOGIN_JPEG_Q", "58"))

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


FALLBACK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
UA_CACHE = os.path.join(DATA_DIR, "chrome_ua.txt")

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
if (!window.chrome) {
  window.chrome = {runtime: {}, app: {isInstalled: false}, csi: function(){}, loadTimes: function(){}};
}
if (navigator.permissions && navigator.permissions.query) {
  const _q = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _q(p));
}
try { delete window.__nightmare; } catch (e) {}
"""

RISK_MARKERS = ("website-login/error", "error_code=300012", "captcha", "verify")


def _load_cached_ua() -> str:
    try:
        with open(UA_CACHE, encoding="utf-8") as f:
            ua = f.read().strip()
        if ua and "HeadlessChrome" not in ua:
            return ua
    except Exception:  # noqa: BLE001
        pass
    return ""


def _save_cached_ua(ua: str) -> None:
    try:
        with open(UA_CACHE, "w", encoding="utf-8") as f:
            f.write(ua)
    except Exception:  # noqa: BLE001
        pass


def _headless_ua() -> str:
    """读取无头实例的真实 UA 并把 HeadlessChrome 换成正常 Chrome。

    小红书按 UA 识别无头浏览器并弹 300012「安全限制/IP 存在风险」页，
    必须用正常 Chrome UA 才能进登录页。
    """
    try:
        v = _http_json(_debug_url("/json/version"), timeout=3.0)
        ua = str(v.get("User-Agent") or "")
        if "HeadlessChrome/" in ua:
            return ua.replace("HeadlessChrome/", "Chrome/")
        return ua or FALLBACK_UA
    except Exception:  # noqa: BLE001
        return FALLBACK_UA


def _probe_ua(chrome: str) -> str:
    """用一次性实例探测本机 Chrome 的正常 UA（把 HeadlessChrome 换成 Chrome）。

    Chrome 只有在真正跑起来后 /json/version 才报出带版本号的 UA，
    所以先空跑一次拿到 UA，再用 --user-agent 正式启动 —— 命令行层面的
    UA 是全局生效的，比 CDP 会话级 setUserAgentOverride 可靠（后者在
    ws 断开后就不作数了）。
    """
    ua = ""
    proc = None
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    args = [
        chrome,
        f"--remote-debugging-port={DEBUG_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--headless=new",
        "about:blank",
    ]
    try:
        proc = subprocess.Popen(  # noqa: S603
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        for _ in range(40):  # 最多 20 秒
            time.sleep(0.5)
            try:
                v = _http_json(_debug_url("/json/version"), timeout=2.0)
                raw = str(v.get("User-Agent") or "")
                if raw:
                    ua = raw.replace("HeadlessChrome/", "Chrome/")
                    break
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    finally:
        if proc is not None:
            try:
                subprocess.run(  # noqa: S603
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            except Exception:  # noqa: BLE001
                pass
    return ua


def _wipe_profile() -> None:
    """清空登录用浏览器 profile（去掉上一次被风控打标的 cookie/本地存储）。"""
    import shutil

    try:
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def start():
    """启动（或复用）登录用浏览器。返回 (ok, message)。"""
    if is_running():
        if _state.get("pid") is None:
            pid = _read_pid()
            if pid and _pid_is_chrome(pid):
                _state["pid"] = pid
        if HEADLESS:
            return True, "登录页已就绪，请在页面内直接操作"
        return True, "浏览器窗口已打开，请在其中完成登录"
    chrome = _chrome_path()
    if not chrome:
        return False, "未找到 Chrome，请先安装 Google Chrome"
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return False, f"创建浏览器数据目录失败：{e}"

    _state["account"] = None
    if HEADLESS:
        return _start_headless(chrome)

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


def _launch_headless(chrome: str, ua: str) -> tuple[bool, str]:
    """带正常 UA 启动无头实例并等待调试端口就绪。"""
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return False, f"创建浏览器数据目录失败：{e}"
    args = [
        chrome,
        f"--remote-debugging-port={DEBUG_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        # 无头内嵌：服务里直接跑（Session 0 无妨），画面走 CDP 投到网页
        "--headless=new",
        f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}",
        "--hide-scrollbars",
        "--mute-audio",
        # 去自动化特征（小红书会查 navigator.webdriver / UA）
        "--disable-blink-features=AutomationControlled",
        # 关键：命令行级 UA。无头默认 UA 带 HeadlessChrome/ 会被判风控
        f"--user-agent={ua}",
        "--lang=zh-CN",
        "--accept-lang=zh-CN,zh;q=0.9",
        LOGIN_URL,
    ]
    try:
        _state["proc"] = subprocess.Popen(  # noqa: S603
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        _state["pid"] = _state["proc"].pid
    except Exception as e:  # noqa: BLE001
        return False, f"启动浏览器失败：{e}"
    _write_pid(_state["pid"])
    _state["started_at"] = time.time()
    for _ in range(60):  # 最多 30 秒
        if is_running():
            return True, ""
        time.sleep(0.5)
    return False, "浏览器启动超时，请重试"


def _current_page_url() -> str:
    target = _pick_page_target()
    return str((target or {}).get("url") or "")


def _wait_settled(timeout: float = 8.0) -> str:
    """等待登录页导航落地，返回最终 URL（用于判断是否被风控页拦下）。"""
    deadline = time.time() + timeout
    url = ""
    while time.time() < deadline:
        cur = _current_page_url()
        if cur:
            url = cur
            if any(m in url for m in RISK_MARKERS) or "xiaohongshu.com/login" in url:
                return url
        time.sleep(0.5)
    return url


def _start_headless(chrome: str) -> tuple[bool, str]:
    """内嵌模式启动：先探 UA -> 正式启动 -> 撞风控页则换干净身份重试一次。"""
    ua = _load_cached_ua()
    if not ua:
        ua = _probe_ua(chrome) or FALLBACK_UA
        _save_cached_ua(ua)
        time.sleep(1.0)  # 等探测实例彻底退出、端口释放

    ok, msg = _launch_headless(chrome, ua)
    if not ok:
        return False, msg
    url = _wait_settled()

    if any(m in url for m in RISK_MARKERS):
        # 上一次留下来的风控 cookie/指纹把这次也带偏了 —— 换干净身份重来
        stop()
        time.sleep(1.0)
        _wipe_profile()
        ok, msg = _launch_headless(chrome, ua)
        if not ok:
            return False, msg
        url = _wait_settled()

    _apply_headless_stealth()
    if any(m in url for m in RISK_MARKERS):
        return True, ("小红书风控拦截（页面提示 IP/环境存在风险）。已尝试换干净身份重试，"
                      "若仍如此，说明当前网络出口被小红书标记，建议改用「Cookie 导入」方式。")
    return True, "登录页已就绪，请在页面内直接操作"


def _apply_headless_stealth() -> None:
    """无头模式反检测：UA/语言头 + 抹掉 navigator.webdriver 等自动化特征。

    注意必须在「页面级」ws 上做 —— 浏览器级 ws 上 Page.* 不生效。
    UA 本身已由命令行 --user-agent 全局设定，这里只补请求头和脚本。
    """
    ws_url = _page_ws_url()
    if not ws_url:
        return
    ua = _load_cached_ua() or FALLBACK_UA
    ws = None
    try:
        ws = _cdp_connect(ws_url)
        try:
            _cdp(ws, "Network.enable", msg_id=5)
            _cdp(ws, "Network.setUserAgentOverride", {
                "userAgent": ua,
                "acceptLanguage": "zh-CN,zh;q=0.9",
                "platform": "Win32",
            }, msg_id=6)
        except Exception:  # noqa: BLE001
            pass
        try:
            _cdp(ws, "Page.enable", msg_id=7)
            _cdp(ws, "Page.addScriptToEvaluateOnNewDocument",
                 {"source": STEALTH_SCRIPT}, msg_id=8)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        return
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
    # 反检测脚本只对之后的导航生效，重新导航一次让登录页带上干净指纹
    _reload_login_page()
    time.sleep(1.5)


def _browser_ws_url() -> str | None:
    try:
        v = _http_json(_debug_url("/json/version"), timeout=3.0)
        return v.get("webSocketDebuggerUrl")
    except Exception:  # noqa: BLE001
        return None


def _reload_login_page() -> None:
    ws_url = _page_ws_url()
    if not ws_url:
        return
    ws = None
    try:
        ws = _cdp_connect(ws_url)
        _cdp(ws, "Page.enable", msg_id=1)
        _cdp(ws, "Page.navigate", {"url": LOGIN_URL}, msg_id=2)
    except Exception:  # noqa: BLE001
        pass
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass


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


# ================= 无头内嵌：画面截图 + 输入注入 =================

def _page_ws_url() -> str | None:
    target = _pick_page_target()
    if not target:
        return None
    return target.get("webSocketDebuggerUrl")


def snapshot() -> tuple[bool, str, int, int]:
    """抓取当前登录页画面。返回 (ok, data_url, width, height)。

    width/height 是 CSS 逻辑尺寸（前端鼠标坐标换算用），图片本身按
    SHOT_DSF 倍分辨率编码，缩小显示时二维码依然清晰。
    """
    if not is_running():
        return False, "", 0, 0
    ws_url = _page_ws_url()
    if not ws_url:
        return False, "", 0, 0
    ws = None
    try:
        ws = _cdp_connect(ws_url)
        _cdp(ws, "Page.enable")
        metrics = _cdp(ws, "Page.getLayoutMetrics", msg_id=2)
        css = (metrics.get("result") or {}).get("cssContentSize") or {}
        cw = int(css.get("width") or VIEWPORT[0])
        ch = int(css.get("height") or VIEWPORT[1])
        # 高分截图走 clip.scale：不动页面布局（坐标换算照旧），
        # 比 Emulation.setDeviceMetricsOverride 稳（后者首次调用会把无头合成器卡死）
        capture = {"format": "jpeg", "quality": SHOT_QUALITY,
                   "captureBeyondViewport": False}
        if SHOT_DSF > 1:
            capture["clip"] = {"x": 0, "y": 0, "width": cw, "height": ch,
                               "scale": SHOT_DSF}
        shot = _cdp(ws, "Page.captureScreenshot", capture, msg_id=3)
        data = (shot.get("result") or {}).get("data") or ""
        if not data:
            # 截图失败时把 CDP 错误带出来，便于诊断
            err = shot.get("error") or {}
            raise RuntimeError(f"captureScreenshot 失败: {err.get('message') or err}")
        return (True, "data:image/jpeg;base64," + data, cw, ch)
    except Exception as e:  # noqa: BLE001
        _state["frame_error"] = str(e)
        return False, "", 0, 0
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass


def _locate_qrcode() -> dict:
    """在页面里找最大的一张可见二维码图（canvas/img），返回视口坐标矩形。"""
    js = (
        "(() => {"
        "  const vw = window.innerWidth || document.documentElement.clientWidth;"
        "  const vh = window.innerHeight || document.documentElement.clientHeight;"
        "  const list = [...document.querySelectorAll('canvas, img')].filter(el => {"
        "    const r = el.getBoundingClientRect();"
        "    const visible = r.width > 80 && r.height > 80 && r.top < vh && r.bottom > 0 && r.left < vw && r.right > 0;"
        "    if (!visible) return false;"
        "    const cs = getComputedStyle(el);"
        "    return cs.display !== 'none' && cs.visibility !== 'hidden';"
        "  });"
        "  if (!list.length) return {ok: false, reason: '页面上没有找到二维码图片'};"
        "  let best = null, bestArea = 0;"
        "  for (const el of list) {"
        "    const r = el.getBoundingClientRect();"
        "    const area = r.width * r.height;"
        "    if (area > bestArea) { best = {r: r, tag: el.tagName}; bestArea = area; }"
        "  }"
        "  const r = best.r;"
        "  return {ok: true, tag: best.tag,"
        "          x: Math.max(0, Math.round(r.left + window.scrollX)),"
        "          y: Math.max(0, Math.round(r.top + window.scrollY)),"
        "          w: Math.round(r.width), h: Math.round(r.height)};"
        "})()"
    )
    ws = _cdp_connect(_page_ws_url())
    try:
        _cdp(ws, "Page.enable")
        res = _cdp(ws, "Runtime.evaluate",
                   {"expression": js, "returnByValue": True}, msg_id=5)
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    value = (((res or {}).get("result") or {}).get("result") or {}).get("value") or {}
    return value if isinstance(value, dict) else {}


def qrcode_snapshot() -> tuple[bool, str, int, int, str]:
    """只截页面里的二维码（PNG 无损 + 2 倍分辨率），专门用来给人眼/手机扫。

    返回 (ok, data_url, width, height, message)。
    """
    if not is_running():
        return False, "", 0, 0, "浏览器未运行"
    info = _locate_qrcode()
    if not info.get("ok"):
        return False, "", 0, 0, info.get("reason") or "页面上没有找到二维码图片"
    # 四周留一点边距，别把二维码贴边裁掉
    margin = 8
    clip = {
        "x": max(0, info["x"] - margin),
        "y": max(0, info["y"] - margin),
        "width": max(1, info["w"] + margin * 2),
        "height": max(1, info["h"] + margin * 2),
        "scale": 2,
    }
    ws = None
    try:
        ws = _cdp_connect(_page_ws_url())
        _cdp(ws, "Page.enable")
        shot = _cdp(ws, "Page.captureScreenshot", {
            "format": "png", "clip": clip, "captureBeyondViewport": True,
            "optimizeForSpeed": False}, msg_id=6)
        data = (shot.get("result") or {}).get("data") or ""
        if not data:
            err = shot.get("error") or {}
            raise RuntimeError(f"captureScreenshot(clip) 失败: {err.get('message') or err}")
        return (True, "data:image/png;base64," + data, clip["width"], clip["height"], "")
    except Exception as e:  # noqa: BLE001
        return False, "", 0, 0, str(e)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass


def _mouse_button_cdp(button: str) -> str:
    return {"left": "left", "middle": "middle", "right": "right"}.get(button, "left")


def inject_input(events: list) -> dict:
    """把网页上采集的输入事件经 CDP 注入无头浏览器。

    事件类型：
      {type:'mouse', action:'pressed'|'released'|'moved', x, y, button}
      {type:'wheel', x, y, dx, dy}
      {type:'key', code, key, text, modifiers}
      {type:'char', text}
    """
    if not is_running():
        return {"ok": False, "error": "浏览器未运行"}
    ws_url = _page_ws_url()
    if not ws_url:
        return {"ok": False, "error": "登录页不可用"}
    sent = 0
    ws = None
    try:
        ws = _cdp_connect(ws_url)
        mid = 10
        for ev in events:
            try:
                if ev.get("type") == "mouse":
                    x = max(0, int(ev.get("x") or 0))
                    y = max(0, int(ev.get("y") or 0))
                    btn = _mouse_button_cdp(str(ev.get("button") or "left"))
                    action = ev.get("action")
                    if action == "moved":
                        params = {"type": "mouseMoved", "x": x, "y": y, "button": btn}
                    else:
                        params = {"type": "mousePressed" if action == "pressed" else "mouseReleased",
                                  "x": x, "y": y, "button": btn,
                                  "clickCount": int(ev.get("clickCount") or 1)}
                    _cdp(ws, "Input.dispatchMouseEvent", params, msg_id=mid)
                elif ev.get("type") == "wheel":
                    _cdp(ws, "Input.dispatchMouseEvent", {
                        "type": "mouseWheel", "x": int(ev.get("x") or 0), "y": int(ev.get("y") or 0),
                        "deltaX": int(ev.get("dx") or 0), "deltaY": int(ev.get("dy") or 0)}, msg_id=mid)
                elif ev.get("type") == "key":
                    # 修饰键/控制键：down=True 发 keyDown，False 发 keyUp
                    mods = int(ev.get("modifiers") or 0)
                    text = ev.get("text") or ""
                    is_down = bool(ev.get("down", True))
                    params = {"type": "keyDown" if is_down else "keyUp", "modifiers": mods,
                              "windowsVirtualKeyCode": int(ev.get("keyCode") or 0),
                              "code": ev.get("code") or "", "key": ev.get("key") or ""}
                    if text and is_down:
                        params["text"] = text
                    _cdp(ws, "Input.dispatchKeyEvent", params, msg_id=mid)
                elif ev.get("type") == "char":
                    _cdp(ws, "Input.dispatchKeyEvent", {
                        "type": "char", "text": str(ev.get("text") or "")}, msg_id=mid)
                else:
                    continue
                sent += 1
            except Exception:  # noqa: BLE001
                continue
            mid += 1
        return {"ok": sent > 0, "sent": sent}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass


def set_account(account: dict) -> None:
    """记录已入库的账号，避免轮询重复建号。"""
    _state["account"] = account


def status() -> dict:
    """轻量状态查询（不读 cookie、不触发校验）。

    只回答「登录浏览器是否还活着」，供前端轮询刷新按钮状态。
    真正的登录态校验由 check() 在用户点「完成登录」时手动触发。
    """
    if _state.get("account"):
        return {"running": is_running(), "message": "登录成功"}
    if not is_running():
        return {"running": False, "message": "浏览器未打开"}
    return {"running": True, "message": "登录页已就绪，登录完成后点「完成登录」按钮"}


def check() -> dict:
    """手动触发登录态校验。返回完整结果（含 cookie 供入库）。

    返回 {running, logged_in, nickname, user_id, cookie, account, message}

    这里只做「是否已检测到登录 cookie」的轻判断（不跑 SDK 签名校验），
    真正的校验 + 入库统一由 app.py 的 _persist_account 完成——避免 check()
    和 _persist_account 各跑一次 check_cookie（双重校验），既拖慢又可能
    两次结果不一致导致「登录成功但入库失败」的假象。
    """
    if _state.get("account"):
        return {"running": is_running(), "logged_in": True, "nickname": "", "user_id": "",
                "cookie": "", "account": _state["account"], "message": "登录成功"}

    if not is_running():
        return {"running": False, "logged_in": False, "nickname": "", "user_id": "",
                "cookie": "", "account": None, "message": "浏览器未打开"}

    # 手机上点完「确认登录」后，正式 web_session 落地有一小段窗口；
    # 立刻取可能读不到，给几次机会（每次间隔 1 秒），否则用户会以为「显示登录成功却没反应」
    cookies = {}
    ws = ""
    for _i in range(3):
        cookies = read_cookies()
        ws = cookies.get("web_session") or ""
        if ws:
            break
        time.sleep(1.0)
    if not cookies or not ws:
        return {"running": True, "logged_in": False, "nickname": "", "user_id": "",
                "cookie": "", "account": None,
                "message": "未检测到登录，请先扫码或输入验证码，再点「完成登录」"}

    # 已检测到 web_session cookie，交给 app.py 校验入库（含游客态判定）
    return {"running": True, "logged_in": True, "nickname": "", "user_id": "",
            "cookie": cookies_to_str(cookies), "account": None,
            "message": "已检测到登录，正在校验登录态…"}


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
