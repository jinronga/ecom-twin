"""原生窗口取证（对照数字分身 window-capture.js / evidence-image.js）：列窗口、按窗口截图、画红框。

为什么由 ecom-twin 提供、不让 agent 自己 screencapture：
- 不抢焦点。agent 最容易想到的是先 `activate` 再截屏，会把负责人正在用的窗口抢走。
  `screencapture -l<CGWindowID>` 截的是窗口自己的内容，被挡着也完整，不用前置。
- 不截到无关内容。整屏截图会把负责人屏幕上别的东西（聊天、密码框）一起带进证据。
- CGWindowID 没有现成命令能拿到（AppleScript 的 window id 是应用自己的编号），要编译一个
  二十几行的 C 小工具，首次调用时用 clang 编译并缓存。只读窗口列表，不碰窗口顺序，也不需要辅助功能权限。
- 红框要 Pillow，agent 的 shell 跑不了 python。

截图需要给运行 ecom-twin 的程序授予「屏幕录制」权限。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .evidence import prepare, validate_png
from .paths import TOOLS_DIR

HELPER_SOURCE = r"""#include <ApplicationServices/ApplicationServices.h>
#include <stdio.h>
#include <string.h>
static void s(CFStringRef v, char *buf, size_t n) {
  buf[0] = 0;
  if (v) CFStringGetCString(v, buf, (CFIndex)n, kCFStringEncodingUTF8);
}
int main(int argc, char **argv) {
  CFArrayRef list = CGWindowListCopyWindowInfo(
      kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID);
  if (!list) return 1;
  for (CFIndex i = 0; i < CFArrayGetCount(list); i++) {
    CFDictionaryRef w = CFArrayGetValueAtIndex(list, i);
    char owner[256], name[512];
    s(CFDictionaryGetValue(w, kCGWindowOwnerName), owner, sizeof owner);
    s(CFDictionaryGetValue(w, kCGWindowName), name, sizeof name);
    int id = 0, layer = 0;
    CFNumberGetValue(CFDictionaryGetValue(w, kCGWindowNumber), kCFNumberIntType, &id);
    CFNumberGetValue(CFDictionaryGetValue(w, kCGWindowLayer), kCFNumberIntType, &layer);
    if (layer != 0) continue;
    CGRect r;
    CGRectMakeWithDictionaryRepresentation(CFDictionaryGetValue(w, kCGWindowBounds), &r);
    if (r.size.width < 200 || r.size.height < 100) continue;
    for (char *p = name; *p; p++) if (*p == '\t' || *p == '\n') *p = ' ';
    printf("%d\t%s\t%d\t%d\t%d\t%d\t%s\n", id, owner, (int)r.origin.x, (int)r.origin.y,
           (int)r.size.width, (int)r.size.height, name);
  }
  return 0;
}
"""

BOX_COLOR = (220, 30, 30)


class DesktopError(Exception):
    pass


def new_path(directory: Path, name: str) -> Path:
    """证据目录里一个还不存在的 .png 路径（重名自动加序号，不覆盖已有证据）。"""
    directory = Path(directory)
    if not name.lower().endswith(".png"):
        name += ".png"
    return prepare(directory.name, name, root=directory.parent)


@dataclass
class Window:
    window_id: int
    app: str
    x: int
    y: int
    width: int
    height: int
    title: str

    def as_dict(self) -> dict:
        return {"window_id": self.window_id, "app": self.app, "title": self.title,
                "bounds": [self.x, self.y, self.width, self.height]}


def _helper(bin_dir: Path = TOOLS_DIR / "bin") -> Path:
    """编译好的窗口查询小工具；源码变了换文件名，旧的不复用。"""
    digest = hashlib.sha256(HELPER_SOURCE.encode()).hexdigest()[:12]
    helper = bin_dir / f"window-list-{digest}"
    if os.access(helper, os.X_OK):
        return helper
    bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = helper.with_suffix(".c")
    source.write_text(HELPER_SOURCE, encoding="utf-8")
    try:
        subprocess.run(["/usr/bin/clang", "-O2", "-o", str(helper), str(source), "-framework", "ApplicationServices"],
                       check=True, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise DesktopError(f"编译窗口查询工具失败（{e}），需要 Xcode 命令行工具：xcode-select --install") from e
    finally:
        source.unlink(missing_ok=True)
    return helper


def _parse(stdout: str) -> list[Window]:
    windows = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        wid, app, x, y, w, h = parts[:6]
        windows.append(Window(int(wid), app, int(x), int(y), int(w), int(h), "\t".join(parts[6:])))
    return windows


def _blocked(window: Window, keywords: list[str]) -> bool:
    text = f"{window.app} {window.title}".lower()
    return any(k and k.lower() in text for k in keywords)


def list_windows(app: str = "", blocked: list[str] | None = None, run=None,
                 helper: Path | None = None) -> list[Window]:
    """屏幕上的普通窗口，按前后顺序（同一应用的第一条就是它最前面那个）。命中禁止项的不列出。"""
    run = run or subprocess.run
    out = run([str(helper or _helper())], check=True, capture_output=True, text=True, timeout=30).stdout
    windows = [w for w in _parse(out) if not _blocked(w, blocked or [])]
    if app:
        windows = [w for w in windows if w.app.lower() == app.lower()]
    return windows


def _scale_boxes(boxes: list, window: Window, image_size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    """屏幕坐标（System Events 的 position / size，单位是点）→ 截图像素。Retina 屏一个点是两个像素。"""
    sx, sy = image_size[0] / window.width, image_size[1] / window.height
    return [(round((x - window.x) * sx), round((y - window.y) * sy), round(w * sx), round(h * sy))
            for x, y, w, h in (_box(b) for b in boxes)]


def _box(box) -> tuple[int, int, int, int]:
    if not (isinstance(box, (list, tuple)) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
        raise ValueError("框写成 [x, y, 宽, 高] 四个数字")
    x, y, w, h = (int(round(v)) for v in box)
    if w <= 0 or h <= 0:
        raise ValueError("框的宽高必须大于 0")
    return x, y, w, h


def annotate(source: Path, boxes: list, destination: Path) -> Path:
    """在截图上画红框，另存一份；原图不动。框画在内侧并裁进图片范围，避免四边被切掉。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:
        raise DesktopError("没有 Pillow，画不了红框：pip install 'ecom-twin[evidence]'；原图仍可交付") from e
    width, height = validate_png(source)
    line = max(3, round(min(width, height) / 200))
    with Image.open(source) as image:
        image = image.convert("RGB")
        draw = ImageDraw.Draw(image)
        for x, y, w, h in (_box(b) for b in boxes):
            left, top = max(0, x), max(0, y)
            right, bottom = min(width - 1, x + w), min(height - 1, y + h)
            if right - left < line * 2 or bottom - top < line * 2:
                raise ValueError(f"框 {[x, y, w, h]} 不在截图范围内（截图 {width}x{height}）")
            draw.rectangle((left, top, right, bottom), outline=BOX_COLOR, width=line)
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}.png")
        image.save(temporary, format="PNG")
    os.replace(temporary, destination)
    validate_png(destination)
    return destination


