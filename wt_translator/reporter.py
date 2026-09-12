"""使用统计上报：启动生成随机设备 ID，按配置间隔（默认 10 分钟）心跳上报。"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
import uuid

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def generate_device_id() -> str:
    return uuid.uuid4().hex


def valid_device_id(device_id) -> bool:
    return bool(device_id) and bool(_DEVICE_ID_RE.match(str(device_id)))


def get_or_create_device_id(cfg) -> str:
    """读取配置里的设备 ID；没有或非法则生成并保存。"""
    device_id = str(cfg.get("device_id", "") or "").strip()
    if not valid_device_id(device_id):
        device_id = generate_device_id()
        cfg.update(device_id=device_id)
        cfg.save()
    return device_id


def report_once(url, device_id, timeout=8) -> bool:
    """向服务器上报一次在线状态。"""
    if not url or not valid_device_id(device_id):
        return False
    payload = json.dumps({"deviceId": device_id}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "WT-Translator",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


class UsageReporter(threading.Thread):
    """后台心跳线程：启动即上报一次，之后每隔 interval 分钟上报。"""

    def __init__(self, cfg, log):
        super().__init__(name="usage-reporter", daemon=True)
        self.cfg = cfg
        self.log = log
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not self.cfg.get("stats_enabled", True):
            return
        device_id = get_or_create_device_id(self.cfg)
        url = str(self.cfg.get("report_url", "") or "").strip()
        try:
            interval = float(self.cfg.get("report_interval_minutes", 10)) * 60
        except (TypeError, ValueError):
            interval = 600.0
        interval = max(30.0, interval)
        self.log.info("使用统计上报已启用（设备 %s…，每 %.0f 秒）", device_id[:8], interval)
        failures = 0
        while not self._stop.wait(0.2):
            if report_once(url, device_id):
                failures = 0
                self.log.debug("上报成功")
            else:
                failures += 1
                if failures == 1:
                    self.log.info("上报失败（网络不通时忽略，后台自动重试）")
            self._stop.wait(interval)
        self.log.debug("上报线程结束")
