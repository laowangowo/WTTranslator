"""Hy-MT2-1.8B 模型：自动下载、加载、推理。"""
from __future__ import annotations

import atexit
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile

from .config import PROJECT_ROOT
from .glossary import read as read_glossary

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_COLOR_MARKUP_RE = re.compile(
    r"<color=\s*#?([0-9A-Fa-f]{6,8})\s*>(.*?)(?:</color>|$)",
    re.IGNORECASE | re.DOTALL,
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_TAB_TOKEN_RE = re.compile(r"(?:(?<=\s)|^)(?:\\t|/t)(?=\s|$)", re.IGNORECASE)
_BLANK_TRANSLATION = str.maketrans(
    {"\r": " ", "\n": " ", "\v": " ", "\f": " ", "\u00a0": " "}
)

# 语言代码 -> 中文名（用于中文提示词模板）
ZH_NAMES = {
    "zh": "中文", "zh-Hant": "繁体中文", "en": "英语", "fr": "法语", "pt": "葡萄牙语",
    "es": "西班牙语", "ja": "日语", "tr": "土耳其语", "ru": "俄语", "ar": "阿拉伯语",
    "ko": "韩语", "th": "泰语", "it": "意大利语", "de": "德语", "vi": "越南语",
    "ms": "马来语", "id": "印尼语", "tl": "菲律宾语", "hi": "印地语", "pl": "波兰语",
    "cs": "捷克语", "nl": "荷兰语", "km": "高棉语", "my": "缅甸语", "fa": "波斯语",
    "gu": "古吉拉特语", "ur": "乌尔都语", "te": "泰卢固语", "mr": "马拉地语",
    "he": "希伯来语", "bn": "孟加拉语", "ta": "泰米尔语", "uk": "乌克兰语",
    "bo": "藏语", "kk": "哈萨克语", "mn": "蒙古语", "ug": "维吾尔语",
}

# 语言代码 -> 英文名（用于非中文对提示词模板）
EN_NAMES = {
    "zh": "Chinese", "zh-Hant": "Traditional Chinese", "en": "English", "fr": "French",
    "pt": "Portuguese", "es": "Spanish", "ja": "Japanese", "tr": "Turkish", "ru": "Russian",
    "ar": "Arabic", "ko": "Korean", "th": "Thai", "it": "Italian", "de": "German",
    "vi": "Vietnamese", "ms": "Malay", "id": "Indonesian", "tl": "Filipino", "hi": "Hindi",
    "pl": "Polish", "cs": "Czech", "nl": "Dutch", "km": "Khmer", "my": "Burmese",
    "fa": "Persian", "gu": "Gujarati", "ur": "Urdu", "te": "Telugu", "mr": "Marathi",
    "he": "Hebrew", "bn": "Bengali", "ta": "Tamil", "uk": "Ukrainian", "bo": "Tibetan",
    "kk": "Kazakh", "mn": "Mongolian", "ug": "Uyghur",
}

# 备用聊天模板（apply_chat_template 不可用时使用）
_CHAT_TEMPLATE_FALLBACK = "<｜hy_begin▁of▁sentence｜><｜hy_User｜>{content}<｜hy_EOT｜>"
# 与 HF chat_template（add_generation_prompt=False）等价的原始提示串
_CHAT_TEMPLATE_RAW = "<｜hy_begin▁of▁sentence｜><｜hy_User｜>{content}<｜hy_EOT｜>"


def _ensure_stderr() -> None:
    """窗口模式（pythonw / exe）下 sys.stderr 为 None，tqdm 写进度条会崩溃，给个空文件兜底。"""
    if sys.stderr is None:
        try:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            pass


def has_cjk(text) -> bool:
    return bool(_CJK_RE.search(text or ""))


def strip_color_markup(text) -> str:
    """去掉战争雷霆聊天里的 <color=#...>...</color> 标记，返回纯文本。"""
    value = str(text or "")
    previous = None
    while previous != value:
        previous = value
        value = _COLOR_MARKUP_RE.sub(r"\2", value)
    return value


def clean_chat_text(text) -> str:
    """清理聊天文本里的制表符/换行/控制字符，兼容字面量 \\t、/t。"""
    value = str(text or "")
    if not value:
        return value
    value = _TAB_TOKEN_RE.sub("", value)
    value = value.replace("\t", "")
    value = value.translate(_BLANK_TRANSLATION)
    value = _CONTROL_CHARS_RE.sub("", value)
    return re.sub(r" {2,}", " ", value).strip()


_LANG_SCRIPTS = (
    ("ja", re.compile(r"[\u3040-\u30ff]")),        # 平假名 / 片假名
    ("ko", re.compile(r"[\uac00-\ud7af]")),        # 韩文
    ("ru", re.compile(r"[\u0400-\u04ff]")),        # 西里尔文
    ("ar", re.compile(r"[\u0600-\u06ff]")),        # 阿拉伯文
    ("th", re.compile(r"[\u0e00-\u0e7f]")),        # 泰文
    ("he", re.compile(r"[\u0590-\u05ff]")),        # 希伯来文
    ("hi", re.compile(r"[\u0900-\u097f]")),        # 天城文
    ("el", re.compile(r"[\u0370-\u03ff]")),        # 希腊文
    ("zh", _CJK_RE),                                # 中文
)


def detect_language(text) -> str:
    """根据字符集粗略判断原文语言，用于在译文后显示 [语言] 标签。"""
    sample = clean_chat_text(strip_color_markup(text))
    for code, pattern in _LANG_SCRIPTS:
        if pattern.search(sample):
            return code
    if re.search(r"[A-Za-z]", sample):
        return "en"
    return "und"


def resolve_target(target, source_text):
    """target 为 'auto' 时：含中文 -> 英语，否则 -> 中文。"""
    if str(target).strip().lower() == "auto":
        return "en" if has_cjk(source_text) else "zh"
    return target


def _lang_name(target, names):
    target = str(target).strip()
    return names.get(target, target)


def parse_glossary(entries):
    """把配置里的术语表（"原文=译文" 列表）解析成 (source, target) 列表。"""
    pairs = []
    for entry in entries or []:
        text = str(entry)
        if "=" in text:
            source, target = text.split("=", 1)
            if source.strip() and target.strip():
                pairs.append((source.strip(), target.strip()))
    return pairs


def current_glossary_terms():
    """读取当前术语表文件中的词条列表。"""
    return read_glossary().get("terms", [])


def _term_prefix(terms, chinese):
    if not terms:
        return ""
    if chinese:
        lines = ["参考下面的翻译："]
        lines += [f"{source} 翻译成 {target}" for source, target in terms]
    else:
        lines = ["Reference the following translations:"]
        lines += [f"{source} translates to {target}" for source, target in terms]
    return "\n".join(lines) + "\n\n"


def build_prompt(source_text, target, terms=None, angry=False):
    """按官方模板构造提示词（支持 Hy-MT2 术语表；ZH 相关用中文模板，否则英文模板）。"""
    source = clean_chat_text(strip_color_markup(source_text)).strip()
    target = resolve_target(target, source)
    term_block = _term_prefix(terms, chinese=(target == "zh" or has_cjk(source)))
    angry_note = ""
    if angry and target == "en":
        angry_note = (
            "\n语气要求：使用暴躁、不耐烦的语气翻译，并自然加入英文脏话"
            "（如 fuck、shit、damn），保持原意，只输出译文。"
        )
    if target == "zh":
        return term_block + f"将以下文本翻译为中文，注意只需要输出翻译后的结果，不要额外解释：\n\n{source}"
    if has_cjk(source):
        name = _lang_name(target, ZH_NAMES)
        return (
            term_block
            + f"将以下文本翻译为{name}，注意只需要输出翻译后的结果，不要额外解释：\n\n{source}"
            + angry_note
        )
    name = _lang_name(target, EN_NAMES)
    if term_block:
        return term_block + (
            f"Translate the following text into {name}. Note that you should ONLY "
            f"output the translated result without any additional explanation:\n\n{source}"
            + angry_note
        )
    return (
        f"Translate the following text into {name}. Note that you should ONLY output "
        f"the translated result without any additional explanation:\n\n{source}"
        + angry_note
    )


def build_reply_prompt(message, terms=None):
    """构造“生成回复”提示词：用对方消息的语言回一句简短的游戏内回复。"""
    text = clean_chat_text(strip_color_markup(message.get("msg"))).strip()
    sender = clean_chat_text(message.get("sender")).strip()
    mode = clean_chat_text(message.get("mode")).strip() or "Chat"
    lang = detect_language(text)
    language_hint = ZH_NAMES.get(lang, "与对方消息相同的语言")
    term_block = _term_prefix(terms or [], chinese=(lang == "zh"))
    return (
        "你是《战争雷霆》玩家。请针对下面这条游戏内聊天消息，写一句简短、自然、"
        f"符合语境的回复。回复必须使用{language_hint}，只输出回复内容，"
        "不要解释、不要加引号，长度控制在 30 个词以内。\n\n"
        f"频道：{mode}\n发送者：{sender or '未知'}\n消息：{text}\n\n"
        + term_block
    )


class HYMTTranslator:
    """负责模型的下载、加载与单条消息翻译。"""

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self._lock = threading.RLock()
        self._model = None
        self._tokenizer = None
        self._device = "cpu"

    @property
    def model_dir(self) -> str:
        md = self.cfg.get("model_dir", "models/Hy-MT2-1.8B")
        if not os.path.isabs(md):
            md = os.path.join(PROJECT_ROOT, md)
        return os.path.normpath(md)

    def is_downloaded(self) -> bool:
        md = self.model_dir
        return os.path.isfile(os.path.join(md, "model.safetensors")) and os.path.isfile(
            os.path.join(md, "config.json")
        )

    def ensure_downloaded(self, progress=None) -> None:
        """模型不存在时自动从 HuggingFace（默认 hf-mirror 镜像）下载。"""
        if self.is_downloaded():
            if progress:
                progress("模型已存在于本地，无需下载")
            return
        _ensure_stderr()
        endpoint = str(self.cfg.get("hf_endpoint", "") or "").strip()
        if endpoint:
            os.environ.setdefault("HF_ENDPOINT", endpoint)
        os.makedirs(self.model_dir, exist_ok=True)
        self.log.info("开始下载模型 %s -> %s", self.cfg.get("model_id"), self.model_dir)
        from huggingface_hub import snapshot_download
        from tqdm.std import tqdm

        kwargs = {
            "repo_id": self.cfg.get("model_id", "tencent/Hy-MT2-1.8B"),
            "local_dir": self.model_dir,
        }
        if progress:
            class _GuiTqdm(tqdm):
                """静默进度条：把下载进度转成状态文本回调。"""

                def __init__(self, *args, **bar_kw):
                    self._gui_n = 0
                    bar_kw["disable"] = True
                    bar_kw["file"] = None
                    super().__init__(*args, **bar_kw)

                def update(self, n=1):
                    self._gui_n += n
                    if progress:
                        total = self.total or 0
                        mb = self._gui_n / 1048576.0
                        tmb = total / 1048576.0
                        progress(f"正在下载模型：{mb:.1f} MB / {tmb:.1f} MB（约 3.5 GB，请耐心等待）")

            try:
                snapshot_download(**kwargs, tqdm_class=_GuiTqdm)
            except TypeError:
                self.log.warning("当前 huggingface_hub 不支持进度回调，改用默认下载")
                snapshot_download(**kwargs)
        else:
            snapshot_download(**kwargs)
        if progress:
            progress("模型下载完成")

    def load(self, progress=None) -> None:
        with self._lock:
            if self._model is not None:
                return
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if progress:
            progress("正在加载模型（首次约需 1-2 分钟）…")
        self.log.info("加载模型：%s", self.model_dir)
        tokenizer = AutoTokenizer.from_pretrained(self.model_dir)
        dtype = self._torch_dtype(torch)
        try:
            model = AutoModelForCausalLM.from_pretrained(
                self.model_dir, dtype=dtype, low_cpu_mem_usage=True
            )
        except Exception as exc:
            self.log.warning("以 %s 加载失败（%s），改用 float32 重试", dtype, exc)
            model = AutoModelForCausalLM.from_pretrained(
                self.model_dir, dtype=torch.float32, low_cpu_mem_usage=True
            )
        device = str(self.cfg.get("device", "cpu")).lower()
        if device in ("cuda", "auto") and torch.cuda.is_available():
            model = model.to("cuda")
            self._device = "cuda"
        else:
            model = model.to("cpu")
            self._device = "cpu"
        model.eval()
        with self._lock:
            self._model = model
            self._tokenizer = tokenizer
        self.log.info("模型就绪，设备=%s", self._device)
        if progress:
            progress("模型加载完成")

    def _torch_dtype(self, torch):
        name = str(self.cfg.get("dtype", "bfloat16")).lower()
        return {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }.get(name, torch.bfloat16)

    def _get_model(self):
        with self._lock:
            if self._model is not None:
                return self._model, self._tokenizer
        self.load()
        with self._lock:
            return self._model, self._tokenizer

    def translate(self, text, target=None) -> str:
        text = str(text or "").strip()
        if not text:
            return ""
        model, tokenizer = self._get_model()
        target = resolve_target(
            target if target is not None else self.cfg.get("target_language", "zh"), text
        )
        terms = parse_glossary(current_glossary_terms())
        prompt = build_prompt(text, target, terms, angry=bool(self.cfg.get("angry_mode", False)))
        try:
            inputs = tokenizer.apply_chat_template(
                [{"role": "user", "content": prompt}],
                tokenize=True,
                add_generation_prompt=False,
                return_tensors="pt",
            )
        except Exception:
            inputs = tokenizer(
                _CHAT_TEMPLATE_FALLBACK.format(content=prompt), return_tensors="pt"
            )["input_ids"]

        import torch

        gen_kwargs = {
            "max_new_tokens": max(1, int(self.cfg.get("max_new_tokens", 256))),
            "do_sample": True,
            "temperature": float(self.cfg.get("temperature", 0.7)),
            "top_p": float(self.cfg.get("top_p", 0.6)),
            "top_k": int(self.cfg.get("top_k", 20)),
            "repetition_penalty": float(self.cfg.get("repetition_penalty", 1.05)),
            "pad_token_id": tokenizer.pad_token_id or tokenizer.eos_token_id,
        }
        self.log.info("翻译：%s -> %s", text[:60], target)
        with self._lock, torch.inference_mode():
            inputs = inputs.to(self._device)
            outputs = model.generate(inputs, **gen_kwargs)
        generated = outputs[0][inputs.shape[1]:]
        return tokenizer.decode(generated, skip_special_tokens=True).strip()

    def generate_reply(self, message) -> str:
        """根据对方消息生成一句简短回复。"""
        terms = parse_glossary(current_glossary_terms())
        prompt = build_reply_prompt(message, terms)
        model, tokenizer = self._get_model()
        try:
            inputs = tokenizer.apply_chat_template(
                [{"role": "user", "content": prompt}],
                tokenize=True,
                add_generation_prompt=False,
                return_tensors="pt",
            )
        except Exception:
            inputs = tokenizer(
                _CHAT_TEMPLATE_FALLBACK.format(content=prompt), return_tensors="pt"
            )["input_ids"]
        import torch

        gen_kwargs = {
            "max_new_tokens": max(16, int(self.cfg.get("reply_max_tokens", 128))),
            "do_sample": True,
            "temperature": 0.8,
            "top_p": 0.9,
            "top_k": 40,
            "repetition_penalty": 1.05,
            "pad_token_id": tokenizer.pad_token_id or tokenizer.eos_token_id,
        }
        with self._lock, torch.inference_mode():
            inputs = inputs.to(self._device)
            outputs = model.generate(inputs, **gen_kwargs)
        generated = outputs[0][inputs.shape[1]:]
        return tokenizer.decode(generated, skip_special_tokens=True).strip()


class LlamaTranslator:
    """基于 llama.cpp 官方 llama-server 的本地翻译后端。

    自动下载：llama.cpp Windows 预编译包 + 腾讯官方 GGUF 量化模型，
    通过本地 HTTP 接口（/completion）推理，CPU 上比 transformers 快很多。
    """

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self._proc = None
        self._base_url = None
        self._lock = threading.RLock()
        atexit.register(self.shutdown)

    # ---------- 路径 ----------
    @property
    def gguf_dir(self) -> str:
        d = self.cfg.get("gguf_dir", "models/Hy-MT2-1.8B-GGUF")
        if not os.path.isabs(d):
            d = os.path.join(PROJECT_ROOT, d)
        return os.path.normpath(d)

    @property
    def gguf_file(self) -> str:
        quant = str(self.cfg.get("gguf_quant", "Q4_K_M"))
        prefix = str(self.cfg.get("gguf_prefix", "Hy-MT2-1.8B"))
        return os.path.join(self.gguf_dir, f"{prefix}-{quant}.gguf")

    @property
    def gguf_repo(self) -> str:
        return str(self.cfg.get("gguf_repo", "tencent/Hy-MT2-1.8B-GGUF"))

    @property
    def runtime_dir(self) -> str:
        d = self.cfg.get("llama_bin_dir", "runtime/llama.cpp")
        if not os.path.isabs(d):
            d = os.path.join(PROJECT_ROOT, d)
        return os.path.normpath(d)

    def _server_exe(self):
        exe_name = self._server_exe_name()
        direct = os.path.join(self.runtime_dir, exe_name)
        if os.path.isfile(direct):
            return direct
        for root, _dirs, files in os.walk(self.runtime_dir):
            for name in files:
                if name.lower() == exe_name.lower():
                    return os.path.join(root, name)
        return None

    @staticmethod
    def _server_exe_name():
        return "llama-server.exe" if os.name == "nt" else "llama-server"

    @staticmethod
    def _runtime_zip_name(release):
        """返回当前平台对应的 llama.cpp 预编译包名（不支持的平台返回 None）。"""
        if os.name == "nt":
            return f"llama-{release}-bin-win-cpu-x64.zip"
        if sys.platform == "darwin":
            machine = _platform.machine().lower()
            arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
            return f"llama-{release}-bin-macos-{arch}.zip"
        return None

    def is_downloaded(self) -> bool:
        return os.path.isfile(self.gguf_file) and self._server_exe() is not None

    # ---------- 自动下载 ----------
    def ensure_downloaded(self, progress=None) -> None:
        _ensure_stderr()
        # 1) llama.cpp 官方预编译运行时（Windows CPU x64）
        if self._server_exe() is None:
            release = str(self.cfg.get("llama_release", "b10686"))
            zip_name = self._runtime_zip_name(release)
            if not zip_name:
                raise RuntimeError(
                    f"当前系统（{sys.platform}）暂不提供 llama.cpp 自动下载，"
                    "请使用 API 模式或在设置中手动配置本地 llama-server"
                )
            zip_path = os.path.join(self.runtime_dir, zip_name)
            url = f"https://github.com/ggml-org/llama.cpp/releases/download/{release}/{zip_name}"
            os.makedirs(self.runtime_dir, exist_ok=True)
            if not os.path.isfile(zip_path):
                bundled = self._bundled_zip(zip_name)
                if bundled:
                    if progress:
                        progress("正在从安装包复制 llama.cpp 运行时…")
                    shutil.copy2(bundled, zip_path)
                else:
                    if progress:
                        progress(f"正在下载 llama.cpp 运行时（{release}，约 17 MB）…")
                    self._download(url, zip_path, progress)
            if progress:
                progress("正在解压 llama.cpp 运行时…")
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(self.runtime_dir)
            if self._server_exe() is None:
                raise RuntimeError(
                    f"解压后未找到 {self._server_exe_name()}，请检查 llama_release 配置"
                )
            if os.name != "nt":
                try:
                    os.chmod(self._server_exe(), 0o755)
                except OSError:
                    pass

        # 2) 腾讯官方 GGUF 量化模型
        if not os.path.isfile(self.gguf_file):
            os.makedirs(self.gguf_dir, exist_ok=True)
            quant = str(self.cfg.get("gguf_quant", "Q4_K_M"))
            direct_url = str(self.cfg.get("gguf_download_url", "") or "").strip()
            if direct_url:
                if progress:
                    progress("正在从配置的下载地址下载模型…")
                try:
                    self._download(direct_url, self.gguf_file, progress, label="模型")
                except Exception as exc:
                    raise RuntimeError(
                        f"按配置的下载地址下载模型失败：{direct_url}\n原因：{exc}"
                    ) from exc
            else:
                from huggingface_hub import hf_hub_download

                if progress:
                    progress(f"正在下载模型 {os.path.basename(self.gguf_file)}（约 1.1 GB）…")
                candidates = []
                endpoint = str(self.cfg.get("hf_endpoint", "") or "").strip()
                if endpoint:
                    candidates.append(endpoint)
                if endpoint != "https://huggingface.co":
                    candidates.append("https://huggingface.co")
                last_error = None
                for endpoint in candidates:
                    os.environ["HF_ENDPOINT"] = endpoint
                    try:
                        hf_hub_download(
                            repo_id=self.gguf_repo,
                            filename=os.path.basename(self.gguf_file),
                            local_dir=self.gguf_dir,
                        )
                    except Exception as exc:
                        last_error = exc
                        self.log.warning("从 %s 下载模型失败：%s", endpoint, exc)
                    else:
                        last_error = None
                        break
                if last_error is not None:
                    raise RuntimeError(
                        f"从镜像下载模型失败：{last_error}。"
                        "可在 config.json 中设置 gguf_download_url 指定其他下载地址"
                    ) from last_error
        if progress:
            progress("模型文件就绪")

    def _bundled_zip(self, zip_name):
        """查找打包进 exe 的运行时压缩包路径（兼容 onefile / onedir 布局）。"""
        meipass = getattr(sys, "_MEIPASS", "") or ""
        candidates = [
            os.path.join(meipass, "runtime", "llama.cpp", zip_name),
            os.path.join(meipass, zip_name),
            os.path.join(PROJECT_ROOT, "runtime", "llama.cpp", zip_name),
            os.path.join(PROJECT_ROOT, "_internal", "runtime", "llama.cpp", zip_name),
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        return None

    def _download(self, url, dest, progress, label="文件") -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        tmp = dest + ".part"
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(f"正在下载{label}：{done / 1048576:.1f} / {total / 1048576:.1f} MB")
        os.replace(tmp, dest)

    # ---------- 服务生命周期 ----------
    def load(self, progress=None) -> None:
        with self._lock:
            port = int(self.cfg.get("llama_port", 19911))
            self._base_url = f"http://127.0.0.1:{port}"
            if self._health():
                self.log.info("检测到端口 %d 已有 llama-server，直接复用", port)
                return
            exe = self._server_exe()
            if exe is None or not os.path.isfile(self.gguf_file):
                raise RuntimeError("翻译引擎文件缺失，请先运行 --download-model")
            cmd = [
                exe,
                "--model", self.gguf_file,
                "--host", "127.0.0.1",
                "--port", str(port),
                "--ctx-size", str(max(512, int(self.cfg.get("llama_ctx", 2048)))),
                "--log-file", os.path.join(self.runtime_dir, "llama-server.log"),
                "--no-webui",
            ]
            threads = int(self.cfg.get("llama_threads", 0) or 0)
            if threads > 0:
                cmd += ["--threads", str(threads)]
            if progress:
                progress("正在启动本地翻译引擎（加载模型约需 10-30 秒）…")
            self.log.info("启动 llama-server：%s …", " ".join(cmd[:4]))
            CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            deadline = time.time() + 240
            while time.time() < deadline:
                if self._proc.poll() is not None:
                    raise RuntimeError(
                        "llama-server 启动失败（进程已退出），详见 runtime/llama.cpp/llama-server.log"
                    )
                if self._health():
                    self.log.info("llama-server 就绪，端口 %d", port)
                    if progress:
                        progress("本地翻译引擎就绪")
                    return
                time.sleep(1)
            raise RuntimeError("llama-server 启动超时，请查看日志")

    def _health(self) -> bool:
        if not self._base_url:
            return False
        try:
            with urllib.request.urlopen(self._base_url + "/health", timeout=2) as resp:
                body = resp.read().decode("utf-8", "replace").strip()
            if body.lower() == "ok":
                return True
            try:
                return json.loads(body).get("status") == "ok"
            except ValueError:
                return False
        except Exception:
            return False

    def shutdown(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ---------- 翻译 ----------
    def translate(self, text, target=None) -> str:
        text = str(text or "").strip()
        if not text:
            return ""
        with self._lock:
            if not self._health():
                self.load()
            target = resolve_target(
                target if target is not None else self.cfg.get("target_language", "zh"), text
            )
            terms = parse_glossary(current_glossary_terms())
            prompt = build_prompt(text, target, terms, angry=bool(self.cfg.get("angry_mode", False)))
            full_prompt = _CHAT_TEMPLATE_RAW.format(content=prompt)
            payload = {
                "prompt": full_prompt,
                "temperature": float(self.cfg.get("temperature", 0.7)),
                "top_k": int(self.cfg.get("top_k", 20)),
                "top_p": float(self.cfg.get("top_p", 0.6)),
                "repeat_penalty": float(self.cfg.get("repetition_penalty", 1.05)),
                "n_predict": max(1, int(self.cfg.get("max_new_tokens", 256))),
                "cache_prompt": False,
            }
            self.log.info("翻译：%s -> %s", text[:60], target)
            req = urllib.request.Request(
                self._base_url + "/completion",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode("utf-8", "replace"))
        return str(result.get("content", "")).strip()

    def generate_reply(self, message) -> str:
        prompt = build_reply_prompt(message, parse_glossary(current_glossary_terms()))
        full_prompt = _CHAT_TEMPLATE_RAW.format(content=prompt)
        with self._lock:
            if not self._health():
                self.load()
            payload = {
                "prompt": full_prompt,
                "temperature": 0.8,
                "top_k": 40,
                "top_p": 0.9,
                "repeat_penalty": 1.05,
                "n_predict": max(16, int(self.cfg.get("reply_max_tokens", 128))),
                "cache_prompt": False,
            }
            self.log.info("生成回复：%s", str(message.get("msg") or "")[:60])
            req = urllib.request.Request(
                self._base_url + "/completion",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode("utf-8", "replace"))
        return str(result.get("content", "")).strip()


class OpenAITranslator:
    """OpenAI 兼容协议（/chat/completions）远程 API 后端。"""

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log

    # ---------- 生命周期 ----------
    def ensure_downloaded(self, progress=None) -> None:
        if progress:
            progress("API 模式无需下载本地模型")

    def load(self, progress=None) -> None:
        base = str(self.cfg.get("api_base_url", "") or "").strip()
        model = str(self.cfg.get("api_model", "") or "").strip()
        if not base:
            raise RuntimeError("未配置 API 地址，请在设置中填写 URL 后重试")
        if not model:
            raise RuntimeError("未配置 API 模型名称，请在设置中填写模型后重试")
        self.log.info("OpenAI API 后端就绪：%s / %s", base.rstrip("/"), model)
        if progress:
            progress("OpenAI API 模式已启用")

    def shutdown(self) -> None:
        pass

    # ---------- 内部 ----------
    def _chat_url(self) -> str:
        base = str(self.cfg.get("api_base_url", "") or "").strip().rstrip("/")
        if not base:
            return ""
        if base.lower().endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        key = str(self.cfg.get("api_key", "") or "").strip()
        if key:
            headers["Authorization"] = "Bearer " + key
        return headers

    # ---------- 翻译 ----------
    def translate(self, text, target=None) -> str:
        text = str(text or "").strip()
        if not text:
            return ""
        url = self._chat_url()
        if not url:
            raise RuntimeError("未配置 API 地址，请在设置中填写 URL")
        target = resolve_target(
            target if target is not None else self.cfg.get("target_language", "zh"), text
        )
        terms = parse_glossary(current_glossary_terms())
        prompt = build_prompt(text, target, terms, angry=bool(self.cfg.get("angry_mode", False)))
        model = str(self.cfg.get("api_model", "") or "").strip()
        if not model:
            raise RuntimeError("未配置 API 模型名称")
        try:
            max_tokens = max(1, int(self.cfg.get("api_max_tokens", 512)))
            temperature = float(self.cfg.get("api_temperature", self.cfg.get("temperature", 0.7)))
            timeout = max(5, float(self.cfg.get("api_timeout", 60)))
        except (TypeError, ValueError):
            max_tokens = 512
            temperature = 0.7
            timeout = 60.0
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        self.log.info("API 翻译：%s -> %s（模型 %s）", text[:60], target, model)
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise RuntimeError(f"API 请求失败（HTTP {exc.code}）：{detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"API 请求失败：{exc.reason}") from exc
        choices = result.get("choices") if isinstance(result, dict) else None
        if not choices:
            error = result.get("error") if isinstance(result, dict) else None
            raise RuntimeError(f"API 返回格式异常：{error or result}")
        content = choices[0].get("message", {}).get("content")
        if content is None:
            raise RuntimeError("API 返回中没有 message.content")
        return str(content).strip()

    def generate_reply(self, message) -> str:
        url = self._chat_url()
        if not url:
            raise RuntimeError("未配置 API 地址，请在设置中填写 URL")
        model = str(self.cfg.get("api_model", "") or "").strip()
        if not model:
            raise RuntimeError("未配置 API 模型名称")
        prompt = build_reply_prompt(message, parse_glossary(current_glossary_terms()))
        try:
            timeout = max(5, float(self.cfg.get("api_timeout", 60)))
        except (TypeError, ValueError):
            timeout = 60.0
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.8,
            "max_tokens": max(32, int(self.cfg.get("reply_max_tokens", 128))),
            "stream": False,
        }
        self.log.info("API 生成回复：%s", str(message.get("msg") or "")[:60])
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise RuntimeError(f"API 请求失败（HTTP {exc.code}）：{detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"API 请求失败：{exc.reason}") from exc
        choices = result.get("choices") if isinstance(result, dict) else None
        if not choices:
            raise RuntimeError(f"API 返回格式异常：{result}")
        content = choices[0].get("message", {}).get("content")
        if content is None:
            raise RuntimeError("API 返回中没有 message.content")
        return str(content).strip()


class TranslatorRouter:
    """根据设置里的 engine 动态选择后端，支持运行中切换本地/API 而无需重启。"""

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self._backends = {}

    def _mode(self) -> str:
        engine = str(self.cfg.get("engine", "llama")).lower()
        if engine in ("openai", "api", "remote", "openai-compatible"):
            return "openai"
        if engine in ("llama", "llama.cpp", "gguf"):
            return "llama"
        return "transformers"

    def _backend(self):
        mode = self._mode()
        backend = self._backends.get(mode)
        if backend is None:
            if mode == "openai":
                backend = OpenAITranslator(self.cfg, self.log)
            elif mode == "llama":
                backend = LlamaTranslator(self.cfg, self.log)
            else:
                backend = HYMTTranslator(self.cfg, self.log)
            self._backends[mode] = backend
        return backend

    def ensure_downloaded(self, progress=None) -> None:
        self._backend().ensure_downloaded(progress)

    def load(self, progress=None) -> None:
        self._backend().load(progress)

    def translate(self, text, target=None) -> str:
        return self._backend().translate(text, target)

    def generate_reply(self, message) -> str:
        return self._backend().generate_reply(message)

    def shutdown(self) -> None:
        for backend in set(self._backends.values()):
            try:
                shutdown = getattr(backend, "shutdown", None)
                if shutdown is not None:
                    shutdown()
            except Exception as exc:
                self.log.warning("关闭翻译后端失败：%s", exc)
        self._backends.clear()


def create_translator(cfg, log):
    """按配置选择翻译后端并支持运行中在本地模型 / OpenAI API 之间切换。"""
    return TranslatorRouter(cfg, log)
