"""selftest.py - 无需模型/网络/界面的本地自检。

用法：
    python main.py --selftest     # 从主程序入口运行
    python selftest.py            # 独立运行

逐项检查核心逻辑与环境健康度，输出 [PASS]/[FAIL]/[INFO]；
存在 [FAIL] 时以退出码 1 结束，全部通过时退出码 0。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

from wt_translator.config import (
    Config,
    DEFAULTS,
    PACKAGED_CONFIG,
    PROJECT_ROOT,
    SETTINGS_FILE,
)
from wt_translator.blacklist import Blacklist, compile_patterns
from wt_translator.chat_source import _decode_messages
from wt_translator.translator import clean_chat_text, has_cjk, strip_color_markup
from wt_translator.updater import compare_versions, should_notify_update
from wt_translator.glossary import read as read_glossary

# (说明, 配置键, 类型构造器或可调用断言)
_CONFIG_CHECKS = [
    ("聊天服务器地址", "server_url", str),
    ("轮询间隔", "poll_interval", float),
    ("翻译引擎", "engine", lambda v: v in ("llama", "transformers", "openai")),
    ("更新检查地址", "update_check_url", lambda v: isinstance(v, str) and v.startswith("http")),
    ("黑名单", "blacklist", list),
    ("最大待翻译数", "max_pending", int),
    ("窗口宽度", "window_width", int),
    ("目标语言", "target_language", str),
    ("模型 ID", "model_id", str),
    ("上报地址", "report_url", str),
]

_results = []  # (name, ok: bool, detail: str)


def check(name, ok, detail=""):
    _results.append((name, bool(ok), str(detail)))


def case_config(cfg):
    """配置分层加载与关键键完整性。"""
    try:
        c = cfg or Config()
    except Exception as exc:
        check("配置加载", False, f"Config() 抛出异常: {exc}")
        return
    check("配置加载", True, f"path={c.path}")
    failed = []
    for label, key, probe in _CONFIG_CHECKS:
        value = c.get(key, "<missing>")
        try:
            ok = probe(value) if not isinstance(probe, type) else isinstance(value, probe)
        except Exception:
            ok = False
        if not ok:
            failed.append(f"{key}={value!r} 不符合 {getattr(probe, '__name__', '条件')}")
    check("配置关键键完整", not failed, "; ".join(failed) or f"{len(_CONFIG_CHECKS)} 项检查通过")


def case_config_files():
    """已有的配置文件必须是合法 JSON。"""
    problems = []
    for path, label in ((PACKAGED_CONFIG, "打包配置"), (SETTINGS_FILE, "用户设置")):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                json.load(fh)
        except (OSError, ValueError) as exc:
            problems.append(f"{label} {path}: {exc}")
    check("配置文件 JSON 合法", not problems, "; ".join(problems) or "无损坏配置文件")


def case_blacklist():
    regexes, errors = compile_patterns(["攻击.*", "hello", "(\\d+"])
    check("黑名单正则编译", len(regexes) == 2 and len(errors) == 1,
          f"编译 {len(regexes)} 条，无效 {len(errors)} 条: {errors}")
    check("黑名单大小写不敏感", Blacklist(["SPAM"]).matches("this is spam"), "Blacklist([\"SPAM\"]) 匹配 spam")
    check("黑名单不误伤", not Blacklist(["x"]).matches("abc"), "不匹配未命中文本")


def case_versions():
    checks = [
        ("新版本检测", compare_versions("1.2.0", "1.3.0"), True),
        ("旧版本忽略", compare_versions("1.3.0", "1.2.0"), False),
        ("同版本忽略", compare_versions("1.2.0", "1.2.0"), False),
        ("补位版本比较", compare_versions("1.2", "1.2.1"), True),
        ("应弹窗", should_notify_update("1.2.0", "1.3.0"), True),
        ("同版本不弹", should_notify_update("1.3.0", "1.2.0"), False),
        ("已跳过不弹", should_notify_update("1.2.0", "1.3.0", "1.3.0"), False),
        ("跳过旧版仍弹", should_notify_update("1.2.0", "1.3.0", "1.2.0"), True),
        ("空版本不弹", should_notify_update("1.2.0", ""), False),
    ]
    failed = [f"{name} 结果={got} 期望={want}" for name, got, want in checks if got != want]
    check("版本比较与更新提示逻辑", not failed,
          "; ".join(failed) or f"{len(checks)} 项分支通过")


def case_chat_source():
    """8111 聊天接口的宽松 JSON 解析。"""
    ok_empty = _decode_messages("") == []
    joined = _decode_messages('{"messages":[{"msg":"a"}]}{"messages":[{"msg":"b"}]}')
    ok_joined = len(joined) == 2 and joined[0]["msg"] == "a" and joined[1]["msg"] == "b"
    # strict=False 允许字符串中的未转义换行
    raw = '{"messages":[{"msg":"line1\nline2"}]}'
    relaxed = _decode_messages(raw)
    ok_relaxed = len(relaxed) == 1 and relaxed[0]["msg"] == "line1\nline2"
    try:
        _decode_messages("this is not json")
        ok_bad = False
    except ValueError:
        ok_bad = True
    check("聊天接口宽松 JSON 解析",
          ok_empty and ok_joined and ok_relaxed and ok_bad,
          f"空={ok_empty} 多段={ok_joined} 宽松={ok_relaxed} 坏输入抛错={ok_bad}")


def case_text_cleanup():
    ok_color = strip_color_markup("<color=#ff0000>red</color> done") == "red done"
    ok_color8 = strip_color_markup("a <color=#00ff00ff>x</color> b") == "a x b"
    ok_tab = clean_chat_text("attack  \\t point") == "attack point"
    ok_real_tab = clean_chat_text("a\tb") == "ab"
    ok_space = clean_chat_text("  hello   world  ") == "hello world"
    ok_ctrl = clean_chat_text("\x00hi\x01") == "hi"
    ok_cjk = has_cjk("你好 world") and not has_cjk("hello")
    check("消息文本清理", all([ok_color, ok_color8, ok_tab, ok_real_tab, ok_space, ok_ctrl, ok_cjk]),
          f"颜色标记={ok_color}/{ok_color8} 制表符={ok_tab}/{ok_real_tab} 空白={ok_space} 控制字符={ok_ctrl} CJK={ok_cjk}")


def case_glossary():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        data = read_glossary(path)
        ok = data == {"version": 1, "terms": []}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    check("术语表容错读取", ok, f"缺失文件返回默认 {data}" if "data" in dir() else "读取失败")


def case_phrases():
    """自定义常用短语数据层：容错读取、增删改查、空值拒绝。"""
    from wt_translator.phrases import (
        add_phrase,
        default_data,
        delete_phrase,
        find_phrase,
        list_phrases,
        read,
        update_phrase,
    )

    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)  # 从缺失状态开始
    try:
        ok_missing = read(path) == default_data()
        a = add_phrase("需要支援", "en", path=path)
        b = add_phrase("感谢队友", "", path=path)
        ok_add = a["id"] == 1 and b["id"] == 2 and len(list_phrases(path)) == 2
        u = update_phrase(1, text="请求支援", lang="ja", path=path)
        found = find_phrase(1, path)
        ok_update = (
            found is not None
            and found["text"] == "请求支援"
            and u["lang"] == "ja"
        )
        try:
            update_phrase(1, text="   ", path=path)
            ok_empty_reject = False
        except ValueError:
            ok_empty_reject = True
        delete_phrase(2, path=path)
        ok_delete = [p["id"] for p in list_phrases(path)] == [1]
        try:
            delete_phrase(999, path=path)
            ok_delete_missing = False
        except KeyError:
            ok_delete_missing = True
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{corrupt json")
        ok_corrupt = read(path) == default_data()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    ok = ok_missing and ok_add and ok_update and ok_empty_reject \
        and ok_delete and ok_delete_missing and ok_corrupt
    check("短语数据层增删改查",
          ok,
          f"缺失容错={ok_missing} 新增={ok_add} 更新={ok_update} "
          f"空文本拒绝={ok_empty_reject} 删除={ok_delete} "
          f"删不存在抛错={ok_delete_missing} 损坏容错={ok_corrupt}")


def info_environment(cfg):
    """环境提示（不参与成败判定）。"""
    c = cfg or Config()
    lines = [f"Python {sys.version.split()[0]} ({sys.platform})", f"项目根目录 {PROJECT_ROOT}"]
    for label, key in (("GGUF 翻译模型", "gguf_dir"), ("Transformers 模型", "model_dir"),
                       ("语音模型", "sensevoice_dir")):
        rel = str(c.get(key, ""))
        lines.append(f"{label}:{'已下载' if rel and os.path.isdir(os.path.join(PROJECT_ROOT, rel)) else '未下载'}")
    lines.append(f"引擎: {c.get('engine')}  目标语言: {c.get('target_language')}")
    for path, label in ((PACKAGED_CONFIG, "打包配置"), (SETTINGS_FILE, "用户设置")):
        lines.append(f"{label}:{'存在' if os.path.exists(path) else '不存在'}")
    try:
        with tempfile.NamedTemporaryFile(dir=PROJECT_ROOT, delete=True):
            writable = True
    except OSError:
        writable = False
    lines.append(f"程序目录可写: {'是' if writable else '否（安装包模式下属正常）'}")
    return lines


def main(cfg=None) -> None:
    if getattr(sys, "frozen", False) and os.path.dirname(sys.executable) not in sys.path:
        sys.path.insert(0, os.path.dirname(sys.executable))

    print("=" * 56)
    print("WTTranslator 自检（无需模型/网络/界面）")
    print("=" * 56)

    case_config(cfg)
    case_config_files()
    case_blacklist()
    case_versions()
    case_chat_source()
    case_text_cleanup()
    case_glossary()
    case_phrases()

    print()
    for name, ok, detail in _results:
        state = "PASS" if ok else "FAIL"
        print(f"[{state}] {name}")
        if detail and not ok:
            print(f"       {detail}")

    print()
    print("-" * 56)
    print("环境提示（不计入成败）:")
    for line in info_environment(cfg):
        print(f"  [INFO] {line}")

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print("-" * 56)
    print(f"结果: {passed}/{len(_results)} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()