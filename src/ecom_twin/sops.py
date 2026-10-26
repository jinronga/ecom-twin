"""场景 SOP 动态加载模块（功能3）

按需加载 skills 目录中的操作指南，提供：
- SOP 清单（名称+描述）
- 按需加载完整 SOP 内容
- 支持多个 SOP 目录
- 同名 SOP 只取第一个
"""

import os
from pathlib import Path
from typing import Optional
import frontmatter  # type: ignore


class SOPManager:
    """SOP（标准操作程序）管理器"""

    def __init__(self, sop_dirs: list[str]):
        """初始化 SOP 管理器

        Args:
            sop_dirs: SOP 目录列表，按优先级排序（第一个优先级最高）
        """
        self.sop_dirs = [Path(d).resolve() for d in sop_dirs]
        self._sops: dict[str, Path] = {}  # name -> SKILL.md 路径
        self._load_index()

    def _load_index(self) -> None:
        """扫描所有 SOP 目录，构建索引（同名只取第一个）"""
        for sop_dir in self.sop_dirs:
            if not sop_dir.exists():
                continue
            # 递归查找所有 SKILL.md
            for skill_file in sop_dir.rglob("SKILL.md"):
                try:
                    post = frontmatter.load(skill_file)
                    name = post.metadata.get("name")
                    if not name or not isinstance(name, str):
                        continue
                    # 同名只取第一个（优先级高的目录）
                    if name not in self._sops:
                        self._sops[name] = skill_file
                except Exception:
                    continue  # 格式错误的文件跳过

    def list_sops(self) -> list[dict[str, str]]:
        """获取所有 SOP 的清单（名称+描述）

        Returns:
            [{"name": "...", "description": "..."}, ...]
        """
        result = []
        for name, path in self._sops.items():
            try:
                post = frontmatter.load(path)
                result.append({
                    "name": name,
                    "description": post.metadata.get("description", ""),
                })
            except Exception:
                continue
        return sorted(result, key=lambda x: x["name"])

    def get_sop(self, name: str) -> Optional[str]:
        """加载指定 SOP 的完整内容

        Args:
            name: SOP 名称

        Returns:
            SOP 完整内容（frontmatter + markdown），不存在时返回 None
        """
        path = self._sops.get(name)
        if not path:
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None

    def format_index(self) -> str:
        """格式化 SOP 清单为可读文本

        Returns:
            格式化的清单文本
        """
        sops = self.list_sops()
        if not sops:
            return "（暂无可用的场景 SOP）"

        lines = ["## 可用的场景 SOP", ""]
        for sop in sops:
            lines.append(f"- **{sop['name']}**: {sop['description']}")
        lines.append("")
        lines.append("使用 `load_sop(name)` 工具加载完整操作指南。")
        return "\n".join(lines)
