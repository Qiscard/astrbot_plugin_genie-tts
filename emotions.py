# -*- coding: utf-8 -*-
"""情绪感知模块（参考 LMG-arch/astrbot-plugin-realistic-persona）"""

from __future__ import annotations

from collections import deque
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    class _L:
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
    logger = _L()


class EmotionType(Enum):
    HAPPY = "开心"
    SAD = "悲伤"
    ANGRY = "生气"
    EXCITED = "兴奋"
    CALM = "平静"
    CONFUSED = "困惑"
    BORED = "无聊"
    CURIOUS = "好奇"
    SURPRISED = "惊讶"
    ANXIOUS = "焦虑"


# 与 realistic-persona 对齐的强度表
EMOTION_INTENSITY_MAP: Dict[EmotionType, float] = {
    EmotionType.EXCITED: 0.8,
    EmotionType.HAPPY: 0.65,
    EmotionType.SAD: 0.65,
    EmotionType.ANGRY: 0.65,
    EmotionType.SURPRISED: 0.5,
    EmotionType.ANXIOUS: 0.6,
    EmotionType.BORED: 0.4,
    EmotionType.CONFUSED: 0.35,
    EmotionType.CURIOUS: 0.45,
    EmotionType.CALM: 0.15,
}

# 语义别名：用于路由匹配 / 网关情绪名模糊对齐
EMOTION_ALIASES: Dict[EmotionType, List[str]] = {
    EmotionType.HAPPY: ["开心", "高兴", "快乐", "愉快", "欢快", "好耶", "太好了"],
    EmotionType.SAD: ["悲伤", "难过", "伤心", "沮丧", "失望", "痛苦", "哭"],
    EmotionType.ANGRY: ["生气", "愤怒", "恼火", "烦", "讨厌", "气死"],
    EmotionType.EXCITED: ["兴奋", "激动", "热情", "元气", "活力", "太棒"],
    EmotionType.CALM: ["平静", "冷静", "淡定", "标准", "默认", "普通", "原始"],
    EmotionType.CONFUSED: ["困惑", "迷惑", "懵", "不懂", "疑惑"],
    EmotionType.BORED: ["无聊", "无趣", "没意思", "枯燥"],
    EmotionType.CURIOUS: ["好奇", "想知道", "有趣", "为什么"],
    EmotionType.SURPRISED: ["惊讶", "震惊", "吃惊", "意外"],
    EmotionType.ANXIOUS: ["焦虑", "担心", "紧张", "害怕", "不安", "恐惧"],
}

DEFAULT_EMOTION_LABELS = ",".join(e.value for e in EmotionType) + ",默认"


class EmotionAnalyzer:
    """关键词情绪分析（参考 realistic-persona）"""

    EMOTION_KEYWORDS: Dict[EmotionType, List[str]] = {
        EmotionType.HAPPY: [
            "开心", "高兴", "快乐", "哈哈", "嘿嘿", "嘻嘻",
            "😊", "😄", "🥰", "😁", "😆",
            "真棒", "太棒", "好棒", "好耶", "太好了", "喜欢", "爱你",
        ],
        EmotionType.SAD: [
            "难过", "伤心", "悲伤", "呜呜", "哭哭", "失望", "沮丧", "痛苦",
            "😢", "😭", "😿", "不想", "心疼",
        ],
        EmotionType.ANGRY: [
            "生气", "愤怒", "讨厌", "烦", "气死", "可恶", "无语", "滚",
            "😠", "😡", "💢",
        ],
        EmotionType.EXCITED: [
            "激动", "兴奋", "哇", "太棒了", "耶", "牛逼", "牛啊", "太牛",
            "厉害", "amazing", "冲呀", "搞起",
            "🎉", "🥳", "✨",
        ],
        EmotionType.CALM: [
            "平静", "安静", "淡定", "冷静", "还好", "一般", "嗯嗯", "好的", "了解",
        ],
        EmotionType.CONFUSED: [
            "困惑", "迷惑", "不懂", "啊？", "懵", "什么意思", "没看懂",
            "❓", "？？？", "???",
        ],
        EmotionType.BORED: [
            "无聊", "无趣", "没意思", "枯燥", "烦闷", "躺平", "😴", "🥱",
        ],
        EmotionType.CURIOUS: [
            "好奇", "想知道", "为什么", "怎么样", "是什么", "如何", "🤔", "有趣",
        ],
        EmotionType.SURPRISED: [
            "惊讶", "震惊", "卧槽", "天哪", "不会吧", "真的假的", "吓我",
            "😲", "😮", "‼️",
        ],
        EmotionType.ANXIOUS: [
            "焦虑", "担心", "紧张", "害怕", "不安", "忐忑", "怎么办", "慌",
            "😰", "😨", "😱",
        ],
    }

    _LOWER_CACHE: Dict[EmotionType, List[str]] = {}

    @classmethod
    def _lower_map(cls) -> Dict[EmotionType, List[str]]:
        if not cls._LOWER_CACHE:
            cls._LOWER_CACHE = {
                emo: [kw.lower() for kw in kws]
                for emo, kws in cls.EMOTION_KEYWORDS.items()
            }
        return cls._LOWER_CACHE

    @classmethod
    def analyze(cls, message: str) -> Tuple[Optional[EmotionType], float, Dict[str, int]]:
        """
        返回 (情绪, 置信度0-1, 分数字典)
        置信度按命中词数相对归一。
        """
        if not message or not str(message).strip():
            return None, 0.0, {}
        message_lower = str(message).lower()
        scores: Dict[EmotionType, int] = {}
        for emotion, keywords in cls._lower_map().items():
            score = 0
            for kw in keywords:
                if kw and kw in message_lower:
                    # 多字词权重略高
                    score += 2 if len(kw) >= 2 else 1
            if score > 0:
                scores[emotion] = score
        if not scores:
            return None, 0.0, {}
        best, best_score = max(scores.items(), key=lambda x: x[1])
        total = sum(scores.values()) or 1
        conf = min(1.0, best_score / max(3.0, total * 0.6))
        # 至少命中 1 个较强词时抬一抬
        if best_score >= 2:
            conf = max(conf, 0.55)
        if best_score >= 4:
            conf = max(conf, 0.8)
        logger.debug(
            f"[GenieTTS][emotion] keyword={best.value} score={best_score} conf={conf:.2f} text={message[:40]}"
        )
        return best, conf, {k.value: v for k, v in scores.items()}

    @classmethod
    def analyze_emotion(cls, message: str) -> Optional[EmotionType]:
        emo, conf, _ = cls.analyze(message)
        return emo if emo and conf >= 0.35 else emo

    @classmethod
    def from_label(cls, label: str) -> Optional[EmotionType]:
        lab = (label or "").strip()
        if not lab:
            return None
        for e in EmotionType:
            if e.value == lab or e.name.lower() == lab.lower():
                return e
        for e, aliases in EMOTION_ALIASES.items():
            if lab in aliases or any(a in lab or lab in a for a in aliases):
                return e
        # 兼容旧标签
        legacy = {
            "高兴": EmotionType.HAPPY,
            "快乐": EmotionType.HAPPY,
            "害怕": EmotionType.ANXIOUS,
            "无奈": EmotionType.BORED,
            "温柔": EmotionType.CALM,
            "默认": EmotionType.CALM,
        }
        return legacy.get(lab)

    @classmethod
    def all_labels(cls) -> List[str]:
        return [e.value for e in EmotionType] + ["默认"]


