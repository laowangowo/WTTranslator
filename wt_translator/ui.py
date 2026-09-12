"""悬浮窗界面（tkinter 实现，无边框置顶、可拖动、自动隐藏）。"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import colorchooser

from .translator import clean_chat_text

BG = "#18181d"
HEADER_BG = "#23232b"
FG = "#ececef"
DIM = "#9aa0a6"
PENDING = "#8b8f96"
BLOCKED = "#7a7f88"
ENEMY = "#ff7b72"

VK_MENU = 0x12  # Alt
VK_RETURN = 0x0D  # 回车

DEFAULT_BG = "#18181d"
DEFAULT_FG = "#ececef"
RESIZE_MARGIN = 6
MIN_WINDOW_WIDTH = 220
MIN_WINDOW_HEIGHT = 120
ANGRY_WORDS = ("fuck", "shit", "damn", "ass", "bastard", "crap", "hell")
DEFAULT_QUICK_REPLIES = [
    "收到", "干得好", "进攻D点", "防守", "需要支援",
    "谢谢", "抱歉", "撤退", "掩护我",
]

_COLOR_TAG_RE = re.compile(
    r"<color=\s*#?([0-9A-Fa-f]{6,8})\s*>(.*?)(?:</color>|$)",
    re.IGNORECASE | re.DOTALL,
)

# 呼出按键名称 -> 虚拟键码
HOTKEY_VK = {
    "alt": VK_MENU,
    "ctrl": 0x11,
    "shift": 0x10,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
}
HOTKEY_LABELS = ["Alt", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "关闭"]

# 呼出按键录制时轮询的候选键（label, vk）
HOTKEY_CAPTURE_CANDIDATES = (
    [("F%d" % i, 0x6F + i) for i in range(1, 13)]
    + [("Alt", VK_MENU), ("Ctrl", 0x11), ("Shift", 0x10)]
    + [(chr(code), code) for code in range(ord("A"), ord("Z") + 1)]
    + [("Num%d" % i, 0x60 + i) for i in range(10)]
    + [("0", 0x30), ("1", 0x31), ("2", 0x32), ("3", 0x33), ("4", 0x34),
       ("5", 0x35), ("6", 0x36), ("7", 0x37), ("8", 0x38), ("9", 0x39)]
)

# macOS 虚拟键码（用于全局按键轮询）
_MAC_KEYCODE_BY_VK = {
    0x12: 58,  # Option / Alt（左）
    0x11: 59,  # Control（左）
    0x10: 56,  # Shift（左）
    0x0D: 36,  # Return
    0x1B: 53,  # Esc
}
_MAC_KEYCODE_BY_VK.update({
    0x70 + i: code for i, code in enumerate(
        [122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111]
    )
})
_MAC_LETTER_CODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37,
    "j": 38, "k": 40, "n": 45, "m": 46,
}
for _code in range(ord("A"), ord("Z") + 1):
    _mac = _MAC_LETTER_CODES.get(chr(_code).lower())
    if _mac is not None:
        _MAC_KEYCODE_BY_VK[_code] = _mac
_MAC_KEYCODE_BY_VK.update({
    0x30: 29, 0x31: 18, 0x32: 19, 0x33: 20, 0x34: 21,
    0x35: 23, 0x36: 22, 0x37: 26, 0x38: 28, 0x39: 25,
})


def resolve_hotkey_vk(value):
    """把配置里的按键名解析成虚拟键码（兼容字母/数字/alt/ctrl/shift/f1-f12）。"""
    key = str(value or "").strip().lower()
    if key in HOTKEY_VK:
        return HOTKEY_VK[key]
    if len(key) == 1:
        if key.isalpha():
            return ord(key.upper())
        if key.isdigit():
            return ord("0") + int(key)
    if key.startswith("num") and key[3:].isdigit():
        return 0x60 + int(key[3:])
    return None


def hotkey_display(value):
    key = str(value or "").strip().lower()
    if not key:
        return "未设置"
    if key in ("alt", "ctrl", "shift"):
        return key.capitalize()
    if key.startswith("f"):
        return key.upper()
    if len(key) == 1:
        return key.upper()
    return key


def _read_alt_state() -> bool:
    """读取 Alt 键当前是否按下（全局，不依赖窗口焦点）。"""
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_MENU) & 0x8000)
    except Exception:
        return False


def _read_key_state(vk: int):
    """返回读取指定虚拟键当前是否按下的函数（全局，不依赖窗口焦点）。"""

    def reader() -> bool:
        if os.name == "nt":
            try:
                import ctypes

                return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
            except Exception:
                return False
        if sys.platform == "darwin":
            try:
                from Quartz import (  # type: ignore
                    CGEventSourceKeyState,
                    kCGEventSourceStateCombinedSessionState,
                )

                keycode = _MAC_KEYCODE_BY_VK.get(vk)
                if keycode is None:
                    return False
                return bool(
                    CGEventSourceKeyState(
                        kCGEventSourceStateCombinedSessionState, keycode
                    )
                )
            except Exception:
                return False
        return False

    return reader


class AltKeyWatcher(threading.Thread):
    """后台线程：监听 Alt 键按下（上升沿触发），回调到 UI 队列。"""

    def __init__(self, on_alt_press, read_state=None, interval=0.03, on_alt_release=None):
        super().__init__(name="alt-key-watcher", daemon=True)
        self._on_press = on_alt_press
        self._on_release = on_alt_release
        self._read = read_state or _read_alt_state
        self._interval = max(0.005, interval)
        self._stop = threading.Event()
        self._prev_down = False

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                down = bool(self._read())
            except Exception:
                down = False
            if down and not self._prev_down:
                try:
                    self._on_press()
                except Exception:
                    pass
            elif not down and self._prev_down and self._on_release is not None:
                try:
                    self._on_release()
                except Exception:
                    pass
            self._prev_down = down

MODE_COLORS = {
    "Team": "#4da6ff",
    "Squad": "#7bd88f",
    "Chat": "#d8d8e0",
    "System": "#ffb454",
}


def _tags(*names):
    return tuple(name for name in names if name)


class _HeaderButton(tk.Label):
    def __init__(self, parent, text, command, font):
        super().__init__(parent, text=text, bg=HEADER_BG, fg=DIM, width=2, cursor="hand2", font=font)
        self._command = command
        self.bind("<Button-1>", lambda _e: command())
        self.bind("<Enter>", lambda _e: self.configure(fg="#ffffff"))
        self.bind("<Leave>", lambda _e: self.configure(fg=DIM))


class OverlayWindow:
    """无边框置顶浮窗：列表形式展示聊天翻译结果。"""

    def __init__(self, cfg, ui_queue, on_quit, log, translate_fn=None, reply_fn=None):
        self.cfg = cfg
        self.queue = ui_queue
        self.on_quit = on_quit
        self.log = log
        self.translate_fn = translate_fn
        self.reply_fn = reply_fn
        self._hidden = False
        self._stopping = False
        self._input_visible = False
        self._last_activity = time.monotonic()
        self._pending_marks = {}  # 消息 id -> Text mark 名，用于翻译完成后原位替换
        self._mark_seq = 0
        self._messages_by_tag = {}
        self._msg_tag_seq = 0
        self._reply_busy = False
        self._quick_menu = None
        self._quick_pick_watchers = []
        self._quick_close_after = None
        self._quick_watcher = None
        self._mode_tags = set(MODE_COLORS)
        self._alt_watcher = None
        self._enter_watcher = None
        self._voice_watcher = None
        self._voice_recorder = None
        self._asr = None
        self._voice_recording = False
        self._voice_busy = False
        self._dragging_mode = None
        self._resize_edge = ""
        self._resize_start = None
        self.bg = DEFAULT_BG
        self.fg = DEFAULT_FG
        self._build()
        self._start_watchers()
        # 首次启动：询问是否启用守护进程
        if self.cfg.get("guardian_ask_on_start", True):
            self.root.after(800, self._ask_guardian_setup)
        elif self.cfg.get("guardian_enabled", False):
            self.root.after(800, self._ensure_guardian_running)

    # ---------- 全局按键监听 ----------
    def _stop_watchers(self):
        for watcher in (
            self._alt_watcher, self._enter_watcher,
            self._voice_watcher, self._quick_watcher,
        ):
            if watcher is not None:
                watcher.stop()
        self._alt_watcher = None
        self._enter_watcher = None
        self._voice_watcher = None
        self._quick_watcher = None
        self._stop_quick_pick_watchers()

    def _start_watchers(self):
        vk = resolve_hotkey_vk(self.cfg.get("show_hotkey", "alt"))
        if self.cfg.get("show_on_alt", True) and vk:
            self._alt_watcher = AltKeyWatcher(
                lambda: self.queue.put({"type": "show"}),
                read_state=_read_key_state(vk),
            )
            self._alt_watcher.start()
        if self.cfg.get("show_input_on_enter", True):
            self._enter_watcher = AltKeyWatcher(
                lambda: self.queue.put({"type": "toggle_input"}),
                read_state=_read_key_state(VK_RETURN),
            )
            self._enter_watcher.start()
        if self.cfg.get("voice_enabled", False):
            voice_vk = resolve_hotkey_vk(self.cfg.get("voice_hotkey", "f8"))
            if voice_vk:
                if str(self.cfg.get("voice_mode", "toggle")).lower() == "hold":
                    self._voice_watcher = AltKeyWatcher(
                        lambda: self.queue.put({"type": "voice_press"}),
                        read_state=_read_key_state(voice_vk),
                        on_alt_release=lambda: self.queue.put({"type": "voice_release"}),
                        interval=0.03,
                    )
                else:
                    self._voice_watcher = AltKeyWatcher(
                        lambda: self.queue.put({"type": "voice_toggle"}),
                        read_state=_read_key_state(voice_vk),
                        interval=0.05,
                    )
                self._voice_watcher.start()
        if self.cfg.get("quick_reply_enabled", True):
            quick_vk = resolve_hotkey_vk(self.cfg.get("quick_reply_hotkey", "n"))
            if quick_vk:
                self._quick_watcher = AltKeyWatcher(
                    lambda: self.queue.put({"type": "quick_toggle"}),
                    read_state=_read_key_state(quick_vk),
                    interval=0.05,
                )
                self._quick_watcher.start()

    # ---------- 界面构建 ----------
    def _font(self, size=None):
        family = self.cfg.get("font_family", "Microsoft YaHei UI")
        size = int(size if size is not None else self.cfg.get("font_size", 11))
        return (family, size)

    def _build(self):
        root = self.root = tk.Tk()
        root.title("WT 翻译")
        root.overrideredirect(True)
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        self._apply_alpha()

        width = max(220, int(self.cfg.get("window_width", 460)))
        height = max(120, int(self.cfg.get("window_height", 320)))
        x = int(self.cfg.get("window_x", -1))
        y = int(self.cfg.get("window_y", -1))
        if x < 0 or y < 0:
            screen_w = root.winfo_screenwidth()
            screen_h = root.winfo_screenheight()
            x, y = screen_w - width - 24, 24
        root.geometry(f"{width}x{height}+{x}+{y}")

        # 标题栏（始终可交互，用于拖动 / 菜单）
        header = self.header = tk.Frame(root, bg=HEADER_BG, height=26, cursor="fleur")
        header.pack(fill="x")
        header.pack_propagate(False)
        title = tk.Label(
            header, text="WT Translator", bg=HEADER_BG, fg=DIM, font=self._font(9)
        )
        title.pack(side="left", padx=8)
        self._btn_quit = _HeaderButton(header, "✕", self._quit, self._font(9))
        self._btn_quit.pack(side="right", padx=(0, 5))
        self._btn_hide = _HeaderButton(header, "—", self._toggle_hide, self._font(9))
        self._btn_hide.pack(side="right")

        # 消息列表
        text = self.text = tk.Text(
            root,
            bg=BG,
            fg=FG,
            font=self._font(),
            wrap="word",
            bd=0,
            highlightthickness=0,
            padx=8,
            pady=6,
            state="disabled",
            cursor="arrow",
        )
        text.pack(fill="both", expand=True)
        self._configure_tags()
        self._bind_events()

        # 底部翻译输入框（默认隐藏）
        self.input_frame = tk.Frame(root, bg=BG)
        self.input_entry = tk.Entry(
            self.input_frame, bg="#121217", fg=FG, insertbackground=FG,
            font=self._font(10), relief="flat", bd=1, highlightthickness=1,
            highlightbackground="#33333d", highlightcolor="#2f7cf6",
        )
        self.input_entry.pack(fill="x", expand=True, padx=8, pady=4)
        self.input_entry.bind("<Return>", self._on_input_enter)
        self.input_entry.bind("<Escape>", lambda _e: self._hide_input())
        self.input_entry.bind("<Key>", lambda _e: self._touch())
        self.input_frame.pack(fill="x", side="bottom", before=self.text)
        self.input_frame.pack_forget()

        # 右下角缩放柄（无边框窗口没有系统边框）
        self._resize_grip = tk.Label(
            root, text="◢", bg=HEADER_BG, fg=DIM, font=self._font(8), bd=0,
        )
        try:
            self._resize_grip.configure(cursor="sizing")
        except tk.TclError:
            pass
        self._resize_grip.place(relx=1.0, rely=1.0, anchor="se")
        self._resize_grip.bind("<Button-1>", self._on_grip_press)
        self._resize_grip.bind("<B1-Motion>", self._on_drag)
        self._resize_grip.bind("<ButtonRelease-1>", self._on_release)
        self._apply_colors()

    def _configure_tags(self):
        for mode, color in MODE_COLORS.items():
            self.text.tag_configure(f"mode_{mode}", foreground=color)
        self.text.tag_configure("mode_default", foreground=MODE_COLORS["Chat"])
        self.text.tag_configure("enemy", foreground=ENEMY)
        self.text.tag_configure("pending", foreground=PENDING)
        self.text.tag_configure("blocked", foreground=BLOCKED)
        self.text.tag_configure("dim", foreground=DIM)
        try:
            self.text.tag_configure(
                "normal", foreground=self.cfg.get("fg_color", DEFAULT_FG)
            )
        except tk.TclError:
            self.text.tag_configure("normal", foreground=DEFAULT_FG)

    def _bind_events(self):
        for widget in (self.root, self.header, self.text):
            widget.bind("<Button-1>", self._on_press, add="+")
            widget.bind("<B1-Motion>", self._on_drag, add="+")
            widget.bind("<ButtonRelease-1>", self._on_release, add="+")
            widget.bind("<Button-3>", self._popup, add="+")
            widget.bind("<Motion>", self._on_motion, add="+")
        self.header.bind("<Double-Button-1>", lambda _e: self._toggle_hide())

    # ---------- 拖动 / 缩放 / 位置记忆 ----------
    def _window_geometry(self):
        return (
            int(self.root.winfo_x()),
            int(self.root.winfo_y()),
            int(self.root.winfo_width()),
            int(self.root.winfo_height()),
        )

    def _resize_edge_at(self, event):
        """根据鼠标在窗口内的位置判断缩放方向（"" 表示拖动移动）。"""
        x = event.x_root - self.root.winfo_rootx()
        y = event.y_root - self.root.winfo_rooty()
        width = max(1, self.root.winfo_width())
        height = max(1, self.root.winfo_height())
        edge = ""
        if x <= RESIZE_MARGIN:
            edge += "w"
        elif x >= width - RESIZE_MARGIN:
            edge += "e"
        if y <= RESIZE_MARGIN:
            edge += "n"
        elif y >= height - RESIZE_MARGIN:
            edge += "s"
        return edge

    def _on_grip_press(self, event):
        self._begin_resize(event, "se")

    def _begin_resize(self, event, edge):
        x, y, width, height = self._window_geometry()
        self._dragging_mode = "resize"
        self._resize_edge = edge
        self._resize_start = (event.x_root, event.y_root, x, y, width, height)

    def _on_press(self, event):
        edge = self._resize_edge_at(event)
        if edge:
            self._begin_resize(event, edge)
            return
        self._dragging_mode = "move"
        self._drag_dx = event.x_root - self.root.winfo_x()
        self._drag_dy = event.y_root - self.root.winfo_y()

    def _on_drag(self, event):
        if self._dragging_mode == "resize" and self._resize_start:
            self._perform_resize(event)
            return
        try:
            self.root.geometry(f"+{event.x_root - self._drag_dx}+{event.y_root - self._drag_dy}")
        except tk.TclError:
            pass

    def _perform_resize(self, event):
        start_x, start_y, win_x, win_y, win_w, win_h = self._resize_start
        dx = event.x_root - start_x
        dy = event.y_root - start_y
        left, top = win_x, win_y
        right, bottom = win_x + win_w, win_y + win_h
        if "e" in self._resize_edge:
            right = max(left + MIN_WINDOW_WIDTH, right + dx)
        if "s" in self._resize_edge:
            bottom = max(top + MIN_WINDOW_HEIGHT, bottom + dy)
        if "w" in self._resize_edge:
            left = min(right - MIN_WINDOW_WIDTH, left + dx)
        if "n" in self._resize_edge:
            top = min(bottom - MIN_WINDOW_HEIGHT, top + dy)
        try:
            self.root.geometry(f"{right - left}x{bottom - top}+{left}+{top}")
        except tk.TclError:
            pass

    def _on_motion(self, event):
        if self._dragging_mode:
            return
        edge = self._resize_edge_at(event)
        cursors = {
            "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
            "e": "sb_h_double_arrow", "w": "sb_h_double_arrow",
            "ne": "sizing", "sw": "sizing",
            "nw": "sizing", "se": "sizing",
        }
        for widget, default in ((self.root, "arrow"), (self.header, "fleur"), (self.text, "arrow")):
            try:
                widget.configure(cursor=cursors.get(edge, default))
            except tk.TclError:
                pass

    def _on_release(self, _event):
        self._dragging_mode = None
        self._resize_edge = ""
        self._resize_start = None
        self._save_position()

    def _save_position(self):
        try:
            self.cfg.update(
                window_x=int(self.root.winfo_x()),
                window_y=int(self.root.winfo_y()),
                window_width=max(MIN_WINDOW_WIDTH, int(self.root.winfo_width())),
                window_height=max(MIN_WINDOW_HEIGHT, int(self.root.winfo_height())),
            )
            self.cfg.save()
        except Exception:
            pass

    def _apply_colors(self):
        """应用主浮窗背景色和文字颜色（设置里可改）。"""
        def valid(value, fallback):
            try:
                self.root.winfo_rgb(str(value))
                return str(value)
            except tk.TclError:
                return fallback

        self.bg = valid(self.cfg.get("bg_color", DEFAULT_BG), DEFAULT_BG)
        self.fg = valid(self.cfg.get("fg_color", DEFAULT_FG), DEFAULT_FG)
        try:
            self.root.configure(bg=self.bg)
            self.header.configure(bg=self.bg)
            for child in self.header.winfo_children():
                try:
                    child.configure(bg=self.bg)
                except tk.TclError:
                    pass
            self.text.configure(bg=self.bg, fg=self.fg, insertbackground=self.fg)
            self.input_frame.configure(bg=self.bg)
            self.input_entry.configure(bg=self.bg, fg=self.fg, insertbackground=self.fg)
            self._resize_grip.configure(bg=self.bg)
            self._configure_tags()
        except tk.TclError:
            pass

    # ---------- 文本操作 ----------
    def _edit(self):
        self.text.configure(state="normal")

    def _freeze(self):
        self.text.configure(state="disabled")

    def _last_line(self) -> int:
        """最后一条内容所在行号。

        Tk 的 "end-1c" 指向末尾幻影空行（内容 "a\\nb\\n" 时返回 3.0），
        真正的最后一行要减一。
        """
        line = int(self.text.index("end-1c").split(".")[0])
        return max(1, line - 1)

    def _insert_parts(self, index, parts):
        """按顺序插入多段带标签文本，返回插入结束位置。"""
        pos = index
        for chunk, tags in parts:
            if chunk:
                if tags:
                    self.text.insert(pos, chunk, tuple(tags))
                else:
                    self.text.insert(pos, chunk)
                pos = self.text.index(f"{pos}+{len(chunk)}c")
        return pos

    @staticmethod
    def _normalize_markup_color(raw):
        """#AARRGGBB / #RRGGBB -> #rrggbb（忽略 alpha）。"""
        value = str(raw or "").strip().lstrip("#")
        if len(value) == 8:
            value = value[2:]
        value = value[:6]
        if len(value) != 6:
            return None
        try:
            int(value, 16)
        except ValueError:
            return None
        return "#" + value.lower()

    def _color_tag(self, color):
        tag = f"wtcolor_{color.lstrip('#')}"
        try:
            self.text.tag_configure(tag, foreground=color)
            self.text.tag_raise(tag)
        except tk.TclError:
            return None
        return tag

    def _parse_color_markup(self, text, base_tags=()):
        """把 <color=#AARRGGBB>...</color> 解析成带颜色的文本片段。"""
        value = str(text or "")
        parts = []
        position = 0
        for match in _COLOR_TAG_RE.finditer(value):
            if match.start() > position:
                parts.append((value[position:match.start()], tuple(base_tags)))
            color = self._normalize_markup_color(match.group(1))
            tags = tuple(base_tags)
            if color:
                tag = self._color_tag(color)
                if tag:
                    tags = tags + (tag,)
            parts.extend(self._parse_color_markup(match.group(2), tags))
            position = match.end()
        if position < len(value):
            parts.append((value[position:], tuple(base_tags)))
        if not parts and value:
            parts.append((value, tuple(base_tags)))
        return parts

    def _trim(self):
        max_lines = max(10, int(self.cfg.get("max_messages", 60)))
        total = self._last_line()
        if total <= max_lines:
            return
        removed = total - max_lines
        self.text.delete("1.0", f"{removed + 1}.0")
        self._prune_message_tags()
        # 被删除行上的 mark 会随文本一起消失，_pending_marks 里的残留条目按 TclError 忽略

    def _append(self, parts):
        """在末尾追加一行，返回该消息所在行号。"""
        self._edit()
        self._insert_parts("end", parts)
        self.text.insert("end", "\n")
        self._trim()
        line = self._last_line()
        self._freeze()
        self.text.see("end")
        return line

    def _replace_line(self, line, parts):
        self._edit()
        self.text.delete(f"{line}.0", f"{line}.0 lineend")
        self._insert_parts(f"{line}.0", parts)
        self._freeze()
        self.text.see("end")

    # ---------- 消息展示 ----------
    def _mode_tag(self, mode):
        mode = str(mode or "Chat")
        return f"mode_{mode}" if mode in self._mode_tags else "mode_default"

    def _tag_message(self, line, msg):
        self._msg_tag_seq += 1
        tag = f"msg_{self._msg_tag_seq}"
        self._messages_by_tag[tag] = msg
        try:
            self.text.tag_add(tag, f"{line}.0", f"{line}.0 lineend")
        except tk.TclError:
            pass
        self._prune_message_tags()

    def _prune_message_tags(self):
        for tag in list(self._messages_by_tag):
            try:
                if not self.text.tag_ranges(tag):
                    self._messages_by_tag.pop(tag, None)
            except tk.TclError:
                self._messages_by_tag.pop(tag, None)

    def _message_at(self, x, y):
        try:
            index = self.text.index(f"@{int(x)},{int(y)}")
            for tag in self.text.tag_names(index):
                if tag.startswith("msg_"):
                    return self._messages_by_tag.get(tag)
        except tk.TclError:
            pass
        return None

    def add_chat_message(self, msg):
        """新消息先以原文显示（灰色），翻译完成后原位替换。"""
        mode = clean_chat_text(msg.get("mode")) or "Chat"
        enemy = bool(msg.get("enemy"))
        sender = clean_chat_text(msg.get("sender"))
        text = clean_chat_text(msg.get("msg"))
        prefix_tags = _tags("enemy" if enemy else self._mode_tag(mode))
        parts = [(f"[{mode}] ", prefix_tags)]
        if self.cfg.get("show_sender") and sender:
            parts.append((f"{sender}: ", ("dim",)))
        parts.extend(self._parse_color_markup(text, ("pending",)))
        line = self._append(parts)
        self._tag_message(line, msg)
        mid = msg.get("id")
        if isinstance(mid, int):
            mark = f"_m{self._mark_seq}"
            self._mark_seq += 1
            self.text.mark_set(mark, f"{line}.0")
            self.text.mark_gravity(mark, "left")
            self._pending_marks[mid] = mark
        self._touch()

    def finish_translation(self, msg, translated, original, blacklisted=False, failed=False):
        """把占位行替换为最终内容。"""
        translated = clean_chat_text(translated)
        original = clean_chat_text(original)
        mid = msg.get("id")
        mark = self._pending_marks.pop(mid, None) if isinstance(mid, int) else None
        if mark is None:
            return
        try:
            pos = self.text.index(mark)
        except tk.TclError:
            return  # 该行已被滚动裁剪删除
        line = int(pos.split(".")[0])
        try:
            self.text.mark_unset(mark)
        except tk.TclError:
            pass
        mode = clean_chat_text(msg.get("mode")) or "Chat"
        enemy = bool(msg.get("enemy"))
        sender = clean_chat_text(msg.get("sender"))
        prefix_tags = _tags("enemy" if enemy else self._mode_tag(mode))
        parts = [(f"[{mode}] ", prefix_tags)]
        if self.cfg.get("show_sender") and sender:
            parts.append((f"{sender}: ", ("dim",)))
        if blacklisted or failed:
            parts.extend(self._parse_color_markup(translated, ("blocked",)))
        else:
            parts.extend(self._parse_color_markup(translated, ("normal",)))
            if self.cfg.get("show_source_language", False):
                from wt_translator.translator import detect_language

                lang = str(msg.get("lang") or detect_language(original))
                parts.append((f" [{lang}]", ("dim",)))
        self._replace_line(line, parts)
        self._tag_message(line, msg)
        if not blacklisted and not failed and self.cfg.get("show_original") and original and original != translated:
            original_parts = [("原文: ", ("dim",))]
            original_parts.extend(self._parse_color_markup(original, ("dim",)))
            self._append(original_parts)
        self._touch()

    def update_status(self, text):
        """更新最后一行系统状态（就地替换，避免刷屏）。"""
        line = self._last_line()
        current = self.text.get(f"{line}.0", f"{line}.0 lineend")
        if current.startswith("[系统]"):
            self._replace_line(line, [("[系统] " + text, ("mode_System",))])
        else:
            self._append([("[系统] " + text, ("mode_System",))])
        self._touch()

    # ---------- 显示 / 隐藏 ----------
    def _touch(self):
        self._last_activity = time.monotonic()
        if self._hidden and self.cfg.get("auto_show", True):
            self._show()

    def _toggle_hide(self):
        if self._hidden:
            self._show()
        else:
            self._hide()

    def _hide(self):
        if not self._hidden:
            self._hidden = True
            self.root.withdraw()

    def _show(self):
        self._hidden = False
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self._apply_alpha()
        self.root.lift()
        if self.cfg.get("auto_move_mouse_on_show", False):
            self.root.update_idletasks()
            self._move_mouse_to_widget(self.root)

    def _apply_alpha(self):
        try:
            alpha = float(self.cfg.get("opacity", 0.9))
            self.root.attributes("-alpha", min(1.0, max(0.2, alpha)))
        except (TypeError, ValueError, tk.TclError):
            pass

    # ---------- 右键菜单 ----------
    def _popup(self, event):
        self._touch()  # 打开菜单也算交互，避免 10 秒自动隐藏正好把窗口收起
        menu = tk.Menu(self.root, tearoff=0)
        message = None
        if getattr(event, "widget", None) is self.text:
            message = self._message_at(event.x, event.y)
        if message is not None:
            menu.add_command(
                label="生成回复", command=lambda: self._generate_reply(message)
            )
            menu.add_command(
                label="复制原文",
                command=lambda: self._copy_text(clean_chat_text(message.get("msg"))),
            )
            menu.add_separator()
        menu.add_command(label="显示 / 隐藏（也可双击标题栏）", command=self._toggle_hide)
        menu.add_command(label="设置", command=self._open_settings)
        menu.add_separator()
        var_topmost = tk.BooleanVar(value=True)
        var_orig = tk.BooleanVar(value=bool(self.cfg.get("show_original", False)))
        var_guard = tk.BooleanVar(value=bool(self.cfg.get("guardian_enabled", False)))
        var_auto = tk.BooleanVar(value=bool(self.cfg.get("guardian_autostart", False)))
        menu.add_checkbutton(label="窗口置顶", variable=var_topmost, command=lambda: self.root.attributes("-topmost", var_topmost.get()))
        menu.add_checkbutton(
            label="同时显示原文", variable=var_orig,
            command=lambda: (self.cfg.update(show_original=var_orig.get()), self.cfg.save()),
        )
        menu.add_separator()
        guard_menu = tk.Menu(menu, tearoff=0)
        guard_menu.add_checkbutton(
            label="启用守护进程", variable=var_guard,
            command=lambda: self._apply_guardian_settings(var_guard.get(), var_auto.get()),
        )
        guard_menu.add_checkbutton(
            label="守护进程开机自启", variable=var_auto,
            command=lambda: self._apply_guardian_settings(var_guard.get(), var_auto.get()),
        )
        menu.add_cascade(label="守护进程", menu=guard_menu)
        menu.add_separator()
        menu.add_command(label="检查更新", command=self._check_update_manual)
        menu.add_command(label="关于", command=self._show_about)
        menu.add_separator()
        menu.add_command(label="退出", command=self._quit)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _show_about(self):
        from wt_translator import __version__

        top, body = self._make_dialog("关于", 420, 300)
        box = tk.Frame(body, bg=BG)
        box.pack(expand=True)
        for line in (
            f"WT 翻译器 v{__version__}",
            "作者：laowangowo",
            f"模型：tencent/Hy-MT2-1.8B（{self.cfg.get('gguf_quant', 'Q4_K_M')}）",
            f"官网：https://wt.ngup.eu.org/",
        ):
            tk.Label(box, text=line, bg=BG, fg=FG, font=self._font(10)).pack(pady=2)
        bar = tk.Frame(body, bg=BG)
        bar.pack(fill="x", pady=(0, 12))
        self._styled_button(bar, "检查更新", self._check_update_manual).pack(side="right", padx=(0, 8))
        self._styled_button(bar, "关闭", top.destroy).pack(side="right", padx=(0, 8))

    # ---------- 更新检查 ----------
    def _make_dialog(self, title, width=420, height=300):
        """创建与主界面同风格的无边框对话框，返回 (top, body_frame)。"""
        family = self.cfg.get("font_family", "Microsoft YaHei UI")
        top = tk.Toplevel(self.root)
        top.title(title)
        top.configure(bg=BG)
        top.resizable(False, False)
        top.attributes("-topmost", True)
        top.overrideredirect(True)  # 隐藏系统标题栏，与主界面一致
        # 默认居中显示，避免首次创建时出现在屏幕左上角而被任务栏/其它窗口遮住
        try:
            screen_w = top.winfo_screenwidth()
            screen_h = top.winfo_screenheight()
            x = max(0, (screen_w - width) // 2)
            y = max(0, (screen_h - height) // 3)
            top.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            top.geometry(f"{width}x{height}")
        header = tk.Frame(top, bg=HEADER_BG, height=34, cursor="fleur")
        header.pack(fill="x")
        header.pack_propagate(False)

        def _on_press(event):
            top._drag_dx = event.x_root - top.winfo_x()
            top._drag_dy = event.y_root - top.winfo_y()

        def _on_motion(event):
            try:
                top.geometry(f"+{event.x_root - top._drag_dx}+{event.y_root - top._drag_dy}")
            except tk.TclError:
                pass

        header.bind("<Button-1>", _on_press, add="+")
        header.bind("<B1-Motion>", _on_motion, add="+")
        tk.Label(
            header, text=title, bg=HEADER_BG, fg=FG, font=(family, 12, "bold"),
        ).pack(side="left", padx=12)
        _HeaderButton(header, "✕", lambda: self._destroy_dialog(top), self._font(9)).pack(side="right", padx=(0, 8))
        body = tk.Frame(top, bg=BG)
        body.pack(fill="both", expand=True)
        top.lift()
        top.focus_force()
        return top, body

    def _styled_button(self, parent, text, command, primary=False):
        family = self.cfg.get("font_family", "Microsoft YaHei UI")
        return tk.Button(
            parent, text=text, command=command,
            bg="#2f7cf6" if primary else HEADER_BG,
            fg="#ffffff" if primary else FG,
            activebackground="#3f8cff" if primary else "#33333d",
            activeforeground="#ffffff",
            relief="flat", bd=0, padx=16, pady=6, cursor="hand2",
            font=(family, 10),
        )

    def _dialog_label(self, parent, text, **kwargs):
        return tk.Label(
            parent, text=text, bg=BG, fg=FG, font=self._font(10),
            justify="left", wraplength=380, **kwargs,
        )

    def _show_info_dialog(self, title, message):
        top, body = self._make_dialog(title, 420, 230)
        bar = tk.Frame(body, bg=BG)
        bar.pack(side="bottom", fill="x", pady=(0, 12))
        self._styled_button(bar, "确定", top.destroy, primary=True).pack(side="right", padx=(0, 12))
        self._dialog_label(body, message).pack(fill="both", expand=True, padx=18, pady=(16, 6))

    def _show_confirm_dialog(self, title, message, on_yes):
        top, body = self._make_dialog(title, 420, 230)
        bar = tk.Frame(body, bg=BG)
        bar.pack(side="bottom", fill="x", pady=(0, 12))
        self._styled_button(bar, "否", top.destroy).pack(side="right", padx=(0, 8))
        self._styled_button(
            bar, "是", lambda: (top.destroy(), on_yes()), primary=True
        ).pack(side="right", padx=(0, 8))
        self._dialog_label(body, message).pack(fill="both", expand=True, padx=18, pady=(16, 6))

    def _check_update_manual(self):
        self.update_status("正在检查更新…")
        from wt_translator.updater import fetch_version_info

        def worker():
            try:
                info = fetch_version_info(
                    str(self.cfg.get("update_check_url", "https://wt.ngup.eu.org/version")),
                    timeout=float(self.cfg.get("update_check_timeout", 8)),
                )
            except Exception as exc:
                self.queue.put({"type": "status", "text": f"检查更新失败：{exc}"})
                self.queue.put({"type": "manual_update_none", "error": str(exc)})
            else:
                self.queue.put({"type": "manual_update_result", "info": info})

        threading.Thread(target=worker, name="update-check-manual", daemon=True).start()

    def _handle_manual_update(self, info):
        from wt_translator import __version__
        from wt_translator.updater import (
            compare_versions,
            should_notify_update,
        )

        version = info.get("version", "")
        skipped = str(self.cfg.get("skipped_update_version", "") or "").strip()
        if version and should_notify_update(__version__, version, skipped):
            self._show_update_dialog(info)
        elif version and compare_versions(__version__, version):
            self._show_info_dialog(
                "检查更新",
                f"版本 v{version} 已被你跳过，不再弹窗提示。\n"
                "出现更新的版本后会再次提醒。",
            )
        else:
            self._show_info_dialog("检查更新", f"当前已是最新版本（{version or '未知'}）")

    def _show_update_dialog(self, info):
        version = info.get("version", "")
        changelog = info.get("changelog", "") or "暂无更新说明"
        update_file = info.get("update_file", "")
        top, body = self._make_dialog(f"发现新版本 v{version}", 460, 360)
        # 按钮栏先打包（底部固定），避免被内容挤掉
        bar = tk.Frame(body, bg=BG)
        bar.pack(side="bottom", fill="x", pady=(0, 12))
        self._styled_button(
            bar, "跳过本次更新", lambda: self._skip_update_version(info, top)
        ).pack(side="left", padx=(8, 0))
        self._styled_button(bar, "稍后", top.destroy).pack(side="right", padx=(0, 8))
        self._styled_button(
            bar, "立即更新", lambda: self._start_update(top, update_file), primary=True
        ).pack(side="right", padx=(0, 8))
        self._dialog_label(body, "更新内容：").pack(anchor="w", padx=14, pady=(12, 4))
        text = tk.Text(
            body, bg="#121217", fg=FG, font=self._font(10), wrap="word", bd=0,
            highlightthickness=1, highlightbackground="#33333d", padx=10, pady=8, height=9,
        )
        text.insert("1.0", changelog)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, padx=14, pady=(0, 8))

    def _skip_update_version(self, info, dialog):
        version = str(info.get("version", "") or "").strip()
        if version:
            self.cfg.update(skipped_update_version=version)
            self.cfg.save()
            self.update_status(f"已跳过版本 v{version}，出现更高版本时再提醒")
        try:
            dialog.destroy()
        except tk.TclError:
            pass

    def _start_update(self, dialog, filename):
        dialog.destroy()
        self.update_status("正在下载更新…")
        from wt_translator.updater import download_update

        protected = sys.executable if getattr(sys, "frozen", False) else None

        def worker():
            try:
                dest = download_update(
                    str(self.cfg.get("update_download_url", "https://wt.ngup.eu.org/update")),
                    filename=filename,
                    progress=lambda done, total: self.queue.put(
                        {"type": "status", "text": f"正在下载更新：{done / total * 100:.0f}%"}
                    ),
                    protected=protected,
                )
            except Exception as exc:
                self.log.exception("更新下载失败")
                self.queue.put({"type": "update_failed", "error": str(exc)})
            else:
                self.queue.put({"type": "update_done", "path": dest})

        threading.Thread(target=worker, name="update-download", daemon=True).start()

    def _update_done(self, path):
        """更新文件即安装包：启动安装程序并退出本程序。"""
        self.update_status(f"更新文件已下载：{path}，正在启动安装程序…")
        try:
            self._open_path(path)
        except Exception as exc:
            self.log.exception("启动安装程序失败")
            self._show_info_dialog("启动失败", f"无法启动安装程序：\n{exc}")
            return
        self._quit()

    def _update_failed(self, error):
        self.update_status(f"更新下载失败：{error}")
        self._show_info_dialog("更新失败", f"下载更新失败：\n{error}")

    def _open_folder(self, path):
        try:
            self._open_path(path)
        except Exception as exc:
            self.log.warning("打开文件夹失败：%s", exc)

    @staticmethod
    def _open_path(path):
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    # ---------- 守护进程 ----------
    def _ask_guardian_setup(self):
        if not self.cfg.get("guardian_ask_on_start", True) or self._stopping:
            return
        top, body = self._make_dialog("守护进程", 460, 280)
        self._dialog_label(
            body,
            "检测到战争雷霆运行时，"
            "由守护进程自动启动翻译器。\n\n是否启用守护进程，并添加到开机自启？",
        ).pack(fill="x", padx=18, pady=(16, 6))
        var_guard = tk.BooleanVar(value=True)
        var_auto = tk.BooleanVar(value=True)
        option = tk.Frame(body, bg=BG)
        option.pack(fill="x", padx=18, pady=4)
        tk.Checkbutton(
            option, text="启用守护进程", variable=var_guard, bg=BG, fg=FG,
            activebackground=BG, activeforeground=FG, selectcolor="#2f2f38",
            font=self._font(10),
        ).pack(anchor="w", pady=2)
        tk.Checkbutton(
            option, text="开机自启", variable=var_auto, bg=BG, fg=FG,
            activebackground=BG, activeforeground=FG, selectcolor="#2f2f38",
            font=self._font(10),
        ).pack(anchor="w", pady=2)
        bar = tk.Frame(body, bg=BG)
        bar.pack(side="bottom", fill="x", pady=(0, 12))
        self._styled_button(bar, "不启用", top.destroy).pack(side="right", padx=(0, 8))
        self._styled_button(
            bar, "确定", lambda: self._confirm_guardian(top, var_guard.get(), var_auto.get()),
            primary=True,
        ).pack(side="right", padx=(0, 8))

    def _confirm_guardian(self, dialog, enabled, autostart):
        dialog.destroy()
        self.cfg.update(guardian_ask_on_start=False)
        self.cfg.save()
        self._apply_guardian_settings(enabled, autostart)

    def _apply_guardian_settings(self, enabled, autostart):
        from guardian import launch_guardian, set_autostart, stop_guardian

        self.cfg.update(guardian_enabled=bool(enabled), guardian_autostart=bool(autostart))
        self.cfg.save()
        try:
            set_autostart(bool(enabled and autostart))
            if enabled:
                launch_guardian()
                self.update_status("守护进程已启用")
            else:
                stop_guardian()
                self.update_status("守护进程已关闭")
        except Exception as exc:
            self.log.warning("守护进程设置失败：%s", exc)
            self.update_status(f"守护进程设置失败：{exc}")

    def _ensure_guardian_running(self):
        if not self.cfg.get("guardian_enabled", False) or self._stopping:
            return
        from guardian import launch_guardian, set_autostart

        try:
            set_autostart(bool(self.cfg.get("guardian_autostart", False)))
            if launch_guardian():
                self.log.info("守护进程已启动")
        except Exception as exc:
            self.log.warning("启动守护进程失败：%s", exc)

    # ---------- 设置 ----------
    def _open_settings(self):
        self._touch()  # 打开设置视为交互，避免自动隐藏与弹窗抢状态
        existing = getattr(self, "_settings_dialog", None)
        if existing is not None:
            try:
                if existing.winfo_exists() and existing.winfo_viewable():
                    existing.lift()
                    existing.attributes("-topmost", True)
                    existing.focus_force()
                    return
            except tk.TclError:
                pass
            try:
                existing.destroy()
            except tk.TclError:
                pass
            self._settings_dialog = None
        top, outer = self._make_dialog("设置", 520, 640)
        self._settings_dialog = top
        family = self.cfg.get("font_family", "Microsoft YaHei UI")

        # 底部固定按钮栏（先打包，保证不被内容挤掉）
        self._settings_bar = tk.Frame(outer, bg=BG)
        self._settings_bar.pack(side="bottom", fill="x", pady=(0, 12))

        # 可滚动内容区
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = tk.Frame(canvas, bg=BG)
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind(
            "<Configure>",
            lambda _e: canvas.itemconfigure(content_window, width=canvas.winfo_width()),
        )
        canvas.bind("<Map>", lambda _e: self._refresh_settings_scrollregion(), add="+")
        self._settings_canvas = canvas
        canvas.bind("<MouseWheel>", self._on_settings_wheel, add="+")
        content.bind("<MouseWheel>", self._on_settings_wheel, add="+")
        self.root.bind_all("<MouseWheel>", self._on_settings_wheel)
        body = content

        hotkey_value = str(self.cfg.get("show_hotkey", "alt") or "").lower()
        if not self.cfg.get("show_on_alt", True):
            hotkey_value = ""
        self._settings_hotkey = hotkey_value
        self._capturing_hotkey = False
        self._settings_voice_hotkey = str(self.cfg.get("voice_hotkey", "f8") or "").lower()
        self._capturing_voice_hotkey = False
        self._settings_quick_hotkey = str(
            self.cfg.get("quick_reply_hotkey", "n") or "n"
        ).lower()
        self._capturing_quick_hotkey = False
        var_width = tk.StringVar(value=str(self.cfg.get("window_width", 460)))
        var_height = tk.StringVar(value=str(self.cfg.get("window_height", 320)))
        var_bg = tk.StringVar(value=str(self.cfg.get("bg_color", DEFAULT_BG) or DEFAULT_BG))
        var_fg = tk.StringVar(value=str(self.cfg.get("fg_color", DEFAULT_FG) or DEFAULT_FG))
        var_hide = tk.StringVar(value=str(self.cfg.get("hide_timeout", 10.0)))
        var_opacity = tk.DoubleVar(value=float(self.cfg.get("opacity", 0.9)))
        var_messages = tk.StringVar(value=str(self.cfg.get("max_messages", 60)))
        var_sender = tk.BooleanVar(value=bool(self.cfg.get("show_sender", True)))
        var_original = tk.BooleanVar(value=bool(self.cfg.get("show_original", False)))
        var_enter = tk.BooleanVar(value=bool(self.cfg.get("show_input_on_enter", True)))
        var_mouse_show = tk.BooleanVar(value=bool(self.cfg.get("auto_move_mouse_on_show", False)))
        var_mouse_input = tk.BooleanVar(value=bool(self.cfg.get("auto_move_mouse_to_input", False)))
        var_show_lang = tk.BooleanVar(value=bool(self.cfg.get("show_source_language", False)))
        var_angry = tk.BooleanVar(value=bool(self.cfg.get("angry_mode", False)))
        var_blacklist_enabled = tk.BooleanVar(value=bool(self.cfg.get("blacklist_enabled", True)))
        var_skip_cn = tk.BooleanVar(value=bool(self.cfg.get("skip_chinese_messages", False)))
        var_quick_enabled = tk.BooleanVar(value=bool(self.cfg.get("quick_reply_enabled", True)))
        var_quick_fg = tk.StringVar(
            value=str(self.cfg.get("quick_menu_fg_color", "#ececef") or "#ececef")
        )
        var_quick_bg = tk.StringVar(
            value=str(self.cfg.get("quick_menu_bg_color", "#23232b") or "#23232b")
        )
        var_quick_opacity = tk.DoubleVar(
            value=float(self.cfg.get("quick_menu_opacity", 0.1) or 0.1)
        )
        var_voice_enabled = tk.BooleanVar(value=bool(self.cfg.get("voice_enabled", False)))
        var_voice_engine = tk.StringVar(
            value=str(self.cfg.get("voice_engine", "sensevoice") or "sensevoice").lower()
        )
        var_voice_mode = tk.StringVar(
            value=str(self.cfg.get("voice_mode", "toggle") or "toggle").lower()
        )
        var_voice_model = tk.StringVar(value=str(self.cfg.get("voice_model", "whisper-1") or ""))
        var_voice_api_base = tk.StringVar(
            value=str(self.cfg.get("voice_api_base_url", "") or "")
        )
        var_voice_api_key = tk.StringVar(value=str(self.cfg.get("voice_api_key", "") or ""))
        var_voice_language = tk.StringVar(
            value=str(self.cfg.get("voice_language", "zh") or "zh")
        )
        var_sensevoice_dir = tk.StringVar(
            value=str(self.cfg.get("sensevoice_dir", "models/sense-voice-small-int8") or "")
        )
        var_engine = tk.StringVar(value=str(self.cfg.get("engine", "llama") or "llama").lower())
        var_api_base = tk.StringVar(value=str(self.cfg.get("api_base_url", "") or ""))
        var_api_key = tk.StringVar(value=str(self.cfg.get("api_key", "") or ""))
        var_api_model = tk.StringVar(value=str(self.cfg.get("api_model", "") or ""))
        var_api_timeout = tk.StringVar(value=str(self.cfg.get("api_timeout", 60)))
        var_api_max_tokens = tk.StringVar(value=str(self.cfg.get("api_max_tokens", 512)))

        rows = []

        def add_row(label_text, widget_factory):
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", padx=18, pady=5)
            tk.Label(
                row, text=label_text, bg=BG, fg=DIM, font=self._font(10), width=14,
                anchor="w",
            ).pack(side="left")
            widget_factory(row).pack(side="left", fill="x", expand=True)

        def add_hotkey_row():
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", padx=18, pady=5)
            tk.Label(
                row, text="呼出按键", bg=BG, fg=DIM, font=self._font(10),
                width=14, anchor="w",
            ).pack(side="left")
            self._hotkey_button = tk.Button(
                row, text=hotkey_display(hotkey_value), bg=HEADER_BG, fg=FG,
                activebackground="#33333d", activeforeground="#ffffff",
                relief="flat", bd=0, padx=10, pady=3, cursor="hand2",
                font=self._font(10),
                command=self._start_hotkey_capture,
            )
            self._hotkey_button.pack(side="left", fill="x", expand=True)
            self._styled_button(
                row, "关闭", self._disable_hotkey, primary=False,
            ).pack(side="left", padx=(8, 0))

        def _engine_choice(parent):
            box = tk.Frame(parent, bg=BG)
            for value, text in (
                ("llama", "本地模型"),
                ("openai", "OpenAI API"),
            ):
                tk.Radiobutton(
                    box, text=text, value=value, variable=var_engine,
                    bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                    selectcolor="#2f2f38", font=self._font(10),
                ).pack(side="left", padx=(0, 16))
            return box

        def _color_choice(parent, var):
            box = tk.Frame(parent, bg=BG)
            entry = tk.Entry(
                box, textvariable=var, bg="#121217", fg=FG,
                insertbackground=FG, relief="flat", font=self._font(10), width=10,
            )
            entry.pack(side="left", fill="x", expand=True)

            def pick():
                chosen = colorchooser.askcolor(color=var.get() or DEFAULT_BG, parent=top)
                if chosen and chosen[1]:
                    var.set(chosen[1])

            tk.Button(
                box, text="选择", command=pick, bg=HEADER_BG, fg=FG,
                activebackground="#33333d", activeforeground="#ffffff",
                relief="flat", bd=0, padx=10, pady=2, cursor="hand2",
                font=self._font(9),
            ).pack(side="left", padx=(8, 0))
            return box

        add_hotkey_row()
        add_row(
            "翻译引擎",
            lambda row: _engine_choice(row),
        )
        api_header = tk.Label(
            body, text="OpenAI 兼容 API（切换为 API 后生效）", bg=BG, fg=DIM,
            font=self._font(10), anchor="w",
        )
        api_header.pack(fill="x", padx=18, pady=(8, 0))

        def add_api_row(label_text, widget_factory):
            row = tk.Frame(api_box, bg=BG)
            row.pack(fill="x", padx=18, pady=3)
            tk.Label(
                row, text=label_text, bg=BG, fg=DIM, font=self._font(10),
                width=14, anchor="w",
            ).pack(side="left")
            widget_factory(row).pack(side="left", fill="x", expand=True)

        self._settings_api_frame = tk.Frame(body, bg=BG)
        self._settings_api_frame.pack(fill="x")
        api_box = self._settings_api_frame
        add_api_row(
            "API 地址",
            lambda row: tk.Entry(
                row, textvariable=var_api_base, bg="#121217", fg=FG,
                insertbackground=FG, relief="flat", font=self._font(10),
                width=10,
            ),
        )
        add_api_row(
            "API 密钥",
            lambda row: tk.Entry(
                row, textvariable=var_api_key, show="•", bg="#121217", fg=FG,
                insertbackground=FG, relief="flat", font=self._font(10),
                width=10,
            ),
        )
        add_api_row(
            "模型名称",
            lambda row: tk.Entry(
                row, textvariable=var_api_model, bg="#121217", fg=FG,
                insertbackground=FG, relief="flat", font=self._font(10),
                width=10,
            ),
        )
        add_api_row(
            "请求超时(秒)",
            lambda row: tk.Entry(
                row, textvariable=var_api_timeout, bg="#121217", fg=FG,
                insertbackground=FG, relief="flat", font=self._font(10),
                width=8,
            ),
        )
        add_api_row(
            "最大Token",
            lambda row: tk.Entry(
                row, textvariable=var_api_max_tokens, bg="#121217", fg=FG,
                insertbackground=FG, relief="flat", font=self._font(10),
                width=8,
            ),
        )

        def set_state(widget, state):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
            for child in widget.winfo_children():
                set_state(child, state)

        def _sync_api_mode(*_args):
            api_mode = var_engine.get().lower() in ("openai", "api")
            set_state(self._settings_api_frame, "normal" if api_mode else "disabled")
            self._refresh_settings_scrollregion()

        var_engine.trace_add("write", _sync_api_mode)
        _sync_api_mode()
        add_row("窗口宽度", lambda row: tk.Entry(
            row, textvariable=var_width, bg="#121217", fg=FG,
            insertbackground=FG, relief="flat", font=self._font(10), width=8,
        ))
        add_row("窗口高度", lambda row: tk.Entry(
            row, textvariable=var_height, bg="#121217", fg=FG,
            insertbackground=FG, relief="flat", font=self._font(10), width=8,
        ))
        add_row("自动隐藏(秒)", lambda row: tk.Entry(
            row, textvariable=var_hide, bg="#121217", fg=FG,
            insertbackground=FG, relief="flat", font=self._font(10), width=8,
        ))
        add_row("消息条数", lambda row: tk.Entry(
            row, textvariable=var_messages, bg="#121217", fg=FG,
            insertbackground=FG, relief="flat", font=self._font(10), width=8,
        ))
        add_row("透明度", lambda row: tk.Scale(
            row, from_=0.2, to=1.0, resolution=0.05, orient="horizontal",
            variable=var_opacity, bg=BG, fg=FG, highlightthickness=0,
            troughcolor="#2f2f38", activebackground="#2f7cf6",
        ))
        add_row("背景颜色", lambda row: _color_choice(row, var_bg))
        add_row("文字颜色", lambda row: _color_choice(row, var_fg))

        option_frame = tk.Frame(body, bg=BG)
        option_frame.pack(fill="x", padx=18, pady=5)
        option_items = (
            ("显示发送者", var_sender),
            ("同时显示原文", var_original),
            ("回车弹出输入框", var_enter),
            ("呼出窗口移动鼠标", var_mouse_show),
            ("输入框移动鼠标", var_mouse_input),
            ("启用黑名单", var_blacklist_enabled),
            ("不翻译中文", var_skip_cn),
            ("愤怒模式", var_angry),
            ("显示原文语言", var_show_lang),
            ("启用快捷回复", var_quick_enabled),
            ("启用语音输入", var_voice_enabled),
        )
        for index, (text, var) in enumerate(option_items):
            row, col = divmod(index, 2)
            tk.Checkbutton(
                option_frame, text=text, variable=var, bg=BG, fg=FG,
                activebackground=BG, activeforeground=FG, selectcolor="#2f2f38",
                font=self._font(10),
            ).grid(row=row, column=col, sticky="w", padx=(0, 16), pady=2)
        for column in range(2):
            option_frame.grid_columnconfigure(column, weight=1)

        voice_header = tk.Frame(body, bg=BG)
        voice_header.pack(fill="x", padx=18, pady=(10, 2))
        tk.Label(
            voice_header, text="语音输入（OpenAI 兼容 /audio/transcriptions）", bg=BG, fg=DIM,
            font=self._font(10), anchor="w",
        ).pack(side="left")
        self._settings_voice_frame = tk.Frame(body, bg=BG)
        self._settings_voice_frame.pack(fill="x")
        voice_box = self._settings_voice_frame

        voice_engine_row = tk.Frame(voice_box, bg=BG)
        voice_engine_row.pack(fill="x", padx=18, pady=3)
        tk.Label(
            voice_engine_row, text="语音引擎", bg=BG, fg=DIM, font=self._font(10),
            width=14, anchor="w",
        ).pack(side="left")
        voice_engine_box = tk.Frame(voice_engine_row, bg=BG)
        voice_engine_box.pack(side="left")
        for value, text in (("sensevoice", "本地 SenseVoice"), ("api", "OpenAI API")):
            tk.Radiobutton(
                voice_engine_box, text=text, value=value, variable=var_voice_engine,
                bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                selectcolor="#2f2f38", font=self._font(10),
            ).pack(side="left", padx=(0, 14))

        voice_mode_row = tk.Frame(voice_box, bg=BG)
        voice_mode_row.pack(fill="x", padx=18, pady=3)
        tk.Label(
            voice_mode_row, text="语音触发", bg=BG, fg=DIM, font=self._font(10),
            width=14, anchor="w",
        ).pack(side="left")
        voice_mode_box = tk.Frame(voice_mode_row, bg=BG)
        voice_mode_box.pack(side="left")
        for value, text in (
            ("hold", "按住说话（松开停止）"),
            ("toggle", "按一次开始 / 再按一次停止"),
        ):
            tk.Radiobutton(
                voice_mode_box, text=text, value=value, variable=var_voice_mode,
                bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                selectcolor="#2f2f38", font=self._font(10),
            ).pack(side="left", padx=(0, 14))

        def add_voice_row(parent, label_text, widget_factory):
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", padx=18, pady=3)
            tk.Label(
                row, text=label_text, bg=BG, fg=DIM, font=self._font(10),
                width=14, anchor="w",
            ).pack(side="left")
            widget_factory(row).pack(side="left", fill="x", expand=True)

        voice_hotkey_row = tk.Frame(voice_box, bg=BG)
        voice_hotkey_row.pack(fill="x", padx=18, pady=3)
        tk.Label(
            voice_hotkey_row, text="语音按键", bg=BG, fg=DIM, font=self._font(10),
            width=14, anchor="w",
        ).pack(side="left")
        self._voice_hotkey_button = tk.Button(
            voice_hotkey_row, text=hotkey_display(self._settings_voice_hotkey),
            bg=HEADER_BG, fg=FG, activebackground="#33333d", activeforeground="#ffffff",
            relief="flat", bd=0, padx=10, pady=3, cursor="hand2", font=self._font(10),
            command=self._start_voice_hotkey_capture,
        )
        self._voice_hotkey_button.pack(side="left", fill="x", expand=True)
        self._styled_button(
            voice_hotkey_row, "关闭", self._disable_voice_hotkey, primary=False,
        ).pack(side="left", padx=(8, 0))

        self._settings_voice_local_frame = tk.Frame(voice_box, bg=BG)
        self._settings_voice_local_frame.pack(fill="x")
        add_voice_row(
            self._settings_voice_local_frame,
            "模型目录",
            lambda row: tk.Entry(
                row, textvariable=var_sensevoice_dir, bg="#121217", fg=FG,
                insertbackground=FG, relief="flat", font=self._font(10), width=10,
            ),
        )
        voice_download_row = tk.Frame(self._settings_voice_local_frame, bg=BG)
        voice_download_row.pack(fill="x", padx=18, pady=3)
        tk.Label(
            voice_download_row, text="", bg=BG, width=14,
        ).pack(side="left")
        self._styled_button(
            voice_download_row, "下载/检查本地模型", self._download_sensevoice_model,
        ).pack(side="left")

        self._settings_voice_api_frame = tk.Frame(voice_box, bg=BG)
        self._settings_voice_api_frame.pack(fill="x")
        add_voice_row(
            self._settings_voice_api_frame,
            "识别模型",
            lambda row: tk.Entry(
                row, textvariable=var_voice_model, bg="#121217", fg=FG,
                insertbackground=FG, relief="flat", font=self._font(10), width=10,
            ),
        )
        add_voice_row(
            self._settings_voice_api_frame,
            "API 地址",
            lambda row: tk.Entry(
                row, textvariable=var_voice_api_base, bg="#121217", fg=FG,
                insertbackground=FG, relief="flat", font=self._font(10), width=10,
            ),
        )
        add_voice_row(
            self._settings_voice_api_frame,
            "API 密钥",
            lambda row: tk.Entry(
                row, textvariable=var_voice_api_key, show="•", bg="#121217", fg=FG,
                insertbackground=FG, relief="flat", font=self._font(10), width=10,
            ),
        )
        add_voice_row(
            self._settings_voice_api_frame,
            "识别语言",
            lambda row: tk.Entry(
                row, textvariable=var_voice_language, bg="#121217", fg=FG,
                insertbackground=FG, relief="flat", font=self._font(10), width=8,
            ),
        )

        def _sync_voice_engine(*_args):
            api_mode = var_voice_engine.get().lower() == "api"
            set_state(self._settings_voice_local_frame, "disabled" if api_mode else "normal")
            set_state(self._settings_voice_api_frame, "normal" if api_mode else "disabled")
            self._refresh_settings_scrollregion()

        def _sync_voice_mode(*_args):
            enabled = bool(var_voice_enabled.get())
            set_state(self._settings_voice_frame, "normal" if enabled else "disabled")
            self._refresh_settings_scrollregion()

        var_voice_engine.trace_add("write", _sync_voice_engine)
        var_voice_enabled.trace_add("write", _sync_voice_mode)
        _sync_voice_engine()
        _sync_voice_mode()

        bl_header = tk.Frame(body, bg=BG)
        bl_header.pack(fill="x", padx=18, pady=(10, 2))
        tk.Label(
            bl_header, text="消息黑名单（每行一条正则，命中不翻译）", bg=BG, fg=DIM,
            font=self._font(10), anchor="w",
        ).pack(side="left")
        self._settings_blacklist = tk.Text(
            body, bg="#121217", fg=FG, font=self._font(10), wrap="word", bd=0,
            highlightthickness=1, highlightbackground="#33333d", padx=8, pady=5,
            height=4,
        )
        self._settings_blacklist.insert("1.0", "\n".join(str(e) for e in self.cfg.get("blacklist", [])))
        self._settings_blacklist.pack(fill="x", padx=18)

        gloss_header = tk.Frame(body, bg=BG)
        gloss_header.pack(fill="x", padx=18, pady=(8, 2))
        tk.Label(
            gloss_header, text="术语表（每行一条：原文=译文）", bg=BG, fg=DIM,
            font=self._font(10), anchor="w",
        ).pack(side="left")
        self._styled_button(
            gloss_header, "检查术语表更新", self._check_glossary_update,
        ).pack(side="right")
        self._settings_glossary = tk.Text(
            body, bg="#121217", fg=FG, font=self._font(10), wrap="word", bd=0,
            highlightthickness=1, highlightbackground="#33333d", padx=8, pady=5,
            height=6,
        )
        from wt_translator.glossary import read as read_glossary

        self._settings_glossary.insert("1.0", "\n".join(read_glossary().get("terms", [])))
        self._settings_glossary.pack(fill="x", padx=18)

        quick_header = tk.Frame(body, bg=BG)
        quick_header.pack(fill="x", padx=18, pady=(8, 2))
        tk.Label(
            quick_header, text="快捷回复（每行一条，最多 9 条；按键见下方）",
            bg=BG, fg=DIM, font=self._font(10), anchor="w",
        ).pack(side="left")
        quick_key_row = tk.Frame(body, bg=BG)
        quick_key_row.pack(fill="x", padx=18, pady=5)
        tk.Label(
            quick_key_row, text="快捷回复按键", bg=BG, fg=DIM, font=self._font(10),
            width=14, anchor="w",
        ).pack(side="left")
        self._quick_hotkey_button = tk.Button(
            quick_key_row, text=hotkey_display(self._settings_quick_hotkey),
            bg=HEADER_BG, fg=FG, activebackground="#33333d", activeforeground="#ffffff",
            relief="flat", bd=0, padx=10, pady=3, cursor="hand2", font=self._font(10),
            command=self._start_quick_hotkey_capture,
        )
        self._quick_hotkey_button.pack(side="left", fill="x", expand=True)
        self._styled_button(
            quick_key_row, "关闭", self._disable_quick_hotkey, primary=False,
        ).pack(side="left", padx=(8, 0))
        self._settings_quick_replies = tk.Text(
            body, bg="#121217", fg=FG, font=self._font(10), wrap="word", bd=0,
            highlightthickness=1, highlightbackground="#33333d", padx=8, pady=5,
            height=5,
        )
        quick_items = self.cfg.get("quick_replies", DEFAULT_QUICK_REPLIES)
        if not isinstance(quick_items, (list, tuple)):
            quick_items = DEFAULT_QUICK_REPLIES
        self._settings_quick_replies.insert(
            "1.0", "\n".join(str(item) for item in list(quick_items)[:9])
        )
        self._settings_quick_replies.pack(fill="x", padx=18)
        add_row("文字颜色", lambda row: _color_choice(row, var_quick_fg))
        add_row("背景颜色", lambda row: _color_choice(row, var_quick_bg))
        add_row("透明度", lambda row: tk.Scale(
            row, from_=0.05, to=1.0, resolution=0.05, orient="horizontal",
            variable=var_quick_opacity, bg=BG, fg=FG, highlightthickness=0,
            troughcolor="#2f2f38", activebackground="#2f7cf6",
        ))

        self._refresh_settings_scrollregion()
        top.after_idle(self._refresh_settings_scrollregion)
        for delay in (50, 150, 300, 600):
            top.after(delay, self._refresh_settings_scrollregion)

        bar = self._settings_bar
        self._styled_button(
            bar, "取消", lambda: self._close_settings_dialog(top)
        ).pack(side="right", padx=(0, 8))
        self._styled_button(
            bar, "保存",
            lambda: self._save_settings(
                top,
                var_width, var_height, var_hide,
                var_opacity, var_messages, var_sender, var_original, var_enter,
                var_mouse_show, var_mouse_input,
                var_bg, var_fg, var_show_lang, var_angry,
                var_blacklist_enabled, var_skip_cn, var_quick_enabled,
                var_quick_fg, var_quick_bg, var_quick_opacity,
                var_voice_enabled, var_voice_model, var_voice_api_base,
                var_voice_api_key, var_voice_language,
                var_voice_engine, var_voice_mode, var_sensevoice_dir,
                var_engine, var_api_base, var_api_key, var_api_model,
                var_api_timeout, var_api_max_tokens,
            ),
            primary=True,
        ).pack(side="right", padx=(0, 8))
        # 内容全部构建完成后再次确保窗口在最前且已映射，避免首次打开时未成功上屏
        top.after_idle(lambda: (top.lift(), top.attributes("-topmost", True), top.focus_force(), top.deiconify()))

    def _save_settings(
        self, dialog,
        var_width, var_height, var_hide,
        var_opacity, var_messages, var_sender, var_original, var_enter,
        var_mouse_show, var_mouse_input,
        var_bg, var_fg, var_show_lang, var_angry,
        var_blacklist_enabled, var_skip_cn, var_quick_enabled,
        var_quick_fg, var_quick_bg, var_quick_opacity,
        var_voice_enabled, var_voice_model, var_voice_api_base,
        var_voice_api_key, var_voice_language,
        var_voice_engine, var_voice_mode, var_sensevoice_dir,
        var_engine, var_api_base, var_api_key, var_api_model,
        var_api_timeout, var_api_max_tokens,
    ):
        def parse_int(value, default, minimum, maximum):
            try:
                number = int(float(value))
                return max(minimum, min(maximum, number))
            except (TypeError, ValueError):
                return default

        try:
            opacity = max(0.2, min(1.0, float(var_opacity.get())))
            hide = max(0.0, min(86400.0, float(var_hide.get())))
        except (TypeError, ValueError):
            opacity = 0.9
            hide = 10.0

        glossary = []
        try:
            raw_lines = self._settings_glossary.get("1.0", "end-1c").splitlines()
        except (AttributeError, tk.TclError):
            raw_lines = []
        for line in raw_lines:
            line = line.strip()
            if line:
                glossary.append(line)

        blacklist_lines = []
        try:
            raw_blacklist = self._settings_blacklist.get("1.0", "end-1c").splitlines()
        except (AttributeError, tk.TclError):
            raw_blacklist = []
        for line in raw_blacklist:
            line = line.strip()
            if line:
                blacklist_lines.append(line)

        quick_lines = []
        try:
            raw_quick = self._settings_quick_replies.get("1.0", "end-1c").splitlines()
        except (AttributeError, tk.TclError):
            raw_quick = []
        for line in raw_quick:
            line = line.strip()
            if line and len(quick_lines) < 9:
                quick_lines.append(line)

        from wt_translator.glossary import read as read_glossary
        from wt_translator.glossary import write as write_glossary

        current = read_glossary()
        current["terms"] = glossary
        try:
            write_glossary(current)
        except OSError as exc:
            self.update_status(f"术语表保存失败：{exc}")

        hotkey = str(getattr(self, "_settings_hotkey", "") or "").lower()
        engine_raw = str(var_engine.get() or "llama").strip().lower()
        engine = "openai" if engine_raw in ("openai", "api", "remote") else "llama"
        api_base = str(var_api_base.get() or "").strip()
        api_key = str(var_api_key.get() or "").strip()
        api_model = str(var_api_model.get() or "").strip()
        bg_color = str(var_bg.get() or DEFAULT_BG).strip() or DEFAULT_BG
        fg_color = str(var_fg.get() or DEFAULT_FG).strip() or DEFAULT_FG
        quick_fg_color = str(var_quick_fg.get() or "#ececef").strip() or "#ececef"
        quick_bg_color = str(var_quick_bg.get() or "#23232b").strip() or "#23232b"
        try:
            quick_opacity = max(0.05, min(1.0, float(var_quick_opacity.get())))
        except (TypeError, ValueError):
            quick_opacity = 0.1
        voice_hotkey = str(getattr(self, "_settings_voice_hotkey", "") or "").lower()

        self.cfg.update(
            engine=engine,
            api_base_url=api_base,
            api_key=api_key,
            api_model=api_model,
            api_timeout=parse_int(var_api_timeout.get(), 60, 5, 600),
            api_max_tokens=parse_int(var_api_max_tokens.get(), 512, 16, 8192),
            show_on_alt=bool(hotkey),
            show_hotkey=hotkey,
            window_width=parse_int(var_width.get(), 460, 220, 4000),
            window_height=parse_int(var_height.get(), 320, 120, 3000),
            hide_timeout=hide,
            opacity=opacity,
            max_messages=parse_int(var_messages.get(), 60, 10, 500),
            show_sender=bool(var_sender.get()),
            show_original=bool(var_original.get()),
            show_input_on_enter=bool(var_enter.get()),
            auto_move_mouse_on_show=bool(var_mouse_show.get()),
            auto_move_mouse_to_input=bool(var_mouse_input.get()),
            bg_color=bg_color,
            fg_color=fg_color,
            show_source_language=bool(var_show_lang.get()),
            angry_mode=bool(var_angry.get()),
            blacklist=blacklist_lines,
            blacklist_enabled=bool(var_blacklist_enabled.get()),
            skip_chinese_messages=bool(var_skip_cn.get()),
            quick_reply_enabled=bool(var_quick_enabled.get()),
            quick_reply_hotkey=str(
                getattr(self, "_settings_quick_hotkey", "n") or ""
            ).lower(),
            quick_replies=quick_lines or DEFAULT_QUICK_REPLIES,
            quick_menu_fg_color=quick_fg_color,
            quick_menu_bg_color=quick_bg_color,
            quick_menu_opacity=quick_opacity,
            voice_enabled=bool(var_voice_enabled.get()),
            voice_engine=str(var_voice_engine.get() or "sensevoice").lower(),
            voice_mode=str(var_voice_mode.get() or "toggle").lower(),
            voice_hotkey=voice_hotkey,
            voice_model=str(var_voice_model.get() or "").strip(),
            voice_api_base_url=str(var_voice_api_base.get() or "").strip(),
            voice_api_key=str(var_voice_api_key.get() or "").strip(),
            voice_language=str(var_voice_language.get() or "zh").strip() or "zh",
            sensevoice_dir=str(var_sensevoice_dir.get() or "").strip()
            or "models/sense-voice-small-int8",
        )
        self.cfg.save()
        self._close_settings_dialog(dialog)
        self._apply_ui_settings()
        engine_label = "OpenAI API" if engine == "openai" else "本地模型"
        self.update_status(f"设置已保存，翻译引擎：{engine_label}（后续消息生效）")

    def _close_settings_dialog(self, dialog):
        self._capturing_hotkey = False
        self._capturing_voice_hotkey = False
        self._capturing_quick_hotkey = False
        if self._settings_dialog is dialog:
            self._settings_dialog = None
        try:
            self.root.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        try:
            dialog.destroy()
        except tk.TclError:
            pass

    def _destroy_dialog(self, top):
        """关闭对话框：设置页走完整清理，其它对话框直接销毁。"""
        if getattr(self, "_settings_dialog", None) is top:
            self._close_settings_dialog(top)
        else:
            try:
                top.destroy()
            except tk.TclError:
                pass

    def _on_settings_wheel(self, event):
        try:
            canvas = self._settings_canvas
            if canvas.winfo_exists():
                steps = int(-event.delta / 120) * 3
                canvas.yview_scroll(steps, "units")
        except Exception:
            pass

    def _refresh_settings_scrollregion(self):
        try:
            canvas = self._settings_canvas
            if not canvas.winfo_exists():
                return
            canvas.update_idletasks()
            region = canvas.bbox("all")
            if region:
                canvas.configure(scrollregion=region)
        except Exception:
            pass

    # ---------- 术语表更新检查 ----------
    def _check_glossary_update(self):
        from wt_translator.glossary import fetch_remote

        url = str(self.cfg.get("glossary_update_url", "") or "").strip()
        if not url:
            self._show_info_dialog("术语表更新", "未配置术语表更新地址")
            return
        self.update_status("正在检查术语表更新…")

        def worker():
            try:
                remote = fetch_remote(url)
            except Exception as exc:
                self.queue.put({"type": "glossary_update_failed", "error": str(exc)})
            else:
                self.queue.put({"type": "glossary_update_result", "remote": remote})

        threading.Thread(target=worker, name="glossary-update-check", daemon=True).start()

    def _on_glossary_update_result(self, remote):
        from wt_translator.glossary import read as read_glossary

        local = read_glossary()
        if remote["version"] <= local["version"]:
            self._show_info_dialog(
                "术语表更新",
                f"当前已是最新术语表（版本 {local['version']}，{len(local['terms'])} 条）。",
            )
            return
        self._show_confirm_dialog(
            "发现术语表更新",
            f"服务器术语表 v{remote['version']}（{len(remote['terms'])} 条）"
            f"高于本地 v{local['version']}（{len(local['terms'])} 条）。\n\n"
            "是否应用更新？（将覆盖本地术语表）",
            on_yes=lambda: self._apply_glossary_remote(remote),
        )

    def _apply_glossary_remote(self, remote):
        from wt_translator.glossary import write as write_glossary

        try:
            write_glossary(remote)
        except OSError as exc:
            self._show_info_dialog("术语表更新", f"保存失败：{exc}")
            return
        self._refresh_glossary_text()
        self.update_status(f"术语表已更新到 v{remote['version']}")

    def _refresh_glossary_text(self):
        from wt_translator.glossary import read as read_glossary

        if not hasattr(self, "_settings_glossary"):
            return
        try:
            self._settings_glossary.delete("1.0", "end")
            self._settings_glossary.insert("1.0", "\n".join(read_glossary().get("terms", [])))
        except tk.TclError:
            pass

    # ---------- 呼出按键录制 ----------
    def _start_hotkey_capture(self):
        if getattr(self, "_capturing_hotkey", False):
            return
        self._capturing_hotkey = True
        if hasattr(self, "_hotkey_button"):
            self._hotkey_button.configure(text="请按下按键…（Esc 取消）")
        threading.Thread(
            target=self._capture_hotkey_worker, args=("show_hotkey",),
            name="hotkey-capture", daemon=True,
        ).start()

    def _start_voice_hotkey_capture(self):
        if getattr(self, "_capturing_voice_hotkey", False):
            return
        self._capturing_voice_hotkey = True
        if hasattr(self, "_voice_hotkey_button"):
            self._voice_hotkey_button.configure(text="请按下按键…（Esc 取消）")
        threading.Thread(
            target=self._capture_hotkey_worker, args=("voice_hotkey",),
            name="voice-hotkey-capture", daemon=True,
        ).start()

    def _start_quick_hotkey_capture(self):
        if getattr(self, "_capturing_quick_hotkey", False):
            return
        self._capturing_quick_hotkey = True
        if hasattr(self, "_quick_hotkey_button"):
            self._quick_hotkey_button.configure(text="请按下按键…（Esc 取消）")
        threading.Thread(
            target=self._capture_hotkey_worker, args=("quick_hotkey",),
            name="quick-hotkey-capture", daemon=True,
        ).start()

    def _disable_hotkey(self):
        self._settings_hotkey = ""
        if hasattr(self, "_hotkey_button"):
            self._hotkey_button.configure(text="未设置")

    def _disable_voice_hotkey(self):
        self._settings_voice_hotkey = ""
        if hasattr(self, "_voice_hotkey_button"):
            self._voice_hotkey_button.configure(text="未设置")

    def _disable_quick_hotkey(self):
        self._settings_quick_hotkey = ""
        if hasattr(self, "_quick_hotkey_button"):
            self._quick_hotkey_button.configure(text="未设置")

    def _capture_hotkey_worker(self, target="show_hotkey"):
        """全局轮询下一次按键（最长 20 秒），Esc 取消。"""

        def state(vk):
            try:
                return bool(_read_key_state(vk)())
            except Exception:
                return False

        esc_vk = 0x1B
        prev = {label: state(vk) for label, vk in HOTKEY_CAPTURE_CANDIDATES}
        esc_prev = state(esc_vk)
        deadline = time.time() + 20
        time.sleep(0.15)
        if target == "quick_hotkey":
            capturing = lambda: self._capturing_quick_hotkey
        elif target == "voice_hotkey":
            capturing = lambda: self._capturing_voice_hotkey
        else:
            capturing = lambda: self._capturing_hotkey
        while time.time() < deadline and capturing():
            esc_down = state(esc_vk)
            if esc_down and not esc_prev:
                self.queue.put({"type": "hotkey_captured", "target": target, "key": None})
                return
            esc_prev = esc_down
            for label, vk in HOTKEY_CAPTURE_CANDIDATES:
                down = state(vk)
                if down and not prev[label]:
                    self.queue.put({"type": "hotkey_captured", "target": target, "key": label})
                    return
                prev[label] = down
            time.sleep(0.02)
        self.queue.put({"type": "hotkey_captured", "target": target, "key": None})

    def _on_hotkey_captured(self, key, target="show_hotkey"):
        if target == "quick_hotkey":
            self._capturing_quick_hotkey = False
            button = getattr(self, "_quick_hotkey_button", None)
            if button is None:
                return
            if key:
                self._settings_quick_hotkey = str(key).lower()
                button.configure(text=hotkey_display(key))
                self.update_status(f"快捷回复按键已设为 {hotkey_display(key)}，保存后生效")
            else:
                button.configure(text=hotkey_display(self._settings_quick_hotkey))
            return
        if target == "voice_hotkey":
            self._capturing_voice_hotkey = False
            button = getattr(self, "_voice_hotkey_button", None)
            if button is None:
                return
            if key:
                self._settings_voice_hotkey = str(key).lower()
                button.configure(text=hotkey_display(key))
                self.update_status(f"语音按键已设为 {hotkey_display(key)}，保存后生效")
            else:
                button.configure(text=hotkey_display(self._settings_voice_hotkey))
            return
        self._capturing_hotkey = False
        if not hasattr(self, "_hotkey_button"):
            return
        if key:
            label = str(key)
            self._settings_hotkey = label.lower()
            self._hotkey_button.configure(text=hotkey_display(label))
            self.update_status(f"呼出按键已设为 {hotkey_display(label)}，保存后生效")
        else:
            self._hotkey_button.configure(text=hotkey_display(self._settings_hotkey))

    def _apply_ui_settings(self):
        width = max(220, int(self.cfg.get("window_width", 460)))
        height = max(120, int(self.cfg.get("window_height", 320)))
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        try:
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            pass
        self._apply_alpha()
        self._apply_colors()
        try:
            self.text.configure(font=self._font())
            self.input_entry.configure(font=self._font(10))
        except Exception:
            pass
        self._stop_watchers()
        self._start_watchers()

    # ---------- 翻译输入框 ----------
    def _on_enter_global(self):
        """窗口可见时按回车 -> 显示底部输入框。"""
        if self._input_locked():
            self.update_status("正在处理语音/回复，请稍候…")
            return
        if self._hidden or self._input_visible:
            return
        self._show_input()

    def _show_input(self):
        if self._input_visible:
            return
        self._input_visible = True
        self.input_frame.pack(fill="x", side="bottom", before=self.text)
        self.input_entry.delete(0, "end")
        if self._input_locked():
            self.input_entry.configure(state="disabled")
            return
        self.input_entry.configure(state="normal")
        self.input_entry.focus_force()
        if self.cfg.get("auto_move_mouse_to_input", False):
            self.root.update_idletasks()
            self._move_mouse_to_widget(self.input_entry)
        self._touch()

    def _move_mouse_to_widget(self, widget):
        """把鼠标指针移动到控件中心（Windows）。"""
        try:
            x = widget.winfo_rootx() + widget.winfo_width() // 2
            y = widget.winfo_rooty() + widget.winfo_height() // 2
            if os.name == "nt":
                import ctypes

                ctypes.windll.user32.SetCursorPos(int(x), int(y))
            elif sys.platform == "darwin":
                from Quartz import CGWarpMouseCursorPosition  # type: ignore

                CGWarpMouseCursorPosition((x, y))
        except Exception:
            pass

    # ---------- 语音输入 ----------
    def _on_voice_toggle(self):
        if self._voice_recording:
            self._stop_voice_recording()
        else:
            self._start_voice_recording()

    def _input_locked(self):
        """录音/识别/生成回复期间禁止用户输入。"""
        return self._voice_busy or self._reply_busy

    def _refresh_input_lock(self):
        if not self._input_visible:
            return
        try:
            if self._input_locked():
                self.input_entry.configure(state="disabled")
            else:
                self.input_entry.configure(state="normal")
        except tk.TclError:
            pass

    def _set_voice_busy(self, busy):
        """语音录音/识别期间禁用输入框，避免用户同时输入。"""
        self._voice_busy = bool(busy)
        self._refresh_input_lock()

    def _set_reply_busy(self, busy):
        self._reply_busy = bool(busy)
        self._refresh_input_lock()

    def _on_voice_press(self):
        self._start_voice_recording()

    def _on_voice_release(self):
        if str(self.cfg.get("voice_mode", "toggle")).lower() == "hold":
            self._stop_voice_recording()

    def _start_voice_recording(self):
        if not self.cfg.get("voice_enabled", False) or self._voice_recording:
            return False
        from wt_translator.voice import VoiceRecorder

        try:
            recorder = VoiceRecorder(
                self.log, max_seconds=self.cfg.get("voice_max_seconds", 60)
            )
            recorder.start()
        except Exception as exc:
            self.update_status(f"无法开始录音：{exc}")
            return False
        self._voice_recorder = recorder
        self._voice_recording = True
        self._set_voice_busy(True)
        hold_mode = str(self.cfg.get("voice_mode", "toggle")).lower() == "hold"
        self.update_status(
            "正在录音，松开语音键结束…" if hold_mode else "正在录音，再按一次语音键结束…"
        )
        self._touch()
        return True

    def _stop_voice_recording(self):
        if not self._voice_recording:
            return False
        from wt_translator.voice import transcribe

        recorder = self._voice_recorder
        self._voice_recorder = None
        self._voice_recording = False
        try:
            path = recorder.stop() if recorder is not None else ""
        except Exception as exc:
            self.update_status(f"语音录制失败：{exc}")
            self._set_voice_busy(False)
            return False
        if not path:
            self.update_status("语音录制失败：没有拿到录音")
            self._set_voice_busy(False)
            return False
        self.update_status("录音结束，正在识别…")
        self._set_voice_busy(True)

        def worker():
            try:
                engine = str(
                    self.cfg.get("voice_engine", "sensevoice") or "sensevoice"
                ).lower()
                if engine == "api":
                    text = transcribe(path, self.cfg, self.log)
                else:
                    from wt_translator.voice import SenseVoiceASR

                    if self._asr is None:
                        self._asr = SenseVoiceASR(self.cfg, self.log)
                    self._asr.ensure_downloaded(
                        progress=lambda msg: self.queue.put(
                            {"type": "status", "text": msg}
                        )
                    )
                    text = self._asr.transcribe(path)
            except Exception as exc:
                self.queue.put({"type": "voice_failed", "error": str(exc)})
            else:
                self.queue.put({"type": "voice_result", "text": text})
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass

        threading.Thread(target=worker, name="voice-transcribe", daemon=True).start()
        return True

    def _voice_result(self, text):
        text = str(text or "").strip()
        self._set_voice_busy(False)
        if not text:
            self.update_status("语音识别结果为空")
            return
        self._show_input()
        self.input_entry.delete(0, "end")
        self.input_entry.insert(0, text)
        self.input_entry.select_range(0, "end")
        self.input_entry.focus_force()
        self.update_status("语音识别完成，按回车翻译")
        self._touch()

    def _voice_failed(self, error):
        self._set_voice_busy(False)
        self.update_status(f"语音识别失败：{error}")

    # ---------- 生成回复 ----------
    def _copy_text(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(text or ""))
        except tk.TclError as exc:
            self.log.warning("复制到剪贴板失败：%s", exc)

    def _generate_reply(self, message):
        if self._reply_busy:
            self.update_status("正在生成回复，请稍候…")
            return
        if self.reply_fn is None:
            self.update_status("翻译引擎未就绪，无法生成回复")
            return
        self._set_reply_busy(True)
        self.update_status("正在生成回复…")

        def worker():
            try:
                text = self.reply_fn(message)
            except Exception as exc:
                self.log.exception("生成回复失败")
                self.queue.put({"type": "reply_failed", "error": str(exc)})
            else:
                self.queue.put({"type": "reply_result", "text": text or ""})

        threading.Thread(target=worker, name="reply-generate", daemon=True).start()

    def _reply_result(self, text):
        self._set_reply_busy(False)
        text = str(text or "").strip()
        if not text:
            self.update_status("生成回复为空")
            return
        self._copy_text(text)
        self._show_input()
        self.input_entry.delete(0, "end")
        self.input_entry.insert(0, text)
        self.input_entry.select_range(0, "end")
        self.input_entry.focus_force()
        self.update_status("已生成回复并复制到剪贴板")
        self._touch()

    def _reply_failed(self, error):
        self._set_reply_busy(False)
        self.update_status(f"生成回复失败：{error}")

    # ---------- 快捷回复菜单 ----------
    def _quick_replies(self):
        raw = self.cfg.get("quick_replies", DEFAULT_QUICK_REPLIES)
        if not isinstance(raw, (list, tuple)):
            raw = DEFAULT_QUICK_REPLIES
        items = [str(item).strip() for item in raw if str(item).strip()]
        return (items or list(DEFAULT_QUICK_REPLIES))[:9]

    def _quick_toggle(self):
        if self._quick_menu is not None:
            self._hide_quick_menu()
            return
        if self._input_locked():
            return
        existing = getattr(self, "_settings_dialog", None)
        try:
            if existing is not None and existing.winfo_exists() and existing.winfo_viewable():
                return
        except tk.TclError:
            pass
        self._show_quick_menu()

    def _quick_menu_position(self, width, height):
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        return x, y

    def _make_no_activate(self, top):
        """Windows 下让快捷菜单不抢焦点、鼠标点击穿透（无碰撞箱）。"""
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            gwl_exstyle = -20
            swp_flags = 0x0001 | 0x0002 | 0x0004 | 0x0010
            # NOSIZE | NOMOVE | NOZORDER | NOACTIVATE

            def apply(hwnd):
                style = user32.GetWindowLongW(hwnd, gwl_exstyle)
                style |= 0x08000000  # WS_EX_NOACTIVATE
                style |= 0x00000080  # WS_EX_TOOLWINDOW
                style |= 0x00000020  # WS_EX_TRANSPARENT：鼠标事件穿透
                user32.SetWindowLongW(hwnd, gwl_exstyle, style)
                user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, swp_flags)

            top_hwnd = top.winfo_id()
            apply(top_hwnd)

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def child_callback(child, _lparam):
                apply(child)
                return True

            user32.EnumChildWindows(top_hwnd, child_callback, 0)
        except Exception:
            pass

    def _show_quick_menu(self):
        items = self._quick_replies()
        if not items:
            self.update_status("没有配置快捷回复")
            return
        try:
            opacity = float(self.cfg.get("quick_menu_opacity", 0.1))
        except (TypeError, ValueError):
            opacity = 0.1
        fg_color = str(self.cfg.get("quick_menu_fg_color", "#ececef") or "#ececef")
        try:
            self.root.winfo_rgb(fg_color)
        except tk.TclError:
            fg_color = "#ececef"
        bg_color = str(
            self.cfg.get("quick_menu_bg_color", "#23232b") or "#23232b"
        )
        try:
            self.root.winfo_rgb(bg_color)
        except tk.TclError:
            bg_color = "#23232b"
        if os.name == "nt":
            try:
                from wt_translator.quickmenu import NativeQuickMenu

                native = NativeQuickMenu(
                    items, opacity=opacity, fg_color=fg_color,
                    bg_color=bg_color, log=self.log,
                )
                if native.show():
                    self._quick_menu = native
                    self._start_quick_pick_watchers(len(items))
                    try:
                        timeout = float(self.cfg.get("quick_menu_timeout", 6.0))
                    except (TypeError, ValueError):
                        timeout = 6.0
                    if timeout > 0:
                        self._quick_close_after = self.root.after(
                            int(timeout * 1000), self._hide_quick_menu
                        )
                    return
            except Exception as exc:
                self.log.warning("原生快捷菜单创建失败，改用 Tk 浮层：%s", exc)
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=bg_color)
        family = self.cfg.get("font_family", "Microsoft YaHei UI")
        grid = tk.Frame(top, bg=bg_color)
        grid.pack(padx=8, pady=8)
        for index, text in enumerate(items):
            row, col = divmod(index, 3)
            label_text = text.replace("\n", " ")
            if len(label_text) > 10:
                label_text = label_text[:10] + "…"
            label = tk.Label(
                grid, text=f"{index + 1}. {label_text}", bg=bg_color, fg=fg_color,
                font=self._font(10), anchor="w", width=14, cursor="hand2",
                padx=6, pady=4,
            )
            label.grid(row=row, column=col, sticky="ew", padx=2, pady=2)
            label.bind("<Button-1>", lambda _e, i=index: self._pick_quick_reply(i))
        top.update_idletasks()
        width = max(120, top.winfo_reqwidth())
        height = max(60, top.winfo_reqheight())
        x, y = self._quick_menu_position(width, height)
        top.geometry(f"{width}x{height}+{x}+{y}")
        top.update_idletasks()
        top.lift()
        try:
            top.update()
        except tk.TclError:
            pass
        opacity = 0.1 if not isinstance(opacity, float) else opacity
        try:
            top.attributes("-alpha", min(1.0, max(0.05, opacity)))
        except tk.TclError:
            pass
        self._make_no_activate(top)
        # Tk 可能在建窗/绘制后调整子窗口样式，稍后再补一次穿透属性
        def _reapply_click_through():
            try:
                if top.winfo_exists():
                    self._make_no_activate(top)
            except tk.TclError:
                pass

        top.after(60, _reapply_click_through)
        self._quick_menu = top
        self._start_quick_pick_watchers(len(items))
        try:
            timeout = float(self.cfg.get("quick_menu_timeout", 6.0))
        except (TypeError, ValueError):
            timeout = 6.0
        if timeout > 0:
            self._quick_close_after = self.root.after(
                int(timeout * 1000), self._hide_quick_menu
            )

    def _hide_quick_menu(self):
        if self._quick_close_after is not None:
            try:
                self.root.after_cancel(self._quick_close_after)
            except tk.TclError:
                pass
            self._quick_close_after = None
        self._stop_quick_pick_watchers()
        top = self._quick_menu
        self._quick_menu = None
        if top is not None:
            try:
                if hasattr(top, "hide"):
                    top.hide()
                else:
                    top.destroy()
            except Exception:
                pass

    def _start_quick_pick_watchers(self, count):
        self._stop_quick_pick_watchers()
        for digit in range(1, min(9, count) + 1):
            vk = ord(str(digit))
            watcher = AltKeyWatcher(
                lambda d=digit: self.queue.put({"type": "quick_pick", "index": d - 1}),
                read_state=_read_key_state(vk),
                interval=0.02,
            )
            watcher.start()
            self._quick_pick_watchers.append(watcher)
        escape = AltKeyWatcher(
            lambda: self.queue.put({"type": "quick_close"}),
            read_state=_read_key_state(0x1B),
            interval=0.02,
        )
        escape.start()
        self._quick_pick_watchers.append(escape)

    def _stop_quick_pick_watchers(self):
        for watcher in self._quick_pick_watchers:
            try:
                watcher.stop()
            except Exception:
                pass
        self._quick_pick_watchers = []

    def _pick_quick_reply(self, index):
        items = self._quick_replies()
        if index < 0 or index >= len(items):
            self._hide_quick_menu()
            return
        text = items[index]
        self._hide_quick_menu()
        self._copy_text(text)
        self.update_status(f"已选择快捷回复并复制：{text}")
        self._touch()

    def _download_sensevoice_model(self):
        self.update_status("正在检查 SenseVoice 本地模型…")

        def worker():
            from wt_translator.voice import SenseVoiceASR

            asr = SenseVoiceASR(self.cfg, self.log)
            try:
                asr.ensure_downloaded(
                    progress=lambda text: self.queue.put({"type": "status", "text": text})
                )
            except Exception as exc:
                self.log.exception("SenseVoice 模型准备失败")
                self.queue.put({"type": "voice_failed", "error": str(exc)})
            else:
                self._asr = asr
                self.queue.put({"type": "status", "text": "SenseVoice 模型已就绪"})

        threading.Thread(
            target=worker, name="sensevoice-download", daemon=True
        ).start()

    def _hide_input(self):
        if not self._input_visible:
            return
        self._input_visible = False
        self.input_frame.pack_forget()
        self._touch()

    def _on_input_enter(self, _event=None):
        if self._input_locked():
            return
        text = self.input_entry.get().strip()
        if not text:
            self._hide_input()
            return
        if not self.translate_fn:
            self.update_status("翻译引擎未就绪")
            return
        self.input_entry.configure(state="disabled")
        self.update_status("正在翻译输入内容…")

        def worker():
            try:
                result = self.translate_fn(text, "en")
                if self.cfg.get("angry_mode", False):
                    from wt_translator.translator import has_cjk

                    if has_cjk(text) and not any(
                        word in result.lower() for word in ANGRY_WORDS
                    ):
                        result = result.rstrip(" .!?") + ", damn it!"
            except Exception as exc:
                self.log.exception("输入翻译失败")
                self.queue.put({"type": "input_failed", "error": str(exc), "original": text})
            else:
                self.queue.put({"type": "input_translated", "text": result or text})

        threading.Thread(target=worker, name="input-translate", daemon=True).start()

    def _input_result(self, text):
        self.input_entry.configure(state="normal")
        self.input_entry.delete(0, "end")
        self.input_entry.insert(0, text)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except tk.TclError as exc:
            self.log.warning("复制到剪贴板失败：%s", exc)
        self.input_entry.select_range(0, "end")
        self.input_entry.focus_force()
        self.update_status("已翻译并复制到剪贴板")
        self._touch()

    # ---------- 事件循环 ----------
    def _pump(self):
        try:
            while True:
                event = self.queue.get_nowait()
                kind = event.get("type")
                if kind == "chat":
                    self.add_chat_message(event["msg"])
                elif kind == "translated":
                    self.finish_translation(event["msg"], event.get("text", ""), event.get("original", ""))
                elif kind == "original":
                    self.finish_translation(
                        event["msg"],
                        str(event["msg"].get("msg") or ""),
                        str(event["msg"].get("msg") or ""),
                        blacklisted=event.get("blacklisted", False),
                        failed=event.get("failed", False),
                    )
                elif kind == "status":
                    self.update_status(event.get("text", ""))
                elif kind == "match_end":
                    self.update_status("对局结束，等待下一局…")
                elif kind == "show":
                    self._touch()
                elif kind == "update_available":
                    self._show_update_dialog(event["info"])
                elif kind == "update_done":
                    self._update_done(event["path"])
                elif kind == "update_failed":
                    self._update_failed(event["error"])
                elif kind == "manual_update_result":
                    self._handle_manual_update(event["info"])
                elif kind == "manual_update_none":
                    self._show_info_dialog("检查更新", f"检查更新失败：\n{event['error']}")
                elif kind == "toggle_input":
                    self._on_enter_global()
                elif kind == "input_translated":
                    self._input_result(event["text"])
                elif kind == "input_failed":
                    self._input_result(event["original"])
                    self.update_status(f"输入翻译失败：{event['error']}")
                elif kind == "hotkey_captured":
                    self._on_hotkey_captured(
                        event.get("key"), event.get("target", "show_hotkey")
                    )
                elif kind == "voice_toggle":
                    self._on_voice_toggle()
                elif kind == "voice_press":
                    self._on_voice_press()
                elif kind == "voice_release":
                    self._on_voice_release()
                elif kind == "voice_result":
                    self._voice_result(event.get("text", ""))
                elif kind == "voice_failed":
                    self._voice_failed(event.get("error", ""))
                elif kind == "quick_toggle":
                    self._quick_toggle()
                elif kind == "quick_pick":
                    self._pick_quick_reply(int(event.get("index", -1)))
                elif kind == "quick_close":
                    self._hide_quick_menu()
                elif kind == "reply_result":
                    self._reply_result(event.get("text", ""))
                elif kind == "reply_failed":
                    self._reply_failed(event.get("error", ""))
                elif kind == "glossary_update_result":
                    self._on_glossary_update_result(event["remote"])
                elif kind == "glossary_update_failed":
                    self._show_info_dialog("术语表更新", f"检查失败：\n{event['error']}")
                elif kind == "quit":
                    self._quit()
        except queue.Empty:
            pass

    def _tick(self):
        if self._stopping:
            return
        self._pump()
        if not self._hidden:
            try:
                timeout = float(self.cfg.get("hide_timeout", 10.0))
            except (TypeError, ValueError):
                timeout = 10.0
            # 输入框打开时（用户在打字）不自动隐藏
            if (
                timeout > 0
                and not self._input_visible
                and not self._input_locked()
                and time.monotonic() - self._last_activity >= timeout
            ):
                self._hide()
        self.root.after(100, self._tick)

    def run(self):
        self.root.after(100, self._tick)
        self.root.mainloop()

    def _quit(self):
        if self._stopping:
            return
        self._stopping = True
        if self._alt_watcher is not None:
            self._alt_watcher.stop()
        if self._enter_watcher is not None:
            self._enter_watcher.stop()
        if self._voice_watcher is not None:
            self._voice_watcher.stop()
        self._hide_quick_menu()
        if self._quick_watcher is not None:
            self._quick_watcher.stop()
        if self._voice_recorder is not None:
            try:
                self._voice_recorder.cancel()
            except Exception:
                pass
        if self._asr is not None:
            try:
                self._asr.shutdown()
            except Exception:
                pass
        self._save_position()
        try:
            self.on_quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
