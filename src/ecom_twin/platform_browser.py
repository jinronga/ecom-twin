"""内部平台浏览器操作（海外仓、OA 审批等）。卖货店铺后台不在此列（DESIGN.md 13.1）。

- 每个平台一个独立的浏览器资料目录，人工用 `ecom-twin browser-login <平台>` 登录一次，之后复用登录状态；
  分身不接触账号密码
- open_page：只读浏览，返回页面文字、可跳转链接、表单字段、按钮、截图。不点击、不填写
- run_steps：回放一串操作步骤（填写、选择、勾选、点击）。只给执行器用，且只回放审批通过的提案
- 所有跳转都校验域名白名单；跳到登录页视为登录失效
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

from .config import PROJECT_ROOT, PlatformSettings, is_store_backend

PROFILE_ROOT = PROJECT_ROOT / "data" / "browser-profiles"
STEP_OPS = ("goto", "fill", "select", "check", "click", "wait_text")


class PlatformError(ValueError):
    """内部平台浏览器操作失败。"""


class PlatformLoginRequired(PlatformError):
    """平台登录已失效。"""


@dataclass
class PageSnapshot:
    url: str
    title: str
    text: str
    links: list[dict[str, str]] = field(default_factory=list)
    fields: list[dict[str, str]] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    screenshot: str = ""


def check_url(platform: PlatformSettings, url: str) -> str:
    """把相对地址补全成完整地址，并校验域名白名单。"""
    url = urljoin(platform.base_url, url)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PlatformError(f"只能打开 http(s) 网页：{url}")
    if is_store_backend(url):
        raise PlatformError("卖货店铺后台不允许由分身用浏览器操作")
    host = parsed.hostname.lower()
    if not any(host == d or host.endswith("." + d) for d in platform.allowed_domains):
        raise PlatformError(f"{host} 不在平台 {platform.key} 的域名白名单内")
    return url


def validate_steps(platform: PlatformSettings, steps: list[dict[str, Any]]) -> list[str]:
    """静态检查操作步骤，返回问题列表（空表示通过）。"""
    problems = []
    if not steps:
        problems.append("没有操作步骤")
        return problems
    if len(steps) > platform.max_steps:
        problems.append(f"步骤数 {len(steps)} 超过平台上限 {platform.max_steps}")
    for i, step in enumerate(steps, 1):
        op = step.get("op")
        if op not in STEP_OPS:
            problems.append(f"第 {i} 步操作 {op} 不支持，只能是 {', '.join(STEP_OPS)}")
            continue
        if op == "goto":
            try:
                check_url(platform, step.get("url", ""))
            except PlatformError as e:
                problems.append(f"第 {i} 步：{e}")
        elif op == "wait_text":
            if not step.get("text"):
                problems.append(f"第 {i} 步 wait_text 缺少 text")
        elif not (step.get("label") or step.get("selector") or step.get("text")):
            problems.append(f"第 {i} 步 {op} 要指定 label、selector 或 text 之一")
        if op in ("fill", "select") and "value" not in step:
            problems.append(f"第 {i} 步 {op} 缺少 value")
    return problems


def describe_step(step: dict[str, Any]) -> str:
    """审批卡片上给人看的一行。"""
    target = step.get("label") or step.get("text") or step.get("selector") or step.get("url", "")
    op = step.get("op")
    mark = "（提交）" if step.get("submit") else ""
    return {
        "goto": f"打开 {step.get('url')}",
        "fill": f"在「{target}」填写：{step.get('value')}",
        "select": f"在「{target}」选择：{step.get('value')}",
        "check": f"勾选「{target}」",
        "click": f"点击「{target}」{mark}",
        "wait_text": f"等待页面出现「{step.get('text')}」",
    }.get(op, str(step))


@contextmanager
def _profile_lock(profile: Path, timeout: float = 120) -> Iterator[None]:
    """同一个平台的浏览器资料同时只能被一个进程使用（MCP server 和执行器是不同进程）。"""
    profile.mkdir(parents=True, exist_ok=True)
    with open(profile.parent / f".{profile.name}.lock", "w") as fh:
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise PlatformError(f"平台 {profile.name} 的浏览器正被占用，稍后再试")
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class PlatformBrowser:
    """在内部平台的登录会话里操作浏览器。"""

    def __init__(self, platform: PlatformSettings, profile_root: Path = PROFILE_ROOT, headless: bool = True,
                 chrome_path: str = "", timeout_ms: int = 30000):
        if is_store_backend(platform.base_url):
            raise PlatformError("卖货店铺后台不允许由分身用浏览器操作")
        self.platform = platform
        self.profile = profile_root / platform.key
        self.headless = headless
        self.chrome_path = chrome_path
        self.timeout_ms = timeout_ms

    @contextmanager
    def _page(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise PlatformError("没有安装 playwright：pip install -e '.[browser]'") from e
        with _profile_lock(self.profile), sync_playwright() as pw:
            kwargs: dict[str, Any] = {"headless": self.headless, "viewport": {"width": 1366, "height": 900}}
            if self.chrome_path:
                kwargs["executable_path"] = self.chrome_path
            else:
                kwargs["channel"] = "chrome"
            ctx = pw.chromium.launch_persistent_context(str(self.profile), **kwargs)
            ctx.set_default_timeout(self.timeout_ms)
            # 很多内部系统用会话 cookie（没有过期时间），浏览器一关就丢；这里每次用完存下来，下次启动再放回去
            state = self.profile / "ecom-twin-cookies.json"
            if state.exists():
                try:
                    ctx.add_cookies(json.loads(state.read_text(encoding="utf-8"))["cookies"])
                except (ValueError, KeyError, OSError):
                    pass
            try:
                yield ctx.pages[0] if ctx.pages else ctx.new_page()
            finally:
                try:
                    state.write_text(json.dumps({"cookies": ctx.cookies()}), encoding="utf-8")
                    os.chmod(state, 0o600)
                finally:
                    ctx.close()

    def _goto(self, page, url: str) -> None:
        page.goto(check_url(self.platform, url), wait_until="domcontentloaded")
        self._guard(page)

    def _guard(self, page) -> None:
        """每次跳转后检查：没被重定向到白名单外，也没掉到登录页。"""
        current = page.url
        try:
            check_url(self.platform, current)
        except PlatformError as e:
            raise PlatformError(f"页面跳转到了白名单外：{current}（{e}）") from e
        if any(k in current.lower() for k in self.platform.login_url_keywords):
            raise PlatformLoginRequired(f"平台 {self.platform.name} 登录已失效，请运行 "
                                        f"`ecom-twin browser-login {self.platform.key}` 重新登录")

    def _snapshot(self, page, evidence_dir: Path | None) -> PageSnapshot:
        data = page.evaluate("""() => {
          const vis = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const labelOf = (el) => (el.labels && el.labels[0] && el.labels[0].innerText.trim())
            || el.getAttribute('aria-label') || el.placeholder || el.name || '';
          return {
            links: [...document.querySelectorAll('a[href]')].filter(vis).slice(0, 80)
              .map(a => ({text: a.innerText.trim().slice(0, 60), href: a.href})).filter(l => l.text),
            fields: [...document.querySelectorAll('input, select, textarea')].filter(vis)
              .filter(el => !['hidden', 'submit', 'button'].includes(el.type)).slice(0, 60)
              .map(el => ({label: labelOf(el), type: el.tagName.toLowerCase() === 'input' ? el.type : el.tagName.toLowerCase(),
                           name: el.name || '', value: el.type === 'password' ? '' : (el.value || '').slice(0, 80)})),
            buttons: [...document.querySelectorAll('button, input[type=submit], input[type=button]')].filter(vis)
              .slice(0, 40).map(b => (b.innerText || b.value || '').trim()).filter(Boolean),
          };
        }""")
        links = []
        for link in data["links"]:
            try:  # 只给出白名单内、可以安全跳转的链接
                check_url(self.platform, link["href"])
                links.append(link)
            except PlatformError:
                pass
        shot = ""
        if evidence_dir:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            shot = str(evidence_dir / f"{self.platform.key}-{int(time.time() * 1000)}.png")
            page.screenshot(path=shot, full_page=True)
        text = " ".join(page.inner_text("body").split())[:6000]
        return PageSnapshot(page.url, page.title(), text, links, data["fields"], data["buttons"], shot)

    # ---- 只读（agent 可用） ----
    def open_page(self, url: str, evidence_dir: Path | None = None) -> PageSnapshot:
        """打开页面并读取内容。不点击、不填写。"""
        with self._page() as page:
            self._goto(page, url)
            return self._snapshot(page, evidence_dir)

    def session_ok(self) -> bool:
        """登录状态是否还有效。"""
        try:
            self.open_page(self.platform.base_url)
            return True
        except PlatformLoginRequired:
            return False

    # ---- 写（只给执行器用） ----
    def run_steps(self, start_url: str, steps: list[dict[str, Any]], evidence_dir: Path) -> dict[str, Any]:
        """回放一串操作步骤。只回放审批通过的提案。"""
        problems = validate_steps(self.platform, steps)
        if problems:
            raise PlatformError("；".join(problems))
        log, shots = [], []
        with self._page() as page:
            self._goto(page, start_url)
            for i, step in enumerate(steps, 1):
                op = step["op"]
                if op == "click" and step.get("submit"):  # 提交前截图：留下填好的表单
                    shots.append(self._shot(page, evidence_dir, f"before-submit-{i}"))
                if op == "goto":
                    self._goto(page, step["url"])
                elif op == "wait_text":
                    page.get_by_text(step["text"]).first.wait_for()
                else:
                    loc = self._locate(page, step)
                    if op == "fill":
                        loc.fill(str(step["value"]))
                    elif op == "select":
                        loc.select_option(label=str(step["value"]))
                    elif op == "check":
                        loc.check()
                    elif op == "click":
                        loc.click()
                        page.wait_for_load_state("domcontentloaded")
                        self._guard(page)
                log.append(f"{i}. {describe_step(step)} ✓")
            shots.append(self._shot(page, evidence_dir, "after"))
            final = self._snapshot(page, None)
        return {"log": log, "screenshots": shots, "final_url": final.url, "final_text": final.text[:3000]}

    def _locate(self, page, step: dict[str, Any]):
        if step.get("label"):
            return page.get_by_label(step["label"], exact=True).first
        if step.get("selector"):
            return page.locator(step["selector"]).first
        role = page.get_by_role("button", name=step["text"], exact=True)
        return role.first if role.count() else page.get_by_text(step["text"], exact=True).first

    def _shot(self, page, evidence_dir: Path, tag: str) -> str:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path = str(evidence_dir / f"{self.platform.key}-{tag}-{int(time.time() * 1000)}.png")
        page.screenshot(path=path, full_page=True)
        return path

    # ---- 人工登录 ----
    def interactive_login(self, wait) -> None:
        """有界面地打开平台首页，等人登录完成（wait 返回）后关闭，登录状态留在资料目录里。"""
        self.headless = False
        with self._page() as page:
            page.goto(self.platform.base_url)
            wait()
