import stat

from ecom_twin.runtime import build_runtime, check


def make_exe(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_configured_path_is_verified_by_version(tmp_path):
    exe = make_exe(tmp_path / "claude", 'echo "2.1.301 (Claude Code)"')
    st = check("claude_code", exe)
    assert st.ok and st.usable and st.source == "配置"
    assert st.version == "2.1.301 (Claude Code)"


def test_failing_cli_reports_error_and_hint(tmp_path):
    exe = make_exe(tmp_path / "cursor", 'echo "Error: No Cursor IDE installation found." >&2; exit 1')
    st = check("cursor", exe)
    assert not st.ok and "No Cursor IDE" in st.error
    assert "cursor-agent" in st.hint


def test_missing_configured_path(tmp_path):
    st = check("codex", str(tmp_path / "nope"))
    assert not st.ok and st.error == "路径不存在或不可执行"


def test_usable_requires_both_cli_and_adapter(tmp_path):
    exe = make_exe(tmp_path / "opencode", "echo 1.2.13")
    st = check("opencode", exe)
    assert st.ok and st.usable
    st.adapter_ready = False
    assert not st.usable


def test_build_runtime_uses_configured_path(tmp_path):
    exe = make_exe(tmp_path / "codex", "echo codex-cli 0.156.0")
    assert build_runtime("codex", {"codex": exe}).binary == exe
