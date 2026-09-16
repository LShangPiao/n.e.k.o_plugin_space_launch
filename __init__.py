# 太空发射插件 —— 查询全球下一次及未来即将进行的太空发射任务
# Copyright (C) 2026 星河拓航工作室 (Galaxy Exploration Studio)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Space Launch Plugin（太空发射）

查询全球下一次及未来即将进行的太空发射任务。

数据来源：Launch Library 2 API
- 官网：https://thespacedevs.com/llapi
- 接口：https://ll.thespacedevs.com/2.3.0/launches/upcoming/

功能：
- next_launch          查询"下一次太空发射"（核心功能）
- upcoming_launches    查询未来多次发射，支持按发射服务商 / 火箭 / 发射场筛选
- 以及两个供猫娘在对话中主动调用的 LLM 工具

说明：Launch Library 2 免费额度约为每小时 15 次请求，因此插件内置了
带 TTL 的内存缓存来避免重复请求。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
)

# ---------------------------------------------------------------------------
# 常量与工具函数
# ---------------------------------------------------------------------------

DEFAULT_API_BASE = "https://ll.thespacedevs.com/2.3.0/"
_USER_AGENT = "N.E.K.O-space-launch-plugin/1.0 (+https://project-neko.online)"

# 本地筛选时最多向 API 请求的条目数，避免一次拉取过多数据
_MAX_FETCH_LIMIT = 50
# 对外可返回的最大条目数
_MAX_RESULT_LIMIT = 20


class _ApiError(Exception):
    """表示一次 LL2 API 调用失败，携带面向用户的友好信息。"""


def _as_dict(value: Any) -> Dict[str, Any]:
    """安全地把任意值转成 dict。"""
    return value if isinstance(value, dict) else {}


