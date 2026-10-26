"""执行后跟进（DESIGN.md 7.1）：回读确认、第 3 天和第 7 天复盘。

- 回读确认：写操作是异步受理的（例如妙手发布），执行后定时轮询确认结果，超过次数仍未确认就通知人工检查
- 复盘：目前还没接入效果数据（P2/P3），先按时提醒负责人复盘，负责人在提醒下回复的结论可沉淀为记忆草稿
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .adapters.base import AdapterError
from .feishu import FeishuClient
from .models import Proposal
from .store import Store, utcnow

CONFIRM_FIRST_DELAY = timedelta(minutes=2)
CONFIRM_INTERVAL = timedelta(minutes=5)
CONFIRM_MAX_ATTEMPTS = 12  # 约 1 小时
REVIEW_DAYS = (3, 7)


class FollowupService:
    def __init__(self, store: Store, feishu: FeishuClient, handlers: dict[str, Any], owner_open_id: str):
        self.store = store
        self.feishu = feishu
        self.handlers = handlers
        self.owner_open_id = owner_open_id

    def schedule(self, p: Proposal, now: datetime | None = None) -> None:
        now = now or utcnow()
        handler = self.handlers.get(p.action)
        if "confirm_publish" in getattr(handler, "followup_kinds", ()):
            self.store.add_followup(p.id, "confirm_publish", now + CONFIRM_FIRST_DELAY)
        for days in REVIEW_DAYS:
            self.store.add_followup(p.id, f"review_{days}d", now + timedelta(days=days))

    def _notify(self, p: Proposal, text: str) -> None:
        msg_id = self.feishu.send_text(p.approver or self.owner_open_id, text)
        self.store.log_message({"message_id": msg_id, "chat_type": "p2p", "content": text},
                               "outbound", p.task_id)

    def run_due(self, now: datetime | None = None) -> int:
        now = now or utcnow()
        handled = 0
        for f in self.store.due_followups(now):
            p = self.store.get_proposal(f["proposal_id"])
            if p is None:
                self.store.update_followup(f["id"], done=True, note="提案不存在")
                continue
            if f["kind"] == "confirm_publish":
                self._confirm(p, f, now)
            elif f["kind"].startswith("review_"):
                days = f["kind"].removeprefix("review_").removesuffix("d")
                msg_id = self.feishu.send_text(
                    p.approver or self.owner_open_id,
                    f"复盘提醒：{p.id}（{p.action}，{p.shop_key}）执行已满 {days} 天。"
                    f"效果数据还未接入，请到平台查看表现。在本消息下回复结论，会转成记忆草稿供审核。",
                )
                self.store.log_message({"message_id": msg_id, "chat_type": "p2p", "content":
                                        f"复盘提醒：{p.id}"}, "outbound", p.task_id)
                self.store.link_report(msg_id, p.task_id)
                self.store.update_followup(f["id"], done=True, note="已提醒")
            handled += 1
        return handled

    def _confirm(self, p: Proposal, f: dict, now: datetime) -> None:
        handler = self.handlers.get(p.action)
        try:
            ok, note = handler.confirm(p)
        except AdapterError as e:
            ok, note = False, f"回读失败：{e}"
        if ok:
            p.result = {**p.result, "回读确认": note}
            self.store.save_proposal(p)
            self.store.update_followup(f["id"], done=True, note=note)
            self.store.audit("proposal", p.id, "confirmed", "system", {"note": note})
            self._notify(p, f"{p.id} 回读确认：{note}")
        elif f["attempts"] + 1 >= CONFIRM_MAX_ATTEMPTS:
            p.result = {**p.result, "回读确认": f"超时未确认：{note}"}
            self.store.save_proposal(p)
            self.store.update_followup(f["id"], done=True, note=note)
            self.store.audit("proposal", p.id, "confirm_timeout", "system", {"note": note})
            self._notify(p, f"{p.id} 执行后约 1 小时仍未确认成功（{note}），请到平台人工检查。")
        else:
            self.store.update_followup(f["id"], due_at=now + CONFIRM_INTERVAL, note=note)
