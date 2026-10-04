"""术语表管理：独立 JSON 文件（带 version 标签）与远程更新检查。"""
from __future__ import annotations

import json
import os
import urllib.request

from .config import PROJECT_ROOT

GLOSSARY_FILE = os.path.join(PROJECT_ROOT, "glossary.json")


def default_data():
    return {"version": 1, "terms": []}


def read(path=None):
    """读取术语表文件，失败/损坏时返回默认空数据。"""
    path = path or GLOSSARY_FILE
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return default_data()
        terms = data.get("terms")
        if not isinstance(terms, list):
            terms = []
        try:
            version = int(data.get("version", 1))
        except (TypeError, ValueError):
            version = 1
        return {"version": version, "terms": [str(t) for t in terms if str(t).strip()]}
    except (OSError, ValueError):
        return default_data()


def write(data, path=None):
    path = path or GLOSSARY_FILE
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def fetch_remote(url, timeout=8):
    """请求服务器术语表 JSON，返回 {version, terms}。"""
    req = urllib.request.Request(url, headers={"User-Agent": "WT-Translator"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if not isinstance(data, dict) or not isinstance(data.get("terms"), list):
        raise ValueError("术语表接口返回格式不正确")
    try:
        version = int(data.get("version", 1))
    except (TypeError, ValueError):
        version = 1
    return {
        "version": version,
        "terms": [str(t) for t in data["terms"] if str(t).strip()],
    }