def _as_text(value: Any) -> str:
    """安全地把任意值转成去空白的字符串。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _parse_iso(value: Any) -> Optional[datetime]:
    """解析 LL2 返回的 ISO 8601 时间（通常以 Z 结尾）。"""
    text = _as_text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _to_local_text(value: Optional[datetime]) -> str:
    """把时间转换成用户本地时区的可读字符串。"""
    if value is None:
        return ""
    local = value.astimezone()
    tzname = local.tzname() or ""
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} {tzname}".strip()


def _humanize_delta(delta_seconds: float) -> str:
    """把秒数差转成“还有 X 天 Y 小时”这样的中文描述。"""
    future = delta_seconds >= 0
    total = abs(int(delta_seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts: List[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    if not parts:
        parts.append(f"{seconds} 秒")

    text = " ".join(parts)
    return f"还有 {text}" if future else f"已过去 {text}"


def _coerce_limit(value: Any, default: int) -> int:
    """把外部传入的 limit 规整到合法区间。"""
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise SdkError(f"limit 必须是整数，收到：{value!r}") from None
    if number < 1:
        number = 1
    return min(number, _MAX_RESULT_LIMIT)


# LL2 中表示任务已经结束的状态缩写
_FINISHED_STATUS = {"Success", "Failure", "Partial Failure"}


def _is_still_upcoming(item: Dict[str, Any]) -> bool:
    """判断一条记录是否仍然算“即将进行”。

    LL2 的 ``launches/upcoming/`` 端点偶尔会返回刚刚发射完、状态尚未刷新
    的条目。如果不过滤，``next_launch`` 就可能把已经结束的任务当成“下一次
    发射”报给用户。
    """
    if _as_text(item.get("status_abbrev")) in _FINISHED_STATUS:
        return False
    seconds = item.get("countdown_seconds")
    if seconds is None:
        # 发射时间待定的任务仍然可能发生，保留
        return True
    # 留 60 秒容差，避免刚过 T-0 的任务被立刻剔除
    return seconds > -60


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------

def _summarize_launch(
    raw: Dict[str, Any],
    *,
    include_description: bool,
    now: datetime,
) -> Dict[str, Any]:
    """把 LL2 的一条发射记录裁剪成插件自己的结构。"""
    rocket_cfg = _as_dict(_as_dict(raw.get("rocket")).get("configuration"))
    mission = _as_dict(raw.get("mission"))
    pad = _as_dict(raw.get("pad"))
    location = _as_dict(pad.get("location"))
    country = _as_dict(pad.get("country"))
    status = _as_dict(raw.get("status"))
    provider = _as_dict(raw.get("launch_service_provider"))
    orbit = _as_dict(mission.get("orbit"))
    net_precision = _as_dict(raw.get("net_precision"))
    image = _as_dict(raw.get("image"))

    net_dt = _parse_iso(raw.get("net"))
    countdown_seconds: Optional[float] = None
    countdown = ""
    if net_dt is not None:
        countdown_seconds = (net_dt - now).total_seconds()
        countdown = _humanize_delta(countdown_seconds)

    item: Dict[str, Any] = {
        "name": _as_text(raw.get("name")),
        "status": _as_text(status.get("name")),
        "status_abbrev": _as_text(status.get("abbrev")),
        "provider": _as_text(provider.get("name")),
        "provider_abbrev": _as_text(provider.get("abbrev")),
        "rocket": _as_text(rocket_cfg.get("full_name")) or _as_text(rocket_cfg.get("name")),
        "mission": _as_text(mission.get("name")),
        "mission_type": _as_text(mission.get("type")),
        "orbit": _as_text(orbit.get("name")),
        "pad": _as_text(pad.get("name")),
        "location": _as_text(location.get("name")),
        "country": _as_text(country.get("name")),
        "net_utc": net_dt.astimezone(timezone.utc).isoformat() if net_dt else "",
        "net_local": _to_local_text(net_dt),
        "net_precision": _as_text(net_precision.get("name")),
        "window_start_local": _to_local_text(_parse_iso(raw.get("window_start"))),
        "window_end_local": _to_local_text(_parse_iso(raw.get("window_end"))),
        "countdown": countdown,
        "countdown_seconds": countdown_seconds,
        "webcast_live": bool(raw.get("webcast_live")),
        "url": _as_text(raw.get("url")),
        "image": _as_text(image.get("image_url")),
    }
    if include_description:
        item["description"] = _as_text(mission.get("description"))
    return item


def _describe_launch(item: Dict[str, Any]) -> str:
    """生成一句适合猫娘念出来的中文描述。"""
    name = item.get("name") or "未知任务"
    text = f"下一次太空发射是「{name}」"

    rocket = item.get("rocket") or ""
    if rocket:
        text += f"，使用 {rocket} 火箭"
    provider = item.get("provider") or ""
    if provider:
        text += f"，由 {provider} 执行"

    net_local = item.get("net_local") or ""
    if net_local:
        text += f"，计划于 {net_local} 发射"
    else:
        text += "，发射时间尚未确定"

    site = item.get("location") or item.get("pad") or ""
    if site:
        text += f"，发射场：{site}"

    countdown = item.get("countdown") or ""
    if countdown:
        text += f"（{countdown}）"

    status = item.get("status") or ""
    if status:
        text += f"。当前状态：{status}"

    mission_type = item.get("mission_type") or ""
    if mission_type:
        text += f"，任务类型：{mission_type}"

    return text + "。"


def _describe_launch_list(items: List[Dict[str, Any]]) -> str:
    """生成发射列表的中文概述。"""
    if not items:
        return "暂时没有查询到符合条件的发射任务。"

    lines = [f"接下来有 {len(items)} 次太空发射："]
    for index, item in enumerate(items, start=1):
        name = item.get("name") or "未知任务"
        net_local = item.get("net_local") or "时间待定"
        countdown = item.get("countdown") or ""
        provider = item.get("provider") or ""
        suffix = f"，{provider}" if provider else ""
        tail = f"（{countdown}）" if countdown else ""
        lines.append(f"{index}. {name}{suffix} — {net_local}{tail}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------

@neko_plugin
class SpaceLaunchPlugin(NekoPluginBase):
    """太空发射查询插件。"""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger

        # 运行配置（在 startup 中从 self.config 读取）
        self._cfg: Dict[str, Any] = {}
        self._api_base: str = DEFAULT_API_BASE
        self._timeout: float = 15.0
        self._default_limit: int = 5
        self._include_descriptions: bool = True
        self._cache_ttl: float = 300.0

        # 按事件循环缓存 httpx 客户端：宿主会在不同的 asyncio.run()
        # 调用中执行 startup、入口命令与 shutdown，跨循环复用客户端会报错。
        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop: Any = None

        # key -> (过期时间, 响应数据)
        self._cache: Dict[str, Tuple[float, Any]] = {}

    # -- 基础设施 ---------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        """获取当前事件循环下可用的 httpx 客户端。"""
        try:
            loop: Any = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 正常情况下不会发生
            loop = None

        if self._client is None or self._client.is_closed or self._client_loop is not loop:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=self._timeout,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/json",
                },
            )
            self._client_loop = loop
        return self._client

    def _cache_get(self, key: str) -> Any:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: str, value: Any) -> None:
        # 简单的容量保护，避免无限增长
        if len(self._cache) > 32:
            self._cache.clear()
        self._cache[key] = (time.monotonic() + self._cache_ttl, value)

    async def _request_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """请求 LL2 接口并返回解析后的 JSON。

        成功结果会按 TTL 缓存，失败抛出 :class:`_ApiError`。
        """
        base = self._api_base if self._api_base.endswith("/") else self._api_base + "/"
        url = f"{base}{path.lstrip('/')}"
        cache_key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))

        cached = self._cache_get(cache_key)
        if cached is not None:
            self.logger.debug("LL2 命中缓存: {}", cache_key)
            return cached

        client = self._get_client()
        try:
            response = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise _ApiError(
                f"请求 Launch Library 2 超时（{self._timeout:.0f} 秒），请稍后重试。"
            ) from exc
        except httpx.HTTPError as exc:
            raise _ApiError(f"无法连接 Launch Library 2：{exc}") from exc

        if response.status_code == 429:
            raise _ApiError(
                "Launch Library 2 免费额度已用完（约每小时 15 次请求），请稍后再试。"
            )
        if response.status_code >= 400:
            raise _ApiError(
                f"Launch Library 2 返回错误状态 {response.status_code}。"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise _ApiError("Launch Library 2 返回的内容不是合法 JSON。") from exc

        if not isinstance(data, dict):
            raise _ApiError("Launch Library 2 返回了非预期的数据结构。")

        self._cache_set(cache_key, data)
        return data

    async def _fetch_upcoming(
        self,
        *,
        limit: int,
        provider: str = "",
        rocket: str = "",
        location: str = "",
    ) -> List[Dict[str, Any]]:
        """查询即将进行的发射，并在本地完成关键字筛选。"""
        provider = _as_text(provider)
        rocket = _as_text(rocket)
        location = _as_text(location)
        needs_filter = bool(provider or rocket or location)

        # 多取一些条目：本地筛选会丢弃部分结果，而且 API 的 upcoming 列表
        # 偶尔混有刚刚结束、状态尚未刷新的任务，需要留出过滤余量。
        fetch_limit = min(max(limit * 4, limit + 10), _MAX_FETCH_LIMIT)

        payload = await self._request_json(
            "launches/upcoming/",
            {"limit": fetch_limit, "mode": "normal"},
        )
        results = payload.get("results")
        if not isinstance(results, list):
            raise _ApiError("Launch Library 2 没有返回 results 字段。")

        now = datetime.now(timezone.utc)
        items = [
            _summarize_launch(
                _as_dict(raw),
                include_description=self._include_descriptions,
                now=now,
            )
            for raw in results
            if isinstance(raw, dict)
        ]

        # 丢弃已经结束的任务，避免把“昨天发射成功”当成“下一次发射”
        items = [item for item in items if _is_still_upcoming(item)]

        # 有确切发射时间的排前面并按时间升序，时间待定的排在最后
        items.sort(key=lambda item: (
            1 if not item.get("net_utc") else 0,
            item.get("net_utc") or "",
        ))

        if needs_filter:
            def matches(item: Dict[str, Any]) -> bool:
                haystack = " ".join(
                    str(item.get(field) or "")
                    for field in ("provider", "provider_abbrev", "rocket", "location", "pad", "name")
                ).lower()
                for keyword in (provider, rocket, location):
                    if keyword and keyword.lower() not in haystack:
                        return False
                return True

            items = [item for item in items if matches(item)]

        return items[:limit]

    # -- 生命周期 ---------------------------------------------------------

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        try:
            cfg = await self.config.dump(timeout=5.0)
        except Exception as exc:  # pragma: no cover - 配置读取失败时退回默认值
            self.logger.warning("读取插件配置失败，使用默认值: {}", exc)
            cfg = {}
        cfg = cfg if isinstance(cfg, dict) else {}
        self._cfg = cfg

        section = _as_dict(cfg.get("space_launch"))

        api_base = _as_text(section.get("api_base_url")) or DEFAULT_API_BASE
        self._api_base = api_base

        try:
            timeout = float(section.get("timeout_seconds", 15))
        except (TypeError, ValueError):
            timeout = 15.0
        self._timeout = min(max(timeout, 3.0), 60.0)

        try:
            default_limit = int(section.get("default_limit", 5))
        except (TypeError, ValueError):
            default_limit = 5
        self._default_limit = min(max(default_limit, 1), _MAX_RESULT_LIMIT)

        self._include_descriptions = bool(section.get("include_descriptions", True))

        try:
            cache_ttl = float(section.get("cache_ttl_seconds", 300))
        except (TypeError, ValueError):
            cache_ttl = 300.0
        self._cache_ttl = max(cache_ttl, 0.0)

        self.logger.info(
            "SpaceLaunch 已就绪，API={} 默认条数={} 缓存={}s",
            self._api_base,
            self._default_limit,
            int(self._cache_ttl),
        )
        return Ok({
            "status": "ready",
            "api_base_url": self._api_base,
            "default_limit": self._default_limit,
            "cache_ttl_seconds": int(self._cache_ttl),
        })

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        client = self._client
        self._client = None
        self._client_loop = None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception as exc:  # pragma: no cover - 关闭失败不影响停机
                self.logger.debug("关闭 HTTP 客户端时出错: {}", exc)
        self._cache.clear()
        self.logger.info("SpaceLaunch 已停止")
        return Ok({"status": "stopped"})

    # -- 插件入口 ---------------------------------------------------------

    @plugin_entry(
        id="next_launch",
        name="下一次太空发射",
        description=(
            "查询全球下一次即将进行的太空发射任务，返回任务名称、火箭型号、发射服务商、"
            "发射时间（本地时区与倒计时）、发射场以及任务简介。可选按发射服务商、"
            "火箭型号或发射场关键字过滤。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "可选，按发射服务商关键字过滤，例如 SpaceX、Roscosmos、CASC",
                },
                "rocket": {
                    "type": "string",
                    "description": "可选，按火箭型号关键字过滤，例如 Falcon 9、Long March",
                },
                "location": {
                    "type": "string",
                    "description": "可选，按发射场关键字过滤，例如 Cape Canaveral、Wenchang",
                },
            },
        },
        timeout=30,
        llm_result_fields=["summary", "launch", "count"],
    )
    async def next_launch(
        self,
        provider: str = "",
        rocket: str = "",
        location: str = "",
        **_,
    ):
        """查询下一次太空发射。"""
        try:
            items = await self._fetch_upcoming(
                limit=1,
                provider=provider,
                rocket=rocket,
                location=location,
            )
        except _ApiError as exc:
            self.logger.warning("查询下一次发射失败: {}", exc)
            return Err(SdkError(str(exc)))
        except Exception as exc:  # pragma: no cover - 兜底，避免插件进程崩溃
            self.logger.exception("查询下一次发射时发生未预期错误")
            return Err(SdkError(f"查询下一次发射失败：{exc}"))

        if not items:
            return Err(SdkError("暂时没有查询到符合条件的即将发射任务。"))

        launch = items[0]
        summary = _describe_launch(launch)
        if launch.get("description"):
            summary += f"\n任务简介：{launch['description']}"

        self.logger.info("下一次发射: {}", launch.get("name"))
        return Ok({
            "summary": summary,
            "launch": launch,
            "count": 1,
        })

    @plugin_entry(
        id="upcoming_launches",
        name="即将进行的发射列表",
        description=(
            "查询未来即将进行的一系列太空发射任务列表，可指定返回条数（1-20），"
            "并可按发射服务商、火箭型号或发射场关键字过滤。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回条数，1-20，默认使用插件配置值",
                    "minimum": 1,
                    "maximum": 20,
                },
                "provider": {
                    "type": "string",
                    "description": "可选，按发射服务商关键字过滤，例如 SpaceX",
                },
                "rocket": {
                    "type": "string",
                    "description": "可选，按火箭型号关键字过滤，例如 Falcon 9",
                },
                "location": {
                    "type": "string",
                    "description": "可选，按发射场关键字过滤，例如 Wenchang",
                },
            },
        },
        timeout=30,
        llm_result_fields=["summary", "launches", "count"],
    )
    async def upcoming_launches(
        self,
        limit: Any = None,
        provider: str = "",
        rocket: str = "",
        location: str = "",
        **_,
    ):
        """查询未来多次太空发射。"""
        try:
            wanted = _coerce_limit(limit, self._default_limit)
        except SdkError as exc:
            return Err(exc)

        try:
            items = await self._fetch_upcoming(
                limit=wanted,
                provider=provider,
                rocket=rocket,
                location=location,
            )
        except _ApiError as exc:
            self.logger.warning("查询发射列表失败: {}", exc)
            return Err(SdkError(str(exc)))
        except Exception as exc:  # pragma: no cover
            self.logger.exception("查询发射列表时发生未预期错误")
            return Err(SdkError(f"查询发射列表失败：{exc}"))

        if not items:
            return Err(SdkError("暂时没有查询到符合条件的即将发射任务。"))

        self.logger.info("返回 {} 条即将进行的发射", len(items))
        return Ok({
            "summary": _describe_launch_list(items),
            "launches": items,
            "count": len(items),
        })

    @plugin_entry(
        id="launch_stats",
        name="发射数据概览",
        description=(
            "返回 Launch Library 2 中即将进行的发射总数，以及插件当前使用的 API 地址与缓存设置。"
        ),
        timeout=30,
        llm_result_fields=["summary", "total_upcoming", "api_base_url"],
    )
    async def launch_stats(self, **_):
        """返回即将进行的发射总数等概览信息。"""
        try:
            payload = await self._request_json(
                "launches/upcoming/",
                {"limit": 1, "mode": "list"},
            )
        except _ApiError as exc:
            return Err(SdkError(str(exc)))
        except Exception as exc:  # pragma: no cover
            self.logger.exception("查询发射概览时发生未预期错误")
            return Err(SdkError(f"查询发射概览失败：{exc}"))

        total = payload.get("count")
        try:
            total_int = int(total) if total is not None else 0
        except (TypeError, ValueError):
            total_int = 0

        return Ok({
            "total_upcoming": total_int,
            "api_base_url": self._api_base,
            "cache_ttl_seconds": int(self._cache_ttl),
            "summary": f"Launch Library 2 目前记录了 {total_int} 次即将进行的发射。",
        })

    # -- LLM 工具（对话中由猫娘主动调用）---------------------------------

    @llm_tool(
        name="get_next_space_launch",
        description=(
            "获取全球下一次即将进行的太空发射信息。当用户询问“下一次火箭发射是什么时候”、"
            "“最近有什么发射任务”等问题时调用。返回任务名称、火箭、发射服务商、"
            "本地发射时间和倒计时。可选按发射服务商或火箭关键字过滤。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "可选，发射服务商关键字，例如 SpaceX。留空表示不限制。",
                },
                "rocket": {
                    "type": "string",
                    "description": "可选，火箭型号关键字，例如 Falcon 9。留空表示不限制。",
                },
            },
        },
        timeout=30,
    )
    async def get_next_space_launch(
        self,
        provider: str = "",
        rocket: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """LLM 工具：返回下一次发射。"""
        try:
            items = await self._fetch_upcoming(limit=1, provider=provider, rocket=rocket)
        except Exception as exc:
            return {"output": None, "is_error": True, "error": str(exc)}

        if not items:
            return {"output": {"summary": "暂时没有查询到符合条件的即将发射任务。"}}

        launch = items[0]
        return {
            "output": {
                "summary": _describe_launch(launch),
                "name": launch.get("name"),
                "rocket": launch.get("rocket"),
                "provider": launch.get("provider"),
                "net_local": launch.get("net_local"),
                "countdown": launch.get("countdown"),
                "location": launch.get("location") or launch.get("pad"),
                "status": launch.get("status"),
            }
        }

    @llm_tool(
        name="list_upcoming_space_launches",
        description=(
            "获取未来即将进行的一系列太空发射任务列表。当用户询问“接下来有哪些发射”、"
            "“这个月有什么火箭发射”等问题时调用。可按发射服务商或火箭关键字过滤。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回条数，1-20，默认 5",
                },
                "provider": {
                    "type": "string",
                    "description": "可选，发射服务商关键字，例如 SpaceX。留空表示不限制。",
                },
                "rocket": {
                    "type": "string",
                    "description": "可选，火箭型号关键字，例如 Long March。留空表示不限制。",
                },
            },
        },
        timeout=30,
    )
    async def list_upcoming_space_launches(
        self,
        limit: Any = None,
        provider: str = "",
        rocket: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """LLM 工具：返回发射列表。"""
        try:
            wanted = _coerce_limit(limit, self._default_limit)
        except SdkError as exc:
            return {"output": None, "is_error": True, "error": str(exc)}

        try:
            items = await self._fetch_upcoming(limit=wanted, provider=provider, rocket=rocket)
        except Exception as exc:
            return {"output": None, "is_error": True, "error": str(exc)}

        if not items:
            return {"output": {"summary": "暂时没有查询到符合条件的即将发射任务。"}}

        return {
            "output": {
                "summary": _describe_launch_list(items),
                "launches": [
                    {
                        "name": item.get("name"),
                        "rocket": item.get("rocket"),
                        "provider": item.get("provider"),
                        "net_local": item.get("net_local"),
                        "countdown": item.get("countdown"),
                        "location": item.get("location") or item.get("pad"),
                        "status": item.get("status"),
                    }
                    for item in items
                ],
                "count": len(items),
            }
        }


__all__ = ["SpaceLaunchPlugin"]
