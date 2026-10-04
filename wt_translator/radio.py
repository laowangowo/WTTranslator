"""识别《战争雷霆》的无线电快捷消息（语音指令）。

游戏里的无线电指令是一批固定文本（进攻D点、掩护我、Attack the D point 等），
客户端会按自己的语言显示；有些客户端/接口返回的是英文原文，所以只靠“含中文就跳过”
拦不住它们 —— 这里直接按内容匹配，跳过翻译。
"""
from __future__ import annotations

import re

_STRIP_RE = re.compile(
    r"[\s\.,;:!?…~、。，；：！？·「」『』“”\"'（）()\[\]【】<>《》\-—_+*/\\|]+"
)
# 万一调用方没先剥掉颜色标记，这里也顺手清一下 <color=...>、</color>
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")

_RADIO_PHRASES_EN = """
    attacktheapoint attackthebpoint attackthecpoint attackthedpoint
    defendtheapoint defendthebpoint defendthecpoint defendthedpoint
    attackpointa attackpointb attackpointc attackpointd
    defendpointa defendpointb defendpointc defendpointd
    coverme followme needbackup needsupport ineedbackup ineedsupport
    supportme requestingsupport requestbackup
    repairing reloading imrepairing imreloading
    enemyspotted attackmytarget engaging enemytarget
    roger affirmative negative copy that understood
    yes no ok okay good
    thanks thankyou welldone sorry
    goodluck havefun goodluckandhavefun glhf
    imhit ihavebeenhit imtakingfire
    defendourbase attacktheirbase
    destroythetarget requestairstrike requestartillerysupport
    retreat fallback regroup
    watchout beware lookout
"""

_RADIO_PHRASES_ZH = """
    进攻a点 进攻b点 进攻c点 进攻d点 防守a点 防守b点 防守c点 防守d点
    攻击a点 攻击b点 攻击c点 攻击d点 保卫a点 保卫b点 保卫c点 保卫d点
    掩护我 跟着我 跟随我 跟上我 需要支援 请求支援 我需要支援 请求协助
    正在修理 修理中 正在装填 装填中 发现敌人 看到敌人 攻击我的目标
    收到 明白 了解 是的 不是 好的 可以 不行
    谢谢 感谢 干得好 做得好 抱歉 对不起
    祝好运 玩得开心 祝你好运
    我被打中了 我被击中了 我中弹了 我受到攻击
    守卫基地 防守基地 摧毁目标 请求空袭 请求火炮支援
    撤退 撤离 集合 归队 小心 注意 警惕
"""

_RADIO_PHRASES_RU = """
    прикройменя замой нужнапомощь ремонтируюсь перезаряжаюсь
    вижупротивника атакуюцель есть принято спасибо молодец извини
"""

# 目标点指令的通用形式（字母可变）
_POINT_PATTERNS = (
    re.compile(r"^(?:attack|defend|secure|capture|bomb)(?:the)?(?:point)?[abcd]$"),
    re.compile(r"^(?:进攻|防守|攻击|保卫|夺取|轰炸)[abcd]点$"),
    re.compile(r"^(?:点|目标)[abcd]$"),
)


def _phrase_set(raw: str) -> frozenset:
    return frozenset(raw.split())


_RADIO_PHRASES = (
    _phrase_set(_RADIO_PHRASES_EN)
    | _phrase_set(_RADIO_PHRASES_ZH)
    | _phrase_set(_RADIO_PHRASES_RU)
)


def normalize(text) -> str:
    """归一化：全小写，去掉空白、标点与装饰符号，便于比较。"""
    value = _TAG_RE.sub("", str(text or "")).lower()
    return _STRIP_RE.sub("", value)


def is_radio_message(text) -> bool:
    """判断一条聊天内容是不是游戏无线电快捷指令。"""
    key = normalize(text)
    if not key:
        return False
    if key in _RADIO_PHRASES:
        return True
    return any(pattern.match(key) for pattern in _POINT_PATTERNS)