def _why_capture_failed(window: Window, blocked, run, helper) -> str:
    """截图失败后再看一眼窗口还在不在屏幕上，按实际情况说原因 —— 只说「缺屏幕录制权限」会把人带偏。"""
    try:
        still = any(w.window_id == window.window_id for w in list_windows(blocked=blocked, run=run, helper=helper))
    except (OSError, subprocess.SubprocessError, DesktopError):
        still = None
    if still is False:
        return ("这个窗口已经不在屏幕上了：可能被最小化、关掉、移到了别的桌面（Space），或者软件换了一个新窗口。"
                "先用 desktop_windows 看现在有哪些窗口，用新的 window_id 再截")
    if still:
        return ("窗口还在屏幕上但截不到：多半是软件正在切页或重绘，等几秒再截；反复失败再请负责人确认"
                "「系统设置 → 隐私与安全性 → 屏幕录制」里给了运行 ecom-twin 的程序权限")
    return "确认运行 ecom-twin 的程序有「系统设置 → 隐私与安全性 → 屏幕录制」权限，窗口还开着"


def capture(directory: Path, *, app: str = "", window_id: int | None = None, name: str = "window.png",
            screen_boxes: list | None = None, blocked: list[str] | None = None, run=None,
            helper: Path | None = None, sleep=time.sleep) -> dict:
    """按应用名或窗口 ID 截一个窗口，不激活任何应用。给了屏幕坐标的框就另存一份带红框的。"""
    if not app and window_id is None:
        raise ValueError("app 和 window_id 至少给一个")
    run = run or subprocess.run
    windows = list_windows(blocked=blocked, run=run, helper=helper)
    if window_id is not None:
        matched = [w for w in windows if w.window_id == window_id]
    else:
        matched = [w for w in windows if w.app.lower() == app.lower()]
    if not matched:
        listed = "、".join(f"{w.app}({w.window_id})" for w in windows) or "无"
        raise DesktopError(f"没有找到{'窗口 ' + str(window_id) if window_id is not None else ' ' + app + ' 的窗口'}"
                           f"（禁止项的窗口不能截）。当前可截的：{listed}")
    window = matched[0]
    path = new_path(directory, name)
    error = None
    for attempt in range(2):  # Electron 窗口切页、重绘时会短暂截不到（2026-11-02 数字分身实测），等一下再试一次
        try:
            run(["/usr/sbin/screencapture", f"-l{window.window_id}", "-o", "-x", str(path)],
                check=True, capture_output=True, text=True, timeout=30)
            size = validate_png(path)
            break
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            path.unlink(missing_ok=True)
            error = e
            if attempt == 0:
                sleep(1.5)
    else:
        raise DesktopError(f"截图失败（{error}）。{_why_capture_failed(window, blocked, run, helper)}") from error
    result = {"screenshot": str(path), "window": window.as_dict(), "image_size": list(size)}
    if screen_boxes:
        marked = new_path(directory, f"{path.stem}-marked.png")
        result["marked"] = str(annotate(path, _scale_boxes(screen_boxes, window, size), marked))
    return result
