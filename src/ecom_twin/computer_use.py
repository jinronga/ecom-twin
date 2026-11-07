"""操作电脑（任意 Mac 软件）：给 agent 挂的 computer MCP（对照数字分身：它借用底座 Codex 自带的 Computer Use）。

本机装了 ChatGPT 桌面端并开了 Codex Computer Use 后，会有一个 cua_repl MCP（OpenAI 的 @oai/cua-repl），
真正动手的是拿着「辅助功能」「屏幕录制」权限的 Codex Computer Use.app。它是普通的 stdio MCP，
所以不止 Codex，Claude Code、Cursor、OpenCode 都能挂。

我们不直接把它交给 agent，而是夹一层代理（本模块的 main）：
- 只放行 js、js_reset 两个工具（不给 js_add_node_module_dir 加载任意模块）
- 每次 js 调用写审计（任务、代码），事后能查 agent 在电脑上做过什么
- Computer Use 每碰一个 app 都要先问「允许使用吗」（MCP elicitation）。这个问题由代理回答、绝不转给 agent：
  只批准 allowed_apps 白名单里的 app（默认空，一个都不批），命中禁止项的、录音请求一律拒绝，每次都写审计
- 代码里出现禁止目标（卖货店铺后台、广告后台、密码管理器）的调用直接拒绝 —— 按文本匹配，只是兜底；
  对外生效、不可撤回的动作先提案或先问负责人，靠提示词约束
- 单次调用的超时封顶

运行：python -m ecom_twin.computer_use --task T-0001 [--config settings.toml]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

SERVER_NAME = "computer"
PRE_INIT_PROBES = ("server/discover",)
ALLOWED_TOOLS = ("js", "js_reset")
PROMPT_FILE = Path(__file__).parent / "prompts" / "computer_use.md"
_PLUGIN_GLOB = "plugins/cache/openai-bundled/unified-computer-use/*/.mcp.json"


def _version_key(path: Path) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in path.parent.name.split("."))


def find_cua(cfg: Any) -> dict | None:
    """找本机的 cua_repl MCP 定义：{command, args, env}。没装（或文件坏了）返回 None。"""
    if cfg.mcp_json:
        candidates = [Path(os.path.expanduser(cfg.mcp_json))]
    else:
        # 插件装在本人的 ~/.codex 下（不是 Codex 底座用的隔离目录）
        candidates = sorted((Path.home() / ".codex").glob(_PLUGIN_GLOB), key=_version_key, reverse=True)
    for path in candidates:
        try:
            server = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["cua_repl"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if Path(server["command"]).exists():
            return {"command": server["command"], "args": list(server.get("args") or []),
                    "env": dict(server.get("env") or {}), "source": str(path)}
    return None


def availability(cfg: Any) -> tuple[bool, str]:
    """给 check / 控制台看的一句话状态。"""
    found = find_cua(cfg)
    if not cfg.enabled:
        return False, "未开启（[computer_use] enabled = false）" + ("；本机已装 Computer Use，可开启" if found else "")
    if not found:
        return False, ("已开启但本机没找到 Computer Use：安装 ChatGPT 桌面端，在 Codex 里启用 Computer Use，"
                       "并在「系统设置 → 隐私与安全性」给 Codex Computer Use 授予辅助功能和屏幕录制权限")
    return True, f"已开启，使用 {found['source']}"


def prompt_section(cfg: Any) -> str:
    apps = "、".join(cfg.allowed_apps) or "（还没有，任何软件都会被拒绝）"
    return PROMPT_FILE.read_text(encoding="utf-8").replace("{allowed_apps}", apps)


APPLESCRIPT_PROMPT = Path(__file__).parent / "prompts" / "applescript.md"


def applescript_section(cfg: Any) -> str:
    """数字分身的做法（Bash + AppleScript，截图走 desktop_capture）对应的提示词。"""
    return APPLESCRIPT_PROMPT.read_text(encoding="utf-8").replace("{shell_commands}", "、".join(cfg.shell_commands))


def blocked_keyword(code: str, keywords: list[str]) -> str | None:
    text = (code or "").lower()
    return next((k for k in keywords if k and k.lower() in text), None)


def _tool_error(msg_id: Any, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}], "isError": True}}


def app_allowed(app: str, display: str, cfg: Any) -> str | None:
    """Computer Use 问「允许使用某个 app 吗」时的答案：None 表示批准，否则是拒绝原因。"""
    names = {app.lower(), display.lower()} - {""}
    if not names:
        return "没有说明是哪个 app"
    hit = next((k for k in cfg.blocked_keywords if k and any(k.lower() in n for n in names)), None)
    if hit:
        return f"命中禁止项「{hit}」"
    if not any(a.strip().lower() in names for a in cfg.allowed_apps):
        return "不在允许操作的软件名单里（[computer_use] allowed_apps）"
    return None


class Proxy:
    """agent ⇄ 本代理 ⇄ cua_repl。按行转发 JSON-RPC：改 tools/list 的结果、拦 tools/call、
    并由代理自己回答 Computer Use 的「允许使用某个 app 吗」（elicitation/create）—— 这个问题绝不转给 agent，
    否则 agent 能自己批准自己。按 allowed_apps 白名单答，相当于数字分身里本人在 Codex 桌面端点的「始终允许」。

    子类可改 allowed_tools / fail_event / wait_timeout_sec 复用转发骨架（chrome_guard 就是）。"""

    allowed_tools: tuple[str, ...] = ALLOWED_TOOLS
    fail_event = "computer_use_failed"

    def __init__(self, upstream: list[str], env: dict[str, str], cfg: Any, audit=None, task_id: str = ""):
        self.upstream, self.env, self.cfg = upstream, env, cfg
        self.audit = audit or (lambda action, detail: None)
        self.task_id = task_id
        self._lock = threading.Lock()
        self._list_ids: set = set()
        self._pending: dict[Any, str] = {}
        self._outstanding: set = set()  # 转给上游、还没回的请求
        self._idle = threading.Condition()
        self._upstream_in: TextIO | None = None
        self._upstream_dead = ""  # 上游进程退出后的说明，之后的请求直接回这个错误
        self._approved: set[str] = set()  # 本轮已批准过的 app：Computer Use 每个动作都会再问一次，审计只记第一次

    def wait_timeout_sec(self) -> float:
        return self.cfg.timeout_ms / 1000

    def _write(self, out: TextIO, msg: dict) -> None:
        with self._lock:
            out.write(json.dumps(msg, ensure_ascii=False) + "\n")
            out.flush()

    def from_client(self, msg: dict) -> dict | None:
        """返回要直接回给 agent 的消息（拦下了），或 None（照常转发；msg 可能被就地改写）。"""
        method, msg_id = msg.get("method"), msg.get("id")
        if method in PRE_INIT_PROBES:
            # 新版 Claude Code 先发新协议的探测；cua_repl（rmcp）在 initialize 之前收到不认识的请求会卡死、
            # 连后面的 initialize 都不回，导致连接超时。由代理直接答「不支持」，客户端随即改走 initialize
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        if method == "initialize":  # 告诉 Computer Use 我们能回答「允许使用某 app 吗」，由代理来答
            msg.setdefault("params", {}).setdefault("capabilities", {})["elicitation"] = {}
        elif method == "tools/list":
            self._list_ids.add(msg_id)
        elif method == "tools/call":
            params = msg.setdefault("params", {})
            name, args = params.get("name"), params.setdefault("arguments", {})
            if name not in ALLOWED_TOOLS:
                return _tool_error(msg_id, f"工具 {name} 没有开放")
            if name == "js":
                code = str(args.get("code") or "")
                hit = blocked_keyword(code, self.cfg.blocked_keywords)
                if hit:
                    self.audit("computer_use_blocked", {"keyword": hit, "code": code[:2000]})
                    return _tool_error(msg_id, f"拦截：操作目标命中禁止项「{hit}」。卖货店铺后台、广告后台只走官方 API "
                                               "和提案；密码管理器、钥匙串不能碰。换别的办法，或在汇报里写明卡点")
                args["timeout_ms"] = min(int(args.get("timeout_ms") or 30_000), self.cfg.timeout_ms)
                self._pending[msg_id] = code
                self.audit("computer_use", {"code": code[:2000]})
        return None

    def answer_elicitation(self, msg: dict) -> dict:
        """代理替负责人回答 Computer Use 的授权征询。"""
        params = msg.get("params") or {}
        meta = params.get("_meta") or {}
        tool = meta.get("tool_name") or ""
        app = str((meta.get("tool_params") or {}).get("app") or "")
        display = next((str(d.get("value") or "") for d in meta.get("tool_params_display") or []
                        if d.get("name") == "app"), "")
        if tool == "start_audio_recording":
            reason = "不允许录音"
        elif meta.get("connector_id") != "computer-use":
            reason = "不是 Computer Use 的授权请求"
        else:
            reason = app_allowed(app, display, self.cfg)
        if reason:
            self.audit("computer_use_app_declined", {"app": app, "name": display, "tool": tool, "reason": reason})
            return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"action": "decline"}}
        if app not in self._approved:
            self._approved.add(app)
            self.audit("computer_use_app_approved", {"app": app, "name": display, "tool": tool})
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"action": "accept", "content": {}}}

    def from_upstream(self, msg: dict) -> dict | None:
        """返回要转给 agent 的消息；None 表示代理自己处理掉了（授权征询）。"""
        if msg.get("method") == "elicitation/create":
            self._send_upstream(self.answer_elicitation(msg))
            return None
        msg_id = msg.get("id")
        if msg_id in self._list_ids and "result" in msg:
            self._list_ids.discard(msg_id)
            msg["result"]["tools"] = [t for t in msg["result"].get("tools", []) if t.get("name") in self.allowed_tools]
        if msg_id in self._pending and msg.get("result", {}).get("isError"):
            self.audit(self.fail_event, {"code": self._pending[msg_id][:500]})
        self._pending.pop(msg_id, None)
        if "method" not in msg:
            with self._idle:
                self._outstanding.discard(msg_id)
                self._idle.notify_all()
        return msg

    def _send_upstream(self, msg: dict) -> None:
        with self._lock:
            assert self._upstream_in is not None
            self._upstream_in.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._upstream_in.flush()

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
        proc = subprocess.Popen(self.upstream, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                env={**os.environ, **self.env}, bufsize=1)
        assert proc.stdin and proc.stdout
        self._upstream_in = proc.stdin

        def pump() -> None:
            for line in proc.stdout:
                if not line.strip():
                    continue
                try:
                    out = self.from_upstream(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if out is not None:
                    self._write(stdout, out)
            # 上游退出了（启动参数错、依赖没装）：在等的请求立刻回错误，不让 agent 干等到连接超时还看不出原因
            self._upstream_dead = f"上游 MCP 已退出（退出码 {proc.wait()}），请检查配置或在汇报里写明卡点"
            with self._idle:
                waiting, self._outstanding = set(self._outstanding), set()
                self._idle.notify_all()
            for msg_id in waiting:
                self._write(stdout, {"jsonrpc": "2.0", "id": msg_id,
                                     "error": {"code": -32000, "message": self._upstream_dead}})

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            for line in stdin:
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                reply = self.from_client(msg)
                if reply is None and self._upstream_dead and "id" in msg:
                    reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": self._upstream_dead}}
                if reply is not None:
                    self._write(stdout, reply)
                    continue
                if "id" in msg and "method" in msg:
                    with self._idle:
                        self._outstanding.add(msg["id"])
                self._send_upstream(msg)
        finally:
            with self._idle:  # agent 那头先关了：等已经转出去的请求回完，再关上游
                self._idle.wait_for(lambda: not self._outstanding, timeout=self.wait_timeout_sec())
            with self._lock:
                proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            reader.join(timeout=2)
        return 0


def main(argv: list[str] | None = None) -> int:
    from .config import load_dotenv, load_settings
    from .store import Store
    parser = argparse.ArgumentParser(prog="ecom-twin-computer-use")
    parser.add_argument("--task", required=True)
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    load_dotenv()
    settings = load_settings(args.config)
    cfg = settings.computer_use
    found = find_cua(cfg) if cfg.enabled else None
    if not found:
        print(availability(cfg)[1], file=sys.stderr)
        return 1
    store = Store(settings.db_path)

    def audit(action: str, detail: dict) -> None:
        store.audit("task", args.task, action, "agent", detail)

    return Proxy([found["command"], *found["args"]], found["env"], cfg, audit, args.task).serve()


if __name__ == "__main__":
    sys.exit(main())
