"""自定义常用短语管理：独立 JSON 文件持久化（增删改查）。

数据格式（projects/phrases.json）：
    {"version": 1, "phrases": [{"id": 1, "text": "需要支援", "lang": "en"}]}

- lang 为发送时默认目标语言代码（空字符串表示自动/未指定），
  当前版本仅存储，供后续“选择语言发送”功能使用。
- 沿用 glossary.py 先例：容错读取（损坏/缺失返回默认数据）、
  原子写入（.part 临时文件 + os.replace）。
"""
from __future__ import annotations

import json
import os

from .config import PROJECT_ROOT

PHRASES_FILE = os.path.join(PROJECT_ROOT, "phrases.json")

_ALLOWED_KEYS = ("id", "text", "lang")


def default_data():
    return {"version": 1, "phrases": []}


def read(path=None):
    """读取短语文件，失败/损坏/格式异常时返回默认空数据，不抛异常。"""
    path = path or PHRASES_FILE
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return default_data()
    if not isinstance(data, dict):
        return default_data()
    phrases = data.get("phrases")
    if not isinstance(phrases, list):
        phrases = []
    cleaned = []
    seen_ids = set()
    for item in phrases:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "") or "").strip()
        if not text:
            continue
        try:
            pid = int(item.get("id", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0:
            continue
        # 去重：同文件内 id 只保留第一个
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        cleaned.append({
            "id": pid,
            "text": text,
            "lang": str(item.get("lang", "") or "").strip(),
        })
    return {"version": 1, "phrases": cleaned}


def write(data, path=None):
    """原子写入短语数据（先写 .part 再 os.replace）。"""
    path = path or PHRASES_FILE
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _next_id(data):
    ids = [p["id"] for p in data.get("phrases", [])]
    return (max(ids) + 1) if ids else 1


def list_phrases(path=None):
    """返回全部短语列表（readonly 快照，不含读写副作用）。"""
    return read(path).get("phrases", [])


def find_phrase(phrase_id, path=None):
    """按 id 查找短语；不存在时返回 None。"""
    for p in read(path).get("phrases", []):
        if p["id"] == phrase_id:
            return dict(p)
    return None


def add_phrase(text, lang="", path=None):
    """新增短语，返回新条目 dict。文本必须非空，lang 去空白。"""
    text = str(text or "").strip()
    if not text:
        raise ValueError("短语内容不能为空")
    data = read(path)
    phrase = {
        "id": _next_id(data),
        "text": text,
        "lang": str(lang or "").strip(),
    }
    data["phrases"].append(phrase)
    write(data, path)
    return dict(phrase)


def update_phrase(phrase_id, text=None, lang=None, path=None):
    """更新短语；text/lang 只传需要修改的字段。返回更新后的条目。

    Raises:
        ValueError: text 传入且为空（不允许清空文本）
        KeyError: 短语不存在
    """
    data = read(path)
    for p in data["phrases"]:
        if p["id"] == phrase_id:
            if text is not None:
                new_text = str(text).strip()
                if not new_text:
                    raise ValueError("短语内容不能为空")
                p["text"] = new_text
            if lang is not None:
                p["lang"] = str(lang or "").strip()
            write(data, path)
            return dict(p)
    raise KeyError(f"短语不存在: {phrase_id}")


def delete_phrase(phrase_id, path=None):
    """删除短语；不存在时抛 KeyError。"""
    data = read(path)
    before = len(data["phrases"])
    data["phrases"] = [p for p in data["phrases"] if p["id"] != phrase_id]
    if len(data["phrases"]) == before:
        raise KeyError(f"短语不存在: {phrase_id}")
    write(data, path)
    return True