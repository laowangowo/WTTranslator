"""消息黑名单：基于正则表达式匹配。"""
from __future__ import annotations

import re
from typing import Iterable, List, Tuple


def compile_patterns(
    patterns: Iterable[str], case_insensitive: bool = True
) -> Tuple[List[re.Pattern], List[str]]:
    flags = re.IGNORECASE if case_insensitive else 0
    regexes: List[re.Pattern] = []
    errors: List[str] = []
    for pattern in patterns:
        if not pattern or not isinstance(pattern, str):
            continue
        try:
            regexes.append(re.compile(pattern, flags))
        except re.error as exc:
            errors.append(f"无效的正则 [{pattern}]：{exc}")
    return regexes, errors


class Blacklist:
    def __init__(self, patterns: Iterable[str] = (), case_insensitive: bool = True):
        self.regexes, self.errors = compile_patterns(patterns, case_insensitive)

    @property
    def count(self) -> int:
        return len(self.regexes)

    def matches(self, text: str) -> bool:
        return any(regex.search(text or "") for regex in self.regexes)
