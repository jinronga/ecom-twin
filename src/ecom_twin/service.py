"""常驻服务交给 launchd（DESIGN.md 15、打包第 3 步）：登录后自启、异常退出自动拉起。

ecom-twin start / stop / restart / status / logs / uninstall 都在这里。只管当前用户的 LaunchAgent
（~/Library/LaunchAgents），不碰系统级服务；uninstall 只移除常驻，数据目录原样保留。

ProgramArguments 用的是运行这条命令的 Python（sys.executable）：仓库里跑就是 .venv 的，以后打包时换成包里
自带的那份，不用改这里。launchd 起的进程环境被清过，PATH 要显式给，否则找不到 claude / codex / npx。
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .paths import DATA_HOME, LOG_DIR, RESOURCE_ROOT

LABEL = "com.ecomtwin.serve"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
# 底座 CLI 常见的安装位置：Homebrew、npm 全局、Claude Code 自带安装器
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin"),
              str(Path.home() / ".npm-global" / "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]

Runner = Callable[..., subprocess.CompletedProcess]


def _domain() -> str:
    return f"gui/{os.getuid()}"


def plist_content(config: str | None = None, python: str | None = None) -> dict[str, Any]:
    args = [python or sys.executable, "-m", "ecom_twin.cli"] + (["--config", str(Path(config).resolve())] if config else [])
    path = os.pathsep.join(dict.fromkeys([*os.environ.get("PATH", "").split(os.pathsep), *EXTRA_PATH]))
    env = {"PATH": path, "PYTHONUNBUFFERED": "1"}
    for key in ("ECOM_TWIN_HOME", "ECOM_TWIN_RESOURCES"):  # 指定过数据 / 资源目录的，常驻进程也要用同一份
        if os.environ.get(key):
            env[key] = os.environ[key]
    return {
        "Label": LABEL,
        "ProgramArguments": args + ["serve"],
        "WorkingDirectory": str(DATA_HOME if DATA_HOME != RESOURCE_ROOT else RESOURCE_ROOT),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        # 只在异常退出时拉起：stop 是正常退出，不该马上又被拉起来
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,  # 配置错了反复崩溃时，30 秒才重试一次，不刷日志
        "StandardOutPath": str(LOG_DIR / "serve.log"),
        "StandardErrorPath": str(LOG_DIR / "serve.err.log"),
        "ProcessType": "Background",
    }


def install(config: str | None = None, plist: Path = PLIST) -> Path:
    """写 LaunchAgent 配置（内容没变就不动）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    data = plistlib.dumps(plist_content(config))
    plist.parent.mkdir(parents=True, exist_ok=True)
    if not plist.exists() or plist.read_bytes() != data:
        plist.write_bytes(data)
    return plist


def _loaded(run: Runner) -> bool:
    return run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True).returncode == 0


def start(config: str | None = None, run: Runner = subprocess.run, plist: Path = PLIST) -> str:
    install(config, plist)
    if _loaded(run):
        run(["launchctl", "kickstart", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
        return "常驻服务已在运行"
    result = run(["launchctl", "bootstrap", _domain(), str(plist)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"启动失败：{(result.stderr or result.stdout).strip()}")
    return "常驻服务已启动，登录后会自动运行，异常退出会自动拉起"


def stop(run: Runner = subprocess.run) -> str:
    if not _loaded(run):
        return "常驻服务没有在运行"
    result = run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"停止失败：{(result.stderr or result.stdout).strip()}")
    return "常驻服务已停止（下次登录还会自动启动；不想要了用 ecom-twin uninstall）"


def restart(config: str | None = None, run: Runner = subprocess.run, plist: Path = PLIST) -> str:
    install(config, plist)
    if not _loaded(run):
        return start(config, run, plist)
    result = run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"重启失败：{(result.stderr or result.stdout).strip()}")
    return "常驻服务已重启"


def status(run: Runner = subprocess.run, plist: Path = PLIST) -> dict[str, Any]:
    out = run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    info: dict[str, Any] = {"installed": plist.exists(), "loaded": out.returncode == 0}
    for line in out.stdout.splitlines() if out.returncode == 0 else []:
        key, _, value = line.strip().partition(" = ")
        if key in ("state", "pid", "last exit code", "runs"):
            info[key.replace(" ", "_")] = value.strip()
    return info


def logs(lines: int = 50) -> str:
    parts = []
    for name in ("serve.log", "serve.err.log"):
        path = LOG_DIR / name
        if path.exists():
            tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
            parts.append(f"== {path} ==\n" + "\n".join(tail))
    return "\n\n".join(parts) or f"还没有日志（{LOG_DIR}）"


def uninstall(run: Runner = subprocess.run, plist: Path = PLIST) -> str:
    if _loaded(run):
        run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    plist.unlink(missing_ok=True)
    return f"已移除常驻服务；数据目录 {DATA_HOME} 原样保留"
