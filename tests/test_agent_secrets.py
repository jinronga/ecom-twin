"""agent 拿不到 ecom-twin 的密钥（2026-10-29 发现、11-06 修复）。

- 环境变量：agent 进程原本复制 ecom-twin 的整个环境，.env 载入后妙手、飞书密钥都在里面
- Codex：只读沙箱仍允许读整个磁盘，有 shell 就能 cat .env；现在关掉 shell_tool / unified_exec
- Claude Code：Bash 本来就禁用，读工作目录外的文件要授权、无人值守时拒绝（11-06 真实 CLI 实测）
"""

import subprocess

from ecom_twin.config import agent_hidden_env
from ecom_twin.runtime import ClaudeCodeRuntime, CodexRuntime
from ecom_twin.runtime.base import RunSpec


def test_hidden_env_covers_dotenv_and_known_secrets_but_keeps_runtime_auth(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MIAOSHOU_APP_SECRET=x\nWMS_PASSWORD=y\nANTHROPIC_API_KEY=z\n# COMMENTED=1\n")
    hidden = agent_hidden_env(env_file)
    assert {"MIAOSHOU_APP_SECRET", "WMS_PASSWORD", "FEISHU_APP_SECRET"} <= hidden
    assert "ANTHROPIC_API_KEY" not in hidden and "COMMENTED" not in hidden   # 底座自己登录要用的留着


def test_agent_process_does_not_inherit_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("MIAOSHOU_APP_SECRET", "FAKE-SECRET")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "keep-me")
    captured = {}

    class Stop(Exception):
        pass

    def popen(cmd, **kwargs):
        captured.update(kwargs["env"])
        raise Stop
    monkeypatch.setattr(subprocess, "Popen", popen)
    spec = RunSpec(prompt="p", system_prompt="s", workdir=tmp_path)
    for runtime in (ClaudeCodeRuntime(), CodexRuntime(home=tmp_path / "codex-home")):
        captured.clear()
        try:
            list(runtime.run(spec))
        except Stop:
            pass
        assert "MIAOSHOU_APP_SECRET" not in captured and captured["ANTHROPIC_API_KEY"] == "keep-me"


def test_codex_has_no_shell(tmp_path):
    cmd = CodexRuntime(home=tmp_path).build_command(RunSpec(prompt="p", system_prompt="s", workdir=tmp_path))
    flags = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-c"]
    assert "features.shell_tool=false" in flags and "features.unified_exec=false" in flags
