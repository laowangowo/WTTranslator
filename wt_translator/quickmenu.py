"""Windows 原生快捷回复浮层（单窗口、逐像素透明）。

使用 UpdateLayeredWindow：
- 背景像素：alpha = 配置透明度
- 文字像素：alpha = 255（完全不透明，不受透明度影响）
- WS_EX_TRANSPARENT：鼠标完全穿透
- 窗口类只注册一次，WNDPROC 全局唯一，避免频繁打开/关闭时回调失效崩溃
"""
from __future__ import annotations

import ctypes
import sys
import threading
import traceback
from ctypes import wintypes

if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    _kernel32 = ctypes.windll.kernel32
else:
    _user32 = _gdi32 = _kernel32 = None


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt", _POINT),
    ]


if _user32 is not None:
    H = ctypes.c_void_p
    _user32.CreateWindowExW.restype = ctypes.c_void_p
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        H, H, H, H,
    ]
    _user32.LoadCursorW.restype = ctypes.c_void_p
    _user32.LoadCursorW.argtypes = [H, H]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.DefWindowProcW.argtypes = [H, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    _user32.ShowWindow.argtypes = [H, ctypes.c_int]
    _user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), H, wintypes.UINT, wintypes.UINT]
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
    _user32.PostMessageW.argtypes = [H, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.DrawTextW.argtypes = [
        H, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(_RECT), wintypes.UINT
    ]
    _user32.FillRect.argtypes = [H, ctypes.POINTER(_RECT), H]
    _user32.FillRect.restype = ctypes.c_int
    _user32.SetTimer.argtypes = [H, ctypes.c_size_t, wintypes.UINT, H]
    _user32.KillTimer.argtypes = [H, ctypes.c_size_t]
    _user32.DestroyWindow.argtypes = [H]
    _user32.GetDC.restype = ctypes.c_void_p
    _user32.GetDC.argtypes = [H]
    _user32.ReleaseDC.argtypes = [H, H]
    _user32.UpdateLayeredWindow.restype = wintypes.BOOL
    _user32.UpdateLayeredWindow.argtypes = [
        H, H, ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE), H,
        ctypes.POINTER(_POINT), wintypes.DWORD, ctypes.POINTER(_BLENDFUNCTION),
        wintypes.DWORD,
    ]
    _gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    _gdi32.CreateCompatibleDC.argtypes = [H]
    _gdi32.DeleteDC.argtypes = [H]
    _gdi32.CreateDIBSection.restype = ctypes.c_void_p
    _gdi32.CreateDIBSection.argtypes = [
        H, ctypes.POINTER(_BITMAPINFOHEADER), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), H, wintypes.DWORD,
    ]
    _gdi32.CreateSolidBrush.restype = ctypes.c_void_p
    _gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
    _gdi32.CreateFontW.restype = ctypes.c_void_p
    _gdi32.CreateFontW.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPCWSTR,
    ]
    _gdi32.SelectObject.restype = ctypes.c_void_p
    _gdi32.SelectObject.argtypes = [H, H]
    _gdi32.DeleteObject.argtypes = [H]
    _gdi32.DeleteObject.restype = wintypes.BOOL
    _gdi32.SetBkMode.argtypes = [H, ctypes.c_int]
    _gdi32.SetTextColor.argtypes = [H, wintypes.DWORD]
    _gdi32.GetStockObject.restype = ctypes.c_void_p
    _gdi32.GetStockObject.argtypes = [ctypes.c_int]
    _gdi32.GdiFlush.restype = wintypes.BOOL
    _kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t
)

WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_TIMER = 0x0113

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
ULW_ALPHA = 0x00000002
SW_SHOWNOACTIVATE = 4
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1

DT_LEFT = 0x00000000
DT_VCENTER = 0x00000004
DT_SINGLELINE = 0x00000020
DT_END_ELLIPSIS = 0x00008000
TRANSPARENT = 1
DEFAULT_GUI_FONT = 17
NONANTIALIASED_QUALITY = 1
DIB_RGB_COLORS = 0

CLASS_NAME = "WTTranslatorQuickMenuWindow"

_WINDOWS = {}
_WINDOWS_LOCK = threading.Lock()
_CLASS_REGISTERED = False
_CLASS_LOCK = threading.Lock()


