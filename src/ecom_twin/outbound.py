"""出站消息闸门（DESIGN.md 5.4、10.1、13.3 代发权限）。

分身发出的回执、汇报、提问都从这里走：
- 任务来自负责人私聊：直接回复（这是和负责人本人对话，不算代发）
- 任务来自群：要看这个群的代发权限（reply / ask），默认全关；没有权限就改为私聊负责人，并注明来源群
每一条都记进代发记录（outbound_messages），并写审计。

内容闸门（对照数字分身 slack-send 的三条硬规则：不 @所有人、不带凭证、单条不超过 500 字）：
- agent 主动发的（notify_chat）不合规就拒收，报错写清哪条不合规、怎么改，agent 改完重发
- 系统发的回执、汇报、提问 agent 没机会再改：密钥打码、@所有人去掉；发进群里超长的，群里发前一段，完整的私聊负责人
"""

from __future__ import annotations

import os
import re
from typing import Any

from .memory import SECRET_PATTERN

KIND_PERMISSION = {"receipt": "reply", "report": "reply", "ask": "ask", "notify": "notify"}
MAX_GROUP_CHARS = 500
AT_ALL = re.compile(r'<at\b[^>]*\b(user_)?id\s*=\s*["\']?all["\']?[^>]*>(.*?</at>)?'
                    r'|@(所有人|全体成员|all|everyone|here|channel)\b', re.I)
_SECRET_ENV = re.compile(r"(SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|APP_?KEY)$", re.I)
MASK = "<已隐藏>"


class OutboundRejected(ValueError):
    """消息不合规被拒收。文字就是给 agent 的改法。"""


def _secret_values() -> list[str]:
    """本进程环境里的密钥值（.env 已载入）：原样出现在正文里比任何正则都确定。"""
    return [v for k, v in os.environ.items() if _SECRET_ENV.search(k) and len(v) >= 8]


def problems(text: str, to_group: bool) -> list[str]:
    found = []
    if AT_ALL.search(text):
        found.append("用了 @所有人：不允许提醒全员，去掉；要提醒具体的人就写名字")
    if SECRET_PATTERN.search(text) or any(v in text for v in _secret_values()):
        found.append("正文里有疑似密钥或令牌：删掉，要说明就写「凭证已配置」这类话")
    if to_group and len(text) > MAX_GROUP_CHARS:
        found.append(f"超过 {MAX_GROUP_CHARS} 字（现在 {len(text)} 字）：先删掉推导过程，只留结论；"
                     "还装不下就按完整意思拆成几条分别发")
    return found


def check(text: str, to_group: bool) -> None:
    """agent 主动发的消息用：不合规直接拒收。"""
    found = problems(text, to_group)
    if found:
        raise OutboundRejected("消息没有发出，改完重发：" + "；".join(found))


def sanitize(text: str) -> tuple[str, list[str]]:
    """系统发的消息用：打码、去掉 @所有人，返回处理后的正文和做了哪些处理。"""
    fixed = []
    if AT_ALL.search(text):
        text, fixed = AT_ALL.sub("", text), fixed + ["去掉 @所有人"]
    masked = SECRET_PATTERN.sub(MASK, text)
    for value in _secret_values():
        masked = masked.replace(value, MASK)
    if masked != text:
        text, fixed = masked, fixed + ["隐藏疑似密钥"]
    return text, fixed


class Outbound:
    def __init__(self, app: Any):
        self.app = app

    def _allowed(self, chat_id: str, kind: str) -> bool:
        perm = self.app.settings.send_permissions.get(chat_id)
        return bool(perm and getattr(perm, KIND_PERMISSION[kind], False))

    def to_task(self, task_id: str, text: str, kind: str = "report", reply_to: str | None = None) -> str:
        """发给任务的来源会话。返回消息 ID（用于把后续回复并回任务）。"""
        task = self.app.store.get_task(task_id)
        owner = task.owner or self.app.settings.feishu.owner_open_id
        target_msg = reply_to or task.origin_message_id
        in_group = task.origin_chat_type == "group"
        text, fixed = sanitize(text)
        if fixed:
            self.app.store.audit("task", task_id, "outbound_sanitized", "system", {"kind": kind, "fixed": fixed})
        if target_msg and (not in_group or self._allowed(task.origin_chat_id, kind)):
            if in_group and len(text) > MAX_GROUP_CHARS:
                # 群里只发前一段，完整的私聊负责人：群里的人要的是结论，负责人要的是全部
                self.app.feishu.send_text(owner, f"[{task_id} 发到群里的内容超过 {MAX_GROUP_CHARS} 字，完整版如下]\n{text}")
                text = text[:MAX_GROUP_CHARS - 20].rstrip() + "…（完整内容已私聊负责人）"
            msg_id = self.app.feishu.reply_text(target_msg, text)
            delivered = "群 thread" if in_group else "私聊 thread"
        else:
            prefix = f"[来自群 {task.origin_chat_id}，该群未开放{kind}代发权限，改为私聊你]\n" if in_group else ""
            msg_id = self.app.feishu.send_text(owner, prefix + text)
            delivered = "私聊负责人"
        self.app.store.add_outbound(task_id, task.origin_chat_id, target_msg or "", kind, delivered, text, msg_id)
        self.app.store.log_message({"message_id": msg_id, "chat_type": task.origin_chat_type or "p2p",
                                    "content": text}, "outbound", task_id)
        if in_group:
            self.app.store.audit("task", task_id, f"outbound_{kind}", "system", {"delivered": delivered})
        return msg_id

    def image_to(self, task_id: str, message_id: str, path: str) -> str:
        msg_id = self.app.feishu.reply_image(message_id, path)
        self.app.store.add_outbound(task_id, "", message_id, "image", "thread", path, msg_id)
        return msg_id
