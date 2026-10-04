"""启动时检查更新与下载更新文件。"""
from __future__ import annotations

import json
import os
import re
import urllib.request

from .config import PROJECT_ROOT

_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n]+')


def compare_versions(current, latest) -> bool:
    """版本号比较：current < latest 返回 True（表示有新版本）。"""

    def parts(version):
        return [int(seg) for seg in re.split(r"\D+", str(version)) if seg.isdigit()]

    a = parts(current)
    b = parts(latest)
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else 0
        y = b[i] if i < len(b) else 0
        if x != y:
            return x < y
    return False


def should_notify_update(current, latest, skipped=None) -> bool:
    """判断某个最新版本是否应该弹窗：
    - 最新版本不高于当前版本：不弹
    - 最新版本等于或低于已跳过版本：不弹（更新到更新的版本后恢复弹窗）
    """
    latest = str(latest or "").strip()
    if not latest or not compare_versions(current, latest):
        return False
    skipped = str(skipped or "").strip()
    if skipped and not compare_versions(skipped, latest):
        return False
    return True


def safe_filename(name) -> str:
    """把 updateFile 清理成安全的本地文件名。"""
    cleaned = _SAFE_NAME_RE.sub("_", str(name or "")).strip(" .")
    return cleaned or "WTTranslator-update"


def fetch_version_info(url, timeout=8):
    """请求版本接口，返回 dict(version, changelog, update_file)。"""
    req = urllib.request.Request(url, headers={"User-Agent": "WT-Translator"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if not isinstance(data, dict):
        raise ValueError("版本接口返回格式不正确")
    return {
        "version": str(data.get("version", "")),
        "changelog": str(data.get("changelog", "") or data.get("changeLog", "")),
        "update_file": safe_filename(data.get("updateFile", "") or data.get("update_file", "")),
    }


def download_update(
    url, dest_dir=None, filename="WTTranslator-update", progress=None, timeout=30, protected=None
):
    """下载更新文件到 dest_dir（默认程序根目录），返回保存路径。

    protected：不允许覆盖的路径（如正在运行的 exe）；若目标与之冲突，
    自动改存为 "<名字>.new<扩展名>"。
    """
    dest_dir = dest_dir or PROJECT_ROOT
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, safe_filename(filename))
    if protected:
        try:
            if os.path.normcase(os.path.abspath(dest)) == os.path.normcase(os.path.abspath(protected)):
                stem, ext = os.path.splitext(dest)
                dest = stem + ".new" + ext
        except Exception:
            pass
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done, total)
    os.replace(tmp, dest)
    return dest
