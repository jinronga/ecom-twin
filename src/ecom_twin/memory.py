"""记忆库（DESIGN.md 9；做法对照本机数字分身的 memory-store / memory-search / memory-proposal）。

- 记忆是人工精炼的 Markdown 文件，头部 frontmatter 带 name / description / type；正文用「## 主题」分节
- 每张任务开局只注入索引（index_text）。description 写的是「做什么动作时该打开这条」，agent 按动作比索引，
  比中了用 read（read_memory）取全文；拿不准时才用 search（search_memory）按业务词兜底。每次读取、检索都留记录
- agent 不能直接写记忆，只能提交草稿（propose），四种模式：append 追加一节、revise 改一段、remove 删一段、
  create 新建一条。revise / remove 的原文在**采纳那一刻**才逐字匹配，中间人工改过就拒绝，不悄悄覆盖
- 索引每一轮都注入，所以有预算：description 上限 120 字是硬的；全库条数和索引大小是软预算，check() 报出来
- 拒绝原因汇总（rejection_digest）：把一段时间的审批拒绝原因整理成一条记忆草稿
- 版本历史（对照数字分身 memory-history）：每次写入前把原内容存进 .history/<标识>/，每条留 20 份，可回滚
- 防覆盖：控制台保存时带上打开那一刻文件的 sha256，期间被改过（比如刚采纳了草稿）就拒绝，不悄悄盖掉
- 关联：正文里的 [[名称]] 是指向另一条记忆的链接；指向不存在的记忆拒绝保存，被链着的记忆不能删、不能改名
- 记忆包（对照数字分身 memory-bundle）：几条记忆的原文按分隔行拼成一个纯文本文件，能直接在聊天窗口里看，也能导回来
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .paths import MEMORY_DIR
from .store import Store, utcnow

DEFAULT_DIR = MEMORY_DIR
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)
_SLUG = re.compile(r"[^0-9A-Za-z一-鿿_-]+")
_SECTION = re.compile(r"^##\s+(.+?)\s*$")

# 索引预算（每一轮 dispatch 都注入索引，多一条就是每一轮都多一份）
MAX_DESCRIPTION_LENGTH = 120
SOFT_MAX_ENTRIES = 100
SOFT_MAX_INDEX_BYTES = 8 * 1024

# 检索：名称、描述是 agent 最先看到的，命中那里比埋在正文里更说明问题
_FIELD_WEIGHTS = {"name": 6, "description": 3, "section": 2, "body": 1}
_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]*|[㐀-䶿一-鿿]+", re.I)
_CJK = re.compile(r"[㐀-䶿一-鿿]")
_STOP_TOKENS = {"怎么做", "最佳实践", "注意事项", "如何", "什么", "怎么", "为什么",
                "the", "a", "an", "of", "to", "is", "how", "what", "why"}

DRAFT_MODES = ("append", "revise", "remove", "create")

# 让本机 CLI 改写描述时给它的标准（对照数字分身 memory-optimizer 的 DESCRIPTION_STANDARD，例子换成电商的）
DESCRIPTION_STANDARD = """description 只有一个用途：让 agent 在索引里判断「这条要不要打开」。它是路由，不是摘要。
写成两段：先说什么时候该打开，再说打开了能拿到什么。触发在前，内容在后。
  好：要调价、改预算、判断备货和上新节奏时打开。各站点大促节点不同，凭印象排期基本会错。
触发场景要列全：正文管着几件事就列几件，漏掉的那件等于不存在。
列的是动作不是名词：
  坏：调价规则、预算上限、大促日期（这是把小节标题抄了一遍）
  好：要调价、改预算、排大促节奏时打开（每一项都是 agent 此刻正在干的事）
