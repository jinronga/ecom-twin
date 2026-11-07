"""专用 Chrome 的操作代理：agent ⇄ 本代理 ⇄ chrome-devtools-mcp（对照数字分身：它的 agent 在专用 Chrome 里能读、能点、能填）。

负责人 2026-10-30 决定内部平台（海外仓、OA 审批等）的网页和数字分身一样全部放开：跳转、点击、填写、执行脚本。
卖货店铺后台、广告后台仍然只走官方 API / ERP API（DESIGN.md 13），所以夹一层代理兜底：
- 只放行 INTERACTIVE_TOOLS（不给上传本机文件、性能分析、设备模拟这类和任务无关的工具）
- navigate_page / new_page 的网址、evaluate_script 的脚本里出现禁止项（[browser] interactive_blocked）直接拒绝
- 每个会改变页面的调用写审计（任务、工具、参数），事后能查 agent 在网页上做过什么
对外生效、不可撤回的动作（提交单据、审批、删除）停在最后一步问负责人 —— 这一条靠提示词，代理分不清哪个按钮是提交。

已知缺口：代理只看网址和脚本文本。负责人自己在这个 Chrome 里开着卖货后台的标签页时，agent select_page 过去再点击拦不住，
所以专用 Chrome 里不要登录卖货后台。

运行：python -m ecom_twin.chrome_guard --task T-0001 --workdir <任务工作目录> [--config settings.toml]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .computer_use import PRE_INIT_PROBES, Proxy, _tool_error, blocked_keyword

READONLY_TOOLS = ("take_screenshot", "take_snapshot", "list_pages", "select_page", "wait_for")
# 会改变页面或浏览器状态的：每次都写审计
ACTION_TOOLS = ("navigate_page", "new_page", "close_page", "click", "fill", "fill_form", "hover", "press_key",
                "type_text", "drag", "handle_dialog", "evaluate_script")
INSPECT_TOOLS = ("list_network_requests", "get_network_request", "list_console_messages", "get_console_message")
INTERACTIVE_TOOLS = READONLY_TOOLS + ACTION_TOOLS + INSPECT_TOOLS
# 参数里哪些字段装着要检查的网址或脚本
_CHECKED_ARGS = {"navigate_page": ("url",), "new_page": ("url",), "evaluate_script": ("function", "script")}


def upstream_command(browser: Any, workdir: Path) -> list[str]:
    """连到专用 Chrome 的 chrome-devtools-mcp 命令（本机装了就用，没装用 npx）。

    只给 --browserUrl：Chrome 由 `ecom-twin browser start` 用 chrome_path / user_data_dir 启动好了，MCP 只管连上去。
    chrome-devtools-mcp 把 --userDataDir、--executablePath 当成「自己启动一个 Chrome」，和 --browserUrl 同时给会直接退出
    （Arguments userDataDir and browserUrl are mutually exclusive）。"""
    binary = shutil.which("chrome-devtools-mcp")
    cmd = [binary] if binary else ["npx", "-y", "chrome-devtools-mcp@latest"]
    return cmd + ["--browserUrl", browser.browser_url, "--workspace", str(workdir), "--no-usage-statistics"]


DEFAULT_PROFILE = Path.home() / ".agent-chrome"  # 和 evidence.start_chrome 的默认用户目录一致


def _flag(args: str, name: str) -> str:
    """从进程参数里取 --name=value（值里可能有空格，取到下一个 -- 参数为止）。"""
    marker = f"--{name}="
    if marker not in args:
        return ""
    return args.split(marker, 1)[1].split(" --", 1)[0].strip()


def verify_dedicated(browser: Any, run=subprocess.run) -> str | None:
    """确认 browser_url 那个端口上是专用 Chrome（用配置的 user_data_dir 启动的），不是负责人的日常 Chrome。
    是就返回 None，否则返回原因。

    为什么要查：端口被占时 `browser start` 会往后换一个端口，配置却还指着原来的端口；负责人的日常 Chrome
    开了远程调试也会占着 9222（2026-11-02 本机就是）。不查的话 agent 会在负责人登录着各种账号的日常浏览器里点来点去。"""
    port = urlsplit(browser.browser_url).port or 80
    try:
        pids = run(["/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                   capture_output=True, text=True, timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError) as e:
        return f"查不了端口 {port} 上的进程（{e}）"
    if not pids:
        return f"端口 {port} 上没有 Chrome，专用浏览器没启动（运行 ecom-twin browser start）"
    expected = Path(browser.user_data_dir).expanduser() if browser.user_data_dir else DEFAULT_PROFILE
    for pid in pids:
        try:
            args = run(["/bin/ps", "-o", "args=", "-p", pid], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        profile = _flag(args, "user-data-dir")
        if profile and Path(profile).expanduser().resolve() == expected.resolve():
            return None
        found = profile or "没有指定用户目录（像是日常 Chrome）"
        return (f"端口 {port} 上的 Chrome 不是专用浏览器（它的用户目录：{found}；配置的专用目录：{expected}），"
                "为了不碰负责人的日常浏览器，这一轮不挂浏览器")
    return f"端口 {port} 上的进程查不到启动参数"


class ChromeGuard(Proxy):
    allowed_tools = INTERACTIVE_TOOLS
    fail_event = "browser_action_failed"

    def wait_timeout_sec(self) -> float:
        return float(self.cfg.timeout_sec)

    def from_client(self, msg: dict) -> dict | None:
        method, msg_id = msg.get("method"), msg.get("id")
        if method in PRE_INIT_PROBES:  # 同 computer_use：新版 Claude Code 的探测由代理答「不支持」
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        if method == "tools/list":
            self._list_ids.add(msg_id)
        elif method == "tools/call":
            params = msg.get("params") or {}
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in self.allowed_tools:
                return _tool_error(msg_id, f"工具 {name} 没有开放")
            text = " ".join(str(args.get(k) or "") for k in _CHECKED_ARGS.get(name, ()))
            hit = blocked_keyword(text, self.cfg.interactive_blocked) if text else None
            if hit:
                self.audit("browser_blocked", {"tool": name, "keyword": hit, "args": _clip(args)})
                return _tool_error(msg_id, f"拦截：目标命中禁止项「{hit}」。卖货店铺后台、广告后台只走官方 API 和提案，"
                                           "不能在浏览器里操作。换别的办法，或在汇报里写明卡点")
            if name in ACTION_TOOLS:
                self._pending[msg_id] = json.dumps({"tool": name, **_clip(args)}, ensure_ascii=False)
                self.audit("browser_action", {"tool": name, "args": _clip(args)})
        return None


def _clip(args: dict) -> dict:
    """审计里的参数截短；fill 填的值可能是业务数据，照记（密码类不该由 agent 填，提示词已禁止）。"""
    return {k: (v[:500] if isinstance(v, str) else v) for k, v in args.items()}


def main(argv: list[str] | None = None) -> int:
    from .config import load_dotenv, load_settings
    from .store import Store
    parser = argparse.ArgumentParser(prog="ecom-twin-chrome-guard")
    parser.add_argument("--task", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    load_dotenv()
    settings = load_settings(args.config)
    store = Store(settings.db_path)

    def audit(action: str, detail: dict) -> None:
        store.audit("task", args.task, action, "agent", detail)

    upstream = upstream_command(settings.browser, Path(args.workdir))
    return ChromeGuard(upstream, {}, settings.browser, audit, args.task).serve()


if __name__ == "__main__":
    sys.exit(main())
