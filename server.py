"""Production HTTP entry point. Startup fails closed if any credential is missing."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from starlette.responses import PlainTextResponse

from xunji.api import API, UserError
from xunji.auth import make_auth, required
from xunji.service import Service
from xunji.validation import BODY_TYPES, check, date_range

READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}
PREVIEW = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}


def safe(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except UserError as exc:
            raise ToolError(str(exc)) from None

    return wrapped


def owner():
    token = get_access_token()
    if token is None:
        raise ToolError("需要通过 OAuth 登录。")
    return str(token.claims["sub"])


def create_server():
    os.umask(0o077)
    auth, store = make_auth()
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(30, connect=10),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=4),
        trust_env=False,
    )
    keys = {
        name: required(f"XUNJI_{name}_KEY") for name in ("TRAIN", "FOOD", "FOOD_SEARCH", "BODY", "TEMPLATE")
    }
    names = set(json.loads((Path(__file__).parent / "movements.json").read_text(encoding="utf-8"))["names"])
    service = Service(
        API(keys, http), names, writes_enabled=os.environ.get("ENABLE_WRITES", "false").lower() == "true"
    )

    async def reap():
        while True:
            await store.cull()
            await asyncio.sleep(3600)

    @asynccontextmanager
    async def lifespan(_):
        task = asyncio.create_task(reap())
        try:
            yield {}
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await http.aclose()

    mcp = FastMCP(
        "Xunji",
        auth=auth,
        lifespan=lifespan,
        mask_error_details=True,
        instructions="仅在用户要求时读取训记数据。写入必须先用 preview 工具，把完整变更摘要展示给用户，等用户明确确认后才调用 commit_write。禁止自动批准。确认前不提交；不确定的动作、食物、营养来源和数量须询问用户。限频时按提示等待；写入超时先核对，勿重新创建相同写入。身体数据不用于医疗诊断。",
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request):
        return PlainTextResponse("ok")

    @mcp.custom_route("/", methods=["GET"])
    async def home(request):
        return PlainTextResponse("Xunji MCP. Connect using /mcp with GitHub OAuth.")

    @mcp.tool(annotations=READ)
    @safe
    async def training_get(datestr: str, include_full_data: bool = False, refresh: bool = False) -> dict:
        """读取 YYYY-MM-DD 的训练。修改前或读取 RPE/未完成组/心率时须用完整数据。"""
        return await service.training_get(datestr, include_full_data, refresh)

    @mcp.tool(annotations=READ)
    @safe
    async def plans_list(refresh: bool = False) -> dict:
        """列出用户正在使用的官方计划，返回 plan_ref。"""
        return await service.api.call(
            "plan", {"schema_version": "plan_open_api_v1", "action": "list"}, fresh=refresh
        )

    @mcp.tool(annotations=READ)
    @safe
    async def plan_get(plan_ref: str, start_date: str, end_date: str, include_movements: bool = True) -> dict:
        """用 plans_list 返回的 plan_ref 读取官方计划；日期范围最多 92 天。"""
        date_range(start_date, end_date, max_days=92)
        check(plan_ref.startswith(("platform:", "universal:")), "请使用 plans_list 返回的 plan_ref。")
        return await service.api.call(
            "plan",
            {
                "schema_version": "plan_open_api_v1",
                "action": "get",
                "plan_ref": plan_ref,
                "start_date": start_date,
                "end_date": end_date,
                "include_movements": include_movements,
            },
        )

    @mcp.tool(annotations=READ)
    @safe
    async def food_records_query(
        start_date: str, end_date: str, include_detail: bool = True, refresh: bool = False
    ) -> dict:
        """读取指定日期范围的饮食记录；只查询用户需要的日期，允许过去一年至未来三个月。"""
        date_range(start_date, end_date, food=True)
        return await service.api.call(
            "food_query",
            {"start_date": start_date, "end_date": end_date, "include_detail": include_detail},
            fresh=refresh,
        )

    @mcp.tool(annotations=READ)
    @safe
    async def food_search(keyword: str, limit: int = 8) -> dict:
        """搜索训记官方食物，后续写入使用返回的 uniquekey、ntr 和单位换算。"""
        check(1 <= len(keyword.strip()) <= 100 and 1 <= limit <= 20, "关键词限 1–100 字，limit 限 1–20。")
        return await service.api.call("food_search", {"keyword": keyword, "limit": limit})

    @mcp.tool(annotations=READ)
    @safe
    async def body_query(
        start_date: str,
        end_date: str,
        types: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        refresh: bool = False,
    ) -> dict:
        """读取身体数据。体重 kg、体脂率 %、围度 cm；分页通过 limit/offset 控制。"""
        date_range(start_date, end_date)
        check(1 <= limit <= 500 and offset >= 0, "limit 限 1–500，offset 须非负。")
        check(types is None or (bool(types) and set(types) <= BODY_TYPES), "身体指标类型无效。")
        payload = {
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
            "offset": offset,
            "include_latest": True,
            "include_records": True,
        }
        if types is not None:
            payload["types"] = types
        return await service.api.call("body_query", payload, fresh=refresh)

    @mcp.tool(annotations=READ)
    @safe
    async def templates_sync() -> dict:
        """获取 Agent 专属文件夹及全部模版、完整版本表；每次编辑前调用。"""
        return await service.api.call("templates_sync", {}, fresh=True)

    @mcp.tool(annotations=READ)
    @safe
    async def movement_names_search(keyword: str, limit: int = 30) -> dict:
        """从固定版本的训记标准中文动作名表搜索；写回只传中文 name。"""
        check(1 <= limit <= 100 and bool(keyword.strip()), "请输入关键词；limit 限 1–100。")
        return {"names": [name for name in sorted(names) if keyword.casefold() in name.casefold()][:limit]}

    @mcp.tool(annotations=PREVIEW)
    @safe
    async def training_preview(datestr: str, trains: list[dict]) -> dict:
        """预览同一天 1–4 条训练的新建/更新。更新需 localid；按原动作顺序传修改项，服务端保留旧字段，禁止删除原组/重排动作/修改时间。每条最多15动作，每动作20组。返回完整内容供用户批准。"""
        return await service.training_preview(datestr, trains, owner())

    @mcp.tool(annotations=PREVIEW)
    @safe
    async def body_preview(records: list[dict]) -> dict:
        """预览身体记录并向训记 dry_run 校验。每条仅含 datestr/type/value；体重kg、体脂率%、围度cm，腰围拼写 weist。不会真正写入。"""
        return await service.body_preview(records, owner())

    @mcp.tool(annotations=PREVIEW)
    @safe
    async def food_preview(foods: list[dict]) -> dict:
        """预览饮食写入。条目含 date/meal_type/name/amount/unit/uniquekey/ntr。ntr 是已确认的每100g cal/protein/fat/carb；须先搜索食物，不能猜营养或份量。有记录id为更新。"""
        return service.food_preview(foods, owner())

    @mcp.tool(annotations=PREVIEW)
    @safe
    async def custom_food_preview(food: dict, nutrition_source: str) -> dict:
        """预览自定义食物。food 含 name/ntr/units。必须向用户说明每100g营养来源并确认；units 与 ntr.foodUnit 一致。优先搜索已有食物。"""
        return service.custom_food_preview(food, nutrition_source, owner())

    @mcp.tool(annotations=PREVIEW)
    @safe
    async def templates_preview(
        upserts: list[dict], deletes: list[dict], folder_update: dict | None = None
    ) -> dict:
        """预览 Agent 专属模板变更。新建用稳定 client_id；更新/删除用 template_id/base_version。upserts 使用完整公开 movements，每个模板最多15动作、每动作20组；总容量14。folder_update 含 name/base_version。先向用户说明删除项及动作/组/递增规则变化。"""
        return await service.templates_preview(upserts, deletes, folder_update, owner())

    @mcp.tool(annotations=WRITE)
    @safe
    async def commit_write(confirmation_id: str) -> dict:
        """真正写入训记。必须先展示预览并得到用户明确确认；客户端必须每次询问。仅执行此ID绑定的内容，一次性有效15分钟。超时不得自动重试，先读取核对。"""
        return await service.commit(confirmation_id, owner())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
    @safe
    async def cancel_write(confirmation_id: str) -> dict:
        """取消尚未提交的预览，不修改训记数据。"""
        return service.cancel(confirmation_id, owner())

    return mcp


def main():
    # Third-party OAuth/access logs can include callback queries or upstream response text.
    logging.basicConfig(level=logging.CRITICAL)
    logging.getLogger("xunji").setLevel(logging.INFO)
    for name in ("httpx", "httpx2", "httpcore", "fastmcp", "mcp", "uvicorn.access"):
        logging.getLogger(name).disabled = True
        logging.getLogger(name).setLevel(logging.CRITICAL)
    mcp = create_server()
    host = urlparse(required("BASE_URL")).hostname
    app = mcp.http_app(
        stateless_http=True,
        allowed_hosts=[host, "127.0.0.1", "localhost"],
        allowed_origins=[required("BASE_URL")],
    )
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False, log_level="critical", proxy_headers=False)


if __name__ == "__main__":
    main()