def _ensure_class():
    global _CLASS_REGISTERED
    if _user32 is None:
        return
    with _CLASS_LOCK:
        if _CLASS_REGISTERED:
            return
        wndclass = _WNDCLASSW()
        wndclass.style = 0
        wndclass.lpfnWndProc = ctypes.cast(_GLOBAL_WNDPROC, ctypes.c_void_p)
        wndclass.hInstance = _kernel32.GetModuleHandleW(None)
        wndclass.hCursor = _user32.LoadCursorW(None, ctypes.c_void_p(32512))
        wndclass.lpszClassName = CLASS_NAME
        _user32.RegisterClassW(ctypes.byref(wndclass))
        _CLASS_REGISTERED = True


def _global_wndproc(hwnd, msg, wparam, lparam):
    with _WINDOWS_LOCK:
        instance = _WINDOWS.get(hwnd)
    if instance is not None:
        return instance._handle(hwnd, msg, wparam, lparam)
    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_GLOBAL_WNDPROC = WNDPROC(_global_wndproc) if _user32 is not None else None


class NativeQuickMenu:

    FONT_FAMILY = "Microsoft YaHei UI"
    FONT_SIZE = 14

    def __init__(
        self, items, opacity=0.1, fg_color="#ececef",
        bg_color="#23232b", log=None,
    ):
        self.items = list(items)[:9]
        self.opacity = max(0.05, min(1.0, float(opacity or 0.1)))
        self.fg_color = self._to_colorref(fg_color)
        self.bg_color = self._to_colorref(bg_color, fallback=0x002B2323)
        self.log = log
        self.hwnd_handle = None
        self._geometry_info = None
        self._thread = None
        self._ready = threading.Event()
        self.last_render = None  # (bg_pixels, text_pixels, bg_alpha)

    @property
    def hwnd(self):
        return self.hwnd_handle

    @property
    def visible(self):
        return self.hwnd_handle is not None

    @staticmethod
    def _to_colorref(value, fallback=0x00EFECEC):
        text = str(value or "").strip().lstrip("#")
        if len(text) == 6:
            try:
                r = int(text[0:2], 16)
                g = int(text[2:4], 16)
                b = int(text[4:6], 16)
                return (b << 16) | (g << 8) | r
            except ValueError:
                pass
        return fallback

    # ---------- 对外 ----------
    def show(self):
        if _user32 is None or not self.items:
            return False
        self._thread = threading.Thread(
            target=self._run, name="quick-menu-native", daemon=True
        )
        self._thread.start()
        self._ready.wait(2.0)
        return self.hwnd_handle is not None

    def hide(self):
        if self.hwnd_handle:
            _user32.PostMessageW(self.hwnd_handle, WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        self.hwnd_handle = None

    # ---------- 窗口 ----------
    def _geometry(self):
        cols = 3
        rows = max(1, (len(self.items) + cols - 1) // cols)
        cell_w, cell_h = 176, 34
        width = cols * cell_w + 16
        height = rows * cell_h + 16
        screen_w = _user32.GetSystemMetrics(0)
        screen_h = _user32.GetSystemMetrics(1)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        return x, y, width, height

    def _run(self):
        try:
            _ensure_class()
            instance = _kernel32.GetModuleHandleW(None)
            x, y, width, height = self._geometry()
            self._geometry_info = (x, y, width, height)
            ex_style = (
                WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT
                | WS_EX_LAYERED | WS_EX_NOACTIVATE
            )
            self.hwnd_handle = _user32.CreateWindowExW(
                ex_style, CLASS_NAME, "WT Quick Reply", WS_POPUP,
                x, y, width, height, None, None, instance, None,
            )
            if not self.hwnd_handle:
                return
            with _WINDOWS_LOCK:
                _WINDOWS[self.hwnd_handle] = self
            self._render()
            _user32.ShowWindow(self.hwnd_handle, SW_SHOWNOACTIVATE)
            self._render()
            _user32.SetTimer(self.hwnd_handle, 1, 80, None)
            self._ready.set()
            msg = _MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            if self.log:
                self.log.warning("快捷菜单原生窗口创建失败：%s\n%s", exc, traceback.format_exc())
        finally:
            self._ready.set()
            with _WINDOWS_LOCK:
                _WINDOWS.pop(self.hwnd_handle, None)
            if self.hwnd_handle:
                try:
                    _user32.DestroyWindow(self.hwnd_handle)
                except Exception:
                    pass
            self.hwnd_handle = None

    def _handle(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TIMER and wparam == 1:
                _user32.KillTimer(hwnd, 1)
                self._render()
                return 0
            if msg == WM_CLOSE:
                return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            if msg == WM_DESTROY:
                with _WINDOWS_LOCK:
                    _WINDOWS.pop(hwnd, None)
                _user32.PostQuitMessage(0)
                return 0
        except Exception as exc:
            if self.log:
                self.log.warning("快捷菜单绘制失败：%s\n%s", exc, traceback.format_exc())
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _render(self):
        if not self.hwnd_handle or not self._geometry_info:
            return
        x, y, width, height = self._geometry_info
        screen_dc = _user32.GetDC(None)
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        info = _BITMAPINFOHEADER()
        info.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.biWidth = width
        info.biHeight = -height  # 自上而下
        info.biPlanes = 1
        info.biBitCount = 32
        info.biCompression = 0
        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(
            screen_dc, ctypes.byref(info), DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0,
        )
        if not mem_dc or not bitmap or not bits:
            return
        old_bitmap = _gdi32.SelectObject(mem_dc, bitmap)
        try:
            rect = _RECT(0, 0, width, height)
            brush = _gdi32.CreateSolidBrush(self.bg_color)
            _user32.FillRect(mem_dc, ctypes.byref(rect), brush)
            _gdi32.DeleteObject(brush)
            _gdi32.GdiFlush()
            buffer = ctypes.cast(bits, ctypes.POINTER(ctypes.c_ubyte))
            self.last_pixel = (
                buffer[0], buffer[1], buffer[2],
            )
            _gdi32.SetBkMode(mem_dc, TRANSPARENT)
            _gdi32.SetTextColor(mem_dc, self.fg_color)
            font = _gdi32.CreateFontW(
                -self.FONT_SIZE, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0,
                NONANTIALIASED_QUALITY, 0, self.FONT_FAMILY,
            )
            stock_font = None
            if not font:
                stock_font = _gdi32.GetStockObject(DEFAULT_GUI_FONT)
                font = stock_font
            old_font = _gdi32.SelectObject(mem_dc, font)
            cols, cell_w, cell_h = 3, 176, 34
            for index, text in enumerate(self.items):
                row, col = divmod(index, cols)
                label = str(text).replace("\n", " ")
                if len(label) > 16:
                    label = label[:16] + "…"
                cell = _RECT(
                    8 + col * cell_w, 8 + row * cell_h,
                    8 + (col + 1) * cell_w - 8, 8 + (row + 1) * cell_h,
                )
                _user32.DrawTextW(
                    mem_dc, f"{index + 1}. {label}", -1, ctypes.byref(cell),
                    DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS,
                )
            _gdi32.SelectObject(mem_dc, old_font)
            if font and font != stock_font:
                _gdi32.DeleteObject(font)
            _gdi32.GdiFlush()

            bg_alpha = int(255 * self.opacity)
            bg_b = (self.bg_color >> 16) & 0xFF
            bg_g = (self.bg_color >> 8) & 0xFF
            bg_r = self.bg_color & 0xFF
            text_pixels = 0
            bg_pixels = 0
            total = width * height
            for index in range(total):
                offset = index * 4
                b = buffer[offset]
                g = buffer[offset + 1]
                r = buffer[offset + 2]
                if abs(b - bg_b) <= 2 and abs(g - bg_g) <= 2 and abs(r - bg_r) <= 2:
                    alpha = bg_alpha
                    bg_pixels += 1
                else:
                    alpha = 255
                    text_pixels += 1
                buffer[offset] = (b * alpha) // 255
                buffer[offset + 1] = (g * alpha) // 255
                buffer[offset + 2] = (r * alpha) // 255
                buffer[offset + 3] = alpha
            self.last_render = (bg_pixels, text_pixels, bg_alpha)

            src = _POINT(0, 0)
            size = _SIZE(width, height)
            dst = _POINT(x, y)
            blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            _user32.UpdateLayeredWindow(
                self.hwnd_handle, screen_dc, ctypes.byref(dst), ctypes.byref(size),
                mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA,
            )
        finally:
            _gdi32.SelectObject(mem_dc, old_bitmap)
            _gdi32.DeleteObject(bitmap)
            _gdi32.DeleteDC(mem_dc)
            _user32.ReleaseDC(None, screen_dc)
