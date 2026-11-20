"""Conservative validation: unsupported destructive edits fail closed."""

import calendar
import copy
import json
import math
import re
from datetime import date

from .api import UserError

BODY_TYPES = {
    "weight",
    "bodyfat",
    "neck",
    "chest",
    "weist",
    "shoulder",
    "bot",
    "arm_left",
    "arm_right",
    "forearm_left",
    "forearm_right",
    "leg_left",
    "leg_right",
    "cav_left",
    "cav_right",
}


def check(condition, message):
    if not condition:
        raise UserError(message)


def day(value):
    try:
        parsed = date.fromisoformat(value)
    except ValueError, TypeError:
        raise UserError("日期必须是 YYYY-MM-DD。") from None
    check(parsed.isoformat() == value, "日期必须是 YYYY-MM-DD。")
    return parsed


def date_range(start, end, *, max_days=None, food=False, today=None):
    a, b = day(start), day(end)
    check(a <= b, "开始日期不能晚于结束日期。")
    if max_days:
        check((b - a).days + 1 <= max_days, f"单次最多 {max_days} 天。")
    if food:
        now = today or date.today()
        lower = date(now.year - 1, now.month, min(now.day, calendar.monthrange(now.year - 1, now.month)[1]))
        year, month = divmod(now.year * 12 + now.month - 1 + 3, 12)
        upper = date(year, month + 1, min(now.day, calendar.monthrange(year, month + 1)[1]))
        check(lower <= a <= b <= upper, "饮食日期只能在过去一年至未来三个月内。")


def finite(value):
    try:
        number = float(value)
    except TypeError, ValueError, OverflowError:
        raise UserError("数值格式无效。") from None
    check(not isinstance(value, bool) and math.isfinite(number) and number >= 0, "数值必须为有限非负数。")
    return number


def movements(items, names):
    check(isinstance(items, list) and len(items) <= 15, "每条训练或模板最多 15 个动作。")
    count = 0
    for move in items:
        check(isinstance(move, dict) and "key" not in move and "moves" not in move, "动作只使用中文 name。")
        if move.get("cardio") is True:
            check("sets" not in move, "原生有氧动作不能带 sets。")
            count += 1
            continue
        sets = move.get("sets", [])
        check(isinstance(sets, list) and 1 <= len(sets) <= 20, "每个动作需有 1–20 组或轮。")
        is_super = any("items" in s for s in sets)
        if not is_super:
            check(move.get("name") in names, "动作名不在训记标准中文名表中，请先搜索。")
        count += max((len(s.get("items", [])) for s in sets), default=0) if is_super else 1
        for field in ("restTime", "warn_restTime"):
            if field in move:
                check(0 <= finite(move[field]) <= 86400, "休息时间须在 0–86400 秒内。")
        for group in sets:
            if "items" in group:
                check(isinstance(group["items"], list) and group["items"], "超级组子项不能为空。")
                for child in group["items"]:
                    check(child.get("name") in names and "key" not in child, "超级组子动作名无效。")
                    validate_set(child.get("set", {}))
            else:
                validate_set(group)
        if "difficulty" in move:
            check(move["difficulty"] in {"easy", "normal", "hard"}, "动作难度无效。")
    check(count <= 15, "超级组按子动作数计算，合计不能超过 15 个动作。")


def validate_set(group):
    check(isinstance(group, dict) and "items" not in group, "不支持嵌套超级组。")
    check(
        any(k in group for k in ("weight", "weight_kg", "reps", "time", "duration_s", "selfWeight")),
        "组缺少重量、次数、时间或自重字段。",
    )
    if "rpe" in group:
        check(group["rpe"] in {"", "6", "6.5", "7", "7.5", "8", "8.5", "9", "9.5", "10"}, "RPE 值无效。")


def merge_preserving(old, patch):
    """Merge a positional patch without silently dropping old groups or metadata."""
    if isinstance(old, dict) and isinstance(patch, dict):
        result = copy.deepcopy(old)
        for key, value in patch.items():
            result[key] = merge_preserving(old[key], value) if key in old else copy.deepcopy(value)
        return result
    if isinstance(old, list) and isinstance(patch, list):
        check(len(patch) >= len(old), "本版禁止通过缩短数组删除原有动作、组或轮次。")
        result = []
        for i, value in enumerate(patch):
            if i < len(old):
                if isinstance(old[i], dict) and isinstance(value, dict) and "name" in old[i]:
                    check(
                        value.get("name", old[i]["name"]) == old[i]["name"], "本版不支持重排或替换已有动作。"
                    )
                result.append(merge_preserving(old[i], value))
            else:
                result.append(copy.deepcopy(value))
        return result
    if isinstance(old, (dict, list)):
        check(isinstance(patch, type(old)), "不能用空值覆盖原有结构。")
    return copy.deepcopy(patch)


def note_object(note):
    if isinstance(note, str):
        try:
            parsed = json.loads(note)
        except ValueError:
            parsed = {"text": note}
        return parsed if isinstance(parsed, dict) else {"text": note}
    check(isinstance(note, dict), "note 必须为对象或字符串。")
    return note


def nutrition(food):
    ntr = food.get("ntr", {})
    check(isinstance(ntr, dict), "ntr 必须为对象。")
    for key in ("cal", "protein", "fat", "carb"):
        check(key in ntr, "必须提供已确认的每 100g 营养数据。")
        finite(ntr[key])
    if "units" in food and "foodUnit" in ntr:
        check(food["units"] == ntr["foodUnit"], "units 与 ntr.foodUnit 必须一致。")


def template_name(name):
    check(
        isinstance(name, str)
        and 1 <= len(name.strip()) <= 32
        and re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9 ]+", name),
        "模板/文件夹名须为 1–32 字的中文、字母、数字或普通空格。",
    )
