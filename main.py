"""战争雷霆聊天翻译器入口。

用法：
    python main.py                       启动悬浮窗
    python main.py --download-model      只下载模型
    python main.py --test-translate TEXT 翻译一段文本后退出（验证模型）
    python main.py --selftest            运行无需模型/界面的自检
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import time

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def _stdout_utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def setup_logging(cfg) -> logging.Logger:
    log_file = os.path.join(BASE_DIR, cfg.get("log_file", "wt_translator.log"))
    # mode="w"：每次启动程序时清空旧日志
    handlers = [logging.FileHandler(log_file, mode="w", encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("wt")


def _enqueue(q, item, log):
    """入队；队列满时丢弃最旧的一条，保证翻译的是最新消息。"""
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
            q.put_nowait(item)
        except (queue.Empty, queue.Full):
            pass


def run_gui(cfg, log) -> None:
    from wt_translator.blacklist import Blacklist
    from wt_translator.chat_source import ChatPoller
    from wt_translator.translator import (
        clean_chat_text,
        create_translator,
        has_cjk,
        strip_color_markup,
    )
    from wt_translator.ui import OverlayWindow

    ui_queue = queue.Queue()
    trans_queue = queue.Queue(maxsize=max(5, int(cfg.get("max_pending", 30))))
    stop = threading.Event()

    translator = create_translator(cfg, log)

    def make_blacklist():
        bl = Blacklist(cfg.get("blacklist", []), cfg.get("blacklist_case_insensitive", True))
        return bl

    def on_messages(messages):
        bl = make_blacklist()
        blacklist_enabled = bool(cfg.get("blacklist_enabled", True))
        skip_chinese = bool(cfg.get("skip_chinese_messages", False))
        for message in messages:
            ui_queue.put({"type": "chat", "msg": message})
            text = str(message.get("msg") or "")
            plain = clean_chat_text(strip_color_markup(text))
            if skip_chinese and has_cjk(plain):
                log.info("中文消息跳过翻译：%s", text[:60])
                ui_queue.put({"type": "original", "msg": message, "blacklisted": True})
                continue
            if blacklist_enabled and bl.matches(plain):
                log.info("黑名单命中，直接拦截不送翻译：%s", text[:60])
                ui_queue.put({"type": "original", "msg": message, "blacklisted": True})
                continue
            _enqueue(trans_queue, message, log)

    def on_state(connected, _error):
        if connected:
            ui_queue.put({"type": "status", "text": "已连接对局服务器…"})
        else:
            ui_queue.put({"type": "status", "text": "对局结束或服务器不可用，等待下一局…"})
            ui_queue.put({"type": "match_end"})

    poller = ChatPoller(cfg, on_messages, on_state, log)
    poller.start()

    # 启动时检查更新（后台线程，失败只记日志不打扰）
    if cfg.get("check_update_on_start", True):
        from wt_translator import __version__
        from wt_translator.updater import (
            compare_versions,
            fetch_version_info,
            should_notify_update,
        )

        def update_checker():
            try:
                info = fetch_version_info(
                    str(cfg.get("update_check_url", "https://wt.ngup.eu.org/version")),
                    timeout=float(cfg.get("update_check_timeout", 8)),
                )
                if should_notify_update(
                    __version__,
                    info["version"],
                    cfg.get("skipped_update_version", ""),
                ):
                    log.info("发现新版本：%s", info["version"])
                    ui_queue.put({"type": "update_available", "info": info})
                else:
                    if compare_versions(__version__, info["version"]):
                        log.info("版本 %s 已被跳过，不弹窗", info["version"])
                    else:
                        log.info("已是最新版本（%s）", info.get("version") or "未知")
            except Exception as exc:
                log.info("检查更新失败（忽略）：%s", exc)

        threading.Thread(target=update_checker, name="update-checker", daemon=True).start()

    def model_worker():
        prepared = False
        error_reported = False
        while not stop.wait(0.2):
            if not prepared:
                try:
                    translator.ensure_downloaded(
                        progress=lambda text: ui_queue.put({"type": "status", "text": text})
                    )
                    translator.load(
                        progress=lambda text: ui_queue.put({"type": "status", "text": text})
                    )
                except Exception as exc:
                    log.exception("模型/API 准备失败")
                    if not error_reported:
                        error_reported = True
                        ui_queue.put(
                            {"type": "status", "text": f"模型/API 准备失败：{exc}（修改设置后会自动重试）"}
                        )
                    # 等待用户修正配置/引擎后自动重试
                    stop.wait(3)
                    continue
                prepared = True
                error_reported = False
                blacklist = make_blacklist()
                log.info("黑名单规则 %d 条：%s", blacklist.count, cfg.get("blacklist", []))
                ui_queue.put(
                    {"type": "status", "text": f"翻译引擎就绪，黑名单 {blacklist.count} 条，开始翻译…"}
                )

            try:
                message = trans_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if message is None:
                break
            try:
                text = str(message.get("msg") or "")
                plain = clean_chat_text(strip_color_markup(text))
                # 每条消息重新编译，保证设置页修改后立即生效
                blacklist = make_blacklist()
                skip_chinese = bool(cfg.get("skip_chinese_messages", False))
                if skip_chinese and has_cjk(plain):
                    log.info("中文消息跳过翻译：%s", text[:60])
                    ui_queue.put({"type": "original", "msg": message, "blacklisted": True})
                    continue
                if blacklist.errors:
                    ui_queue.put(
                        {"type": "status", "text": "黑名单存在无效正则：" + "；".join(blacklist.errors)}
                    )
                if bool(cfg.get("blacklist_enabled", True)) and blacklist.matches(plain):
                    log.info("黑名单命中，跳过翻译：%s", text[:60])
                    ui_queue.put({"type": "original", "msg": message, "blacklisted": True})
                    continue
                result = translator.translate(text)
                if not result.strip():
                    result = text
                ui_queue.put(
                    {"type": "translated", "msg": message, "text": result, "original": text}
                )
            except Exception as exc:
                log.exception("翻译失败")
                ui_queue.put({"type": "original", "msg": message, "failed": True})
                ui_queue.put({"type": "status", "text": f"翻译失败：{exc}"})

    threading.Thread(target=model_worker, name="model-worker", daemon=True).start()

    # 使用统计上报（随机设备 ID，每 10 分钟一次心跳）
    if cfg.get("stats_enabled", True):
        from wt_translator.reporter import UsageReporter

        reporter = UsageReporter(cfg, log)
        reporter.start()
    else:
        reporter = None

    def on_quit():
        stop.set()
        poller.stop()
        translator.shutdown()
        if reporter is not None:
            reporter.stop()

    window = OverlayWindow(
        cfg, ui_queue, on_quit, log,
        translate_fn=translator.translate,
        reply_fn=translator.generate_reply,
    )
    window.run()


def cmd_download_model(cfg, log) -> None:
    from wt_translator.translator import create_translator

    translator = create_translator(cfg, log)
    translator.ensure_downloaded(progress=lambda text: print(text, flush=True))
    translator.shutdown()
    print("模型下载完成。", flush=True)


def cmd_test_translate(cfg, log, text) -> None:
    from wt_translator.translator import create_translator

    translator = create_translator(cfg, log)
    translator.ensure_downloaded(progress=lambda t: print(t, flush=True))
    print("正在加载模型…", flush=True)
    translator.load(progress=lambda t: print(t, flush=True))
    result = translator.translate(text)
    translator.shutdown()
    print("原文:", text)
    print("译文:", result)


def cmd_test_voice(cfg, log, wav_path) -> None:
    from wt_translator.voice import SenseVoiceASR

    asr = SenseVoiceASR(cfg, log)
    asr.ensure_downloaded(progress=lambda text: print(text, flush=True))
    print("识别结果:", asr.transcribe(wav_path), flush=True)
    asr.shutdown()


def cmd_test_record(cfg, log, seconds) -> None:
    from wt_translator.voice import SenseVoiceASR, VoiceRecorder

    seconds = max(1.0, min(30.0, float(seconds)))
    recorder = VoiceRecorder(log, max_seconds=max(10, int(seconds) + 5))
    print(f"开始录音 {seconds:g} 秒…", flush=True)
    recorder.start()
    time.sleep(seconds)
    path = recorder.stop()
    print("录音完成:", path, os.path.getsize(path), "bytes", flush=True)
    asr = SenseVoiceASR(cfg, log)
    print("识别结果:", asr.transcribe(path), flush=True)
    asr.shutdown()


def main(argv=None) -> None:
    _stdout_utf8()
    parser = argparse.ArgumentParser(description="战争雷霆聊天翻译器（本地 Hy-MT2-1.8B）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config.json）")
    parser.add_argument("--download-model", action="store_true", help="只下载模型后退出")
    parser.add_argument(
        "--test-translate",
        nargs="?",
        const="Hello, nice shot!",
        metavar="TEXT",
        help="翻译一段文本后退出（验证模型）",
    )
    parser.add_argument("--selftest", action="store_true", help="运行无需模型/界面的自检")
    parser.add_argument(
        "--test-voice",
        metavar="WAV",
        help="用本地 SenseVoice 模型识别 WAV 文件后退出",
    )
    parser.add_argument(
        "--test-record",
        nargs="?",
        const=3,
        type=float,
        metavar="SECONDS",
        help="录音 N 秒并识别（默认 3 秒），用于检查麦克风",
    )
    args = parser.parse_args(argv)

    from wt_translator.config import Config

    cfg = Config(args.config) if args.config else Config()
    if not os.path.exists(cfg.path):
        cfg.save()  # 首次运行生成配置文件

    if args.selftest:
        import selftest

        selftest.main()
        return

    log = setup_logging(cfg)
    if args.download_model:
        cmd_download_model(cfg, log)
        return
    if args.test_translate is not None:
        cmd_test_translate(cfg, log, args.test_translate)
        return
    if args.test_voice is not None:
        cmd_test_voice(cfg, log, args.test_voice)
        return
    if args.test_record is not None:
        cmd_test_record(cfg, log, args.test_record)
        return

    log.info("启动战争雷霆聊天翻译器，Python %s", sys.version.split()[0])
    run_gui(cfg, log)


if __name__ == "__main__":
    main()
