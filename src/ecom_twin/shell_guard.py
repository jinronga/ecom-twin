"""AppleScript 方式的命令闸门：Claude Code 每次调 Bash 前先跑这里（PreToolUse hook），不合规就拦下。

Bash 只放行 osascript / open / cliclick（按命令前缀），可 AppleScript 自己能跑任意命令：`do shell script`、
JXA 的 doShellScript、`run script` 拼字符串绕过检查、ObjC 桥接直接调系统。原来只靠提示词禁止（PROGRESS 已知缺口），
现在按命令文本拦：命中就以退出码 2 结束，Claude Code 不执行这条命令，把 stderr 里的原因交给 agent。
卖货后台、广告后台、密码管理器、钥匙串这些禁止目标（[computer_use] blocked_keywords）同样拦。每次拦截写审计。

运行：python -m ecom_twin.shell_guard --task T-0001 [--config settings.toml]，stdin 是 hook 输入 JSON。
"""
from __future__ import annotations

import argparse
import json
import re
import sys

# 能让 AppleScript / JXA 跑任意 shell 命令或调系统接口的写法（大小写、空白都容忍）
ESCAPES = [
    (re.compile(r"do\s+shell\s+script", re.I), "do shell script"),
    (re.compile(r"doShellScript", re.I), "doShellScript（JXA）"),
    (re.compile(r"run\s+script", re.I), "run script（拼字符串绕过检查）"),
    (re.compile(r"\bObjC\b|\$\.NSTask|NSTask|NSAppleScript", re.I), "ObjC 桥接"),
    (re.compile(r"\bcurrent\s+application's\b", re.I), "AppleScriptObjC"),
]


def check(command: str, blocked_keywords: list[str]) -> str | None:
    """返回拦截原因；None 表示放行。"""
    for pattern, name in ESCAPES:
        if pattern.search(command):
            return (f"拦截：命令里有「{name}」，它能绕过白名单跑任意命令，不允许。"
                    "只用 System Events 读界面、点按钮，或者用软件自己的 AppleScript 字典读写数据")
    text = command.lower()
    hit = next((k for k in blocked_keywords if k and k.lower() in text), None)
    if hit:
        return (f"拦截：操作目标命中禁止项「{hit}」。卖货店铺后台、广告后台只走官方 API 和提案；"
                "密码管理器、钥匙串不能碰。换别的办法，或在汇报里写明卡点")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ecom-twin-shell-guard")
    parser.add_argument("--task", default="")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        print("拦截：读不懂这次调用的内容", file=sys.stderr)
        return 2
    if event.get("tool_name") != "Bash":
        return 0
    command = str((event.get("tool_input") or {}).get("command") or "")
    from .config import load_dotenv, load_settings
    load_dotenv()
    settings = load_settings(args.config)
    reason = check(command, settings.computer_use.blocked_keywords)
    if reason is None:
        return 0
    if args.task:
        from .store import Store
        Store(settings.db_path).audit("task", args.task, "shell_blocked", "agent",
                                      {"command": command[:2000], "reason": reason})
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
