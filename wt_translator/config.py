"""配置管理。

分层结构（安装包会覆盖 config.json / config.yml，但不覆盖 setting.json）：
1. 代码内置默认值 DEFAULTS
2. 打包默认值 config.json（随版本更新）
3. 用户设置 setting.json（安装更新后保留）

保存时只把用户改动过的键写入 setting.json，避免旧设置冻结新版本的默认值。
"""
from __future__ import annotations

import json
import os
import sys


def _project_root() -> str:
    """源码运行返回项目根目录；打包成 exe 后返回 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        # macOS .app：配置/模型放在 .app 同级目录，而不是只读的包内部
        if sys.platform == "darwin" and os.path.basename(os.path.dirname(exe_dir)) == "Contents":
            return os.path.normpath(os.path.join(exe_dir, "..", "..", ".."))
        return exe_dir
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT_ROOT = _project_root()
PACKAGED_CONFIG = os.path.join(PROJECT_ROOT, "config.json")
SETTINGS_FILE = os.path.join(PROJECT_ROOT, "setting.json")
# 兼容旧引用
CONFIG_FILE = PACKAGED_CONFIG

DEFAULTS = {
    # --- 对局聊天服务器 ---
    "server_url": "http://localhost:8111",
    "poll_interval": 1.0,
    "request_timeout": 5.0,
    "chat_failure_threshold": 5,
    # --- 悬浮窗 ---
    "window_width": 460,
    "window_height": 320,
    "window_x": -1,
    "window_y": -1,
    "opacity": 0.9,
    "hide_timeout": 10.0,
    "auto_show": True,
    "show_on_alt": True,
    "show_hotkey": "alt",
    "show_input_on_enter": True,
    "auto_move_mouse_on_show": False,      # 呼出窗口时自动把鼠标移到浮窗
    "auto_move_mouse_to_input": False,     # 按回车激活输入框时自动把鼠标移到输入框
    "max_messages": 60,
    "font_family": "Microsoft YaHei UI",
    "font_size": 11,
    "show_sender": True,
    "show_original": False,
    "bg_color": "#18181d",
    "fg_color": "#ececef",
    "show_source_language": False,   # 译文后面显示 [语言] 标签
    "angry_mode": False,              # 输入框中文转英文时使用带脏话的语气
    # --- 快捷回复 ---
    "quick_reply_enabled": True,
    "quick_reply_hotkey": "n",
    "quick_menu_timeout": 6.0,
    "quick_menu_opacity": 0.1,
    "quick_menu_fg_color": "#ececef",
    "quick_menu_bg_color": "#23232b",
    "quick_replies": [
        "收到", "干得好", "进攻D点", "防守", "需要支援",
        "谢谢", "抱歉", "撤退", "掩护我",
    ],
    "reply_max_tokens": 128,
    # --- 语音输入 ---
    "voice_enabled": False,
    "voice_engine": "sensevoice",     # sensevoice（本地 ONNX）/ api（OpenAI 兼容）
    "voice_mode": "toggle",           # toggle：按一次开始/再按停止；hold：按住说话/松开停止
    "voice_hotkey": "f8",
    "voice_model": "whisper-1",
    "voice_api_base_url": "",         # 留空时复用 api_base_url
    "voice_api_key": "",              # 留空时复用 api_key
    "voice_language": "zh",
    "voice_max_seconds": 60,
    "sensevoice_dir": "models/sense-voice-small-int8",
    "sensevoice_model_url": "https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx",
    "sensevoice_tokens_url": "https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt",
    "sensevoice_num_threads": 2,
    "sensevoice_use_itn": True,
    # --- 翻译引擎 ---
    "engine": "llama",
    # OpenAI 兼容 API（engine 设为 openai 时使用）
    "api_base_url": "",
    "api_key": "",
    "api_model": "",
    "api_timeout": 60,
    "api_max_tokens": 512,
    "gguf_dir": "models/Hy-MT2-1.8B-GGUF",
    "gguf_repo": "tencent/Hy-MT2-1.8B-GGUF",
    "gguf_quant": "Q4_K_M",
    "gguf_prefix": "Hy-MT2-1.8B",
    "gguf_download_url": "https://modelscope.cn/models/Tencent-Hunyuan/Hy-MT2-1.8B-GGUF/resolve/master/Hy-MT2-1.8B-Q4_K_M.gguf",
    "llama_bin_dir": "runtime/llama.cpp",
    "llama_release": "b10686",
    "llama_port": 19911,
    "llama_ctx": 2048,
    "llama_threads": 0,
    # --- 翻译模型（transformers 引擎） ---
    "model_id": "tencent/Hy-MT2-1.8B",
    "model_dir": "models/Hy-MT2-1.8B",
    "hf_endpoint": "https://hf-mirror.com",
    "device": "cpu",
    "dtype": "bfloat16",
    "max_new_tokens": 256,
    "temperature": 0.7,
    "top_p": 0.6,
    "top_k": 20,
    "repetition_penalty": 1.05,
    "target_language": "zh",
    "max_pending": 30,
    # --- 黑名单 ---
    "blacklist": [],
    "blacklist_case_insensitive": True,
    "blacklist_enabled": True,
    "skip_chinese_messages": False,
    # --- 更新检查 ---
    "check_update_on_start": True,
    "update_check_url": "https://wt.ngup.eu.org/version",
    "update_download_url": "https://wt.ngup.eu.org/update",
    "update_check_timeout": 8,
    "skipped_update_version": "",
    # --- 守护进程 ---
    "guardian_ask_on_start": True,
    "guardian_enabled": False,
    "guardian_autostart": False,
    "guardian_poll_interval": 2.0,
    "guardian_stop_on_game_exit": True,
    "guardian_game_processes": ["aces.exe", "aces-min-cpu.exe"],
    # --- 使用统计上报 ---
    "stats_enabled": True,
    "report_url": "https://wt.ngup.eu.org/api/report",
    "report_interval_minutes": 10,
    "device_id": "",
    # --- 术语表 ---
    "glossary_file": "glossary.json",
    "glossary_update_url": "https://wt.ngup.eu.org/glossary",
    # --- 其他 ---
    "log_file": "wt_translator.log",
}


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class Config:
    """分层配置对象，支持 cfg.get(key, default) 访问。"""

    def __init__(self, path: str | None = None):
        # path 为 None 时使用默认设置文件；显式传路径时按“用户设置文件”处理
        self.path = path or SETTINGS_FILE
        self._overrides = {}
        self._data = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        self._data = dict(DEFAULTS)
        self._overrides = {}

        # 默认模式：先合并打包默认值 config.json，再合并用户 setting.json
        use_packaged = os.path.abspath(self.path) == os.path.abspath(SETTINGS_FILE)
        if use_packaged and os.path.exists(PACKAGED_CONFIG):
            for key, value in _read_json(PACKAGED_CONFIG).items():
                if key in DEFAULTS:
                    self._data[key] = value

        user = _read_json(self.path)
        for key, value in user.items():
            if key in DEFAULTS:
                self._data[key] = value
                self._overrides[key] = value

        # 从旧版 config.json 迁移：setting.json 不存在但 config.json 与当前默认
        # 有差异时，把差异写入 setting.json，避免升级后用户设置丢失
        if use_packaged and not os.path.exists(self.path) and os.path.exists(PACKAGED_CONFIG):
            packaged = _read_json(PACKAGED_CONFIG)
            migrated = {
                key: value for key, value in packaged.items()
                if key in DEFAULTS and value != DEFAULTS[key]
            }
            if migrated:
                self._overrides.update(migrated)
                self._data.update(migrated)
                self.save()

    def reload(self) -> "Config":
        self.load()
        return self

    def get(self, key, default=None):
        return self._data.get(key, default)

    def update(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if key in DEFAULTS:
                self._data[key] = value
                self._overrides[key] = value

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self._overrides, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(f"[config] 保存配置失败：{exc}")
