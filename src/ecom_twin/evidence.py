"""证据截图收集：为浏览器 MCP 和桌面截图提供安全的文件落点。

截图由现成工具产生；本模块只负责准备唯一文件名、校验 PNG 并复制到任务证据目录。
"""
from __future__ import annotations

import binascii
import os
import shutil
import struct
import subprocess
import sys
import socket
from pathlib import Path

from .config import PROJECT_ROOT

MAX_BYTES = 20 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EVIDENCE_ROOT = PROJECT_ROOT / "data" / "evidence"
STAGING_ROOT = PROJECT_ROOT / "data" / "workspaces"


def _safe_name(name: str) -> str:
    name = Path(name).name
    if not name or name in {".", ".."} or any(c in name for c in "\x00/\\"):
        raise ValueError("文件名无效")
    return name


def prepare(task_id: str, name: str = "screenshot.png", root: Path = EVIDENCE_ROOT) -> Path:
    """返回一个尚不存在的截图路径；不会创建可被覆盖的目标文件。"""
    if not task_id or any(c in task_id for c in "/\\\x00"):
        raise ValueError("任务 ID 无效")
    filename = _safe_name(name)
    if Path(filename).suffix.lower() != ".png":
        raise ValueError("证据截图必须使用 .png")
    directory = Path(root) / task_id
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if candidate.exists() or candidate.is_symlink():
        stem = candidate.stem
        for index in range(2, 10000):
            candidate = directory / f"{stem}-{index}.png"
            if not candidate.exists() and not candidate.is_symlink():
                break
        else:
            raise RuntimeError("截图目录中文件过多")
    return candidate


def final_path(task_id: str, name: str = "screenshot.png", root: Path = EVIDENCE_ROOT) -> Path:
    """返回统一证据目录中的不覆盖目标路径。"""
    return prepare(task_id, name, root=root)


def validate_png(path: Path) -> tuple[int, int]:
    """校验签名、IHDR/IEND、尺寸和每个 chunk CRC，拒绝伪造扩展名文件。"""
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError("截图文件不存在或不是普通文件")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("截图超过 20 MB")
    data = path.read_bytes()
    if len(data) < 33 or data[:8] != PNG_SIGNATURE:
        raise ValueError("不是完整 PNG 文件")
    offset, dimensions, saw_iend = 8, None, False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("PNG chunk 不完整")
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length:end])[0]
        if (binascii.crc32(kind + body) & 0xffffffff) != crc:
            raise ValueError("PNG 校验失败")
        if kind == b"IHDR":
            if length != 13 or dimensions is not None:
                raise ValueError("PNG IHDR 无效")
            width, height = struct.unpack(">II", body[:8])
            if not width or not height:
                raise ValueError("PNG 尺寸无效")
            dimensions = (width, height)
        elif kind == b"IEND":
            if length != 0 or saw_iend:
                raise ValueError("PNG IEND 无效")
            saw_iend = True
            if end != len(data):
                raise ValueError("PNG 在 IEND 后还有数据")
            break
        offset = end
    if dimensions is None or not saw_iend:
        raise ValueError("PNG 缺少 IHDR 或 IEND")
    return dimensions


def collect(source: Path, destination: Path) -> Path:
    """校验来源并以不覆盖方式复制到统一证据目录。"""
    source, destination = Path(source), Path(destination)
    validate_png(source)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"目标已存在：{destination}")
    if destination.suffix.lower() != ".png":
        raise ValueError("目标文件必须是 .png")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        with source.open("rb") as source_stream, temporary.open("xb") as destination_stream:
            shutil.copyfileobj(source_stream, destination_stream)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    validate_png(destination)
    return destination


def desktop(window_id: str, destination: Path, box: tuple[int, int, int, int] | None = None) -> Path:
    """截取指定 macOS 窗口；可选用 Pillow 在截图上画红框。"""
    if not window_id.isdigit() or int(window_id) <= 0:
        raise ValueError("窗口 ID 必须是正整数")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"目标已存在：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["/usr/sbin/screencapture", "-l", window_id, "-o", "-x", str(destination)],
                       check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise RuntimeError("桌面截图失败，请确认 macOS 已授予本终端「屏幕录制」权限和窗口 ID 有效") from error
    try:
        validate_png(destination)
        if box:
            x, y, width, height = box
            if width <= 0 or height <= 0 or x < 0 or y < 0:
                raise ValueError("标注框坐标无效")
            try:
                from PIL import Image, ImageDraw
            except ImportError as error:
                # 截图本身仍可交付；调用方应在 issue/汇报里说明未标注原因。
                print("截图已保存，但 Pillow 不可用，未添加红框标注；原图已保留。", file=sys.stderr)
                return destination
            with Image.open(destination) as image:
                draw = ImageDraw.Draw(image)
                draw.rectangle((x, y, x + width, y + height), outline=(220, 30, 30), width=6)
                temporary = destination.with_name(f".{destination.name}.marked-{os.getpid()}.png")
                image.save(temporary, format="PNG")
            os.replace(temporary, destination)
            validate_png(destination)
    except Exception:
        # 原图仍保留，便于交付脱敏后的未标注证据并在任务里说明原因。
        if not destination.exists():
            raise
        raise
    return destination


def start_chrome(chrome_path: str = "", user_data_dir: str = "", port: int = 9222,
                 url: str = "about:blank") -> int:
    """启动隔离的专用 Chrome；不复用日常用户目录，返回实际调试端口。"""
    executable = chrome_path or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not 1 <= int(port) <= 65535:
        raise ValueError("调试端口无效")
    if not Path(executable).is_file():
        raise ValueError(f"找不到 Chrome 可执行文件：{executable}")
    # 普通 Chrome 可能已经占用默认端口；向后找一个本机空闲端口，避免把
    # Agent MCP 错接到日常浏览器。
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                break
        port += 1
        if port > 65535:
            raise RuntimeError("找不到可用的 Chrome 调试端口")
    profile = Path(user_data_dir).expanduser() if user_data_dir else Path.home() / ".agent-chrome"
    profile.mkdir(parents=True, exist_ok=True)
    args = ["-na", "Google Chrome", "--args", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--new-window", url]
    if chrome_path:
        # 自定义可执行文件无法通过 open -na 选择；仍保持独立 profile。
        args = [executable, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                "--new-window", url]
    subprocess.Popen(["/usr/bin/open", *args], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return port