别把正文结论、具体数值、店铺名、链接搬进来；不用「一些经验」「相关内容」「注意事项」这类空词。
长度 60 到 120 字，120 是硬线。写不满 60 通常是触发场景没列全，超了通常是在抄目录或搬结论。"""

HISTORY_DIR = ".history"
# 每条留 20 份：一屏列不完的历史等于没有历史，真要回滚的那一版会被埋在几十条时间戳里（数字分身的取舍）
MAX_SNAPSHOTS = 20
_SNAPSHOT_FILE = re.compile(r"^\d{8}T\d{12}Z\.md$")
_LINK = re.compile(r"\[\[([^\[\]\n]{1,80})\]\]")
# 记忆包分隔行：顶格、刻意难撞（正文是 Markdown，## 和 --- 都可能出现，这一串不会）
BUNDLE_PREFIX = "=== ecom-twin-memory:"
_BUNDLE_LINE = re.compile(r"^=== ecom-twin-memory:\s*(.+?)\s*===\s*$")
MAX_BUNDLE_ITEMS = 2000
# 疑似凭证：草稿正文、why、evidence 里出现就拒收（记忆会反复注入给 agent，不能带密钥）
SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9]{16,}|xox[abpr]-|AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._-]{16,}|"
                     r"(app_?secret|appsecret|password|passwd|token|api_?key)\s*[=:：]\s*\S{6,})", re.I)


@dataclass
class Memory:
    name: str
    description: str
    type: str
    body: str
    path: Path

    @property
    def slug(self) -> str:
        return self.path.stem

    def sections(self) -> list[tuple[str, str]]:
        """正文按「## 主题」切开：[(主题, 该节正文)]；第一个标题之前的内容主题为空串。"""
        result, title, lines = [], "", []
        for line in self.body.splitlines():
            m = _SECTION.match(line)
            if m:
                if title or "".join(lines).strip():
                    result.append((title, "\n".join(lines).strip()))
                title, lines = m.group(1), []
            else:
                lines.append(line)
        if title or "".join(lines).strip():
            result.append((title, "\n".join(lines).strip()))
        return result


def parse_memory(path: Path) -> Memory | None:
    return parse_memory_text(path.read_text(encoding="utf-8"), path)


def parse_memory_text(text: str, path: Path) -> Memory | None:
    m = _FRONTMATTER.match(text.replace("\r\n", "\n").lstrip("\ufeff"))
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    if not meta.get("name"):
        return None
    return Memory(meta["name"], meta.get("description", ""), meta.get("type", ""), m.group(2).strip(), path)


def links_in(body: str) -> list[str]:
    """正文里 [[名称]] 指向的记忆名（去重，保持出现顺序）。"""
    return list(dict.fromkeys(m.strip() for m in _LINK.findall(body or "") if m.strip()))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _one_line(value: str) -> str:
    return " ".join((value or "").split())


def render_memory(name: str, description: str, type_: str, body: str) -> str:
    """frontmatter 字段压成单行，避免换行破坏解析。"""
    return (f"---\nname: {_one_line(name)}\ndescription: {_one_line(description)}\ntype: {_one_line(type_)}\n"
            f"---\n\n{(body or '').strip()}\n")


def make_slug(name: str, fallback: str = "memory") -> str:
    return _SLUG.sub("-", name).strip("-")[:80] or fallback


class MemoryError(ValueError):
    """记忆维护的业务错误（控制台返回 400）。"""


# ---- 检索 ----
def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(unicodedata.normalize("NFKC", text or ""))]


def _bigrams(token: str) -> list[str]:
    return [token[i:i + 2] for i in range(len(token) - 1)] if len(token) > 1 else [token]


def _terms(query: str, keywords: list[str]) -> list[str]:
    """中文按二字组、英文按整词。空词（怎么做、最佳实践……）丢掉，它们只会把该命中的挤下去。"""
    terms: list[str] = []
    for token in _tokens(" ".join([query or "", *keywords])):
        if token in _STOP_TOKENS:
            continue
        for term in (_bigrams(token) if _CJK.match(token) else [token]):
            if term not in terms and term not in _STOP_TOKENS:
                terms.append(term)
    return terms


def _field_score(text: str, terms: list[str]) -> int:
    text = unicodedata.normalize("NFKC", text or "").lower()
    words = set(_tokens(text))
    return sum(1 for t in terms if (t in text if _CJK.match(t) else t in words))


def _excerpt(lines: str, terms: list[str], limit: int = 6) -> str:
    picked = [ln for ln in lines.splitlines() if ln.strip() and _field_score(ln, terms)]
    return "\n".join((picked or [ln for ln in lines.splitlines() if ln.strip()])[:limit])


def _secret_free(*values: str) -> None:
    for value in values:
        if value and SECRET_PATTERN.search(value):
            raise MemoryError("草稿里有疑似凭证（token、密钥、密码），不收。请把它换成 <REDACTED> 再提交")


def _strip_heading(body: str) -> str:
    lines = (body or "").strip().splitlines()
    return "\n".join(lines[1:]).strip() if lines and _SECTION.match(lines[0]) else (body or "").strip()


