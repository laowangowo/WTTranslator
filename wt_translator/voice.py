"""语音录制与 OpenAI 兼容语音识别（/audio/transcriptions）。"""
from __future__ import annotations

import ctypes
import array
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

from .config import PROJECT_ROOT


class VoiceError(RuntimeError):
    pass


WAVE_ERROR_HINTS = {
    2: "未找到录音设备（设备 ID 无效）",
    3: "录音设备未启用或被禁用，请在系统设置中开启麦克风",
    4: "录音设备已被其他程序独占，请关闭占用麦克风的程序后重试",
    5: "无法分配录音缓冲区",
    6: "没有可用的录音驱动",
    32: "录音设备不支持所选音频格式",
}

WAVE_MAPPER = 0xFFFFFFFF
WAVE_FORMAT_PCM = 1
WAVE_HEADER_SIZE = 44


class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_uint),
        ("nAvgBytesPerSec", ctypes.c_uint),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class _WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_char_p),
        ("dwBufferLength", ctypes.c_uint),
        ("dwBytesRecorded", ctypes.c_uint),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint),
        ("dwLoops", ctypes.c_uint),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


class VoiceRecorder:
    """跨平台录音器：Windows 用 MCI，macOS 优先 sounddevice，其次 ffmpeg。"""

    def __init__(self, log, max_seconds=60):
        self.log = log
        self.path = os.path.join(tempfile.gettempdir(), f"wt_voice_{os.getpid()}.wav")
        self._active = False
        self._proc = None
        self._stream = None
        self._frames = []
        self._win_handle = None
        self._win_header = None
        self._win_buffer = None
        self._win_rate = 16000
        self._win_channels = 1
        try:
            self._win_seconds = max(5, min(600, int(max_seconds)))
        except (TypeError, ValueError):
            self._win_seconds = 60

    # ---------- Windows waveIn ----------
    def _start_windows(self) -> None:
        winmm = ctypes.windll.winmm
        handle = ctypes.c_void_p()
        last_code = None
        for rate, channels in (
            (16000, 1), (44100, 1), (48000, 1), (44100, 2), (48000, 2)
        ):
            fmt = _WAVEFORMATEX(
                wFormatTag=WAVE_FORMAT_PCM,
                nChannels=channels,
                nSamplesPerSec=rate,
                nAvgBytesPerSec=rate * channels * 2,
                nBlockAlign=channels * 2,
                wBitsPerSample=16,
                cbSize=0,
            )
            code = winmm.waveInOpen(
                ctypes.byref(handle), WAVE_MAPPER, ctypes.byref(fmt), 0, 0, 0
            )
            if code == 0:
                self._win_rate = rate
                self._win_channels = channels
                break
            last_code = code
        else:
            hint = WAVE_ERROR_HINTS.get(last_code, "未知录音错误")
            raise VoiceError(f"{hint}（waveIn {last_code}）")

        buffer_size = self._win_rate * self._win_channels * 2 * self._win_seconds
        self._win_buffer = ctypes.create_string_buffer(buffer_size)
        header = _WAVEHDR()
        header.lpData = ctypes.cast(self._win_buffer, ctypes.c_char_p)
        header.dwBufferLength = buffer_size
        self._win_header = header
        self._win_handle = handle

        for func, args in (
            (winmm.waveInPrepareHeader, (handle, ctypes.byref(header), ctypes.sizeof(header))),
            (winmm.waveInAddBuffer, (handle, ctypes.byref(header), ctypes.sizeof(header))),
            (winmm.waveInStart, (handle,)),
        ):
            code = func(*args)
            if code != 0:
                self._close_windows()
                hint = WAVE_ERROR_HINTS.get(code, "未知录音错误")
                raise VoiceError(f"{hint}（waveIn {code}）")

    def _close_windows(self) -> None:
        winmm = ctypes.windll.winmm
        handle = self._win_handle
        header = self._win_header
        if handle is not None:
            try:
                if header is not None:
                    winmm.waveInUnprepareHeader(
                        handle, ctypes.byref(header), ctypes.sizeof(header)
                    )
            except Exception:
                pass
            try:
                winmm.waveInClose(handle)
            except Exception:
                pass
        self._win_handle = None
        self._win_header = None
        self._win_buffer = None

    def _stop_windows(self) -> None:
        winmm = ctypes.windll.winmm
        handle = self._win_handle
        header = self._win_header
        if handle is None or header is None:
            raise VoiceError("录音尚未开始")
        winmm.waveInStop(handle)
        winmm.waveInReset(handle)
        written = int(header.dwBytesRecorded)
        raw = self._win_buffer.raw[:written] if written else b""
        self._close_windows()
        import wave

        with wave.open(self.path, "wb") as fh:
            fh.setnchannels(self._win_channels)
            fh.setsampwidth(2)
            fh.setframerate(self._win_rate)
            fh.writeframes(raw)

    # ---------- macOS：sounddevice 优先，ffmpeg 兜底 ----------
    def _start_darwin(self) -> None:
        try:
            import numpy as np  # noqa: F401
            import sounddevice as sd
        except Exception:
            self._start_ffmpeg()
            return

        self._frames = []
        self._stream = sd.InputStream(
            samplerate=16000, channels=1, dtype="int16",
            callback=lambda indata, frames, time_info, status: self._frames.append(indata.copy()),
        )
        self._stream.start()

    def _stop_darwin(self) -> None:
        if self._stream is not None:
            import sounddevice as sd

            self._stream.stop()
            self._stream.close()
            self._stream = None
            if self._frames:
                import numpy as np
                import wave

                audio = np.concatenate(self._frames, axis=0).astype(np.int16)
                with wave.open(self.path, "wb") as fh:
                    fh.setnchannels(1)
                    fh.setsampwidth(2)
                    fh.setframerate(16000)
                    fh.writeframes(audio.tobytes())
            return
        self._stop_ffmpeg()

    def _start_ffmpeg(self) -> None:
        ffmpeg = "ffmpeg"
        cmd = [
            ffmpeg, "-y", "-f", "avfoundation", "-i", ":0",
            "-ar", "16000", "-ac", "1", self.path,
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise VoiceError(
                "macOS 语音输入需要 sounddevice + numpy，或安装 ffmpeg。"
                f"当前无法启动录音设备：{exc}"
            ) from exc

    def _stop_ffmpeg(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    # ---------- Linux / 其它：arecord ----------
    def _start_arecord(self) -> None:
        try:
            self._proc = subprocess.Popen(
                ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", self.path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise VoiceError(f"当前系统无法录音（需要 arecord）：{exc}") from exc

    # ---------- 对外 ----------
    @property
    def recording(self) -> bool:
        return self._active

    def start(self) -> None:
        if self._active:
            return
        try:
            os.remove(self.path)
        except OSError:
            pass
        if os.name == "nt":
            self._start_windows()
        elif sys.platform == "darwin":
            self._start_darwin()
        else:
            self._start_arecord()
        self._active = True
        self.log.info("开始录音：%s", self.path)

    def stop(self) -> str:
        if not self._active:
            raise VoiceError("当前没有在录音")
        self._active = False
        try:
            if os.name == "nt":
                self._stop_windows()
            elif sys.platform == "darwin":
                self._stop_darwin()
            else:
                self._stop_ffmpeg()
        except Exception:
            self._active = False
            raise
        if not os.path.isfile(self.path) or os.path.getsize(self.path) < 1024:
            raise VoiceError("录音文件为空，请检查麦克风权限或设备")
        self.log.info("录音完成：%s", self.path)
        return self.path

    def cancel(self) -> None:
        self._active = False
        try:
            if os.name == "nt":
                self._close_windows()
            elif sys.platform == "darwin":
                self._stop_darwin()
            else:
                self._stop_ffmpeg()
        except Exception:
            pass


def _multipart_body(fields, file_field, file_path, boundary):
    chunks = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    filename = os.path.basename(file_path)
    chunks.append(f"--{boundary}\r\n".encode("utf-8"))
    chunks.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n'
        ).encode("utf-8")
    )
    chunks.append(b"Content-Type: audio/wav\r\n\r\n")
    with open(file_path, "rb") as fh:
        chunks.append(fh.read())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks)


