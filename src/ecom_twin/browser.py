"""只读网页取证：打开公开页面，截图 + 提取文字（功能5 / DESIGN.md 13.1）。

只能「看」，不能「点」——用无头 Chrome 的 --screenshot / --dump-dom 一次性取页面，
本身就没有点击、填表、执行脚本的能力。另外：
- 每次用全新的临时浏览器资料目录，用完删除，不带任何登录状态
- 只允许白名单域名的 http(s) 页面；URL 含卖家后台、登录、结算等关键词一律拒绝
- 截图保存在任务证据目录下，作为汇报证据
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .config import BrowserSettings

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "chromium", "chromium-browser",
)
_TAGS = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

# 卖货店铺后台：永远不允许访问（DESIGN.md 13.1 账号风控）
STORE_BACKEND_MARKERS = (
    "sellercentral", "seller.tiktok", "seller-us.tiktok", "seller-uk.tiktok", "partner.tiktokshop",
    "seller.noon", "noon.partners", "sellercenter", "seller.shopee", "seller.lazada", "seller.walmart",
)


class BrowserError(ValueError):
    """浏览器取证失败。"""


@dataclass
class Capture:
    url: str
    title: str
    text: str
    screenshot: Path


def find_chrome(configured: str = "") -> str:
    """查找可用的 Chrome 可执行文件。"""
    for candidate in ([configured] if configured else []) + list(CHROME_CANDIDATES):
        path = candidate if Path(candidate).exists() else shutil.which(candidate)
        if path:
            return str(path)
    raise BrowserError("没有找到 Chrome，请在 [browser] chrome_path 配置路径")


def check_url(url: str, settings: BrowserSettings) -> str:
    """校验 URL 是否允许取证。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BrowserError(f"只能打开 http(s) 网页：{url}")
    host = parsed.hostname.lower()
    if not any(host == d or host.endswith("." + d) for d in settings.allowed_domains):
        raise BrowserError(f"域名 {host} 不在白名单内（[browser] allowed_domains）")
    lowered = url.lower()
    hit = next((k for k in settings.blocked_keywords if k in lowered), None)
    if hit:
        raise BrowserError(f"拒绝打开：URL 含「{hit}」，卖家后台、登录、结算等页面不允许分身访问")
    # 双重保险：店铺后台域名无论配置如何都拒绝
    if any(marker in host for marker in STORE_BACKEND_MARKERS):
        raise BrowserError(f"拒绝打开：{host} 是卖货店铺后台，分身不允许访问")
    return url


def page_text(dom: str, limit: int = 6000) -> tuple[str, str]:
    """从 DOM 中提取标题和纯文本。"""
    match = _TITLE.search(dom)
    title = html.unescape(match.group(1).strip()) if match else ""
    text = html.unescape(_TAGS.sub(" ", dom))
    text = re.sub(r"\s+", " ", text).strip()
    return title, text[:limit]


def capture_page(url: str, out_dir: Path, settings: BrowserSettings, width: int = 1366,
                 height: int = 2000) -> Capture:
    """一次性抓取公开页面：截图 + 文字。"""
    check_url(url, settings)
    chrome = find_chrome(settings.chrome_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    shot = out_dir / f"capture-{int(time.time() * 1000)}.png"
    profile = tempfile.mkdtemp(prefix="ecom-twin-chrome-")
    base = [chrome, "--headless=new", f"--user-data-dir={profile}", "--no-first-run", "--disable-extensions",
            "--disable-sync", "--hide-scrollbars", f"--window-size={width},{height}",
            "--virtual-time-budget=8000"]
    try:
        try:
            dom = subprocess.run(base + ["--dump-dom", url], capture_output=True, text=True,
                                 timeout=settings.timeout_sec).stdout
            subprocess.run(base + [f"--screenshot={shot}", url], capture_output=True,
                           timeout=settings.timeout_sec)
        except subprocess.TimeoutExpired as error:
            raise BrowserError(f"打开页面超时（{settings.timeout_sec:.0f} 秒）：{url}") from error
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if not shot.exists():
        raise BrowserError(f"截图失败：{url}")
    title, text = page_text(dom or "")
    return Capture(url, title, text, shot)
