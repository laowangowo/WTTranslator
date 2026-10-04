"""战争雷霆聊天翻译器核心包。"""

import os

from .config import PROJECT_ROOT

_DEFAULT_VERSION = "1.2.0"


def _load_version() -> str:
    """从 config.yml 读取版本号（方便迭代时直接改配置文件，无需改代码）。"""
    path = os.path.join(PROJECT_ROOT, "config.yml")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.lower().startswith("version"):
                    value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
    except OSError:
        pass
    return _DEFAULT_VERSION


__version__ = _load_version()
