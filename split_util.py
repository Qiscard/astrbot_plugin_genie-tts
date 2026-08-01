# -*- coding: utf-8 -*-
"""精简版分段工具（借鉴 astrbot_plugin_splitter 核心思路）"""

from __future__ import annotations

import math
import random
import re
from typing import List, Sequence


DEFAULT_SPLIT_CHARS = ["。", "？", "！", "?", "!", "；", ";", "\n", "…"]

PAIR_MAP = {
    '"': '"', "“": "”", "《": "》", "（": "）", "(": ")",
    "[": "]", "{": "}", "'": "'", "【": "】", "<": ">",
}


def unescape(s: str) -> str:
    return (
        str(s or "")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\s", " ")
    )


def apply_replacements(text: str, rules: Sequence) -> str:
    if not text or not rules:
        return text
    parsed = []
    for rule in rules:
        if isinstance(rule, dict):
            find, repl = rule.get("find", ""), rule.get("replace", "")
        elif isinstance(rule, str) and "|" in rule:
            find, repl = rule.split("|", 1)
        else:
            continue
        find = unescape(find)
        if find:
            parsed.append((find, unescape(repl)))
    for find, repl in parsed:
        text = text.replace(find, repl)
    return text


def clean_items(text: str, items: Sequence[str]) -> str:
    if not text:
        return text
    for item in items or []:
        if item:
            text = text.replace(str(item), "")
    return text


def _in_unbalanced_pairs(buf: str) -> bool:
    """粗略判断是否处于未闭合成对符号内。"""
    stack = []
    opens = set(PAIR_MAP.keys())
    closes = {v: k for k, v in PAIR_MAP.items()}
    i = 0
    while i < len(buf):
        ch = buf[i]
        if ch in opens:
            # 同符成对（如 "）用计数
            if PAIR_MAP[ch] == ch:
                if stack and stack[-1] == ch:
                    stack.pop()
                else:
                    stack.append(ch)
            else:
                stack.append(ch)
        elif ch in closes:
            op = closes[ch]
            if stack and stack[-1] == op:
                stack.pop()
        i += 1
    return bool(stack)


def split_text(
    text: str,
    *,
    split_chars: Sequence[str] | None = None,
    max_segments: int = 5,
    min_segment_length: int = 8,
    protect_pairs: bool = True,
) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    chars = [unescape(c) for c in (split_chars or DEFAULT_SPLIT_CHARS) if str(c).strip() != ""]
    if not chars:
        return [text]

    # 长的分隔符优先
    chars_sorted = sorted(set(chars), key=len, reverse=True)
    escaped = [re.escape(c) for c in chars_sorted]
    pattern = re.compile("(" + "|".join(escaped) + "+)")

    parts = pattern.split(text)
    chunks: List[str] = []
    buf = ""
    for part in parts:
        if part is None or part == "":
            continue
        if pattern.fullmatch(part):
            # 分隔符贴到当前段末尾
            buf += part
            if protect_pairs and _in_unbalanced_pairs(buf):
                continue
            if buf.strip():
                chunks.append(buf.strip())
            buf = ""
        else:
            buf += part
    if buf.strip():
        chunks.append(buf.strip())

    if not chunks:
        return [text]

    # 合并过短段
    merged: List[str] = []
    for ch in chunks:
        if merged and len(ch) < min_segment_length:
            merged[-1] = (merged[-1] + ch).strip()
        elif merged and len(merged[-1]) < min_segment_length:
            merged[-1] = (merged[-1] + ch).strip()
        else:
            merged.append(ch)

    # 限制最大段数：均匀合并
    max_segments = max(1, int(max_segments or 1))
    if len(merged) <= max_segments:
        return merged

    # 按目标段数重切
    total = len("".join(merged))
    ideal = max(min_segment_length, total // max_segments)
    out: List[str] = []
    cur = ""
    for ch in merged:
        if not cur:
            cur = ch
            continue
        if len(cur) >= ideal and len(out) < max_segments - 1:
            out.append(cur.strip())
            cur = ch
        else:
            cur = (cur + ch).strip()
    if cur.strip():
        out.append(cur.strip())
    # 若仍超限，硬合并尾部
    while len(out) > max_segments:
        tail = out.pop()
        out[-1] = (out[-1] + tail).strip()
    return [x for x in out if x]


def calc_delay(text: str, style: str = "自然", *, fixed: float = 1.0) -> float:
    style = (style or "自然").strip()
    n = max(0, len(text or ""))
    if style in {"快速", "fast"}:
        return 0.25
    if style in {"慢速", "slow"}:
        return max(1.2, 0.8 + n * 0.05)
    if style in {"固定", "fixed"}:
        return max(0.0, float(fixed))
    if style in {"随机", "random"}:
        return random.uniform(0.6, 2.2)
    # 自然 ~ linear
    return min(4.0, 0.45 + n * 0.08)


def calc_delay_pro(text: str, strategy: str = "linear", **kw) -> float:
    strategy = (strategy or "linear").lower()
    n = len(text or "")
    if strategy == "random":
        return random.uniform(float(kw.get("random_min", 0.8)), float(kw.get("random_max", 2.5)))
    if strategy == "log":
        return min(5.0, float(kw.get("log_base", 0.5)) + float(kw.get("log_factor", 0.8)) * math.log(n + 1))
    if strategy == "fixed":
        return float(kw.get("fixed_delay", 1.0))
    return float(kw.get("linear_base", 0.5)) + n * float(kw.get("linear_factor", 0.08))