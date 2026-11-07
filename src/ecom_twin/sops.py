"""场景 SOP（skill）动态加载（DESIGN.md「数字分身到底要多通用」：Skill 动态载入）。

开局只给 agent 一份 SOP 清单（名字 + 描述），它判断场景后用 load_sop 加载正文。
SOP 里提到的脚本 agent 不能直接运行（没有 shell）；涉及写操作的步骤一律改为提交提案。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FRONTMATTER = re.compile(r"^﻿?---\n(.*?)\n---\n?(.*)$", re.S)


@dataclass
class Sop:
    name: str
    description: str
    path: Path

    def body(self) -> str:
        m = _FRONTMATTER.match(self.path.read_text(encoding="utf-8"))
        return m.group(2).strip() if m else self.path.read_text(encoding="utf-8")


def _parse(path: Path) -> Sop | None:
    m = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return Sop(meta.get("name") or path.parent.name, meta.get("description", ""), path) if meta else None


class SopLibrary:
    def __init__(self, dirs: list[Path]):
        self.dirs = dirs

    def all(self) -> list[Sop]:
        seen, sops = set(), []
        for d in self.dirs:
            for path in sorted(Path(d).glob("**/SKILL.md")) if Path(d).exists() else []:
                sop = _parse(path)
                if sop and sop.name not in seen:
                    seen.add(sop.name)
                    sops.append(sop)
        return sops

    def index_text(self, max_desc: int = 120) -> str:
        lines = [f"- {s.name}：{s.description[:max_desc]}" for s in self.all()]
        return "\n".join(lines) if lines else "（没有配置场景 SOP）"

    def load(self, name: str) -> str:
        sop = next((s for s in self.all() if s.name == name), None)
        if sop is None:
            raise ValueError(f"没有名为 {name} 的 SOP，可用：{', '.join(s.name for s in self.all())}")
        return (f"# SOP：{sop.name}\n\n> 说明：SOP 里的脚本你不能直接运行；需要改动妙手或平台数据的步骤，"
                f"一律用 submit_proposal 提交提案。\n\n{sop.body()}")