def _normalized_dir(value: str) -> str:
    path = str(value or "models/sense-voice-small-int8").strip()
    if not os.path.isabs(path):
        path = os.path.join(PROJECT_ROOT, path)
    return os.path.normpath(path)


def _read_wav(path):
    """读取 16-bit PCM WAV，返回 (采样率, [-1,1] float 列表)。"""
    import wave

    with wave.open(path, "rb") as fh:
        channels = fh.getnchannels()
        width = fh.getsampwidth()
        rate = fh.getframerate()
        raw = fh.readframes(fh.getnframes())
    if width != 2:
        raise VoiceError(f"录音位深不支持（{width * 8} bit），需要 16-bit PCM")
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        merged = array.array("h")
        for index in range(0, len(samples) - channels + 1, channels):
            merged.append(int(sum(samples[index:index + channels]) / channels))
        samples = merged
    return rate, [value / 32768.0 for value in samples]


class SenseVoiceASR:
    """本地 SenseVoiceSmall INT8 ONNX 识别（基于 sherpa-onnx 运行时）。"""

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self._lock = threading.RLock()
        self._recognizer = None

    @property
    def model_dir(self) -> str:
        return _normalized_dir(self.cfg.get("sensevoice_dir", "models/sense-voice-small-int8"))

    @property
    def model_path(self) -> str:
        return os.path.join(self.model_dir, "model.int8.onnx")

    @property
    def tokens_path(self) -> str:
        return os.path.join(self.model_dir, "tokens.txt")

    def is_downloaded(self) -> bool:
        return os.path.isfile(self.model_path) and os.path.isfile(self.tokens_path)

    # ---------- 模型下载 ----------
    def ensure_downloaded(self, progress=None) -> None:
        if self.is_downloaded():
            if progress:
                progress("SenseVoice 模型已存在")
            return
        os.makedirs(self.model_dir, exist_ok=True)
        model_url = str(self.cfg.get("sensevoice_model_url", "") or "").strip()
        tokens_url = str(self.cfg.get("sensevoice_tokens_url", "") or "").strip()
        if not os.path.isfile(self.model_path):
            if not model_url:
                raise VoiceError("未配置 SenseVoice 模型下载地址")
            if progress:
                progress("正在下载 SenseVoiceSmall INT8 模型（约 230 MB）…")
            self._download(model_url, self.model_path, progress, "SenseVoice 模型")
        if not os.path.isfile(self.tokens_path):
            if not tokens_url:
                raise VoiceError("未配置 SenseVoice tokens 下载地址")
            if progress:
                progress("正在下载 SenseVoice tokens.txt…")
            self._download(tokens_url, self.tokens_path, progress, "SenseVoice tokens")
        if not self.is_downloaded():
            raise VoiceError("SenseVoice 模型文件不完整（需要 model.int8.onnx 和 tokens.txt）")
        if progress:
            progress("SenseVoice 模型准备完成")

    @staticmethod
    def _download(url, dest, progress, label):
        tmp = dest + ".part"
        last_error = None
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
                    total = int(resp.headers.get("Content-Length") or 0)
                    done = 0
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if progress and total:
                            progress(
                                f"正在下载{label}：{done / 1048576:.1f} / "
                                f"{total / 1048576:.1f} MB"
                            )
                os.replace(tmp, dest)
                return
            except Exception as exc:
                last_error = exc
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                time.sleep(1.5 * attempt)
        raise VoiceError(f"{label}下载失败：{last_error}")

    # ---------- 推理 ----------
    def load(self, progress=None) -> None:
        with self._lock:
            if self._recognizer is not None:
                return
            if not self.is_downloaded():
                self.ensure_downloaded(progress)
            try:
                import sherpa_onnx
            except ImportError as exc:
                raise VoiceError(
                    "缺少 sherpa-onnx 运行时，请重新安装依赖（pip install sherpa-onnx）"
                ) from exc
            if progress:
                progress("正在加载 SenseVoice 模型…")
            language = str(self.cfg.get("voice_language", "zh") or "auto").strip() or "auto"
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=self.model_path,
                tokens=self.tokens_path,
                num_threads=max(1, int(self.cfg.get("sensevoice_num_threads", 2) or 2)),
                use_itn=bool(self.cfg.get("sensevoice_use_itn", True)),
                language=language,
                provider="cpu",
                debug=False,
            )
            self.log.info("SenseVoice 模型就绪：%s", self.model_path)
            if progress:
                progress("SenseVoice 模型就绪")

    def transcribe(self, path, language=None) -> str:
        self.load()
        rate, samples = _read_wav(path)
        with self._lock:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(rate, samples)
            self._recognizer.decode_stream(stream)
            text = stream.result.text
        return str(text or "").strip()

    def shutdown(self) -> None:
        with self._lock:
            self._recognizer = None


