"""后台控制台（DESIGN.md 12）：先做记忆维护——记忆库增删改、记忆草稿审核、召回记录、变更记录。

- 只用标准库 http.server，默认只监听 127.0.0.1
- 登录：启动时打印带令牌的链接，打开后写入 HttpOnly + SameSite=Strict 的 cookie；API 也接受 X-Console-Token 头
- 所有修改都写审计日志（DESIGN.md 12.3）
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import tomllib
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .config import load_raw, settings_from_raw, validate_settings
from .paths import SETTINGS_FILE
from .config_overrides import FIELDS, REPLACE, coerce, deep_merge, dump_toml, field_for, load_overrides
from .memory import MemoryError, MemoryStore, file_sha256
from .runtime import RUNTIMES, check_all
from .store import Store, utcnow

PAGE = Path(__file__).parent / "web" / "console.html"
COOKIE = "ecom_twin_console"
MEMORY_TYPES = ["experience", "calendar", "fact", "preference"]


def memory_json(m, graph: dict | None = None) -> dict[str, Any]:
    links = (graph or {}).get(m.name, {})
    # sha256：保存时带回来，期间文件被别处改过就拒绝保存（防覆盖）
    return {"slug": m.slug, "name": m.name, "description": m.description, "type": m.type, "body": m.body,
            "updated_at": m.path.stat().st_mtime, "sha256": file_sha256(m.path),
            "links": links.get("links", []), "linked_by": links.get("linked_by", []), "orphan": links.get("orphan", False)}


class ConsoleApp:
    """与 HTTP 无关的业务层，方便测试。"""

    def __init__(self, store: Store, memory: MemoryStore, settings_path: Path | None = None):
        self.store = store
        self.memory = memory
        self.settings_path = Path(settings_path) if settings_path else SETTINGS_FILE

    # ---- 规则配置 ----
    def _overrides_path(self) -> Path:
        return self.settings_path.with_name("settings.console.toml")

    @staticmethod
    def _effective(settings: Any, f: Any) -> Any:
        """当前生效的值（含默认值），而不是只看文件里写了什么。"""
        if f.section == "approvers":
            return {k: vars(v) for k, v in settings.approvers.items()}
        if f.section == "send_permissions":
            return {k: vars(v) for k, v in settings.send_permissions.items()}
        if f.section == "shadow":
            return settings.shadow_actions
        holder = {"limits": settings.limits, "feishu": settings.feishu, "intake": settings.intake,
                  "runtime": settings.runtime, "approval": settings}.get(f.section)
        return getattr(holder, f.key, None)

    def config_view(self) -> dict[str, Any]:
        settings = settings_from_raw(load_raw(self.settings_path))
        fields = []
        for f in FIELDS:
            fields.append({"section": f.section, "key": f.key, "label": f.label, "type": f.type,
                           "high_risk": f.high_risk, "value": self._effective(settings, f)})
        return {"fields": fields, "note": "保存后重启 ecom-twin serve 生效；改动写在 config/settings.console.toml，"
                                          "不会改动你手写的 settings.toml"}

    def _claude_binary(self) -> str:
        """本机 Claude Code 的路径：以配置 [runtime.cli] claude_code 为准，留空自动查找。"""
        try:
            return settings_from_raw(load_raw(self.settings_path)).runtime.cli.get("claude_code", "") or ""
        except (OSError, ValueError, TypeError, AttributeError):
            return ""

    def _doubtful(self) -> list[dict]:
        """判了「不相关」却写不出属于哪一类的：照样没处理，列出来给人看是不是漏了活。"""
        out = []
        for row in self.store.audits_by_event("triage_doubtful"):
            task = self.store.get_task(row["entity_id"])
            detail = json.loads(row["detail"] or "{}")
            out.append({"created_at": row["created_at"], "task_id": row["entity_id"],
                        "title": task.title if task else "", "reason": detail.get("reason", "")})
        return out

    def config_update(self, body: dict[str, Any]) -> tuple[int, Any]:
        f = field_for(body.get("section", ""), body.get("key", ""))
        if f is None:
            return 400, {"error": "这个配置项不能在控制台修改"}
        try:
            value = coerce(f, body.get("value"))
        except (ValueError, TypeError, re.error) as e:
            return 400, {"error": f"值不合法：{e}"}
        overrides = load_overrides(self._overrides_path())
        before = self._effective(settings_from_raw(load_raw(self.settings_path)), f)
        if f.type == "json":
            overrides[f.section] = {REPLACE: True, **value}
        else:
            overrides.setdefault(f.section, {})[f.key] = value
        base = tomllib.loads(self.settings_path.read_text(encoding="utf-8")) if self.settings_path.exists() else {}
        try:
            settings = settings_from_raw(deep_merge(base, overrides))
            errors = [t for level, t in validate_settings(settings, list(RUNTIMES)) if level == "错误"]
        except (TypeError, ValueError) as e:
            return 400, {"error": f"配置无法加载：{e}"}
        if errors:
            return 400, {"error": "保存后配置会出错：" + "；".join(errors)}
        if f.high_risk and not body.get("confirm"):
            return 200, {"need_confirm": True, "label": f.label, "before": before, "after": value}
        self._overrides_path().write_text(
            "# 由控制台写入，叠加在 settings.toml 之上。可以手工删除某项来恢复 settings.toml 的值\n\n"
            + dump_toml(overrides), encoding="utf-8")
        self.store.audit("config", f"{f.section}.{f.key}" if f.key else f.section, "updated", "console",
                         {"before": before, "after": value, "high_risk": f.high_risk})
        return 200, {"ok": True}

    def route(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        parts = [unquote(p) for p in path.split("/") if p][1:]  # 去掉 "api"；中文标识在 URL 里是编码过的
        try:
            if parts == ["memories"] and method == "GET":
                graph = self.memory.graph()
                return 200, {"memories": [memory_json(m, graph) for m in self.memory.all()], "types": MEMORY_TYPES,
                             "index": self.memory.index_text(), "budget": self.memory.budget(),
                             "problems": [{"level": lv, "text": t} for lv, t in self.memory.check()]}
            if parts == ["memories"] and method == "POST":
                m = self.memory.save(body.get("name", ""), body.get("description", ""), body.get("type", ""),
                                     body.get("body", ""), slug=None)
                return 201, memory_json(m)
            if parts == ["memory-bundle"] and method == "GET":
                names = [n for n in (query.get("names") or "").split(",") if n.strip()]
                return 200, {"text": self.memory.export_bundle(names or None)}
            if parts == ["memory-bundle"] and method == "POST":
                return 200, self.memory.import_bundle(body.get("text", ""))
            if len(parts) == 3 and parts[0] == "memories" and parts[2] == "history" and method == "GET":
                return 200, {"history": self.memory.history(parts[1])}
            if len(parts) == 4 and parts[0] == "memories" and parts[2] == "history" and method == "GET":
                return 200, {"text": self.memory.read_snapshot(parts[1], parts[3])}
            if len(parts) == 3 and parts[0] == "memories" and parts[2] == "suggest-description" and method == "POST":
                return 200, self.memory.suggest_description(parts[1], binary=self._claude_binary())
            if len(parts) == 3 and parts[0] == "memories" and parts[2] == "rollback" and method == "POST":
                return 200, memory_json(self.memory.rollback(parts[1], body.get("file", "")))
            if len(parts) == 2 and parts[0] == "memories":
                if method == "PUT":
                    m = self.memory.save(body.get("name", ""), body.get("description", ""), body.get("type", ""),
                                         body.get("body", ""), slug=parts[1], expected_sha=body.get("sha256") or None)
                    return 200, memory_json(m)
                if method == "DELETE":
                    self.memory.delete(parts[1])
                    return 200, {"ok": True}
            if parts == ["drafts"] and method == "GET":
                drafts = self.store.list_memory_drafts(query.get("status", "待审核"))
                for d in drafts:  # 审核时要对着目标记忆的当前内容看
                    target = self.memory.by_name(d.get("target_name") or "") if d.get("mode") != "create" else None
                    d["target_body"] = target.body if target else None
                return 200, {"drafts": drafts, "stats": self.store.draft_stats()}
            if len(parts) == 3 and parts[0] == "drafts" and method == "POST":
                if parts[2] == "approve":
                    edits = {k: body[k] for k in ("name", "description", "type", "body", "target_section", "old_text")
                             if k in body}
                    path = self.memory.approve(parts[1], edits or None)
                    return 200, {"ok": True, "memory": path.stem}
                if parts[2] == "reject":
                    self.memory.reject(parts[1], note=body.get("note", ""))
                    return 200, {"ok": True}
            if parts == ["recalls"] and method == "GET":
                recalls = self.store.list_recalls(query.get("task") or None)
                return 200, {"recalls": list(reversed(recalls))[:500], "summary": self.store.recall_summary()}
            if parts == ["audit"] and method == "GET":
                return 200, {"events": self.store.audit_log(("memory", "memory_draft", "config"))}
            if parts == ["config"] and method == "GET":
                return 200, self.config_view()
            if parts == ["config"] and method == "POST":
                return self.config_update(body)
            if parts == ["intake"] and method == "GET":
                return 200, {"buffer": [dict(r) for g in self.store.buffer_groups() for r in g],
                             "decisions": self.store.merge_decisions(100),
                             "outbound": self.store.outbound_messages()[:100],
                             "doubtful": self._doubtful()}
            if parts == ["runtimes"] and method == "GET":
                settings = settings_from_raw(load_raw(self.settings_path))
                return 200, {"default": settings.runtime.default, "fallback": settings.runtime.fallback,
                             "runtimes": [vars(st) | {"usable": st.usable} for st in check_all(settings.runtime.cli)]}
            if parts == ["shadow"] and method == "GET":
                from .shadow import ShadowService
                settings = settings_from_raw(load_raw(self.settings_path))
                return 200, ShadowService(self.store, settings, {}).report()
            if parts == ["listings"] and method == "GET":
                return 200, {"listings": self.store.listing_diffs(query.get("task") or None)}
            if parts == ["costs"] and method == "GET":
                month = query.get("month") or utcnow().strftime("%Y-%m")
                return 200, {"month": month, **{by: self.store.usage_summary(month, by)
                                                for by in ("runtime", "scene", "shop_key")}}
        except MemoryError as e:
            return 400, {"error": str(e)}
        return 404, {"error": "not found"}


# 角色（DESIGN.md 12：运营只能看和审阅，负责人才能改硬上限和代发权限）
ROLE_OWNER, ROLE_REVIEWER = "负责人", "运营"


def allowed(role: str, method: str, path: str) -> bool:
    """运营：所有页面都能看，能采纳 / 驳回记忆草稿；改配置、改记忆库、导入、回滚都要负责人。"""
    if role == ROLE_OWNER or method == "GET":
        return True
    parts = [p for p in path.split("/") if p][1:]
    return len(parts) == 3 and parts[0] == "drafts" and parts[2] in ("approve", "reject") and method == "POST"


def make_handler(app: ConsoleApp, token: str, review_token: str = "") -> type[BaseHTTPRequestHandler]:
    tokens = {token: ROLE_OWNER, **({review_token: ROLE_REVIEWER} if review_token else {})}

    def role_of(supplied: str) -> str | None:
        return next((r for t, r in tokens.items() if supplied and hmac.compare_digest(supplied, t)), None)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ecom-twin-console"

        def log_message(self, fmt: str, *args: Any) -> None:  # 不打印每个请求
            pass

        def _role(self) -> str | None:
            supplied = self.headers.get("X-Console-Token", "")
            if not supplied:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                supplied = cookie[COOKIE].value if COOKIE in cookie else ""
            return role_of(supplied)

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
                if role_of(supplied):  # 用带令牌的链接登录，然后去掉令牌
                    self._send(HTTPStatus.FOUND, b"", "text/plain", {
                        "Location": "/",
                        "Set-Cookie": f"{COOKIE}={supplied}; HttpOnly; SameSite=Strict; Path=/"})
                    return
                if not self._role():
                    self._send(401, "未登录：请使用启动时打印的带令牌链接打开控制台".encode(), "text/plain; charset=utf-8")
                    return
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                return
            if not url.path.startswith("/api/"):
                self._send(404, {"error": "not found"})
                return
            role = self._role()
            if not role:
                self._send(401, {"error": "未登录"})
                return
            if url.path == "/api/whoami":
                self._send(200, {"role": role})
                return
            if not allowed(role, method, url.path):
                app.store.audit("console", url.path, "forbidden", role, {"method": method})
                self._send(403, {"error": "运营账号只能查看和审阅记忆草稿；改规则配置、记忆库、导入、回滚要用负责人的链接"})
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
                token: str | None = None, settings_path: Path | None = None,
                review_token: str | None = None) -> tuple[ThreadingHTTPServer, str]:
    """返回 (server, 负责人令牌)；运营令牌在 server.review_token。"""
    token = token or secrets.token_urlsafe(24)
    review_token = review_token or secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((host, port), make_handler(ConsoleApp(store, memory, settings_path), token, review_token))
    server.review_token = review_token  # type: ignore[attr-defined]
    return server, token
