"""原生窗口取证（对照数字分身 window-capture / evidence-image）：按窗口截、不激活；红框按屏幕坐标换算；禁止项窗口不列。"""

import subprocess

import pytest

from ecom_twin import desktop
from ecom_twin.desktop import DesktopError, annotate, capture, list_windows
from ecom_twin.evidence import validate_png

LISTING = ("101\tTOPWMS\t100\t50\t800\t600\t备货单\n"
           "102\t1Password\t0\t0\t900\t700\tVault\n"
           "103\tFinder\t10\t10\t400\t300\tDownloads\n")


def png(path, size=(1600, 1200)):
    from PIL import Image
    Image.new("RGB", size, (255, 255, 255)).save(path, format="PNG")


def fake_run(calls, size=(1600, 1200), fail=False):
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0].endswith("screencapture"):
            if fail:
                raise subprocess.CalledProcessError(1, cmd)
            png(cmd[-1], size)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, LISTING, "")
    return run


def test_list_windows_hides_blocked_and_filters_app(tmp_path):
    calls = []
    windows = list_windows(blocked=["1password"], run=fake_run(calls), helper=tmp_path / "h")
    assert [w.app for w in windows] == ["TOPWMS", "Finder"]
    assert [w.window_id for w in list_windows("finder", run=fake_run(calls), helper=tmp_path / "h")] == [103]


def test_capture_by_window_never_activates_and_marks_screen_boxes(tmp_path):
    calls = []
    out = capture(tmp_path / "evidence", app="TOPWMS", screen_boxes=[[300, 150, 100, 40]],
                  run=fake_run(calls), helper=tmp_path / "h")
    shot = [c for c in calls if c[0].endswith("screencapture")]
    assert shot == [["/usr/sbin/screencapture", "-l101", "-o", "-x", out["screenshot"]]]
    assert not any("activate" in " ".join(c) for c in calls)
    assert out["window"]["window_id"] == 101 and out["image_size"] == [1600, 1200]
    assert validate_png(out["marked"]) == (1600, 1200)
    from PIL import Image
    with Image.open(out["marked"]) as marked, Image.open(out["screenshot"]) as original:
        # 屏幕点 (300,150) 相对窗口 (100,50) 是 (200,100)，Retina 2 倍 → 像素 (400,200)
        assert marked.getpixel((400, 220)) == desktop.BOX_COLOR
        assert original.getpixel((400, 220)) == (255, 255, 255)  # 原图不动


def test_capture_refuses_blocked_window_and_lists_candidates(tmp_path):
    with pytest.raises(DesktopError, match="TOPWMS\\(101\\)"):
        capture(tmp_path, window_id=102, blocked=["1password"], run=fake_run([]), helper=tmp_path / "h")
    with pytest.raises(ValueError):
        capture(tmp_path, run=fake_run([]), helper=tmp_path / "h")


def test_capture_failure_retries_once_then_explains(tmp_path):
    calls = []
    with pytest.raises(DesktopError, match="窗口还在屏幕上但截不到"):
        capture(tmp_path / "evidence", app="Finder", run=fake_run(calls, fail=True), helper=tmp_path / "h",
                sleep=lambda s: None)
    assert len([c for c in calls if c[0].endswith("screencapture")]) == 2      # 失败后自动重试一次
    assert not list((tmp_path / "evidence").glob("*.png"))


def test_capture_failure_when_window_is_gone(tmp_path):
    """截图失败后窗口已经不在列表里（被最小化、移到别的桌面）：直接说窗口没了，不说权限。"""
    state = {"listed": 0}
    def run(cmd, **kwargs):
        if cmd[0].endswith("screencapture"):
            raise subprocess.CalledProcessError(1, cmd)
        state["listed"] += 1
        return subprocess.CompletedProcess(cmd, 0, LISTING if state["listed"] == 1 else "", "")
    with pytest.raises(DesktopError, match="已经不在屏幕上") as e:
        capture(tmp_path / "evidence", app="Finder", run=run, helper=tmp_path / "h", sleep=lambda s: None)
    assert "屏幕录制" not in str(e.value)


def test_capture_succeeds_on_retry(tmp_path):
    attempts = {"n": 0}
    def run(cmd, **kwargs):
        if cmd[0].endswith("screencapture"):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise subprocess.CalledProcessError(1, cmd)
            png(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, LISTING, "")
    out = capture(tmp_path / "evidence", app="Finder", run=run, helper=tmp_path / "h", sleep=lambda s: None)
    assert validate_png(out["screenshot"]) == (1600, 1200)


def test_annotate_rejects_box_outside_image(tmp_path):
    source = tmp_path / "a.png"
    png(source, (200, 100))
    with pytest.raises(ValueError, match="不在截图范围"):
        annotate(source, [[500, 500, 10, 10]], tmp_path / "b.png")
    assert validate_png(annotate(source, [[10, 10, 50, 30]], tmp_path / "c.png")) == (200, 100)


def test_desktop_tools_only_with_applescript(env, tmp_path, monkeypatch):
    from ecom_twin.memory import MemoryStore
    from ecom_twin.mcp_server import ToolServer, tool_names
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, env.task.id)
    assert "desktop_capture" not in server.tools and "desktop_capture" not in tool_names()
    # 画红框对所有截图开放（网页取证、专用浏览器的截图也要能标重点），不跟着 AppleScript 开关
    assert "annotate_screenshot" in server.tools and "annotate_screenshot" in tool_names()

    env.app.settings.computer_use.applescript = True
    evidence_dir = tmp_path / "ws" / "evidence"
    server = ToolServer(env.app, MemoryStore(env.app.store, tmp_path), env.miaoshou, env.task.id,
                        evidence_dir=evidence_dir)
    assert [t for t in tool_names(desktop=True) if t.startswith(("desktop_", "annotate_"))] == \
        ["desktop_windows", "desktop_capture", "annotate_screenshot"]
    assert set(tool_names(desktop=True)) == set(server.tools)
    monkeypatch.setattr(desktop, "_helper", lambda: tmp_path / "h")
    monkeypatch.setattr(desktop.subprocess, "run", fake_run([]))
    text, err = server.call("desktop_capture", {"app": "TOPWMS"})
    assert not err and str(evidence_dir) in text
    assert "desktop_capture" in env.app.store.audit_events(env.task.id)
    outside = tmp_path / "other.png"
    png(outside, (100, 100))
    text, err = server.call("annotate_screenshot", {"path": str(outside), "boxes": [[1, 1, 20, 20]]})
    assert err and "证据目录" in text


def test_new_path_adds_png_suffix_and_never_overwrites(tmp_path):
    first = desktop.new_path(tmp_path / "evidence", "shot")
    assert first.name == "shot.png"
    first.write_bytes(b"x")
    assert desktop.new_path(tmp_path / "evidence", "shot.png").name == "shot-2.png"
