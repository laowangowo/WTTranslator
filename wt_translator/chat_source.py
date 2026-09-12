"""轮询战争雷霆本地聊天服务器（http://localhost:8111/gamechat）。"""
from __future__ import annotations

import json
import threading
import urllib.request

from .translator import clean_chat_text


def _decode_messages(raw):
    """解析游戏聊天接口响应，兼容宽松 JSON 与多段拼接 JSON。"""
    text = str(raw or "").strip()
    if not text:
        return []
    decoder = json.JSONDecoder(strict=False)
    objects = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        try:
            obj, index = decoder.raw_decode(text, index)
        except ValueError:
            # 最后再按整段宽松解析一次
            objects.append(json.loads(text, strict=False))
            break
        objects.append(obj)

    messages = []
    for obj in objects:
        if isinstance(obj, dict) and "messages" in obj:
            value = obj.get("messages")
            if isinstance(value, list):
                messages.extend(value)
            elif isinstance(value, dict):
                messages.append(value)
        elif isinstance(obj, list):
            messages.extend(obj)
        elif isinstance(obj, dict):
            messages.append(obj)
    return messages


class ChatPoller(threading.Thread):
    """后台线程：按配置间隔请求聊天接口，维护 lastId。"""

    def __init__(self, cfg, on_messages, on_state_change, log):
        super().__init__(name="chat-poller", daemon=True)
        self.cfg = cfg
        self.on_messages = on_messages          # callable(list[dict])
        self.on_state_change = on_state_change  # callable(connected: bool, error: str | None)
        self.log = log
        self.last_id = 0
        self._seen = set()
        self._stop = threading.Event()
        self._connected = False
        self._failures = 0
        try:
            self._failure_threshold = max(1, int(cfg.get("chat_failure_threshold", 5)))
        except (TypeError, ValueError):
            self._failure_threshold = 5

    # ---------- 对外 ----------
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self.log.info("聊天轮询已启动，lastId=%s", self.last_id)
        while not self._stop.wait(self._interval()):
            try:
                self._poll_once()
            except Exception as exc:  # 兜底，避免线程退出
                self.log.exception("轮询异常")
                self._handle_failure(str(exc))

    # ---------- 内部 ----------
    def _interval(self) -> float:
        try:
            return max(0.05, float(self.cfg.get("poll_interval", 1.0)))
        except (TypeError, ValueError):
            return 1.0

    def _timeout(self) -> float:
        try:
            return max(0.5, float(self.cfg.get("request_timeout", 2.0)))
        except (TypeError, ValueError):
            return 2.0

    def _poll_once(self) -> None:
        url = (
            str(self.cfg.get("server_url", "http://localhost:8111")).rstrip("/")
            + f"/gamechat?lastId={self.last_id}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "WT-Translator/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout()) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            messages = _decode_messages(raw)
        except Exception as exc:
            self._handle_failure(str(exc))
            return

        new = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            mid = item.get("id")
            if not isinstance(mid, int):
                continue
            if mid > self.last_id and mid not in self._seen:
                self._seen.add(mid)
                item["mode"] = clean_chat_text(item.get("mode"))
                item["sender"] = clean_chat_text(item.get("sender"))
                item["msg"] = clean_chat_text(item.get("msg"))
                new.append(item)
        if new:
            new.sort(key=lambda m: (m.get("time", m["id"]) if isinstance(m.get("time"), (int, float)) else m["id"]))
            self.last_id = max(m["id"] for m in new)
            self.log.info("收到 %d 条新消息，lastId=%d", len(new), self.last_id)
            self._mark_connected()
            self.on_messages(new)
        else:
            self._mark_connected()

    def _mark_connected(self) -> None:
        self._failures = 0
        if not self._connected:
            self._connected = True
            self.log.info("已连接聊天服务器")
            self.on_state_change(True, None)

    def _handle_failure(self, error: str) -> None:
        """瞬时失败先重试；连续失败达到阈值才视为对局结束。"""
        self._failures += 1
        if self._failures < self._failure_threshold:
            self.log.info(
                "聊天请求暂时失败（%d/%d），继续重试：%s",
                self._failures, self._failure_threshold, error,
            )
            return
        self.last_id = 0
        self._seen.clear()
        if self._connected:
            self._connected = False
            self.on_state_change(False, error)
            self.log.warning("连续 %d 次请求失败，视为对局结束，lastId 已归零：%s",
                             self._failures, error)
        else:
            self.log.debug("聊天服务器仍不可用：%s", error)
