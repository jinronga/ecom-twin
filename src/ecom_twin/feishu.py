"""飞书访问层。

FeishuClient 是分身用到的最小接口；LarkCliFeishu 通过本机 lark-cli（bot 身份）调用飞书开放接口，
FakeFeishu 用于测试和离线演示。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol


class FeishuError(Exception):
    pass


class FeishuClient(Protocol):
    def send_card(self, open_id: str, card: dict[str, Any]) -> str: ...
    def send_text(self, open_id: str, text: str) -> str: ...
    def reply_text(self, message_id: str, text: str) -> str: ...
    def get_parent_id(self, message_id: str) -> str | None: ...
    def base_create(self, app_token: str, table_id: str, fields: dict[str, Any]) -> str: ...
    def base_update(self, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> None: ...
    # ---- 读上下文（只读） ----
    def get_message(self, message_id: str) -> dict[str, Any] | None: ...
    def chat_messages(self, chat_id: str, start_ts: int, end_ts: int, limit: int = 50) -> list[dict[str, Any]]: ...
    def thread_messages(self, thread_id: str, limit: int = 50) -> list[dict[str, Any]]: ...
    def search_messages(self, query: str, start_ts: int, end_ts: int, limit: int = 20) -> list[dict[str, Any]]: ...


class LarkCliFeishu:
    """用 `lark-cli api` 调用飞书开放接口。鉴权、token 刷新由 lark-cli 负责。"""

    def __init__(self, binary: str = "lark-cli", identity: str = "bot", timeout: float = 30):
        self.binary = binary
        self.identity = identity
        self.timeout = timeout

    def _api(self, method: str, path: str, params: dict | None = None, data: dict | None = None) -> dict:
        cmd = [self.binary, "api", method, path, "--as", self.identity, "--format", "json"]
        if params:
            cmd += ["--params", json.dumps(params, ensure_ascii=False)]
        if data is not None:
            cmd += ["--data", json.dumps(data, ensure_ascii=False)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            try:
                detail = (json.loads(detail).get("error") or {}).get("message") or detail
            except (json.JSONDecodeError, AttributeError):
                pass
            raise FeishuError(f"lark-cli {method} {path} 失败：{detail}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise FeishuError(f"lark-cli 返回非 JSON：{proc.stdout[:200]}") from e
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise FeishuError(f"lark-cli {method} {path} 失败：{(payload.get('error') or {}).get('message')}")
        if isinstance(payload, dict) and payload.get("code", 0) != 0:
            raise FeishuError(f"飞书接口错误 {payload.get('code')}: {payload.get('msg')}")
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def _send(self, open_id: str, msg_type: str, content: dict) -> str:
        data = self._api(
            "POST", "/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            data={"receive_id": open_id, "msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)},
        )
        return data["message_id"]

    def send_card(self, open_id: str, card: dict[str, Any]) -> str:
        return self._send(open_id, "interactive", card)

    def send_text(self, open_id: str, text: str) -> str:
        return self._send(open_id, "text", {"text": text})

    def reply_text(self, message_id: str, text: str) -> str:
        data = self._api(
            "POST", f"/open-apis/im/v1/messages/{message_id}/reply",
            data={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        )
        return data["message_id"]

    def get_parent_id(self, message_id: str) -> str | None:
        data = self._api("GET", f"/open-apis/im/v1/messages/{message_id}")
        items = data.get("items") or []
        return (items[0].get("parent_id") or None) if items else None

    def base_create(self, app_token: str, table_id: str, fields: dict[str, Any]) -> str:
        data = self._api("POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                         data={"fields": fields})
        return data["record"]["record_id"]

    def base_update(self, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> None:
        self._api("PUT", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
                  data={"fields": fields})

    # ---- 上下文拉取（只读） ----
    def get_message(self, message_id: str) -> dict[str, Any] | None:
        """获取单条消息。"""
        try:
            data = self._api("GET", f"/open-apis/im/v1/messages/{message_id}")
            items = data.get("items") or []
            return normalize_message(items[0]) if items else None
        except FeishuError:
            return None

    def _list(self, container_type: str, container_id: str, limit: int, extra_params: dict | None = None) -> list[dict[str, Any]]:
        """通用消息列表拉取（chat/thread）。"""
        params = {"container_id": container_id, "container_id_type": container_type, "page_size": str(min(limit, 100))}
        if extra_params:
            params.update(extra_params)
        items = []
        while len(items) < limit:
            data = self._api("GET", "/open-apis/im/v1/messages", params=params)
            batch = data.get("items") or []
            if not batch:
                break
            items.extend(batch)
            if not data.get("has_more"):
                break
            params["page_token"] = data["page_token"]
        msgs = [normalize_message({**i, "chat_id": i.get("chat_id") or (container_id if container_type == "chat" else "")})
                for i in items[:limit]]
        return sorted(msgs, key=lambda m: m["time"])

    def chat_messages(self, chat_id: str, start_ts: int, end_ts: int, limit: int = 50) -> list[dict[str, Any]]:
        """拉取会话消息（时间范围）。"""
        return self._list("chat", chat_id, limit, {"start_time": str(start_ts), "end_time": str(end_ts)})

    def thread_messages(self, thread_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """拉取 thread 消息。"""
        return self._list("thread", thread_id, limit)

    def search_messages(self, query: str, start_ts: int, end_ts: int, limit: int = 20) -> list[dict[str, Any]]:
        """用负责人本人身份搜索（需要 lark-cli auth login）。"""
        from datetime import datetime, timezone
        start = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        end = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        # lark-cli 的搜索返回结构由命令行定义，走 JSON parse
        cmd = [self.binary, "api", "GET", "/open-apis/search/v2/message",
               "--as", "user", "--format", "json",
               "--params", json.dumps({"query": query, "start_time": start, "end_time": end, "page_size": str(limit)})]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        if proc.returncode != 0:
            raise FeishuError(f"搜索失败：{proc.stderr.strip()}")
        payload = json.loads(proc.stdout).get("data", {})
        found = []

        def walk(node):
            if isinstance(node, dict):
                if "message_id" in node:
                    found.append(normalize_message(node))
                    return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(payload)
        return found[:limit]


def normalize_message(msg: dict[str, Any]) -> dict[str, Any]:
    """将飞书消息规范化为统一格式。"""
    from datetime import datetime
    sender = msg.get("sender") or {}
    content = msg.get("content") or msg.get("body") or {}
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}

    # 提取纯文本
    text = content.get("text", "")
    if not text and "content" in content:  # 有些消息格式是 {"content": "..."}
        text = content["content"]

    return {
        "message_id": msg.get("message_id", ""),
        "chat_id": msg.get("chat_id", ""),
        "thread_id": msg.get("thread_id") or msg.get("parent_id") or "",
        "sender_id": sender.get("id") or sender.get("sender_id") or "",
        "sender_name": (sender.get("name") or sender.get("sender_name") or "未知"),
        "text": text,
        "time": datetime.fromtimestamp(int(msg.get("create_time") or msg.get("msg_time") or 0) / 1000).isoformat(),
        "msg_type": msg.get("msg_type", "text"),
    }


def consume_messages(binary: str = "lark-cli", sleep=None) -> Iterator[dict[str, Any]]:
    """持续消费 im.message.receive_v1（lark-cli 事件总线，NDJSON），逐条产出消息。

    lark-cli 进程意外退出时自动重连（退避 5 秒起，最长 5 分钟），不会让服务悄悄停止接收消息。
    """
    import time
    sleep = sleep or time.sleep
    backoff = 5
    while True:
        proc = subprocess.Popen([binary, "event", "consume", "im.message.receive_v1", "--quiet"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
                backoff = 5
        finally:
            proc.terminate()
        err = proc.stderr.read().strip() if proc.stderr else ""
        print(f"lark-cli 事件流已退出（{err[-300:] or '无错误输出'}），{backoff} 秒后重连", file=sys.stderr)
        sleep(backoff)
        backoff = min(backoff * 2, 300)


class ConsoleFeishu:
    """本地调试：把要发到飞书的内容打印到 stderr，不发任何真实请求（stdout 留给 MCP 协议）。"""

    def __init__(self):
        self._seq = 0

    def _out(self, kind: str, target: str, body: Any) -> str:
        self._seq += 1
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        print(f"[飞书·{kind} -> {target}] {text}", file=sys.stderr, flush=True)
        return f"om_console_{os.getpid()}_{self._seq}"

    def send_card(self, open_id: str, card: dict[str, Any]) -> str:
        texts = [el["text"]["content"] for el in card.get("elements", []) if el.get("tag") == "div"]
        return self._out("卡片", open_id, "\n".join(texts))

    def send_text(self, open_id: str, text: str) -> str:
        return self._out("消息", open_id, text)

    def reply_text(self, message_id: str, text: str) -> str:
        return self._out("回复", message_id, text)

    def get_parent_id(self, message_id: str) -> str | None:
        return None

    def base_create(self, app_token: str, table_id: str, fields: dict[str, Any]) -> str:
        return self._out("看板新增", table_id, fields)

    def base_update(self, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> None:
        self._out("看板更新", table_id, fields)

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        return None

    def chat_messages(self, chat_id: str, start_ts: int, end_ts: int, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def thread_messages(self, thread_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def search_messages(self, query: str, start_ts: int, end_ts: int, limit: int = 20) -> list[dict[str, Any]]:
        return []


def make_feishu() -> FeishuClient:
    """ECOM_TWIN_FEISHU=console 时用 ConsoleFeishu（本地调试，MCP server 子进程也会继承），否则用 lark-cli。"""
    if os.environ.get("ECOM_TWIN_FEISHU") == "console":
        return ConsoleFeishu()
    from .setup_config import feishu_credentials
    app_id, secret = feishu_credentials()
    if app_id and secret:
        from .feishu_native import NativeFeishu
        return NativeFeishu(app_id, secret)
    return LarkCliFeishu()


def task_messages() -> Iterator[dict[str, Any]]:
    from .setup_config import feishu_credentials
    app_id, secret = feishu_credentials()
    if app_id and secret:
        from .feishu_native import consume_native_messages
        yield from consume_native_messages(app_id, secret)
    else:
        yield from consume_messages()


@dataclass
class FakeFeishu:
    """内存实现：记录所有调用，用于测试和离线演示。"""

    sent: list[dict[str, Any]] = field(default_factory=list)
    parents: dict[str, str] = field(default_factory=dict)
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)  # 用于模拟上下文拉取
    fail_base: bool = False
    _seq: int = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"

    def send_card(self, open_id: str, card: dict[str, Any]) -> str:
        mid = self._next("om")
        self.sent.append({"type": "card", "to": open_id, "card": card, "message_id": mid})
        return mid

    def send_text(self, open_id: str, text: str) -> str:
        mid = self._next("om")
        self.sent.append({"type": "text", "to": open_id, "text": text, "message_id": mid})
        return mid

    def reply_text(self, message_id: str, text: str) -> str:
        mid = self._next("om")
        self.sent.append({"type": "reply", "reply_to": message_id, "text": text, "message_id": mid})
        return mid

    def get_parent_id(self, message_id: str) -> str | None:
        return self.parents.get(message_id)

    def base_create(self, app_token: str, table_id: str, fields: dict[str, Any]) -> str:
        if self.fail_base:
            raise FeishuError("模拟限流")
        rid = self._next("rec")
        self.records[rid] = {"table": table_id, **fields}
        return rid

    def base_update(self, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> None:
        if self.fail_base:
            raise FeishuError("模拟限流")
        self.records[record_id].update(fields)

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        return next((m for m in self.history if m["message_id"] == message_id), None)

    def chat_messages(self, chat_id: str, start_ts: int, end_ts: int, limit: int = 50) -> list[dict[str, Any]]:
        from datetime import datetime
        def ts(m):
            return datetime.fromisoformat(m["time"]).timestamp()
        msgs = [m for m in self.history if m["chat_id"] == chat_id and start_ts <= ts(m) <= end_ts]
        return sorted(msgs, key=lambda m: m["time"])[-limit:]

    def thread_messages(self, thread_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return sorted([m for m in self.history if m["thread_id"] == thread_id], key=lambda m: m["time"])[:limit]

    def search_messages(self, query: str, start_ts: int, end_ts: int, limit: int = 20) -> list[dict[str, Any]]:
        return [m for m in self.history if query in m["text"]][:limit]
