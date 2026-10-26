"""测试 SOP 管理器"""

import tempfile
from pathlib import Path

from ecom_twin.sops import SOPManager


def test_load_sops_from_directory():
    """测试从目录加载 SOP 清单"""
    with tempfile.TemporaryDirectory() as tmp:
        sop_dir = Path(tmp) / "skills"
        sop_dir.mkdir()

        # 创建两个 SOP
        (sop_dir / "publish").mkdir()
        (sop_dir / "publish" / "SKILL.md").write_text("""---
name: tiktok-publish
description: TikTok 上架操作指南
---

## 流程
1. 查询采集箱
2. 提交上架提案
""", encoding="utf-8")

        (sop_dir / "pricing").mkdir()
        (sop_dir / "pricing" / "SKILL.md").write_text("""---
name: dynamic-pricing
description: 动态定价策略
---

## 规则
- 毛利率 >= 30%
""", encoding="utf-8")

        mgr = SOPManager([str(sop_dir)])
        sops = mgr.list_sops()

        assert len(sops) == 2
        assert sops[0]["name"] == "dynamic-pricing"
        assert sops[1]["name"] == "tiktok-publish"


def test_get_sop_content():
    """测试加载 SOP 完整内容"""
    with tempfile.TemporaryDirectory() as tmp:
        sop_dir = Path(tmp) / "skills"
        sop_dir.mkdir()
        (sop_dir / "test").mkdir()
        content = """---
name: test-sop
description: 测试 SOP
---

## 内容
测试内容
"""
        (sop_dir / "test" / "SKILL.md").write_text(content, encoding="utf-8")

        mgr = SOPManager([str(sop_dir)])
        result = mgr.get_sop("test-sop")

        assert result == content


def test_sop_priority():
    """测试多个目录时的优先级（第一个目录优先）"""
    with tempfile.TemporaryDirectory() as tmp:
        dir1 = Path(tmp) / "dir1"
        dir2 = Path(tmp) / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        # 两个目录都有同名 SOP
        (dir1 / "test").mkdir()
        (dir1 / "test" / "SKILL.md").write_text("""---
name: same-name
description: 来自 dir1
---
内容 1
""", encoding="utf-8")

        (dir2 / "test").mkdir()
        (dir2 / "test" / "SKILL.md").write_text("""---
name: same-name
description: 来自 dir2
---
内容 2
""", encoding="utf-8")

        mgr = SOPManager([str(dir1), str(dir2)])
        result = mgr.get_sop("same-name")

        assert "内容 1" in result
        assert "内容 2" not in result


def test_format_index():
    """测试格式化 SOP 清单"""
    with tempfile.TemporaryDirectory() as tmp:
        sop_dir = Path(tmp) / "skills"
        sop_dir.mkdir()
        (sop_dir / "test").mkdir()
        (sop_dir / "test" / "SKILL.md").write_text("""---
name: test-sop
description: 测试描述
---
内容
""", encoding="utf-8")

        mgr = SOPManager([str(sop_dir)])
        index = mgr.format_index()

        assert "## 可用的场景 SOP" in index
        assert "test-sop" in index
        assert "测试描述" in index
        assert "load_sop(name)" in index
