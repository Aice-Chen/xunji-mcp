"""Business operations and single-use previews, all personal data stays in memory."""

import copy
import hashlib
import json
import time
import uuid

from cachetools import TTLCache

from .api import API, UserError
from .validation import (
    BODY_TYPES,
    check,
    date_range,
    day,
    finite,
    merge_preserving,
    movements,
    note_object,
    nutrition,
    template_name,
)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


class Service:
    def __init__(self, api: API, names: set[str], *, writes_enabled=False, clock=time.monotonic):
        self.api, self.names, self.writes_enabled = api, names, writes_enabled
        self.pending = TTLCache(maxsize=32, ttl=900, timer=clock)

    def preview(self, endpoint, payload, owner, *, guards=None, server_preview=None):
        check(len(self.pending) < 32, "待确认请求已达上限，请取消旧请求或等其过期。")
        check(len(json.dumps(payload, ensure_ascii=False)) < 200_000, "写入内容过大，请拆分。")
        confirmation_id = str(uuid.uuid4())
        self.pending[confirmation_id] = copy.deepcopy(
            {"endpoint": endpoint, "payload": payload, "owner": owner, "guards": guards or []}
        )
        return {
            "confirmation_id": confirmation_id,
            "expires_in_seconds": 900,
            "operation": endpoint,
            "changes": copy.deepcopy(payload),
            "server_preview": server_preview,
            "instruction": "向用户展示 changes 的日期、名称、数值、单位及删除项；获得明确同意后才调用 commit_write。",
        }

    async def training_get(self, datestr, full=False, fresh=False):
        day(datestr)
        return await self.api.call(
            "training_get",
            {"schema_version": "train_open_api_v2", "datestr": datestr, "include_full_data": full},
            fresh=fresh,
        )

    async def training_preview(self, datestr, trains, owner):
        day(datestr)
        check(isinstance(trains, list) and 1 <= len(trains) <= 4, "单次仅支持同一天的 1–4 条训练。")
        current = await self.training_get(datestr, True, fresh=True)
        originals = current["res"]["trains"]
        check(isinstance(originals, list), "训练响应格式不符合文档。")
        by_id = {str(t["localid"]): t for t in originals if "localid" in t}
        result, seen = [], set()
        for patch in copy.deepcopy(trains):
            check(isinstance(patch, dict), "训练须为对象。")
            check(patch.get("datestr", datestr) == datestr, "训练必须属于同一天。")
            patch["datestr"] = datestr
            if "localid" in patch:
                localid = str(patch["localid"])
                check(localid not in seen, "不能重复更新同一条训练。")
                seen.add(localid)
                check(localid in by_id, "找不到要更新的训练，请重新读取。")
                original = copy.deepcopy(by_id[localid])
                for field in ("start", "end"):
                    check(
                        field not in patch or patch[field] == original.get(field),
                        "本版不支持修改已有训练时间。",
                    )
                if "note" in original:
                    original["note"] = note_object(original["note"])
                if "note" in patch:
                    patch["note"] = note_object(patch["note"])
                patch = merge_preserving(original, patch)
            movements(patch.get("movements", []), self.names)
            result.append(patch)
        payload = {
            "schema_version": "train_open_api_v2",
            "client_request_id": str(uuid.uuid4()),
            "include_full_data": True,
            "res": result,
        }
        # Training dry_run semantics are not fully specified; use local validation and a fresh baseline.
        guard = {
            "endpoint": "training_get",
            "query": {"schema_version": "train_open_api_v2", "datestr": datestr, "include_full_data": True},
            "fingerprint": fingerprint(current["res"]),
        }
        return self.preview("training_write", payload, owner, guards=[guard])

    async def body_preview(self, records, owner):
        check(isinstance(records, list) and 1 <= len(records) <= 100, "一次提供 1–100 条身体记录。")
        seen = set()
        for row in records:
            day(row.get("datestr"))
            check(row.get("type") in BODY_TYPES, "身体指标类型无效；腰围字段使用 weist。")
            check(finite(row.get("value")) > 0, "身体指标须大于 0。")
            if row["type"] == "bodyfat":
                check(float(row["value"]) <= 100, "体脂率不能超过 100%。")
            key = (row["datestr"], row["type"])
            check(key not in seen, "同次请求不能重复同一天的同一身体指标。")
            seen.add(key)
            check(set(row) <= {"datestr", "type", "value"}, "身体记录仅接受 datestr/type/value。")
        payload = {
            "schema_version": "body_open_api_v1",
            "client_request_id": str(uuid.uuid4()),
            "records": records,
        }
        validation = await self.api.call("body_write", {**payload, "dry_run": True})
        return self.preview("body_write", payload, owner, server_preview=validation["res"])

    def food_preview(self, foods, owner):
        check(isinstance(foods, list) and 1 <= len(foods) <= 100, "一次提供 1–100 条饮食记录。")
        for row in foods:
            date_range(row.get("date"), row.get("date"), food=True)
            check(isinstance(row.get("name"), str) and row["name"].strip(), "食物名称不能为空。")
            check(
                row.get("meal_type") in {"breakfast", "lunch", "dinner", "snack"},
                "餐次须为 breakfast/lunch/dinner/snack。",
            )
            check(finite(row.get("amount")) > 0 and bool(row.get("unit")), "数量和单位必须明确。")
            check(bool(row.get("uniquekey")), "请先搜索食物或创建自定义食物，使用返回的 uniquekey。")
            nutrition(row)
        return self.preview("food_write", {"client_request_id": str(uuid.uuid4()), "foods": foods}, owner)

    def custom_food_preview(self, food, nutrition_source, owner):
        check(isinstance(food.get("name"), str) and food["name"].strip(), "食物名称不能为空。")
        check(bool(nutrition_source.strip()), "请注明每 100g 营养数据的来源，供用户确认。")
        nutrition(food)
        check(food.get("units", []) == food["ntr"].get("foodUnit", []), "两处单位换算必须一致。")
        result = self.preview("food_custom", {"client_request_id": str(uuid.uuid4()), "food": food}, owner)
        result["nutrition_source"] = nutrition_source
        return result

    async def templates_preview(self, upserts, deletes, folder_update, owner):
        check(
            len(upserts) <= 14 and len(deletes) <= 14 and (upserts or deletes or folder_update),
            "每次最多 14 个 upsert 和 14 个 delete，且不能是空操作。",
        )
        state = await self.api.call("templates_sync", {}, fresh=True)
        res = state["res"]
        check(
            not res.get("folder_deleted") and isinstance(res.get("folder"), dict),
            "Agent 文件夹不存在或已删除，请在 App 中处理；不会自动重建。",
        )
        versions = res.get("template_versions")
        check(isinstance(versions, dict), "模版响应缺少完整版本表，拒绝写入。")
        versions = {str(k): v for k, v in versions.items()}
        deleted, updated, clients = set(), set(), set()
        for item in deletes:
            template_id = str(item.get("template_id", ""))
            check(
                template_id in versions and item.get("base_version") == versions[template_id],
                "删除模版的版本已变化，请同步后重新确认。",
            )
            check(template_id not in deleted, "删除项重复。")
            deleted.add(template_id)
        for item in upserts:
            check(
                set(item) <= {"template_id", "base_version", "client_id", "name", "color", "movements"},
                "首版只支持公开 movements 结构；不接受原生 movement/rules。",
            )
            template_name(item.get("name"))
            movements(item.get("movements"), self.names)
            if "template_id" in item:
                template_id = str(item["template_id"])
                check(
                    template_id in versions and item.get("base_version") == versions[template_id],
                    "更新模版的版本已变化，请同步后重新确认。",
                )
                check(
                    template_id not in updated and template_id not in deleted,
                    "同一模版不能重复更新或同时删除。",
                )
                updated.add(template_id)
            else:
                check(
                    isinstance(item.get("client_id"), str)
                    and item["client_id"]
                    and item["client_id"] not in clients,
                    "新模版需要唯一、稳定的 client_id。",
                )
                clients.add(item["client_id"])
        check(len(versions) - len(deleted) + len(clients) <= 14, "Agent 文件夹最多 14 个有效模版。")
        if folder_update:
            check(
                set(folder_update) == {"name", "base_version"}, "文件夹只能修改 name，须提供 base_version。"
            )
            template_name(folder_update["name"])
            check(
                folder_update["base_version"]
                == res["folder"].get("base_version", res["folder"].get("version")),
                "文件夹版本已变化。",
            )
        payload = {"mutation_id": str(uuid.uuid4()), "upserts": upserts, "deletes": deletes}
        if folder_update:
            payload["folder_update"] = folder_update
        guard = {"endpoint": "templates_sync", "query": {}, "fingerprint": fingerprint(res)}
        return self.preview("templates_write", payload, owner, guards=[guard])

    async def commit(self, confirmation_id, owner):
        check(self.writes_enabled, "写入尚未启用；管理员须先验证客户端每次提交都会请求用户批准。")
        item = self.pending.get(confirmation_id)
        check(item is not None and item["owner"] == owner, "预览不存在、已过期、已提交或不属于此登录用户。")
        # Remove before the first await. Concurrent calls cannot submit the same preview twice.
        item = self.pending.pop(confirmation_id)
        for guard in item["guards"]:
            current = await self.api.call(guard["endpoint"], guard["query"], fresh=True)
            check(
                fingerprint(current["res"]) == guard["fingerprint"],
                "原数据已变化；本次预览已失效，请重新预览并确认。",
            )
        payload = copy.deepcopy(item["payload"])
        if item["endpoint"] == "templates_write":
            payload["confirmed"] = True
        else:
            payload["dry_run"] = False
            if item["endpoint"] == "body_write":
                payload["confirmed"] = True
        try:
            return await self.api.call(item["endpoint"], payload)
        except UserError as exc:
            raise UserError(f"{exc} 此预览已消耗；先核对记录，再决定是否创建新预览。") from None

    def cancel(self, confirmation_id, owner):
        item = self.pending.get(confirmation_id)
        check(item is not None and item["owner"] == owner, "预览不存在或不属于此用户。")
        self.pending.pop(confirmation_id)
        return {"cancelled": True}
