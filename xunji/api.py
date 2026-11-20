"""Fixed-destination HTTP client with bounded memory, cooldowns and safe errors."""

import asyncio
import copy
import json
import logging
import math
import time
from dataclasses import dataclass

import httpx
from cachetools import TTLCache

log = logging.getLogger("xunji")


class UserError(Exception):
    """A deliberately safe message suitable for a tool response."""


@dataclass(frozen=True)
class Endpoint:
    host: str
    path: str
    key: str
    interval: int = 15
    write: bool = False
    requires_success: bool = True


ENDPOINTS = {
    "training_get": Endpoint("trains", "/api_trains_for_llm_v2", "TRAIN", requires_success=False),
    "training_write": Endpoint("trains", "/api_upsert_trains_for_llm_v2", "TRAIN", 45, True, False),
    "plan": Endpoint("api", "/open/plan/query_gzip", "TRAIN", requires_success=False),
    "food_query": Endpoint("eatings", "/open/food/query_gzip", "FOOD"),
    "food_search": Endpoint("api", "/open_agent/food/search_gzip", "FOOD_SEARCH"),
    "food_write": Endpoint("eatings", "/open/food/upsert_gzip", "FOOD", write=True),
    "food_custom": Endpoint("eatings", "/open/food/custom/upsert_gzip", "FOOD", write=True),
    "body_query": Endpoint("api", "/open/body/query_gzip", "BODY"),
    "body_write": Endpoint("api", "/open/body/upsert_gzip", "BODY", write=True),
    "templates_sync": Endpoint(
        "trains", "/api_agent_templates_sync_for_llm_v2", "TEMPLATE", requires_success=False
    ),
    "templates_write": Endpoint(
        "trains", "/api_agent_templates_mutate_for_llm_v1", "TEMPLATE", write=True, requires_success=False
    ),
}
SAFE_ERRORS = (
    "apikey missing",
    "apikey invalid",
    "仅VIP可用",
    "too frequent",
    "version conflict",
    "user confirmation required",
    "folder_deleted",
    "template limit exceeded",
    "content safety rejected",
)


class API:
    def __init__(self, keys: dict[str, str], http: httpx.AsyncClient, *, clock=time.monotonic):
        self.keys, self.http, self.clock = keys, http, clock
        self.cache = TTLCache(maxsize=64, ttl=300, timer=clock)
        self.next_allowed = TTLCache(maxsize=4096, ttl=86400, timer=clock)
        self.lock = asyncio.Lock()

    @staticmethod
    def bucket(name, payload):
        if name.startswith("training_"):
            day = payload.get("datestr") or payload["res"][0]["datestr"]
            return (name, day)
        if name == "plan":
            return (name, payload.get("action"), payload.get("plan_ref"))
        return (name,)

    async def call(self, name: str, payload: dict, *, fresh=False):
        endpoint = ENDPOINTS[name]
        cache_key = (name, json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False))
        bucket = self.bucket(name, payload)
        interval = 30 if name == "training_get" and payload.get("include_full_data") else endpoint.interval
        async with self.lock:
            if not endpoint.write and not fresh and cache_key in self.cache:
                return copy.deepcopy(self.cache[cache_key])
            remaining = self.next_allowed.get(bucket, 0) - self.clock()
            if remaining > 0:
                raise UserError(f"限频：请 {math.ceil(remaining)} 秒后重试。")
            self.next_allowed[bucket] = self.clock() + interval
        started = self.clock()
        status = 0
        # No automatic retries, including when a write times out after reaching Xunji.
        try:
            async with self.http.stream(
                "POST",
                f"https://{endpoint.host}.xunjiapp.cn{endpoint.path}",
                json=payload,
                headers={"Authorization": f"Bearer {self.keys[endpoint.key]}", "Accept-Encoding": "gzip"},
            ) as response:
                status = response.status_code
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > 2_000_000:
                        raise UserError("响应超过 2 MB，请缩小查询范围。")
                if status == 429:
                    try:
                        delay = max(15, min(86400, float(response.headers.get("retry-after", "15"))))
                    except ValueError:
                        delay = 15
                    self.next_allowed[bucket] = self.clock() + delay
                if status >= 400 and status != 429:
                    raise UserError(f"训记接口 HTTP {status}；请求未自动重试。")
                try:
                    data = json.loads(raw)
                except ValueError, UnicodeError:
                    raise UserError("训记返回非 JSON 响应。") from None
                if not isinstance(data, dict):
                    raise UserError("训记响应结构不符合文档。")
                if data.get("success") is False or "res" not in data or status == 429:
                    message = str(data.get("message", data.get("msg", data.get("error", ""))))
                    safe = next((item for item in SAFE_ERRORS if item in message), "训记拒绝请求")
                    retry = data.get("retry_after_ms")
                    if safe == "too frequent" or status == 429:
                        delay = (
                            min(86400, max(15, retry / 1000)) if isinstance(retry, (int, float)) else interval
                        )
                        self.next_allowed[bucket] = max(
                            self.next_allowed.get(bucket, 0), self.clock() + delay
                        )
                        safe = f"too frequent：请 {math.ceil(delay)} 秒后重试"
                    raise UserError(safe)
                if endpoint.requires_success and data.get("success") is not True:
                    raise UserError("训记响应未确认成功，已停止处理。")
                if endpoint.write:
                    self.cache.clear()
                else:
                    self.cache[cache_key] = copy.deepcopy(data)
                return data
        except httpx.HTTPError:
            raise UserError("训记网络请求失败；写入结果可能未知，请读取核对，勿直接重复提交。") from None
        finally:
            log.info("endpoint=%s status=%d elapsed_ms=%d", name, status, (self.clock() - started) * 1000)
