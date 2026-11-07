"""代码在哪、数据在哪。只有这一个文件决定，其它模块一律从这里取（对照数字分身 desktop/paths.mjs）。

两类东西分开放，升级时整包替换代码，数据一个字节都不能在包里：

- RESOURCE_ROOT：随版本发的只读资源 —— skills/、config/adapters、config/compliance.toml、配置示例、记忆库初始内容
- DATA_HOME：每个人自己的数据 —— config/settings.toml、settings.console.toml、.env、data/（数据库、证据、
  工作区、存档、浏览器登录状态、自动安装的工具）、memory/

同一份代码有两种跑法：
1. 仓库里直接跑（开发）：两者都是仓库根目录，和以前完全一样
2. 装好的版本：数据在 ~/Library/Application Support/EcomTwin/，资源由安装包通过 ECOM_TWIN_RESOURCES 指定

ECOM_TWIN_HOME / ECOM_TWIN_RESOURCES 环境变量可以覆盖默认位置；launchd 服务和 MCP 子进程都继承这两个变量。
目录名「EcomTwin」是磁盘身份，装好之后不要改：改了等于让已经装好的人丢掉全部数据。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_DIR_NAME = "EcomTwin"
_SOURCE_ROOT = Path(__file__).resolve().parents[2]


def in_checkout() -> bool:
    """是不是从仓库直接跑（src/ 布局，旁边有 pyproject.toml）。"""
    return (_SOURCE_ROOT / "pyproject.toml").exists() and (_SOURCE_ROOT / "src" / "ecom_twin").is_dir()


def _resource_root() -> Path:
    if os.environ.get("ECOM_TWIN_RESOURCES"):
        return Path(os.path.expanduser(os.environ["ECOM_TWIN_RESOURCES"])).resolve()
    return _SOURCE_ROOT


def _data_home() -> Path:
    if os.environ.get("ECOM_TWIN_HOME"):
        return Path(os.path.expanduser(os.environ["ECOM_TWIN_HOME"])).resolve()
    if in_checkout():
        return _SOURCE_ROOT
    return Path.home() / "Library" / "Application Support" / APP_DIR_NAME


RESOURCE_ROOT = _resource_root()
DATA_HOME = _data_home()

# ---- 每个人自己的数据 ----
CONFIG_DIR = DATA_HOME / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.toml"
ENV_FILE = DATA_HOME / ".env"
DATA_DIR = DATA_HOME / "data"
WORKSPACES_DIR = DATA_DIR / "workspaces"
EVIDENCE_DIR = DATA_DIR / "evidence"
BROWSER_PROFILES_DIR = DATA_DIR / "browser-profiles"
TOOLS_DIR = DATA_DIR / "tools"
LOG_DIR = DATA_DIR / "logs"
MEMORY_DIR = DATA_HOME / "memory"

# ---- 随版本发的资源 ----
SKILLS_DIR = RESOURCE_ROOT / "skills"
ADAPTERS_DIR = RESOURCE_ROOT / "config" / "adapters"
COMPLIANCE_FILE = RESOURCE_ROOT / "config" / "compliance.toml"
SETTINGS_EXAMPLE = RESOURCE_ROOT / "config" / "settings.example.toml"
MEMORY_SEED_DIR = RESOURCE_ROOT / "memory"


def ensure_data_home() -> Path:
    """建好数据目录（只有本人可读写）；记忆库还没有时，用随版本发的初始内容（大促日历等）垫一份。

    只补缺，不覆盖：本人改过的记忆、配置，不能因为又跑了一次就被恢复成初始值。
    """
    for directory in (DATA_HOME, CONFIG_DIR, DATA_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    if DATA_HOME != RESOURCE_ROOT:
        os.chmod(DATA_HOME, 0o700)
        if not MEMORY_DIR.exists() and MEMORY_SEED_DIR.is_dir():
            shutil.copytree(MEMORY_SEED_DIR, MEMORY_DIR)
    return DATA_HOME
