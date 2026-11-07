"""常驻服务（launchd）：配置内容、启停、状态解析。用假的 launchctl 和临时 plist，不碰真实的 LaunchAgents。"""

import plistlib
import subprocess
import sys

from ecom_twin import service

PRINT_RUNNING = """gui/501/com.ecomtwin.serve = {
	active count = 1
	state = running
	program = /x/python
	runs = 2
	pid = 4242
	last exit code = (never exited)
}"""


class FakeLaunchctl:
    def __init__(self, loaded=False):
        self.loaded, self.calls = loaded, []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd[1])
        if cmd[1] == "print":
            return subprocess.CompletedProcess(cmd, 0 if self.loaded else 113, PRINT_RUNNING if self.loaded else "", "")
        if cmd[1] == "bootstrap":
            self.loaded = True
        if cmd[1] == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_plist_runs_serve_with_this_python_and_is_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("ECOM_TWIN_HOME", "/tmp/eth")
    plist = service.install(str(tmp_path / "settings.toml"), plist=tmp_path / "x.plist")
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"][:3] == [sys.executable, "-m", "ecom_twin.cli"]
    assert data["ProgramArguments"][-1] == "serve" and "--config" in data["ProgramArguments"]
    assert data["KeepAlive"] == {"SuccessfulExit": False} and data["RunAtLoad"] is True
    assert "/opt/homebrew/bin" in data["EnvironmentVariables"]["PATH"]          # launchd 环境被清过，要找得到 claude / codex
    assert data["EnvironmentVariables"]["ECOM_TWIN_HOME"] == "/tmp/eth"
    assert subprocess.run(["plutil", "-lint", str(plist)], capture_output=True).returncode == 0


def test_start_stop_restart_uninstall(tmp_path):
    plist = tmp_path / "x.plist"
    ctl = FakeLaunchctl()
    assert "已启动" in service.start(run=ctl, plist=plist) and "bootstrap" in ctl.calls
    assert "已在运行" in service.start(run=ctl, plist=plist)                       # 重复 start 不再 bootstrap
    assert service.restart(run=ctl, plist=plist) == "常驻服务已重启"
    assert "已停止" in service.stop(run=ctl) and not ctl.loaded
    assert "没有在运行" in service.stop(run=ctl)
    assert "原样保留" in service.uninstall(run=ctl, plist=plist) and not plist.exists()


def test_status_parses_launchctl_print(tmp_path):
    plist = tmp_path / "x.plist"
    plist.write_bytes(b"")
    info = service.status(run=FakeLaunchctl(loaded=True), plist=plist)
    assert info == {"installed": True, "loaded": True, "state": "running", "pid": "4242", "runs": "2",
                    "last_exit_code": "(never exited)"}
    assert service.status(run=FakeLaunchctl(), plist=tmp_path / "none.plist") == {"installed": False, "loaded": False}