class EmotionContext:
    """会话情绪上下文（趋势平滑）"""

    def __init__(self, max_history: int = 8):
        self.emotion_history: Deque[dict] = deque(maxlen=max_history)

    def add(self, emotion: EmotionType, message: str, timestamp: float, intensity: float = 0.5):
        self.emotion_history.append(
            {
                "emotion": emotion,
                "message": message,
                "timestamp": timestamp,
                "intensity": intensity,
            }
        )

    def recent(self) -> Optional[EmotionType]:
        if self.emotion_history:
            return self.emotion_history[-1]["emotion"]
        return None

    def smooth(self, candidate: Optional[EmotionType], min_repeat: int = 2) -> Optional[EmotionType]:
        """
        简单平滑：若历史近几条多数与 candidate 不同，但 candidate 强度高则仍采用；
        若 candidate 为空，回退最近情绪。
        """
        if candidate is None:
            return self.recent()
        if len(self.emotion_history) < min_repeat:
            return candidate
        recent = [x["emotion"] for x in list(self.emotion_history)[-3:]]
        same = sum(1 for e in recent if e == candidate)
        if same >= 1:
            return candidate
        # 与最近完全不同：若最近 2 条一致，则有 50% 粘滞（由调用方决定是否启用）
        if len(recent) >= 2 and recent[-1] == recent[-2]:
            # 高强度情绪允许切换
            intensity = EMOTION_INTENSITY_MAP.get(candidate, 0.5)
            if intensity >= 0.65:
                return candidate
            return recent[-1]
        return candidate

    def trend(self) -> Optional[str]:
        if len(self.emotion_history) < 2:
            return None
        recent = [x["emotion"] for x in list(self.emotion_history)[-3:]]
        positive = {EmotionType.HAPPY, EmotionType.EXCITED, EmotionType.CALM, EmotionType.CURIOUS}
        negative = {EmotionType.SAD, EmotionType.ANGRY, EmotionType.ANXIOUS, EmotionType.BORED}
        pos = sum(1 for e in recent if e in positive)
        neg = sum(1 for e in recent if e in negative)
        if pos > neg:
            return "positive"
        if neg > pos:
            return "negative"
        return "neutral"


def build_semantic_routes_for_gateway_emotions(gateway_emotions: list) -> list:
    """
    为网关情绪列表生成与 EmotionType 对齐的路由草稿。
    gateway_emotions: [{id, emotion, remark, is_default, ...}]
    """
    routes = []
    used = set()

    def pick(hints: List[str]):
        for e in gateway_emotions:
            name = str(e.get("emotion") or e.get("remark") or "")
            if any(h in name for h in hints):
                return e
        # default
        for e in gateway_emotions:
            if e.get("is_default"):
                return e
        return gateway_emotions[0] if gateway_emotions else None

    for emo in EmotionType:
        hints = EMOTION_ALIASES.get(emo, [emo.value])
        matched = pick(hints)
        if not matched:
            continue
        label = emo.value
        if label in used:
            continue
        used.add(label)
        routes.append(
            {
                "__template_key": "route",
                "label": label,
                "aliases": ",".join(hints[:5]),
                "emotion_id": int(matched.get("id") or 0),
                "emotion": str(matched.get("emotion") or matched.get("remark") or ""),
            }
        )
    # 附加网关原始情绪名，便于精确匹配
    for e in gateway_emotions:
        label = str(e.get("emotion") or e.get("remark") or "")
        if not label or label in used:
            continue
        used.add(label)
        routes.append(
            {
                "__template_key": "route",
                "label": label,
                "aliases": "",
                "emotion_id": int(e.get("id") or 0),
                "emotion": label,
            }
        )
    return routes