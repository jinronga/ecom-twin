"""给 agent 用的 MCP server（stdio，JSON-RPC 2.0，按行分隔）。

agent 能用的工具只有这里列出的：只读查询、确定性计算、提交提案、上报步骤、记忆、向负责人提问。
平台写接口不在其中——agent 唯一的写出口是 submit_proposal（DESIGN.md 3、5.5）。

运行：python -m ecom_twin.mcp_server --task T-0001 [--config config/settings.toml]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, TextIO

from .adapters.base import AdapterError
from .memory import MemoryStore
from .models import StepStatus, TaskStatus
from .numbers import price_sku
from .proposals import ProposalRejected

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "ecom"


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    fn: Callable[[dict[str, Any]], Any]


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


class ToolServer:
    def __init__(self, app: Any, memory: MemoryStore, miaoshou: Any, task_id: str):
        self.app = app
        self.memory = memory
        self.miaoshou = miaoshou
        self.task_id = task_id
        self.feishu = app.feishu
        s, i, arr = {"type": "string"}, {"type": "integer"}, {"type": "array"}
        self.tools = {t.name: t for t in [
            Tool("miaoshou_list_shops", "只读：查询妙手已授权店铺。platform 如 tiktok，site 留空表示全部站点",
                 _obj({"platform": s, "site": s}, ["platform"]), self._list_shops),
            Tool("miaoshou_search_collect_box", "只读：查询妙手 TikTok 采集箱商品。status：notPublished / published / timingPublish",
                 _obj({"status": s, "page": i, "size": i}), self._search_collect_box),
            Tool("miaoshou_get_collect_item", "只读：查询妙手 TikTok 采集箱某商品在某店铺的详情（标题、类目、图片等）",
                 _obj({"detail_id": i, "shop_id": i}, ["detail_id", "shop_id"]), self._get_collect_item),
            Tool("miaoshou_tiktok_category_tree", "只读：查询 TikTok 某站点的类目树（如 US、MY），用于匹配类目",
                 _obj({"site": s}, ["site"]), lambda a: self.miaoshou.tiktok_category_tree(a["site"])),
            Tool("miaoshou_tiktok_category_metadata", "只读：查询 TikTok 类目的属性要求（必填属性、尺码表、认证等）",
                 _obj({"site": s, "cid": i, "shop_ids": {"type": "array", "items": i}}, ["site", "cid", "shop_ids"]),
                 lambda a: self.miaoshou.tiktok_category_metadata(a["site"], int(a["cid"]), [int(x) for x in a["shop_ids"]])),
            Tool("price_skus", "确定性计算：按定价参数计算每个 SKU 的售价、成本、毛利、毛利率。需要数字时用它算，不要自己算",
                 _obj({"params": {"type": "object"}, "skus": arr}, ["params", "skus"]), self._price_skus),
            Tool("submit_proposal",
                 "提交写操作提案（唯一的写出口）。提交后推送审批卡片，审批通过才执行。"
                 "action 目前支持 miaoshou.tiktok.publish；rationale 里的数字必须来自工具返回的数据，否则会被拒收",
                 _obj({"shop_key": s, "action": s, "target": {"type": "object"}, "desired": {"type": "object"},
                       "rationale": s}, ["shop_key", "action", "target", "rationale"]), self._submit_proposal),
            Tool("report_step", "上报一个关键步骤的进展，会显示在任务看板上。status：进行中 / 成功 / 失败 / 跳过",
                 _obj({"name": s, "status": s, "summary": s}, ["name", "status"]), self._report_step),
            Tool("load_sop", "加载场景 SOP（标准操作程序）的完整内容作为操作指南。先检查任务初始上下文中的 SOP 清单，再按需调用",
                 _obj({"name": s}, ["name"]), self._load_sop),
            Tool("get_shop_context", "查询店铺背景信息：白名单状态、审批人配置、硬上限数值、ERP信息等",
                 _obj({"shop_key": s}, ["shop_key"]), self._get_shop_context),
            Tool("recall_memory", "按关键词召回人工精炼的记忆（经验、判断、大促日历等）。可传多个同义关键词",
                 _obj({"keywords": {"type": "array", "items": s}}, ["keywords"]), self._recall_memory),
            Tool("propose_memory", "发现记忆缺失或过时时，提交记忆草稿，由人工审核后才入库",
                 _obj({"name": s, "description": s, "body": s, "reason": s, "type": s},
                      ["name", "description", "body", "reason"]), self._propose_memory),
            Tool("ask_owner", "缺少只有负责人知道的信息时，向负责人提问。提问后任务进入「等待回复」，负责人回复后任务会被唤醒",
                 _obj({"question": s}, ["question"]), self._ask_owner),
            Tool("feishu_get_message", "查询飞书单条消息的完整内容，用于补全上下文",
                 _obj({"message_id": s}, ["message_id"]), self._feishu_get_message),
            Tool("feishu_chat_messages", "查询飞书会话的历史消息（按时间范围）",
                 _obj({"chat_id": s, "start_ts": i, "end_ts": i, "limit": i}, ["chat_id", "start_ts", "end_ts"]),
                 self._feishu_chat_messages),
            Tool("feishu_thread_messages", "查询飞书 thread 的所有消息（含根消息）",
                 _obj({"thread_id": s, "limit": i}, ["thread_id"]), self._feishu_thread_messages),
            Tool("feishu_search_messages", "按关键词搜索飞书消息（可跨会话）",
                 _obj({"keyword": s, "start_ts": i, "end_ts": i, "limit": i}, ["keyword", "start_ts", "end_ts"]),
                 self._feishu_search_messages),
        ]}

    # ---- 工具实现 ----
    def _list_shops(self, a):
        return self.miaoshou.list_shops(a["platform"], a.get("site", ""))

    def _search_collect_box(self, a):
        return self.miaoshou.tiktok_search_collect_box(a.get("status", "notPublished"), a.get("page", 1),
                                                       a.get("size", 20))

    def _get_collect_item(self, a):
        return self.miaoshou.tiktok_shop_item_info(int(a["detail_id"]), int(a["shop_id"]))

    def _price_skus(self, a):
        return [price_sku(sku, a["params"]) for sku in a["skus"]]

    def _submit_proposal(self, a):
        p = self.app.proposals.submit(self.task_id, a["shop_key"], a["action"], a["target"],
                                      a.get("desired") or {}, a["rationale"])
        self.app.approvals.request(p)
        return {"proposal_id": p.id, "status": p.status.value, "evidence": p.evidence,
                "consequences": p.consequences, "说明": "已推送审批卡片，等待负责人审批"}

    def _report_step(self, a):
        status = StepStatus(a["status"])
        step = self.app.tasks.start_step(self.task_id, a["name"])
        if status != StepStatus.RUNNING:
            self.app.tasks.finish_step(step, status, summary=a.get("summary", ""))
        return {"ok": True}

    def _load_sop(self, a):
        """加载 SOP 完整内容"""
        sop_content = self.app.sops.get_sop(a["name"])
        if sop_content is None:
            return f"SOP 不存在：{a['name']}"
        return sop_content

    def _get_shop_context(self, a):
        """查询店铺背景信息"""
        shop_key = a["shop_key"]
        settings = self.app.settings

        # 1. 检查白名单
        in_whitelist = shop_key in settings.limits.allowed_shops

        # 2. 审批人配置
        approver = settings.approver_for(shop_key)
        approver_info = {
            "primary": approver.primary if approver else None,
            "delegate": approver.delegate if approver else None,
        }

        # 3. 硬上限
        limits_info = {
            "max_daily_writes": settings.limits.max_daily_writes_per_shop,
            "max_publish_items_per_proposal": settings.limits.max_publish_items_per_proposal,
            "min_margin_rate": settings.limits.min_margin_rate,
            "max_price_change_pct": settings.limits.max_price_change_pct,
        }

        # 4. 尝试从妙手 ERP 查询店铺信息（如果可用）
        erp_info = None
        if self.miaoshou:
            try:
                platform, shop_id = shop_key.split(":", 1)
                shops = self.miaoshou.list_shops(platform, "")
                for shop in shops:
                    if str(shop.get("shop_id")) == shop_id:
                        erp_info = {
                            "shop_name": shop.get("shop_name"),
                            "site": shop.get("site"),
                            "status": shop.get("status"),
                        }
                        break
            except Exception:
                pass  # ERP 查询失败不影响其他信息返回

        result = {
            "shop_key": shop_key,
            "in_whitelist": in_whitelist,
            "approver": approver_info,
            "limits": limits_info,
            "erp": erp_info,
        }

        # 优先警告白名单问题（更严重）
        if not in_whitelist:
            result["warning"] = f"店铺 {shop_key} 不在白名单内，所有写操作提案将被执行器拒绝"
        elif not approver:
            result["warning"] = f"店铺 {shop_key} 未配置审批人，审批卡片将发给系统 owner"

        return result

    def _recall_memory(self, a):
        hits = self.memory.recall(a["keywords"], task_id=self.task_id)
        return [{"name": m.name, "description": m.description, "body": m.body} for m in hits] or "没有命中的记忆"

    def _propose_memory(self, a):
        draft_id = self.memory.propose(a["name"], a["description"], a["body"], a["reason"],
                                       a.get("type") or "experience", task_id=self.task_id)
        return {"draft_id": draft_id, "说明": "已进入人工审核队列"}

    def _ask_owner(self, a):
        task = self.app.store.get_task(self.task_id)
        text = f"[{self.task_id}] 分身提问：{a['question']}\n（在本消息下回复即可，回复会并回任务）"
        if task.origin_message_id:
            msg_id = self.app.feishu.reply_text(task.origin_message_id, text)
        else:
            msg_id = self.app.feishu.send_text(task.owner or self.app.settings.feishu.owner_open_id, text)
        self.app.store.log_message({"message_id": msg_id, "chat_type": "p2p", "content": text},
                                   "outbound", self.task_id)
        self.app.store.link_report(msg_id, self.task_id)
        self.app.tasks.set_status(self.task_id, TaskStatus.WAITING, blocker=a["question"])
        return {"ok": True, "说明": "已提问，任务进入等待回复；可以先结束本轮，回复到达后会被唤醒"}

    def _feishu_get_message(self, a):
        msg = self.feishu.get_message(a["message_id"])
        return msg if msg else "消息未找到"

    def _feishu_chat_messages(self, a):
        return self.feishu.chat_messages(a["chat_id"], a["start_ts"], a["end_ts"], a.get("limit", 50))

    def _feishu_thread_messages(self, a):
        return self.feishu.thread_messages(a["thread_id"], a.get("limit", 50))

    def _feishu_search_messages(self, a):
        return self.feishu.search_messages(a["keyword"], a["start_ts"], a["end_ts"], a.get("limit", 20))

    # ---- MCP 协议 ----
    def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "inputSchema": t.schema} for t in self.tools.values()]

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        tool = self.tools.get(name)
        if tool is None:
            return f"未知工具：{name}", True
        try:
            result = tool.fn(arguments or {})
        except (AdapterError, ProposalRejected, KeyError, ValueError) as e:
            return f"{type(e).__name__}: {e}", True
        self.app.tasks.heartbeat(self.task_id)
        return (result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)), False

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # 通知（如 notifications/initialized）不回复
            return None
        if method == "initialize":
            result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.list_tools()}
        elif method == "tools/call":
            params = msg.get("params") or {}
            text, is_error = self.call(params.get("name", ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"未知方法：{method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(server: ToolServer, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = server.handle(msg)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


class Unavailable:
    """适配器不可用时的占位：任何调用都抛出原始错误。"""

    def __init__(self, error: AdapterError):
        self.error = error

    def __getattr__(self, name: str):
        def fail(*args, **kwargs):
            raise self.error
        return fail


def tool_names() -> list[str]:
    """供运行时设置允许的工具清单。"""
    return ["miaoshou_list_shops", "miaoshou_search_collect_box", "miaoshou_get_collect_item",
            "miaoshou_tiktok_category_tree", "miaoshou_tiktok_category_metadata", "price_skus",
            "submit_proposal", "report_step", "load_sop", "get_shop_context", "recall_memory",
            "propose_memory", "ask_owner",
            "feishu_get_message", "feishu_chat_messages", "feishu_thread_messages", "feishu_search_messages"]


def main(argv: list[str] | None = None) -> int:
    from .adapters.miaoshou import MiaoshouClient
    from .app import build_app
    from .config import load_dotenv, load_settings
    from .feishu import make_feishu
    from .store import Store

    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    load_dotenv()
    settings = load_settings(args.config)
    store = Store(settings.db_path)
    try:
        miaoshou = MiaoshouClient.from_env()
    except AdapterError as e:
        miaoshou = Unavailable(e)  # 没有妙手凭据时，妙手相关工具返回错误，其他工具照常可用
    app = build_app(settings, store, make_feishu(), None if isinstance(miaoshou, Unavailable) else miaoshou)
    serve(ToolServer(app, MemoryStore(store), miaoshou, args.task))
    return 0


if __name__ == "__main__":
    sys.exit(main())
