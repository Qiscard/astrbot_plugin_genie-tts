# -*- coding: utf-8 -*-
"""AstrBot Genie-TTS v2.2: Gateway / warmup / kaomoji / LLM emotion / cascaded voices."""
from __future__ import annotations

import asyncio
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.star.filter.permission import PermissionType

try:
    from pydub import AudioSegment
    from pydub.silence import detect_leading_silence
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False
    logger.warning("[GenieTTS] pydub 未安装，静音裁剪不可用")

DEFAULT_KAOMOJI_PATTERNS = [
    r"[（(][^（()]*[）)]",
    r"[＞>][＿_][＜<]",
    r"[＾^][＿_][＾^]",
    r"[oO][＿_][oO]",
    r"[xX][＿_][xX]",
    r"[－-][＿_][－-]",
    r"[★☆♪♫♬♩♡♥❤💖💕💗💓💝💟💜💛💚💙🧡🤍🖤🤎💔❣💋]",
    r"[→←↑↓↖↗↘↙↔↕↺↻]",
]
DEFAULT_KAOMOJI_WORDS = [
    "ω", "Ω", "σ", "Σ", "ε", "д", "Д", "´", "`", "＝", "∀", "∇",
    "orz", "OTZ", "QAQ", "QWQ", "TAT", "TUT", "www",
]
DEFAULT_REPLACEMENTS = ["233|哈哈哈", "666|厉害", "999|很棒", "555|呜呜呜"]
DEFAULT_EMOTION_LABELS = "高兴,悲伤,生气,平静,温柔,兴奋,惊讶,害怕,无奈,默认"
SEMANTIC_HINTS = {
    "高兴": ["高兴", "开心", "快乐", "欢快", "标准", "愉快"],
    "悲伤": ["悲伤", "难过", "伤心", "哭"],
    "生气": ["生气", "愤怒", "恼"],
    "平静": ["平静", "标准", "默认", "普通", "原始"],
    "温柔": ["温柔", "软", "轻"],
    "兴奋": ["兴奋", "激动", "元气"],
    "惊讶": ["惊讶", "吃惊"],
    "害怕": ["害怕", "恐惧"],
    "无奈": ["无奈", "叹气"],
    "默认": ["标准", "默认", "原始", "平静"],
}

EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002700-\U000027BF"
    "\U0001F900-\U0001F9FF"
    "\U00002600-\U000026FF"
    "\U0001FA70-\U0001FAFF"
    "]+", flags=re.UNICODE)
QQ_FACE_RE = re.compile(r"\[CQ:face[^\]]*\]|\[表情\]")
URL_RE = re.compile(r"(https?://\S+|www\.\S+|file://\S+|[A-Za-z]:\\[^\s]+)")
CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
INLINE_CODE_RE = re.compile(r"`[^`]+`")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
MD_MARK_RE = re.compile(r"[*_~#>]{1,3}")
MULTI_SPACE_RE = re.compile(r"[ \t\u3000]+")

def _cfg_get(config: dict, key: str, default=None, *nested_paths):
    if not isinstance(config, dict):
        return default
    if key in config and config.get(key) is not None:
        return config.get(key)
    for path in nested_paths:
        cur = config
        ok = True
        for part in path:
            if not isinstance(cur, dict) or part not in cur:
                ok = False
                break
            cur = cur[part]
        if ok and cur is not None:
            return cur
    return default

def _as_bool(value, default: bool = False) -> bool:
    if value is None: return default
    if isinstance(value, bool): return value
    if isinstance(value, (int, float)): return bool(value)
    s = str(value).strip().lower()
    if s in {"1","true","yes","on","y"}: return True
    if s in {"0","false","no","off","n",""}: return False
    return default

def _as_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "": return default
        return int(value)
    except (TypeError, ValueError):
        return default

def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "": return default
        return float(value)
    except (TypeError, ValueError):
        return default

