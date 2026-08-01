# -*- coding: utf-8 -*-
"""字符串规范化与模糊匹配，避免输入差异导致对接失败。"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 全角 ASCII 区间
_FW_START, _FW_END = 0xFF01, 0xFF5E
_HW_START = 0x21

# 常见同义/别名（配置枚举 + 角色/情绪对接）
ENUM_ALIASES: Dict[str, Tuple[str, ...]] = {
    # language
    "zh": ("zh", "cn", "chi", "chinese", "中文", "汉语", "华语", "zh-cn", "zh_cn"),
    "en": ("en", "eng", "english", "英文", "英语", "en-us", "en_us"),
    "lang_hybrid": ("hybrid", "mix", "mixed", "双语", "中英", "中英混合"),
    # emotion detect mode
    "keyword": ("keyword", "kw", "keywords", "关键词", "关键字", "词库"),
    "llm": ("llm", "ai", "model", "大模型", "模型", "gpt"),
    "mode_hybrid": ("hybrid", "mix", "mixed", "混合", "智能", "auto", "自动"),
    # send speed
    "自然": ("自然", "normal", "natural", "default", "默认", "linear"),
    "快速": ("快速", "fast", "quick", "高速", "快"),
    "慢速": ("慢速", "slow", "慢", "缓慢"),
    "固定": ("固定", "fixed", "fix"),
    "随机": ("随机", "random", "rand"),
}

# 情绪标准名额外同义（补充 emotions.EMOTION_ALIASES）
EXTRA_EMOTION_ALIASES: Dict[str, Tuple[str, ...]] = {
    "开心": ("开心", "高兴", "快乐", "愉快", "欢快", "happy", "joy", "glad"),
    "悲伤": ("悲伤", "难过", "伤心", "沮丧", "sad", "sorrow", "cry"),
    "生气": ("生气", "愤怒", "恼火", "angry", "mad", "rage"),
    "兴奋": ("兴奋", "激动", "热情", "excited", "hype"),
    "平静": ("平静", "冷静", "淡定", "标准", "默认", "普通", "calm", "neutral", "default", "normal"),
    "困惑": ("困惑", "迷惑", "懵", "confused", "puzzle"),
    "无聊": ("无聊", "无趣", "bored", "meh"),
    "好奇": ("好奇", "curious", "wonder"),
    "惊讶": ("惊讶", "震惊", "吃惊", "surprised", "shock"),
    "焦虑": ("焦虑", "担心", "紧张", "害怕", "不安", "anxious", "worry", "fear", "scared"),
    "默认": ("默认", "标准", "普通", "default", "normal", "none", "null", "0"),
}


def fullwidth_to_halfwidth(text: str) -> str:
    out = []
    for ch in text or "":
        code = ord(ch)
        if _FW_START <= code <= _FW_END:
            out.append(chr(code - _FW_START + _HW_START))
        elif code == 0x3000:  # ideographic space
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def norm_text(value: Any, *, keep_space: bool = False) -> str:
    """通用规范化：NFKC、去首尾空白、全角转半角、统一大小写。"""
    if value is None:
        return ""
    s = str(value)
    s = unicodedata.normalize("NFKC", s)
    s = fullwidth_to_halfwidth(s)
    s = s.strip()
    s = s.replace("\u200b", "").replace("\ufeff", "")
    s = re.sub(r"[\t\r\n]+", " ", s)
    if not keep_space:
        s = re.sub(r"[\s_\-—–·•./\\|]+", "", s)
    else:
        s = re.sub(r"\s+", " ", s).strip()
    return s.lower()


def norm_key(value: Any) -> str:
    """用于字典键/精确模糊比对的强规范化。"""
    return norm_text(value, keep_space=False)


def expand_aliases(label: str, extra_maps: Optional[Sequence[Dict[str, Any]]] = None) -> List[str]:
    """展开一个标签的所有别名（含自身）。"""
    raw = str(label or "").strip()
    if not raw:
        return []
    seen = set()
    out: List[str] = []

    def add(x: str) -> None:
        x = str(x or "").strip()
        if not x:
            return
        k = norm_key(x)
        if not k or k in seen:
            return
        seen.add(k)
        out.append(x)

    add(raw)
    # enum aliases where raw is canonical or alias
    nk = norm_key(raw)
    for canon, aliases in ENUM_ALIASES.items():
        alias_keys = {norm_key(a) for a in aliases} | {norm_key(canon)}
        if nk in alias_keys:
            add(canon)
            for a in aliases:
                add(a)
    for canon, aliases in EXTRA_EMOTION_ALIASES.items():
        alias_keys = {norm_key(a) for a in aliases} | {norm_key(canon)}
        if nk in alias_keys:
            add(canon)
            for a in aliases:
                add(a)
    if extra_maps:
        for mp in extra_maps:
            if not isinstance(mp, dict):
                continue
            for canon, aliases in mp.items():
                vals = aliases if isinstance(aliases, (list, tuple, set)) else [aliases]
                alias_keys = {norm_key(a) for a in vals} | {norm_key(canon)}
                if nk in alias_keys:
                    add(str(canon))
                    for a in vals:
                        add(str(a))
    return out


def score_match(query: str, candidate: str) -> int:
    """
    返回匹配分：
      100 规范化全等
       90 去符号后全等 / 别名全等
       80 前缀
       70 包含
       50 候选包含查询
        0 不匹配
    """
    q = norm_key(query)
    c = norm_key(candidate)
    if not q or not c:
        return 0
    if q == c:
        return 100
    # 数字 id 宽松
    if q.isdigit() and c.isdigit() and int(q) == int(c):
        return 100
    if c.startswith(q) or q.startswith(c):
        # 避免过短误伤
        if min(len(q), len(c)) >= 2:
            return 80
    if q in c:
        return 70 if len(q) >= 2 else 40
    if c in q:
        return 50 if len(c) >= 2 else 30
    return 0


def best_match(
    query: Any,
    candidates: Iterable[Any],
    *,
    aliases_of=None,
    min_score: int = 70,
) -> Tuple[Optional[Any], int]:
    """
    在 candidates 中找最佳匹配。
    aliases_of: callable(candidate) -> Iterable[str] 额外别名
    返回 (candidate_original, score)
    """
    q_raw = str(query or "").strip()
    if not q_raw:
        return None, 0
    q_vars = expand_aliases(q_raw)
    best_item = None
    best_score = 0
    for item in candidates:
        labels = [str(item)]
        if aliases_of is not None:
            try:
                extra = list(aliases_of(item) or [])
                labels.extend(str(x) for x in extra)
            except Exception:
                pass
        # also expand each label
        expanded: List[str] = []
        for lab in labels:
            expanded.extend(expand_aliases(lab))
            expanded.append(lab)
        sc = 0
        for qv in q_vars:
            for lab in expanded:
                sc = max(sc, score_match(qv, lab))
        if sc > best_score:
            best_score = sc
            best_item = item
    if best_score < min_score:
        return None, best_score
    return best_item, best_score


def match_enum(value: Any, canonical_map: Dict[str, Sequence[str]], default: str) -> str:
    """
    canonical_map: {标准值: 别名列表}
    将任意输入映射到标准值。
    """
    raw = str(value or "").strip()
    if not raw:
        return default
    # direct
    nk = norm_key(raw)
    for canon, aliases in canonical_map.items():
        pool = {norm_key(canon)} | {norm_key(a) for a in aliases}
        if nk in pool:
            return canon
    # fuzzy
    cands = list(canonical_map.keys())
    hit, sc = best_match(
        raw,
        cands,
        aliases_of=lambda c: list(canonical_map.get(c, ())),
        min_score=70,
    )
    return hit if hit is not None else default


def match_language(value: Any, default: str = "zh") -> str:
    return match_enum(
        value,
        {
            "zh": ENUM_ALIASES["zh"],
            "en": ENUM_ALIASES["en"],
            "hybrid": ("hybrid", "mix", "mixed", "双语", "中英", "中英混合", "auto"),
        },
        default,
    )


def match_emotion_mode(value: Any, default: str = "hybrid") -> str:
    # 注意 hybrid 在 ENUM_ALIASES 可能被 language 覆盖，这里独立
    return match_enum(
        value,
        {
            "keyword": ("keyword", "kw", "keywords", "关键词", "关键字", "词库"),
            "llm": ("llm", "ai", "model", "大模型", "模型", "gpt"),
            "hybrid": ("hybrid", "mix", "mixed", "混合", "智能", "auto", "自动"),
        },
        default,
    )


def match_send_speed(value: Any, default: str = "自然") -> str:
    return match_enum(
        value,
        {
            "自然": ENUM_ALIASES["自然"],
            "快速": ENUM_ALIASES["快速"],
            "慢速": ENUM_ALIASES["慢速"],
            "固定": ENUM_ALIASES["固定"],
            "随机": ENUM_ALIASES["随机"],
        },
        default,
    )


def match_name_in_list(
    query: Any,
    names: Sequence[str],
    *,
    min_score: int = 70,
) -> Tuple[Optional[str], int]:
    """角色名/模型名模糊匹配，返回原列表中的标准名。"""
    cleaned = [str(n).strip() for n in names if str(n).strip()]
    if not cleaned:
        return None, 0
    # 先强规范全等
    qk = norm_key(query)
    for n in cleaned:
        if norm_key(n) == qk:
            return n, 100
    hit, sc = best_match(query, cleaned, min_score=min_score)
    return (str(hit) if hit is not None else None), sc


def parse_emotion_routes(routes: Any) -> List[Dict[str, Any]]:
    """兼容 list[dict] / list[str] / dict 多种 emotion_routes 输入。"""
    out: List[Dict[str, Any]] = []
    if routes is None:
        return out
    if isinstance(routes, dict):
        # {label: emotion_or_id} or {label: {emotion_id, emotion}}
        for k, v in routes.items():
            if isinstance(v, dict):
                item = dict(v)
                item.setdefault("label", k)
                out.append(item)
            else:
                out.append({"label": str(k), "emotion": str(v), "emotion_id": _safe_int(v)})
        return out
    if isinstance(routes, (list, tuple)):
        for r in routes:
            if isinstance(r, dict):
                out.append(r)
            elif isinstance(r, str):
                # "开心->4:标准" / "开心|4|标准" / "开心:高兴"
                s = r.strip()
                if not s:
                    continue
                if "->" in s:
                    lab, rest = s.split("->", 1)
                    eid, ename = _split_id_name(rest)
                    out.append({"label": lab.strip(), "emotion_id": eid, "emotion": ename})
                elif "|" in s:
                    parts = [p.strip() for p in s.split("|")]
                    lab = parts[0] if parts else ""
                    eid = _safe_int(parts[1]) if len(parts) > 1 else 0
                    ename = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 and not str(parts[1]).isdigit() else "")
                    out.append({"label": lab, "emotion_id": eid, "emotion": ename})
                elif ":" in s:
                    lab, rest = s.split(":", 1)
                    eid, ename = _split_id_name(rest)
                    out.append({"label": lab.strip(), "emotion_id": eid, "emotion": ename})
                else:
                    out.append({"label": s, "emotion": s, "emotion_id": 0})
    return out


def _safe_int(v: Any) -> int:
    try:
        if v is None or v == "":
            return 0
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def _split_id_name(rest: str) -> Tuple[int, str]:
    rest = (rest or "").strip()
    if not rest:
        return 0, ""
    if ":" in rest:
        a, b = rest.split(":", 1)
        if str(a).strip().isdigit():
            return _safe_int(a), b.strip()
        return 0, rest
    if rest.isdigit():
        return _safe_int(rest), ""
    return 0, rest


def pick_by_names(
    items: Sequence[Dict[str, Any]],
    query: str,
    name_keys: Sequence[str] = ("name", "character", "emotion", "remark", "label"),
    *,
    min_score: int = 70,
) -> Tuple[Optional[Dict[str, Any]], int]:
    """在 dict 列表中按多个可能字段模糊匹配。"""
    if not query or not items:
        return None, 0

    def aliases(it: Dict[str, Any]):
        vals = []
        for k in name_keys:
            if k in it and it.get(k) is not None:
                vals.append(str(it.get(k)))
        # id as string
        if "id" in it:
            vals.append(str(it.get("id")))
        if "emotion_id" in it:
            vals.append(str(it.get("emotion_id")))
        return vals

    # wrap to keep original dict
    class _Wrap:
        def __init__(self, d): self.d = d
        def __str__(self): return str(self.d.get(name_keys[0]) or next((self.d.get(k) for k in name_keys if self.d.get(k)), ""))

    wraps = [_Wrap(it) for it in items if isinstance(it, dict)]
    hit, sc = best_match(query, wraps, aliases_of=lambda w: aliases(w.d), min_score=min_score)
    if hit is None:
        return None, sc
    return hit.d, sc
