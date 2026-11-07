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


def content_text(msg_type: str, content: Any) -> str:
    """把飞书消息内容（JSON 字符串）转成纯文本。不认识的类型给出占位说明。"""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return content
    if not isinstance(content, dict):
        return str(content or "")
    if msg_type == "text" or "text" in content:
        return str(content.get("text", ""))
    if msg_type == "post" or "content" in content:
        post = content
        if "content" not in post:  # 多语言：{"zh_cn": {...}}
            post = next((v for v in content.values() if isinstance(v, dict) and "content" in v), {})
        parts = [post.get("title", "")]
        for line in post.get("content", []):
            parts.append("".join(seg.get("text") or seg.get("user_name") or seg.get("href", "")
                                 for seg in line if isinstance(seg, dict)))
        return "\n".join(p for p in parts if p)
    if msg_type == "image":
        return "[图片]"
    if msg_type == "file":
        return f"[文件 {content.get('file_name', '')}]"
    if msg_type == "interactive":
        return "[卡片消息]"
    return f"[{msg_type} 消息]"


def normalize_message(item: dict[str, Any]) -> dict[str, Any]:
    """统一成 agent 看得懂的结构：谁、什么时候、说了什么、属于哪个 thread。"""
    sender = item.get("sender") or {}
    created = item.get("create_time") or ""
    ts = ""
    if str(created).isdigit():
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(int(created) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    body = item.get("body") or {}
    text = content_text(item.get("msg_type", ""), body.get("content", item.get("content", "")))
    return {
        "message_id": item.get("message_id", ""),
        "chat_id": item.get("chat_id", ""),
        "sender_id": sender.get("id", "") if isinstance(sender, dict) else str(sender),
        "sender_type": sender.get("sender_type", "") if isinstance(sender, dict) else "",
        "time": ts or str(created),
        "text": text[:2000],
        "parent_id": item.get("parent_id", ""),
        "root_id": item.get("root_id", ""),
        "thread_id": item.get("thread_id", ""),
        "deleted": bool(item.get("deleted")),
    }


class FeishuClient(Protocol):
    def send_card(self, open_id: str, card: dict[str, Any]) -> str: ...
    def send_text(self, open_id: str, text: str) -> str: ...
    def reply_text(self, message_id: str, text: str) -> str: ...
    def get_parent_id(self, message_id: str) -> str | None: ...
    def reply_image(self, message_id: str, image_path: str) -> str: ...
    def send_chat_text(self, chat_id: str, text: str) -> str: ...
    def base_create(self, app_token: str, table_id: str, fields: dict[str, Any]) -> str: ...
    def base_update(self, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> None: ...
    def base_list(self, app_token: str, table_id: str) -> list[dict[str, Any]]: ...
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

    def send_chat_text(self, chat_id: str, text: str) -> str:
        data = self._api("POST", "/open-apis/im/v1/messages", params={"receive_id_type": "chat_id"},
                         data={"receive_id": chat_id, "msg_type": "text",
                               "content": json.dumps({"text": text}, ensure_ascii=False)})
        return data["message_id"]

    def reply_text(self, message_id: str, text: str) -> str:
        data = self._api(
            "POST", f"/open-apis/im/v1/messages/{message_id}/reply",
            data={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        )
        return data["message_id"]

    def reply_image(self, message_id: str, image_path: str) -> str:
        """上传图片拿 image_key，再回复到消息的 thread（证据截图）。"""
        cmd = [self.binary, "api", "POST", "/open-apis/im/v1/images", "--as", self.identity, "--format", "json",
               "--data", json.dumps({"image_type": "message"}), "--file", f"image={image_path}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        try:
            payload = json.loads(proc.stdout or proc.stderr)
        except json.JSONDecodeError as e:
            raise FeishuError(f"上传图片失败：{(proc.stderr or proc.stdout)[:200]}") from e
        image_key = (payload.get("data") or {}).get("image_key")
        if proc.returncode != 0 or not image_key:
            raise FeishuError(f"上传图片失败：{(payload.get('error') or {}).get('message') or payload.get('msg')}")
        data = self._api("POST", f"/open-apis/im/v1/messages/{message_id}/reply",
                         data={"msg_type": "image", "content": json.dumps({"image_key": image_key})})
        return data["message_id"]

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        data = self._api("GET", f"/open-apis/im/v1/messages/{message_id}")
        items = data.get("items") or []
        return normalize_message(items[0]) if items else None

    def _list(self, container_type: str, container_id: str, limit: int, extra: dict | None = None) -> list[dict]:
        params = {"container_id_type": container_type, "container_id": container_id,
                  "sort_type": "ByCreateTimeDesc", "page_size": str(min(limit, 50)), **(extra or {})}
        items: list[dict] = []
        while len(items) < limit:
            data = self._api("GET", "/open-apis/im/v1/messages", params=params)
            items += data.get("items") or []
            if not data.get("has_more") or not data.get("page_token"):
                break
            params["page_token"] = data["page_token"]
        msgs = [normalize_message({**i, "chat_id": i.get("chat_id") or (container_id if container_type == "chat" else "")})
                for i in items[:limit]]
        return sorted(msgs, key=lambda m: m["time"])

    def chat_messages(self, chat_id: str, start_ts: int, end_ts: int, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("chat", chat_id, limit, {"start_time": str(start_ts), "end_time": str(end_ts)})

    def thread_messages(self, thread_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("thread", thread_id, limit)

    def search_messages(self, query: str, start_ts: int, end_ts: int, limit: int = 20) -> list[dict[str, Any]]:
        """用负责人本人身份搜索（需要 lark-cli auth login）。返回结构以 lark-cli 输出为准，统一后返回。"""
        from datetime import datetime, timezone
        fmt = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="seconds")  # noqa: E731
        cmd = [self.binary, "im", "+messages-search", "--as", "user", "--format", "json", "--query", query,
               "--start", fmt(start_ts), "--end", fmt(end_ts), "--page-size", str(min(limit, 50))]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        try:
            payload = json.loads(proc.stdout or proc.stderr)
        except json.JSONDecodeError as e:
            raise FeishuError(f"搜索消息失败：{(proc.stderr or proc.stdout)[:200]}") from e
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise FeishuError(f"搜索消息失败：{(payload.get('error') or {}).get('message')}")
        found: list[dict] = []

        def walk(node: Any) -> None:  # 输出结构可能随 lark-cli 版本变化，找出所有带 message_id 的对象
            if isinstance(node, dict):
                if node.get("message_id"):
                    found.append(normalize_message(node))
                    return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(payload)
        return found[:limit]

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

    def base_list(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        """读表里全部记录（分页），返回 [{"record_id", "fields"}]。"""
        params: dict[str, Any] = {"page_size": "500"}
        records: list[dict] = []
        while True:
            data = self._api("GET", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records", params=params)
            records += [{"record_id": i.get("record_id"), "fields": i.get("fields") or {}} for i in data.get("items") or []]
            if not data.get("has_more") or not data.get("page_token"):
                return records
            params["page_token"] = data["page_token"]


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

    def reply_image(self, message_id: str, image_path: str) -> str:
        return self._out("图片", message_id, image_path)

    def send_chat_text(self, chat_id: str, text: str) -> str:
        return self._out("群消息", chat_id, text)

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        return None

    def chat_messages(self, chat_id: str, start_ts: int, end_ts: int, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def thread_messages(self, thread_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def search_messages(self, query: str, start_ts: int, end_ts: int, limit: int = 20) -> list[dict[str, Any]]:
        return []

    def base_create(self, app_token: str, table_id: str, fields: dict[str, Any]) -> str:
        return self._out("看板新增", table_id, fields)

    def base_update(self, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> None:
        self._out("看板更新", table_id, fields)

    def base_list(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        return []


def make_feishu() -> FeishuClient:
    """ECOM_TWIN_FEISHU=console 时用 ConsoleFeishu（本地调试，MCP server 子进程也会继承）；
    配置页保存了自建应用凭证时用官方接口（NativeFeishu），否则用 lark-cli。"""
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
    fail_base: bool = False
    # 模拟会话历史：normalize_message 之后的结构，time 为 ISO 字符串
    history: list[dict[str, Any]] = field(default_factory=list)
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

    def reply_image(self, message_id: str, image_path: str) -> str:
        mid = self._next("om")
        self.sent.append({"type": "image", "reply_to": message_id, "path": image_path, "message_id": mid})
        return mid

    def send_chat_text(self, chat_id: str, text: str) -> str:
        mid = self._next("om")
        self.sent.append({"type": "chat", "to_chat": chat_id, "text": text, "message_id": mid})
        return mid

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

    def base_list(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        return [{"record_id": rid, "fields": {k: v for k, v in rec.items() if k != "table"}}
                for rid, rec in self.records.items() if rec.get("table") == table_id]
