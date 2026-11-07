"""控制台规则配置（DESIGN.md 12.2、12.3）。

控制台改的配置写进 config/settings.console.toml，加载时叠加在 settings.toml 之上：
人手写的 settings.toml（含注释）永远不会被程序改写。
- 每个可改项都在 FIELDS 里登记类型和风险；高风险项要二次确认
- 保存前用合并后的完整配置跑 validate_settings，有「错误」级问题就不保存
- 每次保存写审计（改前、改后、操作人）
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import CONFIG_DIR

OVERRIDES_FILE = CONFIG_DIR / "settings.console.toml"


@dataclass(frozen=True)
class Field:
    section: str
    key: str
    label: str
    type: str          # bool / number / text / list / json
    high_risk: bool = False


FIELDS = [
    Field("limits", "allowed_shops", "可操作店铺白名单（平台:店铺ID）", "list", True),
    Field("limits", "max_daily_writes_per_shop", "单店每日写操作上限", "number", True),
    Field("limits", "max_publish_items_per_proposal", "单个上品提案最多商品数", "number", True),
    Field("limits", "min_margin_rate", "上品最低毛利率（0~1）", "number", True),
    Field("limits", "max_bid_change_pct", "广告调价单次幅度上限（%）", "number", True),
    Field("limits", "max_price_change_pct", "改价单次幅度上限（%）", "number", True),
    Field("approval", "expire_hours", "提案有效期（小时）", "number"),
    Field("approval", "remind_before_hours", "过期前提醒（小时）", "number"),
    Field("approval", "batch_window_seconds", "同类提案合并成一张卡的窗口（秒，0 不合并）", "number"),
    Field("approval", "double_approval_risks", "需要两人批准的风险等级", "list", True),
    Field("approvers", "", "店铺审批人（JSON：{\"平台:店铺ID\": {\"primary\": \"ou_\", \"delegate\": \"ou_\"}}）", "json", True),
    Field("send_permissions", "", "群代发权限（JSON：{\"oc_群\": {\"reply\": true, \"ask\": false, \"notify\": false}}）", "json", True),
    Field("feishu", "readable_chats", "agent 可额外读取的会话", "list"),
    Field("feishu", "allow_user_search", "允许用负责人本人身份搜索聊天记录", "bool", True),
    Field("intake", "listen_chats", "监听的群", "list"),
    Field("intake", "bot_name", "群里点名用的名字", "text"),
    Field("intake", "quiet_seconds", "群消息静默攒批（秒）", "number"),
    Field("intake", "ask_window_hours", "问后窗口（小时）", "number"),
    Field("intake", "topic_window_hours", "主题归并候选窗口（小时）", "number"),
    Field("intake", "same_class_whitelist", "同类合并白名单", "list"),
    Field("intake", "same_class_window_days", "同类合并窗口（天）", "number"),
    Field("intake", "closed_statuses", "视为已告一段落的任务状态", "list"),
    Field("intake", "anchor_patterns", "锚点正则", "list"),
    Field("intake", "judge_enabled", "启用廉价模型合并判定", "bool"),
    Field("browser", "interactive", "允许 agent 在专用 Chrome 里跳转、点击、填写（卖货后台仍拦截）", "bool", True),
    Field("computer_use", "enabled", "允许 agent 操作电脑上的任意软件（借用本机 Codex Computer Use）", "bool", True),
    Field("computer_use", "allowed_apps", "允许 agent 操作的软件（名称或 bundle ID）", "list", True),
    Field("computer_use", "applescript", "允许 agent 用 Bash 跑 AppleScript 等命令操作电脑（数字分身的做法）", "bool", True),
    Field("computer_use", "shell_commands", "AppleScript 方式放行的命令", "list", True),
    Field("computer_use", "blocked_keywords", "操作电脑时禁止出现的目标（卖家后台、密码管理器等）", "list", True),
    Field("runtime", "default", "默认底座", "text"),
    Field("runtime", "fallback", "备选底座", "text"),
    Field("shadow", "actions", "影子模式的操作类型（只记录不推送审批）", "list", True),
]


def field_for(section: str, key: str) -> Field | None:
    return next((f for f in FIELDS if f.section == section and f.key == key), None)


def load_overrides(path: Path = OVERRIDES_FILE) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


REPLACE = "__replace__"  # 覆盖文件里带这个标记的表整体替换（控制台用 JSON 编辑的整段配置）


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and v.get(REPLACE):
            out[k] = {kk: vv for kk, vv in v.items() if kk != REPLACE}
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---- 最小 TOML 写出（只需覆盖本文件用到的类型） ----
def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TypeError(f"不支持的配置值：{v!r}")


def _key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k, ensure_ascii=False)


def dump_toml(data: dict[str, Any], prefix: str = "") -> str:
    lines, tables = [], []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append(f"{_key(k)} = {_value(v)}")
    out = ""
    if lines:
        out += (f"[{prefix}]\n" if prefix else "") + "\n".join(lines) + "\n\n"
    for k, v in tables:
        out += dump_toml(v, f"{prefix}.{_key(k)}" if prefix else _key(k))
    return out


def coerce(f: Field, raw: Any) -> Any:
    """把控制台提交的值转成配置类型；不合法抛 ValueError。"""
    if f.type == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).lower() in ("true", "1", "yes", "是", "开")
    if f.type == "number":
        value = float(raw)
        return int(value) if value.is_integer() and "rate" not in f.key else value
    if f.type == "text":
        return str(raw).strip()
    if f.type == "list":
        items = raw if isinstance(raw, list) else [x.strip() for x in str(raw).replace("，", ",").split(",")]
        items = [str(x).strip() for x in items if str(x).strip()]
        if f.key == "anchor_patterns":
            for p in items:
                re.compile(p)
        return items
    if f.type == "json":
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict) or not all(isinstance(v, dict) for v in value.values()):
            raise ValueError("应为 {键: {字段: 值}} 形式的 JSON 对象")
        return value
    raise ValueError(f"未知类型 {f.type}")
