"""守护进程：检测到战争雷霆进程（aces.exe / aces-min-cpu.exe）时自动启动翻译器。

用法：
    python guardian.py              以守护模式运行（前台）
    python guardian.py --stop       停止正在运行的守护进程
    python guardian.py --config X   使用指定配置文件
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
import threading

if os.name == "nt":
    from ctypes import wintypes
else:
    class _Wintypes:
        DWORD = ctypes.c_ulong
        BOOL = ctypes.c_int
        HANDLE = ctypes.c_void_p
        LPCWSTR = ctypes.c_wchar_p

    wintypes = _Wintypes()

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_GAME_PROCESSES = ["aces.exe", "aces-min-cpu.exe"]
MAC_GAME_PROCESSES = ["aces", "aces-min-cpu", "War Thunder", "warthunder"]
TRANSLATOR_EXE = "WTTranslator.exe"

MUTEX_NAME = "WT_Translator_Guardian_Mutex"
STOP_EVENT_NAME = "WT_Translator_Guardian_Stop"

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102
EVENT_MODIFY_STATE = 0x0002
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def running_process_names() -> set:
    """返回当前所有进程名（小写）集合，Windows 下用 Toolhelp32 快照。"""
    if os.name != "nt":
        try:
            result = subprocess.run(
                ["ps", "-A", "-o", "comm="],
                capture_output=True, text=True, timeout=5,
            )
            names = set()
            for line in result.stdout.splitlines():
                name = os.path.basename(line.strip()).lower()
                if name:
                    names.add(name)
            return names
        except (OSError, subprocess.SubprocessError):
            return set()
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return set()
    names = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            names.add(entry.szExeFile.lower())
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return names


def any_running(names) -> bool:
    wanted = {str(name).lower() for name in names}
    return bool(wanted & running_process_names())


def acquire_single_instance():
    """单实例互斥体；已存在其他守护进程时返回 None。"""
    if os.name != "nt":
        import fcntl

        path = os.path.join(APP_DIR, ".guardian.lock")
        handle = open(path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        return handle
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        if handle:
            kernel32.CloseHandle(handle)
        return None
    return handle


def create_stop_event():
    if os.name != "nt":
        return threading.Event()
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    return kernel32.CreateEventW(None, False, False, STOP_EVENT_NAME)


def signal_stop() -> bool:
    if os.name != "nt":
        for pattern in ("[W]TGuardian", "[g]uardian.py"):
            try:
                result = subprocess.run(
                    ["pkill", "-f", pattern],
                    capture_output=True,
                )
            except OSError:
                continue
            if result.returncode == 0:
                return True
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, STOP_EVENT_NAME)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def wait_stop(handle, timeout_ms):
    """等待停止事件，返回 True 表示应退出。"""
    if os.name != "nt":
        return handle.wait(max(0.01, timeout_ms / 1000.0))
    kernel32 = ctypes.windll.kernel32
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    return kernel32.WaitForSingleObject(handle, timeout_ms) == WAIT_OBJECT_0


def guardian_command():
    """返回守护进程自身的启动命令（列表）。"""
    if getattr(sys, "frozen", False):
        names = ["WTGuardian.exe"] if os.name == "nt" else ["WTGuardian"]
        for name in names:
            path = os.path.join(APP_DIR, name)
            if os.path.isfile(path):
                return [path]
        return None
    return [sys.executable, os.path.join(APP_DIR, "guardian.py")]


def launch_guardian() -> bool:
    """启动守护进程（已运行时会自行退出，不影响）。"""
    cmd = guardian_command()
    if not cmd:
        return False
    try:
        subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
        return True
    except OSError:
        return False


def stop_guardian() -> bool:
    """结束所有守护进程实例。"""
    if os.name != "nt":
        ok = False
        for pattern in ("[W]TGuardian", "[g]uardian.py"):
            try:
                result = subprocess.run(["pkill", "-f", pattern], capture_output=True)
            except OSError:
                continue
            ok = ok or result.returncode == 0
        return ok
    try:
        result = subprocess.run(
            ["taskkill", "/IM", "WTGuardian.exe", "/F"],
            creationflags=CREATE_NO_WINDOW,
            capture_output=True,
        )
        return result.returncode == 0
    except OSError:
        return False


def _run_key_value(cmd):
    if os.name != "nt" or not cmd:
        return None
    import subprocess as _sp

    return _sp.list2cmdline(cmd)


def set_autostart(enabled: bool) -> None:
    """设置/取消守护进程开机自启（当前用户注册表 Run 项）。"""
    if os.name != "nt":
        if sys.platform != "darwin":
            return
        import plistlib

        label = "com.wttranslator.guardian"
        agents = os.path.expanduser("~/Library/LaunchAgents")
        path = os.path.join(agents, label + ".plist")
        if enabled:
            cmd = guardian_command()
            if not cmd:
                return
            os.makedirs(agents, exist_ok=True)
            payload = {
                "Label": label,
                "ProgramArguments": cmd,
                "RunAtLoad": True,
            }
            with open(path, "wb") as fh:
                plistlib.dump(payload, fh)
            subprocess.run(["launchctl", "load", "-w", path], capture_output=True)
        else:
            try:
                subprocess.run(["launchctl", "unload", "-w", path], capture_output=True)
            except OSError:
                pass
            try:
                os.remove(path)
            except OSError:
                pass
        return
    import winreg

    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    value_name = "WTTranslatorGuardian"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE)
    except OSError:
        return
    try:
        if enabled:
            cmd = guardian_command()
            command = _run_key_value(cmd)
            if command:
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, value_name)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


def is_autostart_enabled() -> bool:
    if os.name != "nt":
        if sys.platform == "darwin":
            path = os.path.expanduser(
                "~/Library/LaunchAgents/com.wttranslator.guardian.plist"
            )
            return os.path.isfile(path)
        return False
    import winreg

    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_READ)
    except OSError:
        return False
    try:
        winreg.QueryValueEx(key, "WTTranslatorGuardian")
        return True
    except FileNotFoundError:
        return False
    finally:
        winreg.CloseKey(key)


def translator_command():
    if getattr(sys, "frozen", False):
        names = [TRANSLATOR_EXE] if os.name == "nt" else ["WTTranslator"]
        for name in names:
            path = os.path.join(APP_DIR, name)
            if os.path.isfile(path):
                return [path]
        return None
    return [sys.executable, os.path.join(APP_DIR, "main.py")]


def start_translator(log):
    cmd = translator_command()
    if not cmd:
        log.error("未找到翻译器可执行文件")
        return None
    log.info("启动翻译器：%s", " ".join(cmd))
    try:
        return subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
    except OSError as exc:
        log.error("启动翻译器失败：%s", exc)
        return None


def stop_translator(child, log):
    """尽量优雅地关闭由本守护启动的翻译器。"""
    if child is None or child.poll() is not None:
        return
    log.info("关闭翻译器…")
    if sys.platform == "darwin":
        try:
            pattern = "WTTranslator" if getattr(sys, "frozen", False) else "main.py"
            subprocess.run(["pkill", "-f", pattern], capture_output=True)
            child.wait(timeout=20)
        except (subprocess.TimeoutExpired, OSError):
            child.terminate()
    elif getattr(sys, "frozen", False):
        try:
            subprocess.run(
                ["taskkill", "/IM", TRANSLATOR_EXE],
                creationflags=CREATE_NO_WINDOW,
                capture_output=True,
            )
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.terminate()
        except OSError as exc:
            log.error("关闭翻译器失败：%s", exc)
            child.terminate()
    else:
        child.terminate()


def setup_logging(log_file=None):
    log_file = log_file or os.path.join(APP_DIR, "guardian.log")
    handlers = [logging.FileHandler(log_file, mode="w", encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("guardian")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="战争雷霆翻译器守护进程")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--interval", type=float, default=None, help="轮询间隔（秒）")
    parser.add_argument("--stop", action="store_true", help="停止正在运行的守护进程")
    parser.add_argument("--log", default=None, help="日志文件路径")
    args = parser.parse_args(argv)

    if args.stop:
        if signal_stop():
            print("已发送停止信号")
            return 0
        print("没有检测到正在运行的守护进程")
        return 1

    if os.name != "nt" and sys.platform != "darwin":
        print("守护进程仅支持 Windows / macOS")
        return 1

    from wt_translator.config import Config

    cfg = Config(args.config) if args.config else Config()
    log = setup_logging(args.log)

    if acquire_single_instance() is None:
        log.info("已有守护进程在运行，本次退出")
        return 0

    game_processes = list(cfg.get("guardian_game_processes", DEFAULT_GAME_PROCESSES)) or DEFAULT_GAME_PROCESSES
    if sys.platform == "darwin":
        for name in MAC_GAME_PROCESSES:
            if name not in game_processes:
                game_processes.append(name)
    try:
        interval = args.interval if args.interval is not None else float(cfg.get("guardian_poll_interval", 2.0))
        interval = max(0.2, interval)
    except (TypeError, ValueError):
        interval = 2.0
    stop_on_exit = bool(cfg.get("guardian_stop_on_game_exit", True))

    log.info("守护进程启动，轮询间隔 %.1fs，监控进程：%s", interval, ", ".join(game_processes))
    stop_event = create_stop_event()
    child = None
    game_was_running = any_running(game_processes)
    session_stopped = False   # 本次游戏运行期间翻译器被退出/关闭过
    translator_seen = False   # 本次游戏运行期间翻译器是否启动过

    while True:
        if wait_stop(stop_event, int(interval * 1000)):
            log.info("收到停止信号，守护进程退出")
            break
        game_running = any_running(game_processes)
        if game_running and not game_was_running:
            log.info("检测到游戏进程，开始新的一局")
            session_stopped = False
            translator_seen = False
        if not game_running:
            if child is not None:
                if child.poll() is not None:
                    child = None
                elif stop_on_exit:
                    log.info("游戏进程已退出，关闭翻译器")
                    stop_translator(child, log)
                    child = None
            session_stopped = False
            translator_seen = False
        else:
            running = child is not None and child.poll() is None
            if child is not None and not running:
                log.info("翻译器已退出，本次游戏运行期间不再自动启动")
                session_stopped = True
                child = None
            if not running and getattr(sys, "frozen", False):
                exe_name = TRANSLATOR_EXE if os.name == "nt" else "WTTranslator"
                running = any_running([exe_name])
            if running:
                translator_seen = True
            elif translator_seen or session_stopped:
                if not session_stopped:
                    log.info("翻译器已退出，等待下次启动游戏时再启动")
                    session_stopped = True
            else:
                child = start_translator(log)
                if child is not None:
                    translator_seen = True
        game_was_running = game_running

    if stop_on_exit and child is not None and child.poll() is None:
        stop_translator(child, log)
    log.info("守护进程结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
