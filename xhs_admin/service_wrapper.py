# -*- coding: utf-8 -*-
"""XHS Admin 的 Windows 系统服务包装（PyWin32）。

把 xhs_admin (uvicorn, http://127.0.0.1:8000) 注册为 Windows 系统服务：
不登录 Windows 也会运行；崩溃自动重启；随服务停止而终止。

用法（管理员）:
    python service_wrapper.py install    # 注册服务（开机自动启动）
    python service_wrapper.py start      # 启动服务
    python service_wrapper.py stop       # 停止服务
    python service_wrapper.py restart    # 重启服务
    python service_wrapper.py remove     # 卸载服务
    python service_wrapper.py debug      # 前台调试（不走 SCM，Ctrl+C 退出）
"""
import os
import subprocess
import sys
import time

import servicemanager
import win32event
import win32service
import win32serviceutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = r"C:\Workbuddy\tools\_xhs_admin_service.log"
CRASH_RESTART_DELAY = 5  # 秒

# 注意：SCM 启动模式下 sys.executable 是 pythonservice.exe（不是 python.exe），
# 因此这里必须显式指定 python 解释器，不能用 sys.executable 派生。
_PYTHON = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not os.path.isfile(_PYTHON):
    _PYTHON = sys.executable if not sys.executable.lower().endswith("pythonservice.exe") else "python.exe"


class XhsAdminService(win32serviceutil.ServiceFramework):
    _svc_name_ = "XhsAdmin"
    _svc_display_name_ = "XHS Admin (Xiaohongshu Monitor Backend)"
    _svc_description_ = "Xiaohongshu blogger monitor backend at http://127.0.0.1:8000"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.proc = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        try:
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            self._run_forever()
        except Exception as e:  # noqa: BLE001
            try:
                servicemanager.LogErrorMsg(f"XhsAdmin service crashed: {e}")
            except Exception:  # noqa: BLE001
                pass

    def _env(self):
        env = dict(os.environ)
        env.update({
            "NO_PROXY": "*",
            "PYTHONUTF8": "1",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
        })
        return env

    def _run_forever(self):
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        while True:
            log = open(LOG_PATH, "ab")
            log.write(f"\n=== service start uvicorn @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
            log.flush()
            proc = subprocess.Popen(  # noqa: S603
                [_PYTHON, "-m", "uvicorn", "app:app",
                 "--host", "127.0.0.1", "--port", "8000"],
                cwd=BASE_DIR,
                env=self._env(),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            log.close()
            self.proc = proc
            # 每 5 秒检查一次：停止信号 or 子进程意外退出
            stop_fired = False
            while True:
                rc = win32event.WaitForSingleObject(self.stop_event, 5000)
                if rc == win32event.WAIT_OBJECT_0:
                    stop_fired = True
                    break
                if proc.poll() is not None:
                    break
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    proc.kill()
            if stop_fired:
                break
            # 崩溃自动重启
            time.sleep(CRASH_RESTART_DELAY)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # SCM 启动入口（由 pythonservice.exe 调起）
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(XhsAdminService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(XhsAdminService)