def _section_span(lines: list[str], title: str) -> tuple[int, int] | None:
    """「## title」这一节在 lines 里的 [起始行（标题行）, 结束行) ；没有返回 None。"""
    start = next((i for i, ln in enumerate(lines) if (m := _SECTION.match(ln)) and m.group(1) == title), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _SECTION.match(lines[i])), len(lines))
    return start, end


def _collapse_blank_runs(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class MemoryStore:
    def __init__(self, store: Store, directory: Path = DEFAULT_DIR):
        self.store = store
        self.directory = Path(directory)

    def all(self) -> list[Memory]:
        if not self.directory.exists():
            return []
        memories = [parse_memory(p) for p in sorted(self.directory.glob("*.md")) if p.name != "README.md"]
        return [m for m in memories if m]

    def by_name(self, name: str) -> Memory | None:
        return next((m for m in self.all() if m.name == name), None)

    # ---- 控制台维护（功能 7） ----
    def _path(self, slug: str) -> Path:
        if not slug or _SLUG.search(slug) or slug == "README":  # 防路径穿越
            raise MemoryError(f"非法的记忆标识：{slug}")
        return self.directory / f"{slug}.md"

    def get(self, slug: str) -> Memory | None:
        path = self._path(slug)
        return parse_memory(path) if path.exists() else None

    def save(self, name: str, description: str, type_: str, body: str, slug: str | None = None,
             actor: str = "console", expected_sha: str | None = None) -> Memory:
        """新建（slug 为空）或更新记忆。新建时同名文件已存在则拒绝，避免误覆盖。

        expected_sha：控制台打开这条记忆时文件的 sha256。保存时对不上，说明期间被别处改过（采纳了草稿、
        回滚、导入、手工编辑），拒绝保存，免得把那次改动悄悄盖掉。"""
        if not _one_line(name):
            raise MemoryError("记忆名称不能为空")
        if not _one_line(description):
            raise MemoryError("描述不能为空：它会出现在每一轮开局注入的索引里")
        if len(_one_line(description)) > MAX_DESCRIPTION_LENGTH:
            raise MemoryError(f"描述 {len(_one_line(description))} 字，超过上限 {MAX_DESCRIPTION_LENGTH} 字："
                              "只写「做什么动作时打开它、打开能拿到什么」，结论留在正文里")
        creating = not slug
        slug = slug or make_slug(name)
        path = self._path(slug)
        if creating and path.exists():
            raise MemoryError(f"已存在同名记忆「{slug}」，请直接编辑它")
        if not creating and not path.exists():
            raise MemoryError(f"记忆「{slug}」不存在")
        if creating and self.by_name(_one_line(name)):
            raise MemoryError(f"已存在名为「{_one_line(name)}」的记忆，名称同时是读取的键，不能重名")
        if expected_sha and path.exists() and file_sha256(path) != expected_sha:
            raise MemoryError("这条记忆在你打开之后被改过（可能刚采纳了草稿或回滚过），先刷新看最新内容再改")
        before = parse_memory(path) if path.exists() else None
        self._check_links(_one_line(name), body)
        if before and before.name != _one_line(name):
            referrers = self.referrers(before.name)
            if referrers:
                raise MemoryError(f"《{'》《'.join(referrers)}》还链着《{before.name}》，改名会让链接断掉，先改掉那几处")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._snapshot(slug)
        path.write_text(render_memory(name, description, type_ or "experience", body), encoding="utf-8")
        self.store.audit("memory", slug, "created" if creating else "updated", actor,
                         {"before": {"name": before.name, "description": before.description, "type": before.type,
                                     "body": before.body} if before else None,
                          "after": {"name": name, "description": description, "type": type_}})
        return parse_memory(path)

    def delete(self, slug: str, actor: str = "console") -> None:
        path = self._path(slug)
        memory = parse_memory(path) if path.exists() else None
        if memory is None:
            raise MemoryError(f"记忆「{slug}」不存在")
        referrers = self.referrers(memory.name)
        if referrers:
            raise MemoryError(f"《{'》《'.join(referrers)}》还链着《{memory.name}》，删掉会让链接断掉，先改掉那几处")
        self._snapshot(slug)  # 删之前也留一份，误删能从历史里恢复
        path.unlink()
        self.store.audit("memory", slug, "deleted", actor, {"name": memory.name, "body": memory.body})

    # ---- 关联：[[名称]] ----
    def _check_links(self, name: str, body: str, extra_names: set[str] | None = None) -> None:
        known = {m.name for m in self.all()} | {name} | (extra_names or set())
        dangling = [target for target in links_in(body) if target not in known]
        if dangling:
            raise MemoryError(f"[[{']] [['.join(dangling)}]] 指向不存在的记忆。链接只能指向已有的记忆名，"
                              "skill、工具、命令写成普通文字")

    def referrers(self, name: str) -> list[str]:
        """哪些记忆链着《name》。"""
        return [m.name for m in self.all() if m.name != name and name in links_in(m.body)]

    def graph(self) -> dict[str, dict]:
        """每条记忆链出去、被谁链着；两边都没有的是孤立条目（数字分身控制台会标出来）。"""
        memories = self.all()
        out = {m.name: links_in(m.body) for m in memories}
        incoming = {m.name: [n for n, targets in out.items() if m.name in targets and n != m.name] for m in memories}
        return {m.name: {"links": out[m.name], "linked_by": incoming[m.name],
                         "orphan": not out[m.name] and not incoming[m.name]} for m in memories}

    # ---- 版本历史 ----
    def _history_dir(self, slug: str) -> Path:
        self._path(slug)  # 校验标识，防路径穿越
        return self.directory / HISTORY_DIR / slug

    def _snapshot(self, slug: str) -> Path | None:
        """写入前把现在的内容存一份；文件还不存在（新建）就不存。写完再裁旧的，不在写前裁。"""
        path = self._path(slug)
        if not path.exists():
            return None
        folder = self._history_dir(slug)
        folder.mkdir(parents=True, exist_ok=True)
        at = utcnow()
        snap = folder / f"{at.strftime('%Y%m%dT%H%M%S%f')}Z.md"
        while snap.exists():  # 同一微秒连写两次：往后挪一微秒，不覆盖已有的快照
            at += timedelta(microseconds=1)
            snap = folder / f"{at.strftime('%Y%m%dT%H%M%S%f')}Z.md"
        snap.write_bytes(path.read_bytes())
        for stale in self.history(slug)[MAX_SNAPSHOTS:]:
            (folder / stale["file"]).unlink(missing_ok=True)
        return snap

    def history(self, slug: str) -> list[dict]:
        """某条记忆的历史版本，新的在前。"""
        folder = self._history_dir(slug)
        files = sorted((f for f in folder.glob("*.md") if _SNAPSHOT_FILE.match(f.name)), reverse=True) \
            if folder.exists() else []
        return [{"file": f.name, "taken_at": datetime.strptime(f.name[:15], "%Y%m%dT%H%M%S").isoformat() + "Z",
                 "size": f.stat().st_size} for f in files]

    def read_snapshot(self, slug: str, file: str) -> str:
        if not _SNAPSHOT_FILE.match(file or ""):
            raise MemoryError("历史版本标识不对")
        path = self._history_dir(slug) / file
        if not path.exists():
            raise MemoryError("这个历史版本不存在（可能已经超出保留的 20 份被清掉了）")
        return path.read_text(encoding="utf-8")

    def rollback(self, slug: str, file: str, actor: str = "console") -> Memory:
        """回到某个历史版本。回滚本身也是一次写入：先把现在的内容存一份，回滚错了还能再回来。"""
        text = self.read_snapshot(slug, file)
        restored = parse_memory_text(text, self._path(slug))
        if restored is None:
            raise MemoryError("这个历史版本的头部格式不对，不能回滚到它")
        current = self.get(slug)
        if current and current.name != restored.name and self.referrers(current.name):
            raise MemoryError(f"《{'》《'.join(self.referrers(current.name))}》还链着《{current.name}》，"
                              "回滚到旧名字会让链接断掉")
        self._check_links(restored.name, restored.body)
        self._snapshot(slug)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path(slug).write_text(text, encoding="utf-8")
        self.store.audit("memory", slug, "rolled_back", actor, {"to": file})
        return parse_memory(self._path(slug))

    # ---- 记忆包：导出 / 导入 ----
    def export_bundle(self, names: list[str] | None = None) -> str:
        memories = self.all()
        chosen = [m for m in memories if not names or m.name in names]
        missing = [n for n in names or [] if n not in {m.name for m in memories}]
        if missing:
            raise MemoryError(f"没有《{'》《'.join(missing)}》这几条记忆")
        colliding = [m.name for m in chosen
                     if any(line.startswith(BUNDLE_PREFIX) for line in m.path.read_text(encoding="utf-8").splitlines())]
        if colliding:
            raise MemoryError(f"《{'》《'.join(colliding)}》的正文里有一行以 {BUNDLE_PREFIX} 开头，"
                              "装进记忆包会让分隔位置错乱，先把那一行改掉")
        inside = {m.name for m in chosen}
        outside = sorted({t for m in chosen for t in links_in(m.body) if t not in inside})
        preamble = [f"# ecom-twin 记忆包 · {len(chosen)} 条", f"# 导出时间：{utcnow().isoformat(timespec='seconds')}"]
        if outside:
            preamble.append(f"# 注意：包里引用了不在包内的《{'》《'.join(outside)}》，导入的那台机器上得已经有它们，否则会因断链被拒")
        preamble.append("# 用 ecom-twin memory import 这个文件，或在控制台「记忆库 → 导入」里粘贴")
        blocks = [f"{BUNDLE_PREFIX} {m.name} ===\n{m.path.read_text(encoding='utf-8').rstrip()}\n" for m in chosen]
        return "\n".join(preamble) + "\n\n" + "\n".join(blocks)

    def import_bundle(self, text: str, actor: str = "console") -> dict[str, list[str]]:
        """导回记忆包。格式不对、名字对不上、重名、断链，整包拒绝，不半懂半猜地写进去。
        已有的同名记忆会被覆盖（覆盖前留快照，可回滚）；内容完全一样的跳过。"""
        items, current = [], None
        for line in (text or "").lstrip("\ufeff").replace("\r\n", "\n").split("\n"):
            m = _BUNDLE_LINE.match(line)
            if m:
                current = {"name": m.group(1), "lines": []}
                items.append(current)
            elif current is not None:  # 第一条分隔行之前是前言，丢掉
                current["lines"].append(line)
        if not items:
            raise MemoryError(f"这个文件里没有 {BUNDLE_PREFIX} 分隔行，不像是 ecom-twin 导出的记忆包")
        if len(items) > MAX_BUNDLE_ITEMS:
            raise MemoryError(f"包里有 {len(items)} 条，超过一次导入 {MAX_BUNDLE_ITEMS} 条的上限，先确认文件是不是拼坏了")
        seen = Counter(i["name"] for i in items)
        dupes = [n for n, c in seen.items() if c > 1]
        if dupes:
            raise MemoryError(f"记忆包里有两条都叫《{'》《'.join(dupes)}》，先在文件里删掉一条")
        parsed = []
        for item in items:
            content = "\n".join(item["lines"]).strip() + "\n"
            memory = parse_memory_text(content, self.directory / "_.md")
            if memory is None:
                raise MemoryError(f"《{item['name']}》的头部格式不对（要有 --- 包起来的 name / description / type）")
            if memory.name != item["name"]:
                raise MemoryError(f"分隔行写的是《{item['name']}》，正文里的 name 却是《{memory.name}》，包可能被手改过")
            if not memory.description or len(memory.description) > MAX_DESCRIPTION_LENGTH:
                raise MemoryError(f"《{memory.name}》的描述为空或超过 {MAX_DESCRIPTION_LENGTH} 字")
            _secret_free(memory.body, memory.description)
            parsed.append((memory, content))
        incoming = {m.name for m, _ in parsed}
        for memory, _ in parsed:
            self._check_links(memory.name, memory.body, extra_names=incoming)
        result = {"created": [], "updated": [], "unchanged": []}
        existing = {m.name: m for m in self.all()}
        for memory, content in parsed:
            old = existing.get(memory.name)
            if old and old.path.read_text(encoding="utf-8").strip() == content.strip():
                result["unchanged"].append(memory.name)
                continue
            slug = old.slug if old else make_slug(memory.name)
            if not old and self._path(slug).exists():  # 标识撞了别的记忆：加序号
                slug = next(f"{slug}-{i}" for i in range(2, 1000) if not self._path(f"{slug}-{i}").exists())
            self.directory.mkdir(parents=True, exist_ok=True)
            self._snapshot(slug)
            self._path(slug).write_text(content, encoding="utf-8")
            self.store.audit("memory", slug, "imported", actor, {"name": memory.name, "replaced": bool(old)})
            result["updated" if old else "created"].append(memory.name)
        return result

    # ---- 让本机 CLI 改写描述（只给建议，人看过再保存） ----
    def suggest_description(self, slug: str, binary: str = "", run=subprocess.run, timeout: float = 180) -> dict:
        """调本机 Claude Code 按 DESCRIPTION_STANDARD 改写一条记忆的描述，返回建议，不落盘。

        对照数字分身 memory-optimizer，但不让 CLI 直接改文件：工具全关、不挂 MCP、ecom-twin 的密钥不进它的环境，
        只要它输出一行建议；这里校验长度后交回控制台，负责人看过再点保存。"""
        memory = self.get(slug)
        if memory is None:
            raise MemoryError(f"记忆「{slug}」不存在")
        binary = binary or shutil.which("claude") or ""
        if not binary:
            raise MemoryError("本机没找到 Claude Code（claude），在配置 [runtime.cli] claude_code 里填路径")
        others = "\n".join(f"- {m.name} — {m.description}" for m in self.all() if m.name != memory.name) or "（没有）"
        prompt = (f"把下面这条记忆的 description 按标准重写。只输出一行，用 <description></description> 包起来，"
                  f"不要输出别的。\n\n{DESCRIPTION_STANDARD}\n\n同一个索引里的其它记忆（写出来要能和它们区分开）：\n"
                  f"{others}\n\n要改写的记忆：\nname: {memory.name}\n现在的 description: {memory.description}\n"
                  f"正文：\n{memory.body}")
        from .config import agent_hidden_env
        env = {k: v for k, v in os.environ.items() if k not in agent_hidden_env()}
        with tempfile.TemporaryDirectory() as cwd:
            try:
                out = run([binary, "-p", prompt, "--output-format", "text", "--strict-mcp-config",
                           "--mcp-config", '{"mcpServers":{}}', "--disallowedTools",
                           "Bash,Read,Write,Edit,Grep,Glob,WebFetch,WebSearch,NotebookEdit,Task"],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
            except (OSError, subprocess.SubprocessError) as e:
                raise MemoryError(f"调用本机 Claude Code 失败：{e}") from e
        m = re.search(r"<description>(.*?)</description>", out.stdout or "", re.S)
        suggestion = _one_line(m.group(1)) if m else ""
        if not suggestion:
            raise MemoryError("Claude Code 没给出建议" + (f"：{(out.stderr or out.stdout or '').strip()[-200:]}" if out else ""))
        if len(suggestion) > MAX_DESCRIPTION_LENGTH:
            raise MemoryError(f"给出的建议 {len(suggestion)} 字，超过上限 {MAX_DESCRIPTION_LENGTH} 字，再试一次：{suggestion}")
        self.store.audit("memory", slug, "description_suggested", "console", {"before": memory.description, "after": suggestion})
        return {"before": memory.description, "suggestion": suggestion}

    # ---- 索引与预算 ----
    def index_text(self) -> str:
        """开局注入的记忆索引：每行「名称 — 触发动作」，不放正文。"""
        lines = [f"- {m.name} — {m.description}" for m in self.all()]
        return "\n".join(lines) if lines else "（记忆库还是空的，本轮没有可召回的经验）"

    def check(self) -> list[tuple[str, str]]:
        """格式与预算校验：[(级别, 说明)]。error 会让记忆读不到，warning 是软预算超了。"""
        problems: list[tuple[str, str]] = []
        files = sorted(p for p in self.directory.glob("*.md") if p.name != "README.md") \
            if self.directory.exists() else []
        names: Counter = Counter()
        for path in files:
            memory = parse_memory(path)
            if memory is None:
                problems.append(("error", f"{path.name}：头部格式不对（要有 --- 包起来的 name / description / type）"))
                continue
            names[memory.name] += 1
            if not memory.description:
                problems.append(("error", f"《{memory.name}》没有描述，索引里看不出什么时候该打开它"))
            elif len(memory.description) > MAX_DESCRIPTION_LENGTH:
                problems.append(("error", f"《{memory.name}》描述 {len(memory.description)} 字，"
                                          f"超过上限 {MAX_DESCRIPTION_LENGTH} 字"))
        problems += [("error", f"有 {n} 条记忆都叫《{name}》，读取时分不出是哪条") for name, n in names.items() if n > 1]
        for memory in self.all():
            for target in links_in(memory.body):
                if target not in names:
                    problems.append(("error", f"《{memory.name}》里的 [[{target}]] 指向不存在的记忆"))
        size = len(self.index_text().encode("utf-8"))
        if len(files) > SOFT_MAX_ENTRIES:
            problems.append(("warning", f"记忆共 {len(files)} 条，超过软预算 {SOFT_MAX_ENTRIES} 条"))
        if size > SOFT_MAX_INDEX_BYTES:
            problems.append(("warning", f"索引 {size} 字节，超过软预算 {SOFT_MAX_INDEX_BYTES} 字节；"
                                        "它会进每一轮的上下文，考虑合并或删掉不常用的"))
        return problems

    def budget(self) -> dict:
        return {"entries": len(self.all()), "max_entries": SOFT_MAX_ENTRIES,
                "index_bytes": len(self.index_text().encode("utf-8")), "max_index_bytes": SOFT_MAX_INDEX_BYTES,
                "max_description": MAX_DESCRIPTION_LENGTH}

    # ---- 召回：按名字读全文（主路径），按业务词检索（兜底） ----
    def read(self, name: str, task_id: str = "") -> Memory | None:
        memory = self.by_name(name)
        self.store.log_recall(task_id, name, [memory.name] if memory else [], command="read")
        return memory

    def search(self, query: str = "", keywords: list[str] | None = None, task_id: str = "",
               limit: int = 8) -> list[dict]:
        """兜底检索：返回 [{name, description, section, excerpt, score}]。excerpt 只是命中那一节的几行，不是正文。"""
        keywords = keywords or []
        terms = _terms(query, keywords)
        scored = []
        for m in self.all() if terms else []:
            head = _FIELD_WEIGHTS["name"] * _field_score(m.name, terms) + \
                _FIELD_WEIGHTS["description"] * _field_score(m.description, terms)
            best_section, best = "", 0
            for title, text in m.sections():
                s = _FIELD_WEIGHTS["section"] * _field_score(title, terms) + _FIELD_WEIGHTS["body"] * _field_score(text, terms)
                if s > best:
                    best_section, best = title, s
            if head + best:
                section_text = dict(m.sections()).get(best_section, m.body)
                scored.append({"name": m.name, "description": m.description, "section": best_section,
                               "excerpt": _excerpt(section_text, terms), "score": head + best})
        hits = sorted(scored, key=lambda h: -h["score"])[:max(1, min(int(limit), 20))]
        self.store.log_recall(task_id, " ".join([query, *keywords]).strip(), [h["name"] for h in hits],
                              command="search")
        return hits

    # ---- 草稿：agent 唯一的写权 ----
    def propose(self, draft: dict, task_id: str = "") -> str:
        """提交草稿。提交时只查字段在不在，原文匹配和格式等采纳那一刻再校验。同样的草稿重复提交不重复入队。"""
        mode = draft.get("mode") or "create"
        if mode not in DRAFT_MODES:
            raise MemoryError(f"mode 只能是 {' / '.join(DRAFT_MODES)}")
        fields = {k: (draft.get(k) or "").strip() for k in
                  ("target_name", "target_section", "old_text", "name", "description", "type", "body",
                   "why", "evidence")}
        if not fields["why"]:
            raise MemoryError("why 必填：一句话说清为什么值得记")
        if mode == "create":
            if not (fields["name"] and fields["description"] and fields["body"]):
                raise MemoryError("create 要写 name、description、body")
        else:
            if not fields["target_name"]:
                raise MemoryError(f"{mode} 要写 target_name（索引里那条记忆的名称）")
            if mode == "append" and not _SECTION.match((fields["body"].splitlines() or [""])[0]):
                raise MemoryError("append 的 body 第一行要是「## 主题」，已有主题就用原标题")
            if mode in ("revise", "remove") and not (fields["target_section"] and fields["old_text"]):
                raise MemoryError(f"{mode} 要写 target_section 和 old_text（照抄原文，先 read_memory 取回来）")
            if mode == "revise" and not fields["body"]:
                raise MemoryError("revise 要写 body（替换后的那一段）")
        _secret_free(fields["body"], fields["why"], fields["evidence"], fields["old_text"])
        record = {"task_id": task_id, "mode": mode, "type": fields["type"] or "experience",
                  "name": fields["name"] or fields["target_name"], "reason": fields["why"], **fields}
        existing = self.store.find_memory_draft(record)
        return existing or self.store.add_memory_draft(record)

    def apply_draft(self, draft: dict) -> tuple[str, str, str, str, str | None]:
        """按草稿算出采纳后的记忆：(name, description, type, body, 已有记忆的 slug 或 None)。对不上就抛 MemoryError。"""
        mode = draft.get("mode") or "create"
        if mode == "create":
            if self.by_name(draft["name"]):
                raise MemoryError(f"已经有《{draft['name']}》了，应该用 append / revise 改它")
            return draft["name"], draft.get("description") or "", draft.get("type") or "experience", draft["body"], None
        target = self.by_name(draft.get("target_name") or "")
        if target is None:
            raise MemoryError(f"记忆《{draft.get('target_name')}》不存在（可能已改名或删除）")
        lines = target.body.splitlines()
        if mode == "append":
            fragment = (draft.get("body") or "").strip().splitlines()
            title = _SECTION.match(fragment[0]).group(1)
            span = _section_span(lines, title)
            if span:  # 已有这个主题：接在这一节末尾
                insert = span[1]
                while insert > span[0] + 1 and not lines[insert - 1].strip():
                    insert -= 1
                lines[insert:insert] = "\n".join(fragment[1:]).strip("\n").splitlines()
                body = "\n".join(lines)
            else:
                body = f"{target.body.rstrip()}\n\n" + "\n".join(fragment)
        else:
            span = _section_span(lines, draft.get("target_section") or "")
            if span is None:
                raise MemoryError(f"《{target.name}》里没有「## {draft.get('target_section')}」这一节")
            section = "\n".join(lines[span[0] + 1:span[1]])
            old = draft.get("old_text") or ""
            count = section.count(old)
            if count != 1:
                raise MemoryError(f"old_text 在「{draft.get('target_section')}」这一节里出现 {count} 次，"
                                  "必须恰好 1 次（可能人工已经改过这段）")
            section = section.replace(old, _strip_heading(draft.get("body") or "") if mode == "revise" else "")
            lines[span[0] + 1:span[1]] = section.splitlines()
            body = "\n".join(lines)
        description = draft.get("description") or target.description
        return target.name, description, target.type, _collapse_blank_runs(body), target.slug

    def approve(self, draft_id: str, edits: dict | None = None, actor: str = "console") -> Path:
        """人工审核通过：可以先改草稿再采纳（改后的版本和原稿分开记，原稿不覆盖）。"""
        draft = self.store.get_memory_draft(draft_id)
        if not draft or draft["status"] != "待审核":
            raise MemoryError(f"草稿 {draft_id} 不存在或已处理")
        editable = ("name", "description", "type", "body", "target_section", "old_text")
        edits = {k: v for k, v in (edits or {}).items() if k in editable and v is not None and v != draft.get(k)}
        name, description, type_, body, slug = self.apply_draft({**draft, **edits})
        memory = self.save(name, description, type_, body, slug=slug, actor=actor)
        self.store.decide_memory_draft(draft_id, "已采纳", final=edits or None)
        self.store.audit("memory_draft", draft_id, "approved", actor,
                         {"memory": memory.slug, "mode": draft.get("mode"), "edited": sorted(edits)})
        return memory.path

    def reject(self, draft_id: str, actor: str = "console", note: str = "") -> None:
        draft = self.store.get_memory_draft(draft_id)
        if not draft or draft["status"] != "待审核":
            raise MemoryError(f"草稿 {draft_id} 不存在或已处理")
        self.store.decide_memory_draft(draft_id, "已驳回", note=note)
        self.store.audit("memory_draft", draft_id, "rejected", actor, {"note": note})

    def rejection_digest(self, days: int = 7, now: datetime | None = None) -> str | None:
        """把最近 days 天的审批拒绝原因汇总成一条记忆草稿（由代码汇总，不依赖模型）。没有拒绝时返回 None。"""
        now = now or utcnow()
        rejected = self.store.rejected_proposals_since(now - timedelta(days=days))
        if not rejected:
            return None
        counts = Counter(p.reject_reason.value for p in rejected if p.reject_reason)
        lines = ["## 拒绝原因统计", "", f"统计周期：截至 {now:%Y-%m-%d} 的 {days} 天，共 {len(rejected)} 个提案被拒绝。",
                 "", "按原因："]
        lines += [f"- {reason}：{n} 次" for reason, n in counts.most_common()]
        lines += ["", "## 明细", "", "提案号 / 操作 / 原因 / 备注："]
        lines += [f"- {p.id} / {p.action} / {p.reject_reason.value if p.reject_reason else ''} / {p.reject_note or '无'}"
                  for p in rejected]
        lines += ["", "审核时请把明细归纳成可复用的经验（怎么判断、要注意什么），删去一次性信息后再采纳。"]
        return self.propose({
            "mode": "create", "name": f"审批拒绝复盘 {now:%Y-%m-%d}",
            "description": "准备提交同类提案前打开：看最近被负责人拒绝的原因，避免再犯（待人工归纳）",
            "body": "\n".join(lines), "why": "定期汇总审批拒绝原因（DESIGN.md 9.2 拒绝反馈闭环）",
            "evidence": f"{len(rejected)} 个被拒绝的提案：{', '.join(p.id for p in rejected[:10])}",
        })