def transcribe(path, cfg, log, timeout=None):
    """调用 OpenAI 兼容语音识别接口，返回识别文本。"""
    base = str(
        cfg.get("voice_api_base_url", "") or cfg.get("api_base_url", "") or ""
    ).strip().rstrip("/")
    if not base:
        raise VoiceError("未配置语音识别 API 地址")
    if base.lower().endswith("/audio/transcriptions"):
        url = base
    else:
        url = base + "/audio/transcriptions"
    key = str(cfg.get("voice_api_key", "") or cfg.get("api_key", "") or "").strip()
    model = str(cfg.get("voice_model", "whisper-1") or "whisper-1").strip()
    language = str(cfg.get("voice_language", "zh") or "zh").strip()
    if timeout is None:
        try:
            timeout = max(10, float(cfg.get("api_timeout", 60)))
        except (TypeError, ValueError):
            timeout = 60.0
    fields = {"model": model, "response_format": "json"}
    if language:
        fields["language"] = language
    boundary = "----WTTranslator" + uuid.uuid4().hex
    body = _multipart_body(fields, "file", path, boundary)
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    log.info("语音识别请求：%s（模型 %s）", url, model)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise VoiceError(f"语音识别失败（HTTP {exc.code}）：{detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise VoiceError(f"语音识别失败：{exc.reason}") from exc
    text = data.get("text") if isinstance(data, dict) else None
    if not text:
        raise VoiceError(f"语音识别返回异常：{data}")
    return str(text).strip()
