"""后台控制台（DESIGN.md 12）：先做记忆维护——记忆库增删改、记忆草稿审核、召回记录、变更记录。

- 只用标准库 http.server，默认只监听 127.0.0.1
- 登录：启动时打印带令牌的链接，打开后写入 HttpOnly + SameSite=Strict 的 cookie；API 也接受 X-Console-Token 头
- 所有修改都写审计日志（DESIGN.md 12.3）
"""

from __future__ import annotations

import hmac
import json
import secrets
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .memory import MemoryError, MemoryStore
from .store import Store

PAGE = Path(__file__).parent / "web" / "console.html"
COOKIE = "ecom_twin_console"
MEMORY_TYPES = ["experience", "calendar", "fact", "preference"]


def memory_json(m) -> dict[str, Any]:
    return {"slug": m.slug, "name": m.name, "description": m.description, "type": m.type, "body": m.body,
            "updated_at": m.path.stat().st_mtime}


class ConsoleApp:
    """与 HTTP 无关的业务层，方便测试。"""

    def __init__(self, store: Store, memory: MemoryStore):
        self.store = store
        self.memory = memory

    def route(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        parts = [unquote(p) for p in path.split("/") if p][1:]  # 去掉 "api"；中文标识在 URL 里是编码过的
        try:
            if parts == ["memories"] and method == "GET":
                return 200, {"memories": [memory_json(m) for m in self.memory.all()], "types": MEMORY_TYPES,
                             "index": self.memory.index_text()}
            if parts == ["memories"] and method == "POST":
                m = self.memory.save(body.get("name", ""), body.get("description", ""), body.get("type", ""),
                                     body.get("body", ""), slug=None)
                return 201, memory_json(m)
            if len(parts) == 2 and parts[0] == "memories":
                if method == "PUT":
                    m = self.memory.save(body.get("name", ""), body.get("description", ""), body.get("type", ""),
                                         body.get("body", ""), slug=parts[1])
                    return 200, memory_json(m)
                if method == "DELETE":
                    self.memory.delete(parts[1])
                    return 200, {"ok": True}
            if parts == ["drafts"] and method == "GET":
                return 200, {"drafts": self.store.list_memory_drafts(query.get("status", "待审核"))}
            if len(parts) == 3 and parts[0] == "drafts" and method == "POST":
                if parts[2] == "approve":
                    edits = {k: body[k] for k in ("name", "description", "type", "body") if k in body}
                    path = self.memory.approve(parts[1], edits or None)
                    return 200, {"ok": True, "memory": path.stem}
                if parts[2] == "reject":
                    self.memory.reject(parts[1], note=body.get("note", ""))
                    return 200, {"ok": True}
            if parts == ["recalls"] and method == "GET":
                recalls = self.store.list_recalls(query.get("task") or None)
                return 200, {"recalls": list(reversed(recalls))[:500]}
            if parts == ["audit"] and method == "GET":
                return 200, {"events": self.store.audit_log(("memory", "memory_draft"))}
        except MemoryError as e:
            return 400, {"error": str(e)}
        return 404, {"error": "not found"}


def make_handler(app: ConsoleApp, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ecom-twin-console"

        def log_message(self, fmt: str, *args: Any) -> None:  # 不打印每个请求
            pass

        def _authed(self) -> bool:
            supplied = self.headers.get("X-Console-Token", "")
            if not supplied:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                supplied = cookie[COOKIE].value if COOKIE in cookie else ""
            return bool(supplied) and hmac.compare_digest(supplied, token)

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8",
                  headers: dict[str, str] | None = None) -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False,
                                                                         default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/" and method == "GET":
                supplied = query.get("token", "")
                if supplied and hmac.compare_digest(supplied, token):  # 用带令牌的链接登录，然后去掉令牌
                    self._send(HTTPStatus.FOUND, b"", "text/plain", {
                        "Location": "/",
                        "Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"})
                    return
                if not self._authed():
                    self._send(401, "未登录：请使用启动时打印的带令牌链接打开控制台".encode(), "text/plain; charset=utf-8")
                    return
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                return
            if not url.path.startswith("/api/"):
                self._send(404, {"error": "not found"})
                return
            if not self._authed():
                self._send(401, {"error": "未登录"})
                return
            body: dict[str, Any] = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    self._send(400, {"error": "请求体不是合法 JSON"})
                    return
            status, payload = app.route(method, url.path, query, body)
            self._send(status, payload)

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

    return Handler


def make_server(store: Store, memory: MemoryStore, host: str = "127.0.0.1", port: int = 8765,
                token: str | None = None) -> tuple[ThreadingHTTPServer, str]:
    token = token or secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((host, port), make_handler(ConsoleApp(store, memory), token))
    return server, token
