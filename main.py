# -*- coding: utf-8 -*-
"""
AstrBot Genie-TTS 插件
适配 Genie TTS Gateway API（/api/v1/* + X-API-Key）
"""

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


def _cfg_get(config: dict, key: str, default=None, *nested_paths):
    """读取扁平或分组配置。nested_paths 示例: ('text_process', 'filter_code')"""
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
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {'1', 'true', 'yes', 'on', 'y'}:
        return True
    if s in {'0', 'false', 'no', 'off', 'n', ''}:
        return False
    return default


def _as_int(value, default: int = 0) -> int:
    try:
        if value is None or value == '':
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == '':
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

try:
    from pydub import AudioSegment
    from pydub.silence import detect_leading_silence
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False
    logger.warning("[GenieTTS] pydub 未安装，静音裁剪不可用。可选安装: pip install pydub")


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
    "]+",
    flags=re.UNICODE,
)
QQ_FACE_RE = re.compile(r"\[CQ:face[^\]]*\]|\[表情\]|/\w{1,8}")
URL_RE = re.compile(
    r"(https?://\S+|www\.\S+|file://\S+|[A-Za-z]:\\[^\s]+|/[^\s]+?\.[A-Za-z0-9]{1,6}\b)"
)
CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
INLINE_CODE_RE = re.compile(r"`[^`]+`")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
MD_MARK_RE = re.compile(r"[*_~#>]{1,3}")
MULTI_SPACE_RE = re.compile(r"[ \t\u3000]+")
MULTI_NL_RE = re.compile(r"\n{3,}")


@dataclass
class SessionState:
    last_tts_time: float = 0.0
    last_tts_text: str = ""
    character: Optional[str] = None
    emotion_id: Optional[int] = None
    emotion: Optional[str] = None
    language: Optional[str] = None