def _parse_replacements(items: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in items or []:
        s = str(raw).strip()
        if not s or "|" not in s: continue
        a, b = s.split("|", 1)
        if a: out[a] = b
    return out

@dataclass
class SessionState:
    last_tts_time: float = 0.0
    last_tts_text: str = ""
    character: Optional[str] = None
    emotion_id: Optional[int] = None
    emotion: Optional[str] = None
    language: Optional[str] = None
    last_emotion_label: str = ""

@register(
    "genie-tts",
    "victical",
    "基于 Genie TTS Gateway 的语音合成插件",
    "2.2.0",
    "https://github.com/Qiscard/astrbot_plugin_genie-tts",
)
class GenieTTSPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self.temp_dir = os.path.join(os.path.dirname(__file__), "temp_audio")
        os.makedirs(self.temp_dir, exist_ok=True)
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._ready = False
        self._last_error = ""
        self._emotions_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._characters_cache: List[Dict[str, Any]] = []
        self._session_state: Dict[str, SessionState] = {}
        self.enabled_sessions: List[str] = list(self.config.get("enabled_sessions", []) or [])
        self.disabled_sessions: List[str] = list(self.config.get("disabled_sessions", []) or [])
        self._model_hot = False
        self._warming_up = False
        self._warmup_task: Optional[asyncio.Task] = None
        self._warmup_started_at = 0.0
        self._warmup_eta = 30.0
        self._last_status: Dict[str, Any] = {}
        self._status_checked_at = 0.0
        self._voices_synced = False
        self._kaomoji_regex: List[re.Pattern] = []
        self._repeat_regex: Optional[re.Pattern] = None
        self._replacements: Dict[str, str] = {}
        self._apply_runtime_config()
        logger.info(f"[GenieTTS] init base={self.base_url} char={self.character} voices={len(self.voices)} emo_detect={self.emotion_detect_enabled}")

    def _apply_runtime_config(self) -> None:
        raw_base = self.config.get("base_url") or self.config.get("server_host") or "http://127.0.0.1:19880"
        self.base_url = self._normalize_base_url(str(raw_base), self.config.get("server_port"))
        self.api_key = str(self.config.get("api_key", "") or "").strip()
        self.character = str(self.config.get("character") or self.config.get("character_name") or "lxh").strip()
        self.language = str(self.config.get("language", "zh") or "zh").strip().lower()
        if self.language not in {"zh", "en", "hybrid"}: self.language = "zh"
        self.emotion_id = _as_int(self.config.get("emotion_id", 0), 0)
        emotion = str(self.config.get("emotion", "") or "").strip()
        if emotion in {"0", "none", "null", "default"}: emotion = ""
        self.emotion = emotion

        voices = self.config.get("voices", [])
        self.voices: List[Dict[str, Any]] = list(voices) if isinstance(voices, list) else []

        self.auto_sync_voices = _as_bool(_cfg_get(self.config, "auto_sync_voices", True, ("model_select", "auto_sync_voices")), True)
        self.auto_select_emotion = _as_bool(_cfg_get(self.config, "auto_select_emotion", True, ("model_select", "auto_select_emotion")), True)
        self.overwrite_on_sync = _as_bool(_cfg_get(self.config, "overwrite_on_sync", False, ("model_select", "overwrite_on_sync")), False)

        self.emotion_detect_enabled = _as_bool(_cfg_get(self.config, "emotion_detect_enabled", True, ("emotion_detect", "enabled")), True)
        self.emotion_provider_id = str(_cfg_get(self.config, "emotion_provider_id", "", ("emotion_detect", "provider_id")) or "").strip()
        self.emotion_labels = str(_cfg_get(self.config, "emotion_labels", DEFAULT_EMOTION_LABELS, ("emotion_detect", "labels")) or DEFAULT_EMOTION_LABELS)
        self.emotion_timeout = max(3, _as_int(_cfg_get(self.config, "emotion_timeout", 12, ("emotion_detect", "timeout")), 12))
        self.emotion_fallback = str(_cfg_get(self.config, "emotion_fallback", "默认", ("emotion_detect", "fallback_label")) or "默认")

        self.split_sentence = _as_bool(self.config.get("split_sentence", True), True)
        self.save_on_server = _as_bool(self.config.get("save_on_server", False), False)
        self.timeout = max(10, _as_int(self.config.get("timeout", 300), 300))
        self.retry_attempts = max(0, _as_int(self.config.get("retry_attempts", 3), 3))
        self.global_enable = _as_bool(self.config.get("global_enable", True), True)
        self.prob = _as_float(self.config.get("prob", 1.0), 1.0)
        self.text_limit = _as_int(self.config.get("text_limit", 200), 200)
        self.cooldown = _as_int(self.config.get("cooldown", 0), 0)

        self.filter_code = _as_bool(_cfg_get(self.config, "filter_code", True, ("text_process", "filter_code")), True)
        self.filter_emoji = _as_bool(_cfg_get(self.config, "filter_emoji", True, ("text_process", "filter_emoji")), True)
        self.filter_url = _as_bool(_cfg_get(self.config, "filter_url", True, ("text_process", "filter_url")), True)
        self.filter_markdown = _as_bool(_cfg_get(self.config, "filter_markdown", True, ("text_process", "filter_markdown")), True)
        self.filter_kaomoji = _as_bool(_cfg_get(self.config, "filter_kaomoji", True, ("text_process", "filter_kaomoji")), True)
        self.trim_silence = _as_bool(_cfg_get(self.config, "trim_silence", True, ("text_process", "trim_silence")), True)
        self.replace_text = _as_bool(_cfg_get(self.config, "replace_text", True, ("text_process", "replace_text")), True)
        self.send_text_with_audio = _as_bool(_cfg_get(self.config, "send_text_with_audio", False, ("text_process", "send_text_with_audio")), False)

        patterns = _cfg_get(self.config, "kaomoji_patterns", DEFAULT_KAOMOJI_PATTERNS, ("text_process", "kaomoji_patterns")) or DEFAULT_KAOMOJI_PATTERNS
        words = _cfg_get(self.config, "kaomoji_words", DEFAULT_KAOMOJI_WORDS, ("text_process", "kaomoji_words")) or DEFAULT_KAOMOJI_WORDS
        repl = _cfg_get(self.config, "replacement_words", DEFAULT_REPLACEMENTS, ("text_process", "replacement_words")) or DEFAULT_REPLACEMENTS
        self.kaomoji_words = [str(x) for x in words]
        self.max_repeat_count = _as_int(_cfg_get(self.config, "max_repeat_count", 2, ("text_process", "max_repeat_count")), 2)
        self._replacements = _parse_replacements([str(x) for x in repl])
        self._kaomoji_regex = []
        for p in patterns:
            try: self._kaomoji_regex.append(re.compile(str(p)))
            except re.error as e: logger.warning(f"[GenieTTS] 无效颜文字正则 {p}: {e}")
        if self.max_repeat_count > 0:
            self._repeat_regex = re.compile(rf"(.)\1{{{self.max_repeat_count},}}")
        else:
            self._repeat_regex = None

        self.warmup_mode = _as_bool(_cfg_get(self.config, "warmup_mode", True, ("warmup", "enabled")), True)
        self.warmup_tip = _as_bool(_cfg_get(self.config, "warmup_tip", False, ("warmup", "show_tip")), False)
        self.warmup_status_ttl = max(3, _as_int(_cfg_get(self.config, "warmup_status_ttl", 8, ("warmup", "status_ttl")), 8))
        self.auto_check_on_start = _as_bool(self.config.get("auto_check_on_start", True), True)

    @staticmethod
    def _normalize_base_url(host_or_url: str, port: Any = None) -> str:
        value = (host_or_url or "").strip().rstrip("/")
        if not value: value = "http://127.0.0.1:19880"
        if value.startswith("http://http://"): value = value[len("http://"):]
        if value.startswith("https://https://"): value = value[len("https://"):]
        if "://" not in value:
            if port not in (None, ""):
                try:
                    port_i = int(port)
                    if ":" not in value.split("/")[0]: value = f"{value}:{port_i}"
                except (TypeError, ValueError): pass
            value = f"http://{value}"
        else:
            try:
                parsed = urlparse(value)
                if port not in (None, "") and not parsed.port:
                    netloc = f"{parsed.hostname}:{int(port)}"
                    value = urlunparse((parsed.scheme, netloc, parsed.path, "", "", "")).rstrip("/")
            except Exception: pass
        return value.rstrip("/")

    def _auth_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"Accept": "*/*", "User-Agent": "AstrBot-GenieTTS/2.2"}
        if self.api_key: headers["X-API-Key"] = self.api_key
        if extra: headers.update(extra)
        return headers

    def _persist(self) -> None:
        """写入 AstrBot 插件配置文件。"""
        try:
            self.config["base_url"] = self.base_url
            self.config["api_key"] = self.api_key
            self.config["character"] = self.character
            self.config["language"] = self.language
            self.config["emotion_id"] = self.emotion_id
            self.config["emotion"] = self.emotion
            self.config["voices"] = self.voices
            self.config["global_enable"] = self.global_enable
            self.config["enabled_sessions"] = self.enabled_sessions
            self.config["disabled_sessions"] = self.disabled_sessions
            self.config["model_select"] = {
                "auto_sync_voices": self.auto_sync_voices,
                "auto_select_emotion": self.auto_select_emotion,
                "overwrite_on_sync": self.overwrite_on_sync,
            }
            self.config["emotion_detect"] = {
                "enabled": self.emotion_detect_enabled,
                "provider_id": self.emotion_provider_id,
                "labels": self.emotion_labels,
                "timeout": self.emotion_timeout,
                "fallback_label": self.emotion_fallback,
            }
            self.config["warmup"] = {
                "enabled": self.warmup_mode,
                "show_tip": self.warmup_tip,
                "status_ttl": self.warmup_status_ttl,
            }
            tp = self.config.get("text_process") if isinstance(self.config.get("text_process"), dict) else {}
            tp.update({
                "filter_code": self.filter_code,
                "filter_emoji": self.filter_emoji,
                "filter_url": self.filter_url,
                "filter_markdown": self.filter_markdown,
                "filter_kaomoji": self.filter_kaomoji,
                "trim_silence": self.trim_silence,
                "replace_text": self.replace_text,
                "send_text_with_audio": self.send_text_with_audio,
                "max_repeat_count": self.max_repeat_count,
            })
            self.config["text_process"] = tp
            if hasattr(self.config, "save_config"):
                self.config.save_config()
            elif hasattr(self.config, "save"):
                self.config.save()
        except Exception as e:
            logger.warning(f"[GenieTTS] 保存配置失败: {e}")

    def _save_config(self) -> None:
        self._persist()

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout, connect=15))
            return self._session

    async def _request(self, method: str, path: str, *, json_body=None, params=None, expect_json=True, timeout=None):
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        headers = self._auth_headers({"Content-Type": "application/json"} if json_body is not None else None)
        req_timeout = aiohttp.ClientTimeout(total=timeout or self.timeout, connect=15)
        async with session.request(method, url, headers=headers, json=json_body, params=params, timeout=req_timeout) as resp:
            status = resp.status
            resp_headers = {k: v for k, v in resp.headers.items()}
            if expect_json:
                try: data = await resp.json(content_type=None)
                except Exception: data = await resp.text()
            else:
                data = await resp.read()
            return status, data, resp_headers

    async def _api_json(self, method: str, path: str, *, json_body=None, params=None):
        status, data, _ = await self._request(method, path, json_body=json_body, params=params, expect_json=True)
        if status >= 400:
            raise RuntimeError(f"HTTP {status}: {data}")
        return data

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        if self.auto_check_on_start:
            await self._health_bootstrap()
        else:
            self._ready = bool(self.api_key)

    async def _health_bootstrap(self) -> bool:
        try:
            if not self.api_key:
                self._ready = False
                self._last_error = "未配置 api_key"
                logger.warning("[GenieTTS] 未配置 api_key")
                return False
            try:
                status, data, _ = await self._request("GET", "/health", expect_json=True, timeout=15)
                logger.info(f"[GenieTTS] /health => {status}")
            except Exception as e:
                logger.warning(f"[GenieTTS] /health 失败: {e}")
            me = await self._api_json("GET", "/api/v1/me")
            logger.info(f"[GenieTTS] key ok: {me}")
            await self._refresh_catalog(force=True)
            # 首次自动同步音色层叠配置
            if self.auto_sync_voices and (not self.voices):
                n = self._sync_voices_from_catalog(overwrite=False, persist=True)
                logger.info(f"[GenieTTS] 首次同步音色配置 {n} 个模型，请在面板查看/微调后重载插件")
            elif self.voices:
                self._voices_synced = True
            self._apply_voice_defaults_from_profile()
            await self._fetch_public_status(force=True)
            self._ready = True
            self._last_error = ""
            return True
        except Exception as e:
            self._ready = False
            self._last_error = str(e)
            logger.error(f"[GenieTTS] bootstrap 失败: {e}", exc_info=True)
            return False

    async def terminate(self):
        try:
            if self._session and not self._session.closed: await self._session.close()
        except Exception: pass
        try:
            if os.path.exists(self.temp_dir):
                for name in os.listdir(self.temp_dir):
                    path = os.path.join(self.temp_dir, name)
                    if os.path.isfile(path):
                        try: os.remove(path)
                        except OSError: pass
        except Exception as e:
            logger.error(f"[GenieTTS] 清理失败: {e}")

    async def _refresh_catalog(self, force: bool = False) -> None:
        if self._characters_cache and not force: return
        data = await self._api_json("GET", "/api/v1/characters")
        characters = data.get("characters", data if isinstance(data, list) else [])
        self._characters_cache = characters or []
        cache: Dict[str, List[Dict[str, Any]]] = {}
        for item in self._characters_cache:
            name = item.get("name") or item.get("character") or ""
            if name: cache[name] = list(item.get("emotions") or item.get("references") or [])
        try:
            emotions_data = await self._api_json("GET", "/api/v1/emotions")
            for group in emotions_data.get("characters", []) or []:
                char = group.get("character") or group.get("name") or ""
                emos = group.get("emotions", []) or []
                if char and emos: cache[char] = list(emos)
        except Exception as e:
            logger.warning(f"[GenieTTS] emotions 拉取失败: {e}")
        self._emotions_cache = cache

    def _get_voice_profile(self, character: Optional[str] = None) -> Optional[Dict[str, Any]]:
        character = (character or self.character or "").strip()
        for v in self.voices:
            if not isinstance(v, dict): continue
            if str(v.get("character") or "").strip() == character and _as_bool(v.get("enabled", True), True):
                return v
        for v in self.voices:
            if isinstance(v, dict) and str(v.get("character") or "").strip() == character:
                return v
        return None

    def _build_voice_profile(self, char_item: Dict[str, Any]) -> Dict[str, Any]:
        name = str(char_item.get("name") or char_item.get("character") or "").strip()
        emos = list(char_item.get("emotions") or char_item.get("references") or self._emotions_cache.get(name) or [])
        default_id, default_name = 0, ""
        for e in emos:
            if e.get("is_default") and e.get("enabled", True) is not False:
                default_id = _as_int(e.get("id"), 0)
                default_name = str(e.get("emotion") or e.get("remark") or "")
                break
        if not default_id and emos:
            default_id = _as_int(emos[0].get("id"), 0)
            default_name = str(emos[0].get("emotion") or emos[0].get("remark") or "")

        routes = []
        # 1) 每个网关情绪本身作为可识别标签
        for e in emos:
            label = str(e.get("emotion") or e.get("remark") or f"id{e.get('id')}")
            routes.append({
                "__template_key": "route",
                "label": label,
                "aliases": "",
                "emotion_id": _as_int(e.get("id"), 0),
                "emotion": label,
            })
        # 2) 语义标签映射到最接近的网关情绪
        existing_labels = {str(r["label"]) for r in routes}
        for sem, hints in SEMANTIC_HINTS.items():
            if sem in existing_labels: continue
            matched = None
            for e in emos:
                en = str(e.get("emotion") or e.get("remark") or "")
                if any(h in en for h in hints):
                    matched = e; break
            if matched is None and default_id:
                matched = {"id": default_id, "emotion": default_name}
            if matched is None: continue
            routes.append({
                "__template_key": "route",
                "label": sem,
                "aliases": ",".join(hints[:4]),
                "emotion_id": _as_int(matched.get("id"), 0),
                "emotion": str(matched.get("emotion") or matched.get("remark") or default_name),
            })

        return {
            "__template_key": "voice",
            "character": name,
            "enabled": True,
            "language": "zh",
            "default_emotion_id": default_id,
            "default_emotion": default_name,
            "emotion_routes": routes,
        }

    def _sync_voices_from_catalog(self, overwrite: bool = False, persist: bool = True) -> int:
        """把 Gateway 角色/情绪写入 voices 层叠配置。"""
        if not self._characters_cache:
            return 0
        existing = {str(v.get("character") or ""): v for v in self.voices if isinstance(v, dict)}
        new_list: List[Dict[str, Any]] = []
        count = 0
        for item in self._characters_cache:
            name = str(item.get("name") or item.get("character") or "").strip()
            if not name: continue
            if name in existing and not overwrite:
                new_list.append(existing[name])
                continue
            new_list.append(self._build_voice_profile(item))
            count += 1
        # 保留手工添加但不在网关的配置
        gateway_names = {str(i.get("name") or i.get("character") or "") for i in self._characters_cache}
        for name, v in existing.items():
            if name and name not in gateway_names and not overwrite:
                new_list.append(v)
        self.voices = new_list
        self.config["voices"] = new_list
        self._voices_synced = True
        if persist: self._persist()
        return count

    def _apply_voice_defaults_from_profile(self) -> None:
        prof = self._get_voice_profile(self.character)
        if not prof: return
        if not self.emotion_id and not self.emotion:
            self.emotion_id = _as_int(prof.get("default_emotion_id"), 0)
            self.emotion = str(prof.get("default_emotion") or "")
        lang = str(prof.get("language") or "").strip().lower()
        if lang in {"zh", "en", "hybrid"}:
            self.language = lang

    def _resolve_emotion_from_label(self, character: str, label: str) -> Tuple[int, str, str]:
        """返回 (emotion_id, emotion_name, matched_label)"""
        label = (label or "").strip()
        prof = self._get_voice_profile(character)
        routes = []
        if prof and isinstance(prof.get("emotion_routes"), list):
            routes = [r for r in prof["emotion_routes"] if isinstance(r, dict)]

        def match_route(lab: str) -> Optional[Dict[str, Any]]:
            lab_l = lab.lower()
            for r in routes:
                rlab = str(r.get("label") or "").strip()
                if not rlab: continue
                if rlab == lab or rlab.lower() == lab_l: return r
                aliases = [a.strip() for a in str(r.get("aliases") or "").split(",") if a.strip()]
                if lab in aliases or lab_l in [a.lower() for a in aliases]: return r
            for r in routes:
                rlab = str(r.get("label") or "")
                aliases = str(r.get("aliases") or "")
                if lab in rlab or rlab in lab or lab in aliases: return r
            return None

        hit = match_route(label) if label else None
        if not hit and label in SEMANTIC_HINTS:
            for h in SEMANTIC_HINTS[label]:
                hit = match_route(h)
                if hit: break
        if hit:
            return _as_int(hit.get("emotion_id"), 0), str(hit.get("emotion") or ""), str(hit.get("label") or label)

        if prof:
            return _as_int(prof.get("default_emotion_id"), 0), str(prof.get("default_emotion") or ""), "默认"
        return self.emotion_id, self.emotion, "默认"

    def _get_emotion_provider(self):
        if self.emotion_provider_id:
            p = self.context.get_provider_by_id(self.emotion_provider_id)
            if p: return p
        p = self.context.get_using_provider()
        if p: return p
        all_p = self.context.get_all_providers()
        return all_p[0] if all_p else None

    async def _detect_emotion_label(self, text: str, character: str) -> str:
        if not self.emotion_detect_enabled:
            return self.emotion_fallback or "默认"
        provider = self._get_emotion_provider()
        if not provider:
            logger.warning("[GenieTTS] 无可用 LLM，跳过情绪识别")
            return self.emotion_fallback or "默认"

        labels = [x.strip() for x in self.emotion_labels.split(",") if x.strip()]
        prof = self._get_voice_profile(character)
        if prof and isinstance(prof.get("emotion_routes"), list):
            for r in prof["emotion_routes"]:
                if isinstance(r, dict):
                    lab = str(r.get("label") or "").strip()
                    if lab and lab not in labels: labels.append(lab)
        label_str = "、".join(labels[:30]) or DEFAULT_EMOTION_LABELS
        snippet = (text or "").strip()
        if len(snippet) > 280: snippet = snippet[:280]
        prompt = (
            "你是情绪分类器。根据文本判断说话者情绪，只输出一个标签，不要解释。\n"
            f"可选标签：{label_str}\n"
            f"文本：{snippet}\n"
            "标签："
        )
        try:
            async def _call():
                return await provider.text_chat(prompt=prompt, session_id=None, contexts=[], image_urls=[], system_prompt="")
            resp = await asyncio.wait_for(_call(), timeout=self.emotion_timeout)
            raw = ""
            if getattr(resp, "role", None) == "assistant" or hasattr(resp, "completion_text"):
                raw = str(getattr(resp, "completion_text", "") or "").strip()
            else:
                raw = str(resp or "").strip()
            raw = raw.splitlines()[0].strip()
            for ch in '[]()<>"\' ':
                raw = raw.strip(ch)
            for lab in labels:
                if lab and lab in raw: return lab
            if raw: return raw[:16]
        except Exception as e:
            logger.warning(f"[GenieTTS] 情绪识别失败: {e}")
        return self.emotion_fallback or "默认"

    def _clean_text(self, text: str) -> Tuple[str, List[str]]:
        references: List[str] = []
        cleaned = (text or "").strip()
        if not cleaned: return "", references
        if self.filter_code:
            cleaned = CODE_BLOCK_RE.sub(" ", cleaned)
            cleaned = INLINE_CODE_RE.sub(" ", cleaned)
        if self.filter_url:
            cleaned = URL_RE.sub(" ", cleaned)
        if self.filter_markdown:
            cleaned = MD_IMAGE_RE.sub(r"\1", cleaned)
            cleaned = MD_LINK_RE.sub(r"\1", cleaned)
            cleaned = MD_MARK_RE.sub("", cleaned)
        if self.filter_emoji:
            cleaned = EMOJI_RE.sub("", cleaned)
            cleaned = QQ_FACE_RE.sub("", cleaned)
        if self.filter_kaomoji:
            for rgx in self._kaomoji_regex:
                cleaned = rgx.sub("", cleaned)
            for word in self.kaomoji_words:
                if word: cleaned = cleaned.replace(word, "")
            for src, dst in self._replacements.items():
                cleaned = cleaned.replace(src, dst)
            if self._repeat_regex is not None:
                cleaned = self._repeat_regex.sub(lambda m: m.group(1) * self.max_repeat_count, cleaned)
            cleaned = re.sub(r"['""\u201c\u201d]\s*['""\u201c\u201d]", "", cleaned)
            cleaned = re.sub(r"[,，、;；]\s*(?=[,，、;；\s])", "", cleaned)
            cleaned = re.sub(r"^\s*[,，、;；]+|[,，、;；]+\s*$", "", cleaned)
        cleaned = MULTI_SPACE_RE.sub(" ", cleaned).strip(" \n\t\r-—_~")
        return cleaned, references

    def _sess_id(self, event: AstrMessageEvent) -> str:
        try:
            gid = event.get_group_id()
            if gid: return f"group_{gid}"
        except Exception: pass
        return f"user_{event.get_sender_id()}"

    def _is_session_enabled(self, sid: str) -> bool:
        if self.global_enable: return sid not in self.disabled_sessions
        return sid in self.enabled_sessions

    def _get_state(self, sid: str) -> SessionState:
        return self._session_state.setdefault(sid, SessionState())

    def _list_character_names(self) -> List[str]:
        names = []
        for item in self._characters_cache:
            n = item.get("name") or item.get("character")
            if n: names.append(str(n))
        if not names:
            for v in self.voices:
                if isinstance(v, dict) and v.get("character"): names.append(str(v["character"]))
        return names

    def _get_emotions_for(self, character: str) -> List[Dict[str, Any]]:
        character = (character or "").strip()
        if character in self._emotions_cache: return list(self._emotions_cache[character])
        for item in self._characters_cache:
            if (item.get("name") or item.get("character")) == character:
                return list(item.get("emotions") or item.get("references") or [])
        return []

    def _pick_default_emotion(self, character: str) -> Tuple[int, str]:
        prof = self._get_voice_profile(character)
        if prof and (_as_int(prof.get("default_emotion_id"), 0) or prof.get("default_emotion")):
            return _as_int(prof.get("default_emotion_id"), 0), str(prof.get("default_emotion") or "")
        emos = self._get_emotions_for(character)
        for e in emos:
            if e.get("is_default") and e.get("enabled", True) is not False:
                return _as_int(e.get("id"), 0), str(e.get("emotion") or e.get("remark") or "")
        if emos: return _as_int(emos[0].get("id"), 0), str(emos[0].get("emotion") or emos[0].get("remark") or "")
        return 0, ""

    def _apply_character(self, character: str, *, persist: bool = True, auto_emotion: bool = True) -> str:
        character = (character or "").strip()
        if not character: raise ValueError("角色名不能为空")
        self.character = character
        msg = f"角色 => {character}"
        if auto_emotion:
            eid, ename = self._pick_default_emotion(character)
            self.emotion_id, self.emotion = eid, ename
            msg += f" | 情绪 => {eid}:{ename or '默认'}"
        self._model_hot = False
        if persist: self._persist()
        return msg

    def _apply_emotion(self, raw: str, *, character: Optional[str] = None, persist: bool = True, session_state: Optional[SessionState] = None) -> str:
        raw = (raw or "").strip()
        char = character or (session_state.character if session_state and session_state.character else None) or self.character
        emos = self._get_emotions_for(char)
        eid, ename = 0, ""
        if raw.isdigit():
            num = int(raw)
            by_id = next((e for e in emos if _as_int(e.get("id"), -1) == num), None)
            if by_id:
                eid, ename = num, str(by_id.get("emotion") or by_id.get("remark") or "")
            elif 1 <= num <= len(emos):
                e = emos[num - 1]; eid = _as_int(e.get("id"), 0); ename = str(e.get("emotion") or e.get("remark") or "")
            else: eid = num
        else:
            # 先当感知标签
            eid, ename, _ = self._resolve_emotion_from_label(char, raw)
            if not eid and not ename:
                ename = raw
                for e in emos:
                    label = str(e.get("emotion") or ""); remark = str(e.get("remark") or "")
                    if raw in {label, remark}: eid = _as_int(e.get("id"), 0); ename = label or remark; break
        if session_state is not None:
            session_state.emotion_id = eid if eid > 0 else None
            session_state.emotion = ename or raw
            session_state.character = char
        else:
            self.emotion_id = eid if eid > 0 else 0
            self.emotion = ename or raw
            if persist: self._persist()
        return f"角色={char} 情绪={eid or '-'}:{ename or raw}"

    async def _fetch_public_status(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if not force and self._last_status and (now - self._status_checked_at) < self.warmup_status_ttl:
            return self._last_status
        try:
            data = await self._api_json("GET", "/api/v1/public-status")
            if not isinstance(data, dict): data = {"raw": data}
        except Exception:
            try:
                data = await self._api_json("GET", "/api/v1/memory")
                if not isinstance(data, dict): data = {"raw": data}
            except Exception as e:
                logger.debug(f"[GenieTTS] status fail: {e}")
                data = dict(self._last_status) if self._last_status else {}
        self._last_status = data or {}
        self._status_checked_at = now
        sleeping = bool(data.get("sleeping"))
        loaded = data.get("loaded_characters") or []
        state = str(data.get("model_state") or "").lower()
        if sleeping or state in {"sleeping", "sleep", "idle"}: self._model_hot = False
        elif loaded and (self.character in loaded or not self.character): self._model_hot = True
        elif loaded: self._model_hot = False
        timing = data.get("timing") or {}
        try: self._warmup_eta = float(timing.get("avg_warmup_seconds") or self._warmup_eta or 30)
        except (TypeError, ValueError): pass
        return self._last_status

    async def _is_model_ready_for_tts(self) -> bool:
        if self._warming_up: return False
        status = await self._fetch_public_status(force=False)
        if not status: return self._model_hot
        if bool(status.get("sleeping")): return False
        state = str(status.get("model_state") or "").lower()
        if state in {"sleeping", "sleep", "warming", "loading", "cold"}: return False
        loaded = status.get("loaded_characters") or []
        if loaded and self.character and self.character not in loaded: return False
        self._model_hot = True
        return True

    async def _warmup_worker(self, reason: str = "") -> None:
        self._warming_up = True
        self._warmup_started_at = time.time()
        logger.info(f"[GenieTTS] warmup start reason={reason} char={self.character}")
        try:
            try: await self._api_json("POST", "/api/v1/wake", json_body={})
            except Exception as e: logger.warning(f"[GenieTTS] wake fail: {e}")
            payload = self._resolve_speak_options(None)
            payload["text"] = "预热。"
            payload["save"] = False
            status, data, _ = await self._request("POST", "/api/v1/speak", json_body=payload, expect_json=False)
            if status >= 400:
                detail = data.decode("utf-8", errors="ignore") if isinstance(data, (bytes, bytearray)) else str(data)
                raise RuntimeError(f"warmup HTTP {status}: {detail[:200]}")
            self._model_hot = True
            await self._fetch_public_status(force=True)
            logger.info(f"[GenieTTS] warmup done {time.time()-self._warmup_started_at:.1f}s")
        except Exception as e:
            self._model_hot = False
            self._last_error = f"预热失败: {e}"
            logger.error(f"[GenieTTS] warmup fail: {e}", exc_info=True)
        finally:
            self._warming_up = False
            self._warmup_task = None

    def _start_warmup(self, reason: str = "") -> bool:
        if self._warming_up and self._warmup_task and not self._warmup_task.done(): return False
        try: loop = asyncio.get_running_loop()
        except RuntimeError: return False
        self._warmup_task = loop.create_task(self._warmup_worker(reason))
        return True

    async def _ensure_ready_or_warmup(self) -> Tuple[bool, str]:
        if not self.warmup_mode: return True, "warmup_disabled"
        if await self._is_model_ready_for_tts(): return True, "hot"
        started = self._start_warmup("auto-tts")
        eta = self._warmup_eta or 30
        if self._warming_up:
            elapsed = time.time() - self._warmup_started_at if self._warmup_started_at else 0
            remain = max(0, int(eta - elapsed))
            tip = f"模型预热中，约需 {int(eta)}s，已过 {int(elapsed)}s，预计剩余 {remain}s"
            if started: tip = f"模型处于休眠，已开始预热（约 {int(eta)}s）"
            return False, tip
        return False, f"模型未就绪，正在准备预热（约 {int(eta)}s）"

    def _resolve_speak_options(self, sid: Optional[str] = None, emotion_override: Optional[Tuple[int, str]] = None) -> Dict[str, Any]:
        character = self.character
        language = self.language
        emotion_id = self.emotion_id
        emotion = self.emotion
        prof = self._get_voice_profile(character)
        if prof:
            if prof.get("language") in {"zh", "en", "hybrid"}: language = prof["language"]
            if not emotion_id and not emotion:
                emotion_id = _as_int(prof.get("default_emotion_id"), 0)
                emotion = str(prof.get("default_emotion") or "")
        if sid:
            state = self._session_state.get(sid)
            if state:
                if state.character: character = state.character
                if state.language: language = state.language
                if state.emotion_id is not None: emotion_id = state.emotion_id
                if state.emotion: emotion = state.emotion
        if emotion_override is not None:
            emotion_id, emotion = emotion_override
        payload: Dict[str, Any] = {
            "text": "",
            "character": character,
            "language": language,
            "split_sentence": self.split_sentence,
            "save": self.save_on_server,
        }
        if emotion_id and int(emotion_id) > 0: payload["emotion_id"] = int(emotion_id)
        elif emotion: payload["emotion"] = emotion
        return payload

    def _trim_silence(self, audio_path: str) -> str:
        if not self.trim_silence or not PYDUB_AVAILABLE: return audio_path
        try:
            audio = AudioSegment.from_file(audio_path)
            start_trim = detect_leading_silence(audio, silence_threshold=-40)
            end_trim = detect_leading_silence(audio.reverse(), silence_threshold=-40)
            duration = len(audio)
            end_index = max(start_trim + 1, duration - max(0, end_trim - 80))
            trimmed = audio[start_trim:end_index]
            if len(trimmed) < 100: return audio_path
            trimmed.export(audio_path, format="wav")
            return audio_path
        except Exception as e:
            logger.warning(f"[GenieTTS] trim fail: {e}")
            return audio_path

    async def _cleanup_later(self, path: str, delay: float = 30.0) -> None:
        await asyncio.sleep(delay)
        try:
            if os.path.exists(path): os.remove(path)
        except Exception as e:
            logger.warning(f"[GenieTTS] cleanup fail {path}: {e}")

    async def _generate_audio(self, text: str, *, sid: Optional[str] = None, emotion_override: Optional[Tuple[int, str]] = None) -> str:
        if not self.api_key: raise RuntimeError("未配置 api_key")
        cleaned, _ = self._clean_text(text)
        if not cleaned: raise RuntimeError("文本为空或过滤后无有效内容")
        payload = self._resolve_speak_options(sid, emotion_override=emotion_override)
        payload["text"] = cleaned
        last_error = None
        for attempt in range(self.retry_attempts + 1):
            try:
                status, data, headers = await self._request("POST", "/api/v1/speak", json_body=payload, expect_json=False)
                if status >= 400:
                    detail = data.decode("utf-8", errors="ignore") if isinstance(data, (bytes, bytearray)) else str(data)
                    raise RuntimeError(f"合成失败 HTTP {status}: {detail[:300]}")
                if not isinstance(data, (bytes, bytearray)) or len(data) < 128:
                    raise RuntimeError(f"音频过短: {0 if not data else len(data)} bytes")
                if data[:1] in (b"{", b"[") and b"RIFF" not in data[:12]:
                    raise RuntimeError(f"非音频内容: {data[:200]!r}")
                filepath = os.path.join(self.temp_dir, f"tts_{uuid.uuid4().hex}.wav")
                with open(filepath, "wb") as f: f.write(data)
                filepath = self._trim_silence(filepath)
                self._ready = True; self._model_hot = True; self._last_error = ""
                logger.info(f"[GenieTTS] ok bytes={len(data)} char={payload.get('character')} emo={payload.get('emotion_id') or payload.get('emotion')}")
                return filepath
            except Exception as e:
                last_error = e
                if attempt < self.retry_attempts:
                    logger.warning(f"[GenieTTS] retry {attempt+1}: {e}")
                    await asyncio.sleep(1 + attempt)
                else: break
        self._last_error = str(last_error) if last_error else "unknown"
        raise RuntimeError(f"音频生成失败: {self._last_error}")

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent, *args):
        try:
            if not self.api_key: return
            sid = self._sess_id(event)
            if not self._is_session_enabled(sid): return
            result = event.get_result()
            if not result or not getattr(result, "chain", None): return
            is_llm = False
            try: is_llm = bool(result.is_llm_result())
            except Exception:
                is_llm = getattr(result, "result_content_type", None) == ResultContentType.LLM_RESULT
            if not is_llm: return

            plain_indices: List[int] = []
            chunks: List[str] = []
            for i, component in enumerate(result.chain):
                if isinstance(component, Comp.Plain):
                    plain_indices.append(i); chunks.append(component.text or "")
            raw_text = " ".join(chunks).strip()
            cleaned, _ = self._clean_text(raw_text)
            if not cleaned or len(cleaned) < 2: return
            if random.random() > max(0.0, min(1.0, self.prob)): return
            if self.text_limit > 0 and len(cleaned) > self.text_limit: return
            state = self._get_state(sid)
            now = time.time()
            if self.cooldown > 0 and (now - state.last_tts_time) < self.cooldown: return

            ready, warm_msg = await self._ensure_ready_or_warmup()
            if not ready:
                logger.info(f"[GenieTTS] warmup text fallback: {warm_msg}")
                if self.warmup_tip and plain_indices:
                    tip = f"\n\n⏳ {warm_msg}，预热完成后将恢复语音。"
                    last_i = plain_indices[-1]
                    try:
                        old = result.chain[last_i].text or ""
                        result.chain[last_i] = Comp.Plain(old + tip)
                    except Exception:
                        result.chain.append(Comp.Plain(tip))
                return

            # LLM 情绪识别 -> 路由到音色映射
            character = state.character or self.character
            emotion_override = None
            if self.emotion_detect_enabled:
                label = await self._detect_emotion_label(cleaned, character)
                eid, ename, matched = self._resolve_emotion_from_label(character, label)
                state.last_emotion_label = matched or label
                emotion_override = (eid, ename)
                logger.info(f"[GenieTTS] emotion detect label={label} -> {eid}:{ename}")

            audio_path = await self._generate_audio(cleaned, sid=sid, emotion_override=emotion_override)
            state.last_tts_time = now
            state.last_tts_text = cleaned
            record = Comp.Record(file=audio_path, url=audio_path)
            if self.replace_text and plain_indices:
                for idx in sorted(plain_indices, reverse=True): del result.chain[idx]
                insert_at = plain_indices[0]
                result.chain.insert(insert_at, record)
                if self.send_text_with_audio: result.chain.insert(insert_at + 1, Comp.Plain(cleaned))
            else:
                result.chain.insert(0, record)
            asyncio.create_task(self._cleanup_later(audio_path, 30.0))
        except Exception as e:
            logger.error(f"[GenieTTS] auto tts fail: {e}", exc_info=True)
            try:
                if self.warmup_mode: self._start_warmup("tts-failed")
            except Exception: pass

    @filter.command_group("gentts")
    def gentts_group(self):
        """Genie TTS 指令组"""
        pass

    @gentts_group.command("help", alias={"帮助", "h", "?"})
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "🎙️ Genie TTS v2.2\n"
            "gentts test/on/off/status\n"
            "gentts list / characters / emotions\n"
            "gentts use <角色> [情绪] / char / emo\n"
            "gentts sync [force]  同步网关模型到层叠配置\n"
            "gentts filter <文本> 预览过滤效果\n"
            "gentts voices        查看音色映射\n"
            "管理员: wake/sleep/unload/reload/globalon/off"
        )

    @gentts_group.command("test", alias={"t"})
    async def cmd_test(self, event: AstrMessageEvent, text: str = ""):
        raw = (text or "").strip()
        if not raw:
            raw = (event.message_str or "").strip()
            for prefix in ("gentts test", "gentts t", "gentts-test"):
                if raw.lower().startswith(prefix):
                    raw = raw[len(prefix):].strip(); break
        if not raw:
            yield event.plain_result("用法: gentts test <文本>"); return
        yield event.plain_result("⏳ 正在合成...")
        try:
            sid = self._sess_id(event)
            cleaned, _ = self._clean_text(raw)
            label = await self._detect_emotion_label(cleaned, self.character) if self.emotion_detect_enabled else self.emotion_fallback
            eid, ename, matched = self._resolve_emotion_from_label(self.character, label)
            path = await self._generate_audio(raw, sid=sid, emotion_override=(eid, ename))
            yield event.chain_result([Comp.Record(file=path, url=path), Comp.Plain(f"情绪识别: {label} -> {matched} ({eid}:{ename})")])
            asyncio.create_task(self._cleanup_later(path, 30.0))
        except Exception as e:
            yield event.plain_result(f"❌ 合成失败: {e}")

    @gentts_group.command("filter", alias={"过滤预览", "sanitize"})
    async def cmd_filter(self, event: AstrMessageEvent, text: str = ""):
        raw = (text or "").strip() or (event.message_str or "")
        for prefix in ("gentts filter", "gentts 过滤预览", "gentts sanitize"):
            if raw.lower().startswith(prefix.lower()):
                raw = raw[len(prefix):].strip(); break
        cleaned, _ = self._clean_text(raw)
        yield event.plain_result(f"原文({len(raw)}):\n{raw}\n\n过滤后({len(cleaned)}):\n{cleaned or '(空)'}")

    @gentts_group.command("on")
    async def cmd_on(self, event: AstrMessageEvent):
        sid = self._sess_id(event)
        if self.global_enable:
            if sid in self.disabled_sessions: self.disabled_sessions.remove(sid)
        else:
            if sid not in self.enabled_sessions: self.enabled_sessions.append(sid)
        self._persist()
        yield event.plain_result("✅ 本会话 TTS 已启用")

    @gentts_group.command("off")
    async def cmd_off(self, event: AstrMessageEvent):
        sid = self._sess_id(event)
        if self.global_enable:
            if sid not in self.disabled_sessions: self.disabled_sessions.append(sid)
        else:
            if sid in self.enabled_sessions: self.enabled_sessions.remove(sid)
        self._persist()
        yield event.plain_result("❌ 本会话 TTS 已禁用")

    @gentts_group.command("status", alias={"状态", "info"})
    async def cmd_status(self, event: AstrMessageEvent):
        sid = self._sess_id(event)
        enabled = self._is_session_enabled(sid)
        mode = "黑名单(默认开)" if self.global_enable else "白名单(默认关)"
        state = self._get_state(sid)
        opts = self._resolve_speak_options(sid)
        warm_state = "预热中" if self._warming_up else ("热" if self._model_hot else "休眠/未加载")
        public_status = "未知"
        try:
            data = await self._api_json("GET", "/api/v1/public-status")
            if isinstance(data, dict):
                public_status = f"state={data.get('model_state')} sleeping={data.get('sleeping')} loaded={data.get('loaded_characters')}"
            else: public_status = str(data)
        except Exception as e: public_status = f"获取失败: {e}"
        last = f"\n最后TTS: {int(time.time()-state.last_tts_time)}s前 / 情绪={state.last_emotion_label or '-'}" if state.last_tts_time else ""
        yield event.plain_result(
            "📊 Genie TTS 状态\n"
            f"🔗 {self.base_url}\n"
            f"🔑 Key: {'已配置' if self.api_key else '未配置'} | 插件: {'✅' if self._ready else '❌'} {self._last_error}\n"
            f"🔥 模型: {warm_state} | 预热: {'开' if self.warmup_mode else '关'} ETA≈{int(self._warmup_eta or 30)}s\n"
            f"🌐 {public_status}\n"
            f"🎭 角色={opts.get('character')} 情绪={opts.get('emotion_id') or opts.get('emotion') or '默认'} 语言={opts.get('language')}\n"
            f"📚 音色配置: {len(self.voices)} 个 | 同步: {'是' if self._voices_synced or self.voices else '否'}\n"
            f"🧠 情绪识别: {'开' if self.emotion_detect_enabled else '关'} | 颜文字过滤: {'开' if self.filter_kaomoji else '关'}\n"
            f"⚡ 会话: {'启用' if enabled else '禁用'} ({mode}) 概率={self.prob} 限制={self.text_limit or '无'}{last}"
        )

    @gentts_group.command("sync", alias={"同步"})
    async def cmd_sync(self, event: AstrMessageEvent, mode: str = ""):
        try:
            await self._refresh_catalog(force=True)
            force = str(mode or "").strip().lower() in {"force", "overwrite", "强制", "覆盖"}
            n = self._sync_voices_from_catalog(overwrite=force or self.overwrite_on_sync, persist=True)
            self._apply_voice_defaults_from_profile()
            yield event.plain_result(
                f"✅ 已同步网关模型到配置 voices\n"
                f"更新/写入: {n} 个 | 当前共 {len(self.voices)} 个\n"
                f"覆盖模式: {'是' if (force or self.overwrite_on_sync) else '否(仅补新)'}\n"
                "可在仪表盘「音色配置」中查看层叠情绪映射；修改后重载插件生效更稳妥。"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 同步失败: {e}")

    @gentts_group.command("voices", alias={"音色", "映射"})
    async def cmd_voices(self, event: AstrMessageEvent, character: str = ""):
        char = (character or "").strip() or self.character
        if not self.voices:
            yield event.plain_result("暂无音色配置，请 gentts sync 或等待首次自动同步"); return
        lines = [f"🎚️ 音色层叠配置 (当前关注: {char})"]
        for idx, v in enumerate(self.voices, 1):
            if not isinstance(v, dict): continue
            name = str(v.get("character") or "?")
            if char and name != char and character: continue
            en = "✔" if _as_bool(v.get("enabled", True), True) else "✖"
            lines.append(f"{idx}. [{en}] {name} 默认={v.get('default_emotion_id')}:{v.get('default_emotion')} lang={v.get('language')}")
            routes = v.get("emotion_routes") or []
            for r in routes[:12]:
                if not isinstance(r, dict): continue
                lines.append(f"   - {r.get('label')} -> id={r.get('emotion_id')} {r.get('emotion')} aliases={r.get('aliases') or '-'}")
            if len(routes) > 12: lines.append(f"   ... 另有 {len(routes)-12} 条")
        yield event.plain_result("\n".join(lines) if len(lines) > 1 else "未找到匹配音色")

    @gentts_group.command("list", alias={"列表", "ls"})
    async def cmd_list(self, event: AstrMessageEvent):
        try:
            await self._refresh_catalog(force=True)
            await self._fetch_public_status(force=True)
            loaded = set(self._last_status.get("loaded_characters") or [])
            lines = ["📚 模型/情绪总览"]
            for idx, item in enumerate(self._characters_cache, 1):
                name = item.get("name") or item.get("character") or "?"
                flags = []
                if item.get("is_default"): flags.append("默认")
                if name in loaded or item.get("loaded"): flags.append("已加载")
                if name == self.character: flags.append("当前")
                flag_s = (" [" + ",".join(flags) + "]") if flags else ""
                lines.append(f"{idx}. {name}{flag_s}")
                for j, e in enumerate(item.get("emotions") or item.get("references") or [], 1):
                    mark = "*" if e.get("is_default") else " "
                    lines.append(f"   {mark}{j}) id={e.get('id')} {e.get('emotion') or e.get('remark')}")
            lines.append("\n切换: gentts use <角色|序号> [情绪] | 同步: gentts sync")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"❌ {e}")

    @gentts_group.command("characters", alias={"角色", "chars"})
    async def cmd_characters(self, event: AstrMessageEvent):
        async for r in self.cmd_list(event):
            yield r

    @gentts_group.command("emotions", alias={"情绪", "emo_list"})
    async def cmd_emotions(self, event: AstrMessageEvent, character: str = ""):
        try:
            char = (character or "").strip() or self.character
            await self._refresh_catalog(force=False)
            emotions = self._get_emotions_for(char)
            if not emotions:
                yield event.plain_result(f"角色 {char} 暂无情绪"); return
            lines = [f"😊 {char} 情绪:"]
            for idx, e in enumerate(emotions, 1):
                cur = " ←" if self.emotion_id and self.emotion_id == _as_int(e.get("id"), -1) else ""
                lines.append(f"{idx}. id={e.get('id')} {e.get('emotion') or e.get('remark')}{' [默认]' if e.get('is_default') else ''}{cur}")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"❌ {e}")

    @gentts_group.command("use", alias={"切换", "select"})
    async def cmd_use(self, event: AstrMessageEvent, character: str = "", emotion: str = ""):
        character = (character or "").strip(); emotion = (emotion or "").strip()
        if not character:
            yield event.plain_result("用法: gentts use <角色名|序号> [情绪]"); return
        try:
            await self._refresh_catalog(force=False)
            names = self._list_character_names()
            if character.isdigit():
                n = int(character)
                if 1 <= n <= len(names): character = names[n - 1]
                else: yield event.plain_result(f"序号无效 1-{len(names)}"); return
            if character not in names:
                hit = next((x for x in names if character.lower() in x.lower()), None)
                if hit: character = hit
                else: yield event.plain_result(f"未找到角色 {character}"); return
            msg = self._apply_character(character, persist=True, auto_emotion=not bool(emotion))
            if emotion: msg = self._apply_emotion(emotion, character=character, persist=True)
            if self.warmup_mode:
                self._start_warmup("switch-character")
                msg += "\n⏳ 已触发预热，期间先文本回复。"
            yield event.plain_result(f"✅ {msg}")
        except Exception as e:
            yield event.plain_result(f"❌ {e}")

    @gentts_group.command("char", alias={"角色选择", "model"})
    async def cmd_char(self, event: AstrMessageEvent, character: str = ""):
        async for r in self.cmd_use(event, character, ""): yield r

    @gentts_group.command("emo", alias={"情绪选择", "emotion"})
    async def cmd_emo(self, event: AstrMessageEvent, emotion: str = ""):
        emotion = (emotion or "").strip()
        if not emotion:
            async for r in self.cmd_emotions(event, self.character): yield r
            return
        try:
            yield event.plain_result(f"✅ {self._apply_emotion(emotion, persist=True)}")
        except Exception as e:
            yield event.plain_result(f"❌ {e}")

    @gentts_group.command("me")
    async def cmd_me(self, event: AstrMessageEvent):
        try: yield event.plain_result(f"🔑 {await self._api_json('GET', '/api/v1/me')}")
        except Exception as e: yield event.plain_result(f"❌ {e}")

    @gentts_group.command("set")
    async def cmd_set(self, event: AstrMessageEvent, field: str = "", value: str = ""):
        field = (field or "").strip().lower(); value = (value or "").strip()
        if not field or not value:
            yield event.plain_result("gentts set character/emotion/language <值> 或 global_*"); return
        sid = self._sess_id(event); state = self._get_state(sid)
        global_set = False
        if field.startswith("global_"):
            if not event.is_admin():
                yield event.plain_result("🚫 仅管理员"); return
            global_set = True; field = field[len("global_"):]
        if field in {"character", "char", "角色"}:
            if global_set: yield event.plain_result(f"✅ {self._apply_character(value, persist=True)}")
            else:
                state.character = value
                eid, ename = self._pick_default_emotion(value)
                state.emotion_id, state.emotion = eid, ename
                yield event.plain_result(f"✅ 会话角色 {value} 情绪 {eid}:{ename}")
            self._model_hot = False; return
        if field in {"language", "lang", "语言"}:
            value = value.lower()
            if value not in {"zh", "en", "hybrid"}:
                yield event.plain_result("仅 zh/en/hybrid"); return
            if global_set: self.language = value; self._persist(); yield event.plain_result(f"✅ 全局语言 {value}")
            else: state.language = value; yield event.plain_result(f"✅ 会话语言 {value}")
            return
        if field in {"emotion", "emo", "情绪", "emotion_id"}:
            if global_set: yield event.plain_result(f"✅ {self._apply_emotion(value, persist=True)}")
            else: yield event.plain_result(f"✅ {self._apply_emotion(value, session_state=state, persist=False)}")
            return
        yield event.plain_result(f"未知字段 {field}")

    @filter.permission_type(PermissionType.ADMIN)
    @gentts_group.command("globalon")
    async def cmd_globalon(self, event: AstrMessageEvent):
        self.global_enable = True; self._persist()
        yield event.plain_result("✅ 全局启用(黑名单)")

    @filter.permission_type(PermissionType.ADMIN)
    @gentts_group.command("globaloff")
    async def cmd_globaloff(self, event: AstrMessageEvent):
        self.global_enable = False; self._persist()
        yield event.plain_result("❌ 全局禁用(白名单)")

    @filter.permission_type(PermissionType.ADMIN)
    @gentts_group.command("wake")
    async def cmd_wake(self, event: AstrMessageEvent):
        try:
            if not self._start_warmup("manual-wake") and self._warming_up:
                yield event.plain_result("⏳ 预热已在进行"); return
            try: data = await self._api_json("POST", "/api/v1/wake", json_body={})
            except Exception: data = {}
            yield event.plain_result(f"✅ 开始预热(~{int(self._warmup_eta or 30)}s)\n{data}")
        except Exception as e: yield event.plain_result(f"❌ {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @gentts_group.command("sleep")
    async def cmd_sleep(self, event: AstrMessageEvent):
        try:
            data = await self._api_json("POST", "/api/v1/sleep", json_body={})
            self._model_hot = False
            yield event.plain_result(f"✅ 已休眠\n{data}")
        except Exception as e: yield event.plain_result(f"❌ {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @gentts_group.command("unload")
    async def cmd_unload(self, event: AstrMessageEvent, target: str = ""):
        try:
            target = (target or "").strip()
            body = {"all": True} if (not target or target.lower() == "all") else {"character_name": target}
            data = await self._api_json("POST", "/api/v1/unload", json_body=body)
            self._model_hot = False
            yield event.plain_result(f"✅ 卸载完成: {data}")
        except Exception as e: yield event.plain_result(f"❌ {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @gentts_group.command("reload")
    async def cmd_reload(self, event: AstrMessageEvent):
        self._apply_runtime_config()
        ok = await self._health_bootstrap()
        yield event.plain_result("✅ 重载成功" if ok else f"❌ 重载后不可用: {self._last_error}")

    @filter.command("gentts-test")
    async def compat_test(self, event: AstrMessageEvent, text: str = ""):
        async for r in self.cmd_test(event, text): yield r

    @filter.command("gentts-on")
    async def compat_on(self, event: AstrMessageEvent):
        async for r in self.cmd_on(event): yield r

    @filter.command("gentts-off")
    async def compat_off(self, event: AstrMessageEvent):
        async for r in self.cmd_off(event): yield r

    @filter.command("gentts-status")
    async def compat_status(self, event: AstrMessageEvent):
        async for r in self.cmd_status(event): yield r

