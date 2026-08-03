# -*- coding: utf-8 -*-
"""精简版分段工具（借鉴 astrbot_plugin_splitter 核心思路）"""

from __future__ import annotations

import math
import random
import re
from typing import List, Sequence


DEFAULT_SPLIT_CHARS = ["。", "？", "！", "?", "!", "…", "."]

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
    max_segments: int = 0,
    min_segment_length: int = 0,
    protect_pairs: bool = True,
) -> List[str]:
    """按完整句末标点分句，不合并短句，也不按数量重切。"""
    if not isinstance(text, str):
        return []
    text = text.strip()
    if not text:
        return []

    configured = [unescape(str(char)) for char in (split_chars or DEFAULT_SPLIT_CHARS)]
    sentence_endings = {"。", "？", "！", "?", "!", "…", "."}
    endings = {char for char in configured if char in sentence_endings}
    if not endings:
        endings = sentence_endings

    closing_chars = set("\\\"'”’»》〉】）)]}>")
    segments: List[str] = []
    buffer: List[str] = []
    pending_boundary = False

    def flush() -> None:
        value = "".join(buffer).strip()
        if value:
            segments.append(value)
        buffer.clear()

    for index, char in enumerate(text):
        if pending_boundary:
            if char in closing_chars:
                buffer.append(char)
                flush()
                pending_boundary = False
                continue
            if char in endings or char.isspace():
                buffer.append(char)
                continue
            if protect_pairs and _in_unbalanced_pairs("".join(buffer)):
                buffer.append(char)
                continue
            flush()
            pending_boundary = False

        buffer.append(char)
        if char not in endings:
            continue
        if char == ".":
            previous = text[index - 1] if index > 0 else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if previous.isdigit() and following.isdigit():
                continue
        pending_boundary = True

    flush()
    return segments or [text]

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