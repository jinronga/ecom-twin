"""出站消息闸门（DESIGN.md 5.4、10.1、13.3 代发权限）。

分身发出的回执、汇报、提问都从这里走：
- 任务来自负责人私聊：直接回复（这是和负责人本人对话，不算代发）
- 任务来自群：要看这个群的代发权限（reply / ask），默认全关；没有权限就改为私聊负责人，并注明来源群
每一条都记进代发记录（outbound_messages），并写审计。

内容闸门（对照数字分身 slack-send 的三条硬规则：不 @所有人、不带凭证、单条不超过 500 字）：
- agent 主动发的（notify_chat）不合规就拒收，报错写清哪条不合规、怎么改，agent 改完重发
- 系统发的回执、汇报、提问 agent 没机会再改：密钥打码、@所有人去掉；发进群里超长的，群里发前一段，完整的私聊负责人
- 群消息的用词和长短（对照数字分身 0.3.1 人设）：禁用黑话、分句太多 / 太长 / 前后几乎同字都拒收，报错写清怎么改。
  反引号里的代码和原文引用不查，贴报错、贴单号照样可以
"""

from __future__ import annotations

import os
import re
from typing import Any

from .feishu import FeishuError
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


def parse_banned_words(lines: list[str]) -> list[tuple[list[str], str]]:
    """["兜住 没兜住 => 说实际发生了什么", ...] → [(词表, 改法)]；# 开头的行是注释。"""
    rules = []
    for raw in lines or []:
        line = str(raw).strip()
        if not line or line.startswith("#"):
            continue
        words_part, _, hint = line.partition("=>")
        words = [w for w in re.split(r"[\s,，、|]+", words_part) if w]
        if words:
            rules.append((words, hint.strip() or "换成这件事本来的说法"))
    return rules


def _plain(text: str) -> str:
    """去掉代码块和行内反引号：引用的原文、报错、单号不算自己说的话。"""
    return re.sub(r"`[^`\n]*`", " ", re.sub(r"```.*?```", " ", text, flags=re.S))


def split_clauses(text: str) -> list[str]:
    """按逗号、句号、问号、换行拆分句；只剩一个字的（「嗯，」）不算一句。"""
    return [c.strip() for c in re.split(r"[，,。；;！!？?…\n]+", text) if len(re.sub(r"\s", "", c)) >= 2]


def _bigrams(text: str) -> set[str]:
    chars = re.sub(r"[\s\W_]+", "", text.lower())
    return {chars[i:i + 2] for i in range(len(chars) - 1)}


REPEAT_OVERLAP = 0.6  # 两句的二字片段重合到短的那句的六成，就当在说同一件事（数字分身实测的阈值）


def wording_problems(text: str, cfg: Any) -> list[str]:
    """禁用词和啰嗦检查，返回问题列表（空 = 过）。cfg 是 [outbound] 配置。"""
    if cfg is None:
        return []
    plain = _plain(text)
    found = []
    for words, hint in parse_banned_words(cfg.banned_words):
        hits = [w for w in words if w in plain]
        hit = max(hits, key=len) if hits else None  # 「没兜住」里也有「兜住」：报原文里实际写的那个更准
        if hit:
            found.append(f"用了「{hit}」：{hint}")
    if cfg.wordy_check:
        clauses = split_clauses(plain)
        if len(clauses) > cfg.max_clauses:
            found.append(f"这段话有 {len(clauses)} 个分句，最多 {cfg.max_clauses} 个：删掉推导过程，只留结论和要对方做的事")
        for clause in clauses:
            n = len(re.sub(r"\s", "", clause))
            if n > cfg.clause_max:
                found.append(f"「{clause[:30]}…」一句就 {n} 字，每个分句不超过 {cfg.clause_max} 字：拆开说")
        grams = [_bigrams(c) for c in clauses]
        for i in range(len(clauses)):
            for j in range(i + 1, len(clauses)):
                if len(grams[i]) >= 3 and len(grams[j]) >= 3 and \
                        len(grams[i] & grams[j]) / min(len(grams[i]), len(grams[j])) >= REPEAT_OVERLAP:
                    found.append(f"「{clauses[i]}」和「{clauses[j]}」说的几乎是同样的字：删掉一句")
    return found


