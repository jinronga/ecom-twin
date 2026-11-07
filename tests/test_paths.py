"""代码和数据分开：装好的版本把每个人自己的数据放在独立目录，仓库里一个字节都不写。"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def run(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "ECOM_TWIN_HOME": str(home), "ECOM_TWIN_RESOURCES": str(REPO)}
    return subprocess.run([sys.executable, "-m", "ecom_twin.cli", *args], env=env, capture_output=True,
                          text=True, timeout=60)


def test_checkout_uses_repo_for_both_roots():
    from ecom_twin import paths
    if os.environ.get("ECOM_TWIN_HOME") or os.environ.get("ECOM_TWIN_RESOURCES"):
        return
    assert paths.in_checkout() and paths.DATA_HOME == paths.RESOURCE_ROOT == REPO


def test_separate_data_home_gets_db_config_and_seeded_memory(tmp_path):
    home = tmp_path / "EcomTwin"
    result = run(home, "init")
    assert result.returncode == 0, result.stderr
    assert str(home) in result.stdout
    assert (home / "data").is_dir() and list((home / "data").glob("*.db"))
    assert (home / "memory" / "promo-calendar.md").exists()  # 记忆库用随版本发的初始内容垫一份
    assert oct(home.stat().st_mode & 0o777) == "0o700"
    # 资源仍从安装包读：项目自带 SOP 能被找到
    check = run(home, "memory", "check")
    assert check.returncode == 0, check.stdout + check.stderr


def test_seeded_memory_is_not_overwritten(tmp_path):
    home = tmp_path / "EcomTwin"
    run(home, "init")
    edited = home / "memory" / "promo-calendar.md"
    edited.write_text(edited.read_text(encoding="utf-8").replace("每年更新", "本人改过"), encoding="utf-8")
    run(home, "init")
    assert "本人改过" in edited.read_text(encoding="utf-8")