@register(
    "genie-tts",
    "victical",
    "基于 Genie TTS Gateway 的语音合成插件",
    "2.1.0",
    "https://github.com/victical/astrbot_plugin_genie-tts",
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

        # 预热状态
        self._model_hot = False
        self._warming_up = False
        self._warmup_task: Optional[asyncio.Task] = None
        self._warmup_started_at = 0.0
        self._warmup_eta = 0.0
        self._last_status: Dict[str, Any] = {}
        self._status_checked_at = 0.0

        self._apply_runtime_config()
        logger.info(
            f"[GenieTTS] 初始化完成 base_url={self.base_url} character={self.character} "
            f"language={self.language} emotion_id={self.emotion_id} emotion={self.emotion!r}"
        )

    def _apply_runtime_config(self) -> None:
        raw_base = (
            self.config.get("base_url")
            or self.config.get("server_host")
            or "http://127.0.0.1:19880"
        )
        self.base_url = self._normalize_base_url(str(raw_base), self.config.get("server_port"))
        self.api_key = str(self.config.get("api_key", "") or "").strip()
        self.character = str(
            self.config.get("character")
            or self.config.get("character_name")
            or "lxh"
        ).strip()
        self.language = str(self.config.get("language", "zh") or "zh").strip().lower()
        if self.language not in {"zh", "en", "hybrid"}:
            self.language = "zh"

        emotion_id = _cfg_get(self.config, "emotion_id", 0)
        self.emotion_id = _as_int(emotion_id, 0)
        emotion = str(_cfg_get(self.config, "emotion", "") or "").strip()
        # 兼容误填 "0"
        if emotion in {"0", "none", "null", "default"}:
            emotion = ""
        self.emotion = emotion

        self.split_sentence = _as_bool(self.config.get("split_sentence", True), True)
        self.save_on_server = _as_bool(self.config.get("save_on_server", False), False)
        self.timeout = max(10, _as_int(self.config.get("timeout", 300), 300))
        self.retry_attempts = max(0, _as_int(self.config.get("retry_attempts", 3), 3))

        self.global_enable = _as_bool(self.config.get("global_enable", True), True)
        self.prob = _as_float(self.config.get("prob", 1.0), 1.0)
        self.text_limit = _as_int(self.config.get("text_limit", 200), 200)
        self.cooldown = _as_int(self.config.get("cooldown", 0), 0)

        # 文本处理与音频裁剪（支持分组 text_process）
        self.filter_code = _as_bool(
            _cfg_get(self.config, "filter_code", True, ("text_process", "filter_code")), True
        )
        self.filter_emoji = _as_bool(
            _cfg_get(self.config, "filter_emoji", True, ("text_process", "filter_emoji")), True
        )
        self.filter_url = _as_bool(
            _cfg_get(self.config, "filter_url", True, ("text_process", "filter_url")), True
        )
        self.filter_markdown = _as_bool(
            _cfg_get(self.config, "filter_markdown", True, ("text_process", "filter_markdown")), True
        )
        self.trim_silence = _as_bool(
            _cfg_get(self.config, "trim_silence", True, ("text_process", "trim_silence")), True
        )
        self.replace_text = _as_bool(
            _cfg_get(self.config, "replace_text", True, ("text_process", "replace_text")), True
        )
        self.send_text_with_audio = _as_bool(
            _cfg_get(
                self.config,
                "send_text_with_audio",
                False,
                ("text_process", "send_text_with_audio"),
            ),
            False,
        )

        # 预热模式
        self.warmup_mode = _as_bool(
            _cfg_get(self.config, "warmup_mode", True, ("warmup", "enabled")), True
        )
        self.warmup_tip = _as_bool(
            _cfg_get(self.config, "warmup_tip", False, ("warmup", "show_tip")), False
        )
        self.warmup_status_ttl = max(
            3,
            _as_int(
                _cfg_get(self.config, "warmup_status_ttl", 8, ("warmup", "status_ttl")),
                8,
            ),
        )
        self.auto_select_emotion = _as_bool(
            _cfg_get(
                self.config,
                "auto_select_emotion",
                True,
                ("model_select", "auto_select_emotion"),
            ),
            True,
        )
        self.auto_check_on_start = _as_bool(self.config.get("auto_check_on_start", True), True)


    @staticmethod
    def _normalize_base_url(host_or_url: str, port: Any = None) -> str:
        value = (host_or_url or "").strip().rstrip("/")
        if not value:
            value = "http://127.0.0.1:19880"

        if value.startswith("http://http://"):
            value = value[len("http://"):]
        if value.startswith("https://https://"):
            value = value[len("https://"):]

        if "://" not in value:
            if port not in (None, ""):
                try:
                    port_i = int(port)
                    if ":" not in value.split("/")[0]:
                        value = f"{value}:{port_i}"
                except (TypeError, ValueError):
                    pass
            value = f"http://{value}"
        else:
            try:
                parsed = urlparse(value)
                if port not in (None, "") and not parsed.port:
                    netloc = f"{parsed.hostname}:{int(port)}"
                    value = urlunparse((parsed.scheme, netloc, parsed.path, "", "", "")).rstrip("/")
            except Exception:
                pass
        return value.rstrip("/")

    def _auth_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "*/*",
            "User-Agent": "AstrBot-GenieTTS/2.1",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if extra:
            headers.update(extra)
        return headers

    def _save_config(self) -> None:
        try:
            self.config["base_url"] = self.base_url
            self.config["api_key"] = self.api_key
            self.config["character"] = self.character
            self.config["language"] = self.language
            self.config["emotion_id"] = self.emotion_id
            self.config["emotion"] = self.emotion
            self.config["global_enable"] = self.global_enable
            self.config["prob"] = self.prob
            self.config["text_limit"] = self.text_limit
            self.config["cooldown"] = self.cooldown
            self.config["enabled_sessions"] = self.enabled_sessions
            self.config["disabled_sessions"] = self.disabled_sessions
            if hasattr(self.config, "save"):
                self.config.save()
        except Exception as e:
            logger.warning(f"[GenieTTS] 保存配置失败: {e}")

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=self.timeout, connect=15)
                self._session = aiohttp.ClientSession(timeout=timeout)
            return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        expect_json: bool = True,
        timeout: Optional[int] = None,
    ) -> Tuple[int, Any, Dict[str, str]]:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        headers = self._auth_headers(
            {"Content-Type": "application/json"} if json_body is not None else None
        )
        req_timeout = aiohttp.ClientTimeout(total=timeout or self.timeout, connect=15)
        async with session.request(
            method,
            url,
            headers=headers,
            json=json_body,
            params=params,
            timeout=req_timeout,
        ) as resp:
            status = resp.status
            resp_headers = {k: v for k, v in resp.headers.items()}
            if expect_json:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = await resp.text()
            else:
                data = await resp.read()
            return status, data, resp_headers

    async def _api_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        status, data, _ = await self._request(
            method, path, json_body=json_body, params=params, expect_json=True
        )
        if status >= 400:
            detail = data if isinstance(data, str) else str(data)
            raise RuntimeError(f"HTTP {status}: {detail}")
        return data

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        if not self.auto_check_on_start:
            self._ready = bool(self.api_key)
            return
        await self._health_bootstrap()

    async def _health_bootstrap(self) -> bool:
        try:
            if not self.api_key:
                self._ready = False
                self._last_error = "未配置 api_key"
                logger.warning("[GenieTTS] 未配置 api_key，语音功能不可用")
                return False

            try:
                status, data, _ = await self._request("GET", "/health", expect_json=True, timeout=15)
                logger.info(f"[GenieTTS] /health => {status} {data}")
            except Exception as e:
                logger.warning(f"[GenieTTS] /health 检查失败: {e}")

            me = await self._api_json("GET", "/api/v1/me")
            logger.info(f"[GenieTTS] API Key 校验成功: {me}")
            await self._refresh_catalog(force=True)
            await self._fetch_public_status(force=True)
            self._ready = True
            self._last_error = ""
            logger.info(
                f"[GenieTTS] Gateway 就绪 character={self.character} "
                f"emotion_id={self.emotion_id} emotion={self.emotion!r} hot={self._model_hot}"
            )
            return True
        except Exception as e:
            self._ready = False
            self._last_error = str(e)
            logger.error(f"[GenieTTS] Gateway 初始化失败: {e}", exc_info=True)
            return False

    async def terminate(self):
        try:
            if self._session and not self._session.closed:
                await self._session.close()
        except Exception:
            pass
        try:
            if os.path.exists(self.temp_dir):
                for name in os.listdir(self.temp_dir):
                    path = os.path.join(self.temp_dir, name)
                    if os.path.isfile(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
            logger.info("[GenieTTS] 插件已卸载，临时文件已清理")
        except Exception as e:
            logger.error(f"[GenieTTS] 清理失败: {e}")

    async def _refresh_catalog(self, force: bool = False) -> None:
        if self._characters_cache and not force:
            return
        data = await self._api_json("GET", "/api/v1/characters")
        characters = data.get("characters", data if isinstance(data, list) else [])
        self._characters_cache = characters or []

        cache: Dict[str, List[Dict[str, Any]]] = {}
        # 优先从 characters 自带 emotions 构建
        for item in self._characters_cache:
            name = item.get("name") or item.get("character") or ""
            if not name:
                continue
            emos = item.get("emotions") or item.get("references") or []
            cache[name] = list(emos)

        try:
            emotions_data = await self._api_json("GET", "/api/v1/emotions")
            for group in emotions_data.get("characters", []) or []:
                char = group.get("character") or group.get("name") or ""
                if not char:
                    continue
                emos = group.get("emotions", []) or []
                if emos:
                    cache[char] = list(emos)
        except Exception as e:
            logger.warning(f"[GenieTTS] 拉取 emotions 失败，改用 characters 内嵌数据: {e}")

        self._emotions_cache = cache
        if self.auto_select_emotion:
            self._auto_bind_default_emotion(persist=False)


    def _list_character_names(self) -> List[str]:
        names: List[str] = []
        for item in self._characters_cache:
            name = item.get("name") or item.get("character")
            if name:
                names.append(str(name))
        return names

    def _get_character_entry(self, character: str) -> Optional[Dict[str, Any]]:
        character = (character or "").strip()
        for item in self._characters_cache:
            name = item.get("name") or item.get("character")
            if name == character:
                return item
        return None

    def _get_emotions_for(self, character: str) -> List[Dict[str, Any]]:
        character = (character or "").strip()
        emos = self._emotions_cache.get(character)
        if emos:
            return list(emos)
        entry = self._get_character_entry(character)
        if entry:
            return list(entry.get("emotions") or entry.get("references") or [])
        return []

    def _pick_default_emotion(self, character: str) -> Tuple[int, str]:
        emos = self._get_emotions_for(character)
        if not emos:
            return 0, ""
        for e in emos:
            if e.get("is_default") and e.get("enabled", True) is not False:
                return _as_int(e.get("id"), 0), str(e.get("emotion") or e.get("remark") or "")
        for e in emos:
            if e.get("enabled", True) is not False:
                return _as_int(e.get("id"), 0), str(e.get("emotion") or e.get("remark") or "")
        e0 = emos[0]
        return _as_int(e0.get("id"), 0), str(e0.get("emotion") or e0.get("remark") or "")

    def _auto_bind_default_emotion(self, persist: bool = False) -> None:
        """若未指定有效情绪，则自动绑定当前角色默认情绪。"""
        if self.emotion_id > 0:
            return
        if self.emotion:
            # 名称已指定时，尝试反查 id
            for e in self._get_emotions_for(self.character):
                label = str(e.get("emotion") or "")
                remark = str(e.get("remark") or "")
                if self.emotion in {label, remark}:
                    self.emotion_id = _as_int(e.get("id"), 0)
                    if persist:
                        self._save_config()
                    return
            return
        eid, ename = self._pick_default_emotion(self.character)
        if eid > 0:
            self.emotion_id = eid
            self.emotion = ename
            logger.info(f"[GenieTTS] 自动选择情绪 character={self.character} id={eid} name={ename}")
            if persist:
                self._save_config()

    def _apply_character(self, character: str, *, persist: bool = True, auto_emotion: bool = True) -> str:
        character = (character or "").strip()
        if not character:
            raise ValueError("角色名不能为空")
        self.character = character
        msg = f"角色 => {character}"
        if auto_emotion and self.auto_select_emotion:
            eid, ename = self._pick_default_emotion(character)
            self.emotion_id = eid
            self.emotion = ename
            msg += f" | 情绪 => {eid}:{ename or '默认'}"
        # 切角色后模型可能需要重新加载
        self._model_hot = False
        if persist:
            self._save_config()
        return msg

    def _apply_emotion(
        self,
        raw: str,
        *,
        character: Optional[str] = None,
        persist: bool = True,
        session_state: Optional[SessionState] = None,
    ) -> str:
        raw = (raw or "").strip()
        char = (character or (session_state.character if session_state and session_state.character else None) or self.character)
        emos = self._get_emotions_for(char)

        eid = 0
        ename = ""
        if raw.isdigit():
            # 可能是 emotion_id，也可能是列表序号(1-based)
            num = int(raw)
            by_id = next((e for e in emos if _as_int(e.get("id"), -1) == num), None)
            if by_id:
                eid = num
                ename = str(by_id.get("emotion") or by_id.get("remark") or "")
            elif 1 <= num <= len(emos):
                e = emos[num - 1]
                eid = _as_int(e.get("id"), 0)
                ename = str(e.get("emotion") or e.get("remark") or "")
            else:
                eid = num
                ename = ""
        else:
            ename = raw
            for e in emos:
                label = str(e.get("emotion") or "")
                remark = str(e.get("remark") or "")
                if raw == label or raw == remark or raw.lower() in {label.lower(), remark.lower()}:
                    eid = _as_int(e.get("id"), 0)
                    ename = label or remark
                    break

        if session_state is not None:
            session_state.emotion_id = eid if eid > 0 else None
            session_state.emotion = ename or raw
            session_state.character = char
        else:
            self.emotion_id = eid if eid > 0 else 0
            self.emotion = ename or raw
            if persist:
                self._save_config()
        return f"角色={char} 情绪={eid or '-'}:{ename or raw}"

    async def _fetch_public_status(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if (
            not force
            and self._last_status
            and (now - self._status_checked_at) < self.warmup_status_ttl
        ):
            return self._last_status
        try:
            data = await self._api_json("GET", "/api/v1/public-status")
            if not isinstance(data, dict):
                data = {"raw": data}
        except Exception:
            try:
                data = await self._api_json("GET", "/api/v1/memory")
                if not isinstance(data, dict):
                    data = {"raw": data}
            except Exception as e:
                logger.debug(f"[GenieTTS] 状态查询失败: {e}")
                data = dict(self._last_status) if self._last_status else {}
        self._last_status = data or {}
        self._status_checked_at = now

        sleeping = bool(data.get("sleeping"))
        loaded = data.get("loaded_characters") or []
        state = str(data.get("model_state") or "").lower()
        if sleeping or state in {"sleeping", "sleep", "idle"}:
            self._model_hot = False
        elif loaded:
            # 当前角色已加载则视为热
            if self.character in loaded or not self.character:
                self._model_hot = True
            else:
                # 其它角色已加载，切角色仍可能冷
                self._model_hot = False
        timing = data.get("timing") or {}
        try:
            self._warmup_eta = float(timing.get("avg_warmup_seconds") or self._warmup_eta or 30)
        except (TypeError, ValueError):
            pass
        return self._last_status

    async def _is_model_ready_for_tts(self) -> bool:
        if self._warming_up:
            return False
        status = await self._fetch_public_status(force=False)
        if not status:
            # 状态未知时：若已有成功合成记录则允许
            return self._model_hot
        if bool(status.get("sleeping")):
            return False
        state = str(status.get("model_state") or "").lower()
        if state in {"sleeping", "sleep", "warming", "loading", "cold"}:
            return False
        loaded = status.get("loaded_characters") or []
        if loaded and self.character and self.character not in loaded:
            return False
        self._model_hot = True
        return True

    async def _warmup_worker(self, reason: str = "") -> None:
        self._warming_up = True
        self._warmup_started_at = time.time()
        logger.info(f"[GenieTTS] 开始预热 reason={reason} character={self.character}")
        try:
            # 1) 主动 wake
            try:
                await self._api_json("POST", "/api/v1/wake", json_body={})
            except Exception as e:
                logger.warning(f"[GenieTTS] wake 调用失败（将继续试合成预热）: {e}")

            # 2) 用极短文本触发真实冷加载
            payload = self._resolve_speak_options(None)
            payload["text"] = "预热。"
            payload["save"] = False
            status, data, _headers = await self._request(
                "POST",
                "/api/v1/speak",
                json_body=payload,
                expect_json=False,
            )
            if status >= 400:
                detail = (
                    data.decode("utf-8", errors="ignore")
                    if isinstance(data, (bytes, bytearray))
                    else str(data)
                )
                raise RuntimeError(f"预热合成失败 HTTP {status}: {detail[:200]}")

            self._model_hot = True
            await self._fetch_public_status(force=True)
            cost = time.time() - self._warmup_started_at
            logger.info(f"[GenieTTS] 预热完成 cost={cost:.1f}s character={self.character}")
        except Exception as e:
            self._model_hot = False
            self._last_error = f"预热失败: {e}"
            logger.error(f"[GenieTTS] 预热失败: {e}", exc_info=True)
        finally:
            self._warming_up = False
            self._warmup_task = None

    def _start_warmup(self, reason: str = "") -> bool:
        """启动后台预热，返回是否新启动。"""
        if self._warming_up and self._warmup_task and not self._warmup_task.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._warmup_task = loop.create_task(self._warmup_worker(reason))
        return True

    async def _ensure_ready_or_warmup(self) -> Tuple[bool, str]:
        """
        返回 (可立即TTS, 说明)
        若模型休眠/未加载：触发预热并建议走文本。
        """
        if not self.warmup_mode:
            return True, "warmup_disabled"

        ready = await self._is_model_ready_for_tts()
        if ready:
            return True, "hot"

        started = self._start_warmup("auto-tts")
        eta = self._warmup_eta or 30
        if self._warming_up:
            elapsed = time.time() - self._warmup_started_at if self._warmup_started_at else 0
            remain = max(0, int(eta - elapsed))
            tip = f"模型预热中，约需 {int(eta)}s，已过 {int(elapsed)}s，预计剩余 {remain}s"
            if started:
                tip = f"模型处于休眠，已开始预热（约 {int(eta)}s）"
            return False, tip
        return False, f"模型未就绪，正在准备预热（约 {int(eta)}s）"

    def _resolve_speak_options(self, sid: Optional[str] = None) -> Dict[str, Any]:
        character = self.character
        language = self.language
        emotion_id = self.emotion_id
        emotion = self.emotion

        if sid:
            state = self._session_state.get(sid)
            if state:
                if state.character:
                    character = state.character
                if state.language:
                    language = state.language
                if state.emotion_id is not None:
                    emotion_id = state.emotion_id
                if state.emotion:
                    emotion = state.emotion

        payload: Dict[str, Any] = {
            "text": "",
            "character": character,
            "language": language,
            "split_sentence": self.split_sentence,
            "save": self.save_on_server,
        }
        if emotion_id and int(emotion_id) > 0:
            payload["emotion_id"] = int(emotion_id)
        elif emotion:
            payload["emotion"] = emotion
        return payload

    def _clean_text(self, text: str) -> Tuple[str, List[str]]:
        references: List[str] = []
        cleaned = (text or "").strip()
        if not cleaned:
            return "", references

        if self.filter_code:
            for block in CODE_BLOCK_RE.findall(cleaned):
                references.append(f"[code] {block[:120]}")
            cleaned = CODE_BLOCK_RE.sub(" ", cleaned)
            cleaned = INLINE_CODE_RE.sub(" ", cleaned)

        if self.filter_url:
            for m in URL_RE.findall(cleaned):
                references.append(f"[url] {m}")
            cleaned = URL_RE.sub(" ", cleaned)

        if self.filter_markdown:
            cleaned = MD_IMAGE_RE.sub(r"\1", cleaned)
            cleaned = MD_LINK_RE.sub(r"\1", cleaned)
            cleaned = MD_MARK_RE.sub("", cleaned)

        if self.filter_emoji:
            cleaned = EMOJI_RE.sub("", cleaned)
            cleaned = QQ_FACE_RE.sub("", cleaned)

        cleaned = MULTI_SPACE_RE.sub(" ", cleaned)
        cleaned = MULTI_NL_RE.sub("\n\n", cleaned)
        cleaned = cleaned.strip(" \n\t\r-—_~")
        return cleaned, references

    def _sess_id(self, event: AstrMessageEvent) -> str:
        try:
            gid = event.get_group_id()
            if gid:
                return f"group_{gid}"
        except Exception:
            pass
        return f"user_{event.get_sender_id()}"

    def _is_session_enabled(self, sid: str) -> bool:
        if self.global_enable:
            return sid not in self.disabled_sessions
        return sid in self.enabled_sessions

    def _get_state(self, sid: str) -> SessionState:
        return self._session_state.setdefault(sid, SessionState())

    def _trim_silence(self, audio_path: str) -> str:
        if not self.trim_silence or not PYDUB_AVAILABLE:
            return audio_path
        try:
            audio = AudioSegment.from_file(audio_path)
            start_trim = detect_leading_silence(audio, silence_threshold=-40)
            end_trim = detect_leading_silence(audio.reverse(), silence_threshold=-40)
            duration = len(audio)
            keep_tail = 80
            end_index = max(start_trim + 1, duration - max(0, end_trim - keep_tail))
            trimmed = audio[start_trim:end_index]
            if len(trimmed) < 100:
                return audio_path
            trimmed.export(audio_path, format="wav")
            logger.info(
                f"[GenieTTS] 去静音: start={start_trim}ms end_trim={end_trim}ms keep={keep_tail}ms"
            )
            return audio_path
        except Exception as e:
            logger.warning(f"[GenieTTS] 去静音失败: {e}")
            return audio_path

    async def _cleanup_later(self, path: str, delay: float = 20.0) -> None:
        await asyncio.sleep(delay)
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.debug(f"[GenieTTS] 已清理临时文件: {path}")
        except Exception as e:
            logger.warning(f"[GenieTTS] 清理失败 {path}: {e}")

    async def _generate_audio(
        self,
        text: str,
        *,
        sid: Optional[str] = None,
        character: Optional[str] = None,
        emotion_id: Optional[int] = None,
        emotion: Optional[str] = None,
        language: Optional[str] = None,
    ) -> str:
        if not self.api_key:
            raise RuntimeError("未配置 api_key，请在插件配置中填写 X-API-Key")

        cleaned, _ = self._clean_text(text)
        if not cleaned or len(cleaned.strip()) < 1:
            raise RuntimeError("文本为空或过滤后无有效内容")

        payload = self._resolve_speak_options(sid)
        payload["text"] = cleaned
        if character:
            payload["character"] = character
        if language:
            payload["language"] = language
        if emotion_id is not None and int(emotion_id) > 0:
            payload["emotion_id"] = int(emotion_id)
            payload.pop("emotion", None)
        elif emotion:
            payload["emotion"] = emotion
            payload.pop("emotion_id", None)

        last_error: Optional[Exception] = None
        for attempt in range(self.retry_attempts + 1):
            try:
                status, data, headers = await self._request(
                    "POST",
                    "/api/v1/speak",
                    json_body=payload,
                    expect_json=False,
                )
                if status >= 400:
                    detail = (
                        data.decode("utf-8", errors="ignore")
                        if isinstance(data, (bytes, bytearray))
                        else str(data)
                    )
                    raise RuntimeError(f"合成失败 HTTP {status}: {detail[:300]}")

                if not isinstance(data, (bytes, bytearray)) or len(data) < 128:
                    raise RuntimeError(
                        f"返回音频过短或不合法: {0 if not data else len(data)} bytes"
                    )

                if data[:1] in (b"{", b"[") and b"RIFF" not in data[:12]:
                    raise RuntimeError(f"服务端返回非音频内容: {data[:200]!r}")

                filename = f"tts_{uuid.uuid4().hex}.wav"
                filepath = os.path.join(self.temp_dir, filename)
                with open(filepath, "wb") as f:
                    f.write(data)

                filepath = self._trim_silence(filepath)
                ref_id = headers.get("X-Reference-Id") or headers.get("x-reference-id")
                logger.info(
                    f"[GenieTTS] 合成成功 bytes={len(data)} file={filepath} "
                    f"character={payload.get('character')} ref={ref_id}"
                )
                self._ready = True
                self._model_hot = True
                self._last_error = ""
                return filepath
            except Exception as e:
                last_error = e
                if attempt < self.retry_attempts:
                    logger.warning(
                        f"[GenieTTS] 合成失败，重试 {attempt + 1}/{self.retry_attempts}: {e}"
                    )
                    await asyncio.sleep(1 + attempt)
                else:
                    break

        self._last_error = str(last_error) if last_error else "unknown"
        raise RuntimeError(f"音频生成失败: {self._last_error}")

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent, *args):
        try:
            if not self.api_key:
                return

            sid = self._sess_id(event)
            if not self._is_session_enabled(sid):
                logger.debug(f"[GenieTTS] 会话 {sid} 未启用，跳过")
                return

            result = event.get_result()
            if not result or not getattr(result, "chain", None):
                return

            is_llm = False
            try:
                is_llm = bool(result.is_llm_result())
            except Exception:
                is_llm = (
                    getattr(result, "result_content_type", None)
                    == ResultContentType.LLM_RESULT
                )
            if not is_llm:
                return

            plain_indices: List[int] = []
            chunks: List[str] = []
            for i, component in enumerate(result.chain):
                if isinstance(component, Comp.Plain):
                    plain_indices.append(i)
                    chunks.append(component.text or "")
            raw_text = " ".join(chunks).strip()
            cleaned, _ = self._clean_text(raw_text)
            if not cleaned or len(cleaned) < 2:
                return

            if random.random() > max(0.0, min(1.0, self.prob)):
                logger.info(f"[GenieTTS] 概率门控未通过 prob={self.prob}")
                return

            if self.text_limit > 0 and len(cleaned) > self.text_limit:
                logger.info(
                    f"[GenieTTS] 文本过长 {len(cleaned)} > {self.text_limit}，跳过"
                )
                return

            state = self._get_state(sid)
            now = time.time()
            if self.cooldown > 0 and (now - state.last_tts_time) < self.cooldown:
                logger.info(
                    f"[GenieTTS] 冷却中 {now - state.last_tts_time:.1f}s < {self.cooldown}s"
                )
                return

            # 预热模式：休眠/加载中时保持文本输出，后台预热
            ready, warm_msg = await self._ensure_ready_or_warmup()
            if not ready:
                logger.info(f"[GenieTTS] 预热降级为文本: {warm_msg}")
                if self.warmup_tip and plain_indices:
                    tip = f"\n\n⏳ {warm_msg}，预热完成后将恢复语音。"
                    # 附加提示到最后一条文本
                    last_i = plain_indices[-1]
                    try:
                        old = result.chain[last_i].text or ""
                        result.chain[last_i] = Comp.Plain(old + tip)
                    except Exception:
                        result.chain.append(Comp.Plain(tip))
                return

            audio_path = await self._generate_audio(cleaned, sid=sid)
            state.last_tts_time = now
            state.last_tts_text = cleaned
            self._model_hot = True

            record = Comp.Record(file=audio_path, url=audio_path)

            if self.replace_text and plain_indices:
                for idx in sorted(plain_indices, reverse=True):
                    del result.chain[idx]
                insert_at = plain_indices[0]
                result.chain.insert(insert_at, record)
                if self.send_text_with_audio:
                    result.chain.insert(insert_at + 1, Comp.Plain(cleaned))
            else:
                result.chain.insert(0, record)

            asyncio.create_task(self._cleanup_later(audio_path, 30.0))
        except Exception as e:
            # 合成异常时不吞掉原文
            logger.error(f"[GenieTTS] 自动 TTS 失败: {e}", exc_info=True)
            # 若疑似冷启动，触发预热，后续恢复语音
            try:
                if self.warmup_mode:
                    self._start_warmup("tts-failed")
            except Exception:
                pass

    @filter.command_group("gentts")
    def gentts_group(self):
        """Genie TTS 指令组"""
        pass

    @gentts_group.command("help", alias={"帮助", "h", "?"})
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "🎙️ Genie TTS 指令\n"
            "gentts test <文本>  测试合成\n"
            "gentts on/off      当前会话开关\n"
            "gentts status      查看状态\n"
            "gentts characters  角色列表\n"
            "gentts emotions [角色]  情绪列表\n"
            "gentts set character <名>\n"
            "gentts set emotion <ID或名>\n"
            "gentts set language <zh|en|hybrid>\n"
            "gentts me          当前 Key 信息\n"
            "管理员:\n"
            "gentts globalon/globaloff\n"
            "gentts wake / sleep / unload [角色|all]\n"
            "gentts reload"
        )

    @gentts_group.command("test", alias={"t"})
    async def cmd_test(self, event: AstrMessageEvent, text: str = ""):
        raw = (text or "").strip()
        if not raw:
            raw = (event.message_str or "").strip()
            for prefix in ("gentts test", "gentts t", "gentts-test"):
                if raw.lower().startswith(prefix):
                    raw = raw[len(prefix):].strip()
                    break
        if not raw:
            yield event.plain_result("用法: gentts test <要合成的文本>")
            return

        yield event.plain_result("⏳ 正在合成语音...")
        try:
            sid = self._sess_id(event)
            path = await self._generate_audio(raw, sid=sid)
            yield event.chain_result([Comp.Record(file=path, url=path)])
            asyncio.create_task(self._cleanup_later(path, 30.0))
        except Exception as e:
            yield event.plain_result(f"❌ 合成失败: {e}")

    @gentts_group.command("on")
    async def cmd_on(self, event: AstrMessageEvent):
        sid = self._sess_id(event)
        if self.global_enable:
            if sid in self.disabled_sessions:
                self.disabled_sessions.remove(sid)
        else:
            if sid not in self.enabled_sessions:
                self.enabled_sessions.append(sid)
        self._save_config()
        yield event.plain_result("✅ 本会话 TTS 已启用")

    @gentts_group.command("off")
    async def cmd_off(self, event: AstrMessageEvent):
        sid = self._sess_id(event)
        if self.global_enable:
            if sid not in self.disabled_sessions:
                self.disabled_sessions.append(sid)
        else:
            if sid in self.enabled_sessions:
                self.enabled_sessions.remove(sid)
        self._save_config()
        yield event.plain_result("❌ 本会话 TTS 已禁用")

    @gentts_group.command("status", alias={"状态", "info"})
    async def cmd_status(self, event: AstrMessageEvent):
        sid = self._sess_id(event)
        enabled = self._is_session_enabled(sid)
        mode = "黑名单模式（默认启用）" if self.global_enable else "白名单模式（默认禁用）"
        state = self._get_state(sid)
        last_tts = ""
        if state.last_tts_time > 0:
            last_tts = f"\n最后 TTS: {int(time.time() - state.last_tts_time)} 秒前"

        opts = self._resolve_speak_options(sid)
        emotion_desc = (
            f"id={opts.get('emotion_id')}"
            if opts.get("emotion_id")
            else (opts.get("emotion") or "默认")
        )

        public_status = "未知"
        try:
            data = await self._api_json("GET", "/api/v1/public-status")
            public_status = str(data)
            if isinstance(data, dict):
                public_status = (
                    f"running={data.get('running') or data.get('status') or data.get('state')} "
                    f"loaded={data.get('loaded_characters') or data.get('characters')}"
                )
        except Exception as e:
            public_status = f"获取失败: {e}"

        warm_state = '预热中' if self._warming_up else ('热' if self._model_hot else '休眠/未加载')
        text = (
            "📊 Genie TTS 状态\n"
            f"🔗 服务: {self.base_url}\n"
            f"🔑 Key: {'已配置' if self.api_key else '未配置'}\n"
            f"📡 插件就绪: {'✅' if self._ready else '❌'} {self._last_error}\n"
            f"🔥 模型状态: {warm_state}\n"
            f"🌐 网关: {public_status}\n"
            f"⏳ 预热模式: {'开' if self.warmup_mode else '关'} (ETA≈{int(self._warmup_eta or 30)}s)\n"
            f"🔧 会话模式: {mode}\n"
            f"⚡ 当前会话: {'✅ 启用' if enabled else '❌ 禁用'}\n"
            f"🎭 角色: {opts.get('character')}\n"
            f"😊 情绪: {emotion_desc}\n"
            f"🗣 语言: {opts.get('language')}\n"
            f"🎲 概率: {self.prob}\n"
            f"📏 长度限制: {self.text_limit if self.text_limit > 0 else '无'}\n"
            f"⏰ 冷却: {self.cooldown}s{last_tts}"
        )
        yield event.plain_result(text)

    @gentts_group.command("characters", alias={"角色", "chars"})
    async def cmd_characters(self, event: AstrMessageEvent):
        try:
            await self._refresh_catalog(force=True)
            if not self._characters_cache:
                yield event.plain_result("暂无角色数据")
                return
            lines = ["🎭 可用角色（gentts use <名|序号> / gentts char <名|序号>）:"]
            for idx, item in enumerate(self._characters_cache, 1):
                name = item.get("name") or item.get("character") or "?"
                default_flag = " (默认)" if item.get("is_default") else ""
                loaded_flag = " [已加载]" if item.get("loaded") else ""
                cur = " ←当前" if name == self.character else ""
                refs = item.get("emotions") or item.get("references") or []
                ref_names = []
                for r in refs[:6]:
                    label = r.get("emotion") or r.get("remark") or str(r.get("id"))
                    ref_names.append(f"{r.get('id')}:{label}")
                extra = (" | " + ", ".join(ref_names)) if ref_names else ""
                lines.append(f"{idx}. {name}{default_flag}{loaded_flag}{cur}{extra}")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"❌ 获取角色失败: {e}")

    @gentts_group.command("emotions", alias={"情绪", "emo"})
    async def cmd_emotions(self, event: AstrMessageEvent, character: str = ""):
        try:
            char = (character or "").strip() or self.character
            await self._refresh_catalog(force=True)
            emotions = self._emotions_cache.get(char)
            if emotions is None:
                try:
                    data = await self._api_json("GET", f"/api/v1/emotions/{char}")
                    emotions = data.get("emotions", data if isinstance(data, list) else [])
                except Exception:
                    emotions = []
            if not emotions:
                yield event.plain_result(f"角色 {char} 暂无情绪/参考音频")
                return
            lines = [f"😊 角色 {char} 的情绪（gentts emo <ID|名|序号>）:"]
            for idx, e in enumerate(emotions, 1):
                default_flag = " [默认]" if e.get("is_default") else ""
                enabled = "" if e.get("enabled", True) else " [禁用]"
                lang = e.get("language") or ""
                cur = ""
                if self.emotion_id and self.emotion_id == _as_int(e.get("id"), -1):
                    cur = " ←当前"
                elif self.emotion and self.emotion in {
                    str(e.get("emotion") or ""),
                    str(e.get("remark") or ""),
                }:
                    cur = " ←当前"
                lines.append(
                    f"{idx}. id={e.get('id')}  {e.get('emotion') or e.get('remark')}"
                    f"{default_flag}{enabled}{cur}  lang={lang}"
                )
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"❌ 获取情绪失败: {e}")

    @gentts_group.command("me")
    async def cmd_me(self, event: AstrMessageEvent):
        try:
            data = await self._api_json("GET", "/api/v1/me")
            yield event.plain_result(f"🔑 当前 Key 信息:\n{data}")
        except Exception as e:
            yield event.plain_result(f"❌ 获取失败: {e}")


    @gentts_group.command("list", alias={"列表", "ls"})
    async def cmd_list(self, event: AstrMessageEvent):
        try:
            await self._refresh_catalog(force=True)
            await self._fetch_public_status(force=True)
            lines = ["📚 模型/情绪总览"]
            loaded = set(self._last_status.get("loaded_characters") or [])
            for idx, item in enumerate(self._characters_cache, 1):
                name = item.get("name") or item.get("character") or "?"
                flags = []
                if item.get("is_default"):
                    flags.append("默认")
                if name in loaded or item.get("loaded"):
                    flags.append("已加载")
                if name == self.character:
                    flags.append("当前")
                flag_s = (" [" + ",".join(flags) + "]") if flags else ""
                lines.append(f"{idx}. {name}{flag_s}")
                emos = item.get("emotions") or item.get("references") or self._emotions_cache.get(name) or []
                for j, e in enumerate(emos, 1):
                    mark = "*" if e.get("is_default") else " "
                    lines.append(
                        f"   {mark} {j}) id={e.get('id')} {e.get('emotion') or e.get('remark')}"
                    )
            lines.append("\n切换: gentts use <角色序号/名> [情绪序号/名/ID]")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"❌ 列表失败: {e}")

    @gentts_group.command("use", alias={"切换", "select"})
    async def cmd_use(self, event: AstrMessageEvent, character: str = "", emotion: str = ""):
        character = (character or "").strip()
        emotion = (emotion or "").strip()
        if not character:
            yield event.plain_result("用法: gentts use <角色名或序号> [情绪名/ID/序号]")
            return
        try:
            await self._refresh_catalog(force=False)
            names = self._list_character_names()
            if character.isdigit():
                n = int(character)
                if 1 <= n <= len(names):
                    character = names[n - 1]
                else:
                    # 不存在该序号
                    yield event.plain_result(f"角色序号无效，范围 1-{len(names)}")
                    return
            # 模糊匹配角色
            if character not in names:
                lowered = character.lower()
                hit = next((x for x in names if x.lower() == lowered or lowered in x.lower()), None)
                if hit:
                    character = hit
                else:
                    yield event.plain_result(f"未找到角色: {character}\n可用: {', '.join(names)}")
                    return

            msg = self._apply_character(character, persist=True, auto_emotion=not bool(emotion))
            if emotion:
                msg = self._apply_emotion(emotion, character=character, persist=True)
            # 切模型后通常需要预热
            if self.warmup_mode:
                self._start_warmup("switch-character")
                msg += "\n⏳ 已触发预热，预热期间对话将先以文本回复。"
            yield event.plain_result(f"✅ 已切换\n{msg}")
        except Exception as e:
            yield event.plain_result(f"❌ 切换失败: {e}")

    @gentts_group.command("char", alias={"角色选择", "model"})
    async def cmd_char(self, event: AstrMessageEvent, character: str = ""):
        async for r in self.cmd_use(event, character, ""):
            yield r

    @gentts_group.command("emo", alias={"情绪选择", "emotion"})
    async def cmd_emo(self, event: AstrMessageEvent, emotion: str = ""):
        emotion = (emotion or "").strip()
        if not emotion:
            # 无参数则展示当前角色情绪
            async for r in self.cmd_emotions(event, self.character):
                yield r
            return
        try:
            await self._refresh_catalog(force=False)
            msg = self._apply_emotion(emotion, persist=True)
            yield event.plain_result(f"✅ 情绪已更新\n{msg}")
        except Exception as e:
            yield event.plain_result(f"❌ 设置情绪失败: {e}")

    @gentts_group.command("set")
    async def cmd_set(self, event: AstrMessageEvent, field: str = "", value: str = ""):
        field = (field or "").strip().lower()
        value = (value or "").strip()
        if not field or not value:
            yield event.plain_result(
                "用法:\n"
                "gentts set character <角色名>\n"
                "gentts set emotion <ID或名称>\n"
                "gentts set language <zh|en|hybrid>\n"
                "管理员全局: gentts set global_character lxh"
            )
            return

        sid = self._sess_id(event)
        state = self._get_state(sid)
        global_set = False
        if field.startswith("global_"):
            if not event.is_admin():
                yield event.plain_result("🚫 仅管理员可修改全局默认")
                return
            global_set = True
            field = field[len("global_"):]

        if field in {"character", "char", "角色"}:
            if global_set:
                msg = self._apply_character(value, persist=True, auto_emotion=True)
                yield event.plain_result(f"✅ 全局已更新\n{msg}")
            else:
                state.character = value
                if self.auto_select_emotion:
                    eid, ename = self._pick_default_emotion(value)
                    state.emotion_id = eid if eid > 0 else None
                    state.emotion = ename or None
                    yield event.plain_result(
                        f"✅ 本会话角色已设为: {value}\n情绪自动: {eid}:{ename or '默认'}"
                    )
                else:
                    yield event.plain_result(f"✅ 本会话角色已设为: {value}")
            self._model_hot = False
            return

        if field in {"language", "lang", "语言"}:
            value = value.lower()
            if value not in {"zh", "en", "hybrid"}:
                yield event.plain_result("语言仅支持: zh / en / hybrid（日文已禁用）")
                return
            if global_set:
                self.language = value
                self._save_config()
                yield event.plain_result(f"✅ 全局语言已设为: {value}")
            else:
                state.language = value
                yield event.plain_result(f"✅ 本会话语言已设为: {value}")
            return

        if field in {"emotion", "emo", "情绪", "emotion_id"}:
            if value.isdigit():
                eid = int(value)
                if global_set:
                    self.emotion_id = eid
                    self.emotion = ""
                    self._save_config()
                    yield event.plain_result(f"✅ 全局情绪 ID 已设为: {eid}")
                else:
                    state.emotion_id = eid
                    state.emotion = None
                    yield event.plain_result(f"✅ 本会话情绪 ID 已设为: {eid}")
            else:
                if global_set:
                    self.emotion = value
                    self.emotion_id = 0
                    self._save_config()
                    yield event.plain_result(f"✅ 全局情绪名已设为: {value}")
                else:
                    state.emotion = value
                    state.emotion_id = None
                    yield event.plain_result(f"✅ 本会话情绪名已设为: {value}")
            return

        yield event.plain_result(f"未知字段: {field}")

    @filter.permission_type(PermissionType.ADMIN)
    @gentts_group.command("globalon")
    async def cmd_globalon(self, event: AstrMessageEvent):
        self.global_enable = True
        self._save_config()
        yield event.plain_result("✅ 全局 TTS 已启用（黑名单模式）")

    @filter.permission_type(PermissionType.ADMIN)
    @gentts_group.command("globaloff")
    async def cmd_globaloff(self, event: AstrMessageEvent):
        self.global_enable = False
        self._save_config()
        yield event.plain_result("❌ 全局 TTS 已禁用（白名单模式）")

    @filter.permission_type(PermissionType.ADMIN)
    @gentts_group.command("wake")
    async def cmd_wake(self, event: AstrMessageEvent):
        try:
            started = self._start_warmup("manual-wake")
            if not started and self._warming_up:
                yield event.plain_result("⏳ 预热已在进行中...")
                return
            # 也直接打一次 wake 接口，worker 里还会再打
            try:
                data = await self._api_json("POST", "/api/v1/wake", json_body={})
            except Exception:
                data = {}
            yield event.plain_result(
                f"✅ 已开始预热（约 {int(self._warmup_eta or 30)}s）。预热期间自动回复走文本。\n{data}"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 唤醒失败: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @gentts_group.command("sleep")
    async def cmd_sleep(self, event: AstrMessageEvent):
        try:
            data = await self._api_json("POST", "/api/v1/sleep", json_body={})
            self._model_hot = False
            yield event.plain_result(f"✅ 已请求休眠: {data}\n之后自动回复将先走文本并后台预热。")
        except Exception as e:
            yield event.plain_result(f"❌ 休眠失败: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @gentts_group.command("unload")
    async def cmd_unload(self, event: AstrMessageEvent, target: str = ""):
        try:
            target = (target or "").strip()
            if not target or target.lower() == "all":
                body = {"all": True}
            else:
                body = {"character_name": target}
            data = await self._api_json("POST", "/api/v1/unload", json_body=body)
            yield event.plain_result(f"✅ 卸载完成: {data}")
        except Exception as e:
            yield event.plain_result(f"❌ 卸载失败: {e}")

    @filter.permission_type(PermissionType.ADMIN)
    @gentts_group.command("reload")
    async def cmd_reload(self, event: AstrMessageEvent):
        self._apply_runtime_config()
        ok = await self._health_bootstrap()
        if ok:
            yield event.plain_result("✅ 配置已重载，Gateway 连接正常")
        else:
            yield event.plain_result(f"❌ 重载后仍不可用: {self._last_error}")

    @filter.command("gentts-test")
    async def compat_test(self, event: AstrMessageEvent, text: str = ""):
        async for r in self.cmd_test(event, text):
            yield r

    @filter.command("gentts-on")
    async def compat_on(self, event: AstrMessageEvent):
        async for r in self.cmd_on(event):
            yield r

    @filter.command("gentts-off")
    async def compat_off(self, event: AstrMessageEvent):
        async for r in self.cmd_off(event):
            yield r

    @filter.command("gentts-status")
    async def compat_status(self, event: AstrMessageEvent):
        async for r in self.cmd_status(event):
            yield r