def problems(text: str, to_group: bool, cfg: Any = None) -> list[str]:
    found = []
    if AT_ALL.search(text):
        found.append("用了 @所有人：不允许提醒全员，去掉；要提醒具体的人就写名字")
    if SECRET_PATTERN.search(text) or any(v in text for v in _secret_values()):
        found.append("正文里有疑似密钥或令牌：删掉，要说明就写「凭证已配置」这类话")
    if to_group and len(text) > MAX_GROUP_CHARS:
        found.append(f"超过 {MAX_GROUP_CHARS} 字（现在 {len(text)} 字）：先删掉推导过程，只留结论；"
                     "还装不下就按完整意思拆成几条分别发")
    if to_group:
        found += wording_problems(text, cfg)
    return found


def check(text: str, to_group: bool, cfg: Any = None) -> None:
    """agent 主动发的消息用：不合规直接拒收。cfg：[outbound] 配置，给了就加查禁用词和啰嗦。"""
    found = problems(text, to_group, cfg)
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
        if task.source.value == "控制台对话":
            # 「问分身」对话：提问、中途汇报都写回对话框，不发飞书（负责人在对话框里回答，回答会续跑这个任务）
            text, _ = sanitize(text)
            mid = f"chat:{task_id}:{self.app.store.add_chat_message(task_id, 'assistant', text)}"
            self.app.store.add_outbound(task_id, "", "", kind, "控制台对话", text, mid)
            return mid
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
            msg_id, threaded = self._reply(task_id, target_msg, text)
            delivered = ("群话题" if threaded else "群 thread") if in_group else ("私聊话题" if threaded else "私聊 thread")
        else:
            # 照数字分身：从别的会话来的活，在私聊里单开一条，之后同一个任务的汇报都跟在这一条下面
            anchor = self.app.store.kv_get(f"dm_anchor:{task_id}")
            msg_id = ""
            if anchor:
                try:
                    msg_id, threaded = self._reply(task_id, anchor, text)
                    delivered = "私聊负责人（跟在这个任务的那一条下面）"
                except FeishuError as e:  # 那一条被撤回、删掉或回复不了了：改发新的一条，并以它为新的锚点
                    self.app.store.audit("task", task_id, "dm_anchor_lost", "system", {"error": str(e)[:200]})
            if not msg_id:
                prefix = f"[{task_id} 来自群 {task.origin_chat_id}，该群未开放{kind}代发权限，改为私聊你]\n" if in_group else ""
                msg_id = self.app.feishu.send_text(owner, prefix + text)
                self.app.store.kv_set(f"dm_anchor:{task_id}", msg_id)
                delivered = "私聊负责人"
        self.app.store.add_outbound(task_id, task.origin_chat_id, target_msg or "", kind, delivered, text, msg_id)
        self.app.store.log_message({"message_id": msg_id, "chat_type": task.origin_chat_type or "p2p",
                                    "content": text}, "outbound", task_id)
        if in_group:
            self.app.store.audit("task", task_id, f"outbound_{kind}", "system", {"delivered": delivered})
        return msg_id

    def _reply(self, task_id: str, message_id: str, text: str) -> tuple[str, bool]:
        """按配置用话题回复；会话不支持话题（如飞书报 230071）就退回普通引用回复，回复不丢。"""
        if not self.app.settings.feishu.reply_in_thread:
            return self.app.feishu.reply_text(message_id, text), False
        try:
            return self.app.feishu.reply_text(message_id, text, in_thread=True), True
        except FeishuError as e:
            self.app.store.audit("task", task_id, "thread_reply_fallback", "system", {"error": str(e)[:200]})
            return self.app.feishu.reply_text(message_id, text), False

    def image_to(self, task_id: str, message_id: str, path: str) -> str:
        if self.app.settings.feishu.reply_in_thread:
            try:
                msg_id = self.app.feishu.reply_image(message_id, path, in_thread=True)
            except FeishuError:
                msg_id = self.app.feishu.reply_image(message_id, path)
        else:
            msg_id = self.app.feishu.reply_image(message_id, path)
        self.app.store.add_outbound(task_id, "", message_id, "image", "thread", path, msg_id)
        self.app.store.link_report(msg_id, task_id)
        return msg_id
