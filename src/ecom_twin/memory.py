"""记忆库（DESIGN.md 9）。

- 记忆是人工精炼的 Markdown 文件，头部 frontmatter 带 name / description / type
- 每张任务开局只注入索引（index_text），agent 按需用关键词召回（recall），每次召回留记录
- agent 不能直接写记忆，只能提交草稿（propose），人工审核通过后才写成文件
- 拒绝原因汇总（rejection_digest）：把一段时间的审批拒绝原因整理成一条记忆草稿
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config import PROJECT_ROOT
from .store import Store, utcnow

DEFAULT_DIR = PROJECT_ROOT / "memory"
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)
_SLUG = re.compile(r"[^0-9A-Za-z一-鿿_-]+")
_LATIN_WORD = re.compile(r"[a-z][a-z0-9_-]{1,}", re.I)
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}")
_STOPWORDS = {
    "可以", "需要", "帮我", "请你", "这个", "那个", "相关", "一下", "一个", "任务", "处理",
    "查看", "看看", "查询", "分析", "整理", "告诉", "进行", "以及", "或者", "然后", "我们",
    "你们", "没有", "目前", "现在", "什么", "怎么", "如何", "如果", "已经", "是否", "所有",
    "please", "could", "would", "should", "about", "from", "with", "this", "that", "have", "your",
}


@dataclass
class Memory:
    name: str
    description: str
    type: str
    body: str
    path: Path


def parse_memory(path: Path) -> Memory | None:
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER.match(text)
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    if not meta.get("name"):
        return None
    return Memory(meta["name"], meta.get("description", ""), meta.get("type", ""), m.group(2).strip(), path)


def render_memory(name: str, description: str, type_: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\ntype: {type_}\n---\n\n{body.strip()}\n"


class MemoryStore:
    def __init__(self, store: Store, directory: Path = DEFAULT_DIR):
        self.store = store
        self.directory = Path(directory)

    def all(self) -> list[Memory]:
        if not self.directory.exists():
            return []
        memories = [parse_memory(p) for p in sorted(self.directory.glob("*.md")) if p.name != "README.md"]
        return [m for m in memories if m]

    def index_text(self) -> str:
        """开局注入的记忆索引：只有名字和一句话描述，不放正文。"""
        lines = [f"- {m.name}（{m.type}）：{m.description}" for m in self.all()]
        return "\n".join(lines) if lines else "（记忆库为空）"

    def recall(self, keywords: list[str], task_id: str = "", limit: int = 5) -> list[Memory]:
        """关键词召回：名字、描述、正文里命中关键词越多越靠前。每次召回都记录。"""
        words = [w.lower() for w in keywords if w.strip()]
        scored = []
        for m in self.all():
            haystack_head = f"{m.name} {m.description}".lower()
            body = m.body.lower()
            score = sum(3 * haystack_head.count(w) + body.count(w) for w in words)
            if score:
                scored.append((score, m))
        hits = [m for _, m in sorted(scored, key=lambda x: -x[0])[:limit]]
        self.store.log_recall(task_id, " ".join(keywords), [m.name for m in hits])
        return hits

    @staticmethod
    def keywords_for_text(text: str, limit: int = 24) -> list[str]:
        """从任务文本抽取用于启动期召回的轻量关键词，不需要外部模型或向量服务。"""
        text = unicodedata.normalize("NFKC", text or "")
        tokens = [m.group(0).lower() for m in _LATIN_WORD.finditer(text)]
        for run in _CJK_RUN.findall(text):
            tokens.append(run)
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        result, seen = [], set()
        for token in tokens:
            if token in _STOPWORDS or token in seen:
                continue
            seen.add(token)
            result.append(token)
            if len(result) >= limit:
                break
        return result

    def recall_for_task(self, text: str, task_id: str, limit: int = 5) -> list[Memory]:
        """任务开跑时自动召回并写入召回日志。"""
        return self.recall(self.keywords_for_text(text), task_id=task_id, limit=limit)

    def propose(self, name: str, description: str, body: str, reason: str, type_: str = "experience",
                task_id: str = "") -> str:
        return self.store.add_memory_draft({"task_id": task_id, "name": name, "description": description,
                                            "type": type_, "body": body, "reason": reason})

    def approve(self, draft_id: str) -> Path:
        """人工审核通过：写成记忆文件（同名则覆盖，视为更新）。"""
        draft = self.store.get_memory_draft(draft_id)
        if not draft or draft["status"] != "待审核":
            raise ValueError(f"草稿 {draft_id} 不存在或已处理")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{_SLUG.sub('-', draft['name']).strip('-') or draft_id}.md"
        path.write_text(render_memory(draft["name"], draft["description"], draft["type"], draft["body"]),
                        encoding="utf-8")
        self.store.decide_memory_draft(draft_id, "已采纳")
        return path

    def reject(self, draft_id: str) -> None:
        self.store.decide_memory_draft(draft_id, "已驳回")

    def rejection_digest(self, days: int = 7, now: datetime | None = None) -> str | None:
        """把最近 days 天的审批拒绝原因汇总成一条记忆草稿（由代码汇总，不依赖模型）。没有拒绝时返回 None。"""
        now = now or utcnow()
        rejected = self.store.rejected_proposals_since(now - timedelta(days=days))
        if not rejected:
            return None
        counts = Counter(p.reject_reason.value for p in rejected if p.reject_reason)
        lines = [f"统计周期：最近 {days} 天，共 {len(rejected)} 个提案被拒绝。", "", "按原因："]
        lines += [f"- {reason}：{n} 次" for reason, n in counts.most_common()]
        lines += ["", "明细（提案号 / 操作 / 原因 / 备注）："]
        lines += [f"- {p.id} / {p.action} / {p.reject_reason.value if p.reject_reason else ''} / {p.reject_note or '无'}"
                  for p in rejected]
        lines += ["", "审核时请把明细归纳成可复用的经验（怎么判断、要注意什么），删去一次性信息后再采纳。"]
        return self.propose(
            name=f"审批拒绝复盘 {now:%Y-%m-%d}",
            description=f"最近 {days} 天审批拒绝原因汇总，待人工归纳成经验",
            body="\n".join(lines), reason="定期汇总审批拒绝原因（DESIGN.md 9.2 拒绝反馈闭环）",
        )
