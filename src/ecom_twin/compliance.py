"""Listing 合规检查（DESIGN.md 8.4、8.5、13.2）：规则 + 词库，确定性判断，不依赖模型。

- block：暂存区编辑直接拒绝；发布提案拒收
- warn：写进审批卡片，提醒审批人
规则在 config/compliance.toml，运营可以直接改词库。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .paths import COMPLIANCE_FILE

RULES_FILE = COMPLIANCE_FILE


@dataclass
class Violation:
    rule: str
    level: str  # block / warn
    field: str
    detail: str

    def __str__(self) -> str:
        return f"[{'禁止' if self.level == 'block' else '提醒'}] {self.field}：{self.detail}"


class ComplianceChecker:
    def __init__(self, rules: dict | None = None, path: Path = RULES_FILE):
        self.rules = rules if rules is not None else (tomllib.loads(path.read_text(encoding="utf-8"))
                                                      if path.exists() else {})
        self.common = self.rules.get("common", {})
        self.levels = self.rules.get("levels", {})

    def _level(self, rule: str) -> str:
        return self.levels.get(rule, "block")

    @staticmethod
    def _contains_word(text: str, word: str) -> bool:
        if re.fullmatch(r"[\w .#%-]+", word, re.ASCII):  # 英文词按词边界匹配，避免 "treat" 命中 "treatment"
            return re.search(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", text, re.I) is not None
        return word in text

    def check(self, text: str, platform: str = "tiktok", field: str = "标题") -> list[Violation]:
        text = text or ""
        found: list[Violation] = []
        own = {b.lower() for b in self.common.get("own_brands", [])}
        for rule in ("absolute_words", "medical_claims", "brand_terms"):
            for word in self.common.get(rule, []):
                if rule == "brand_terms" and word.lower() in own:
                    continue
                if self._contains_word(text, word):
                    label = {"absolute_words": "绝对化用语", "medical_claims": "医疗 / 功效宣称",
                             "brand_terms": "疑似侵权品牌词"}[rule]
                    found.append(Violation(rule, self._level(rule), field, f"{label}「{word}」"))
        for pattern in self.common.get("contact_patterns", []):
            m = re.search(pattern, text)
            if m:
                found.append(Violation("contact_patterns", self._level("contact_patterns"), field,
                                       f"站外联系方式或链接「{m.group(0)}」"))
        if field == "标题":
            limits = self.rules.get("platforms", {}).get(platform, {})
            n = len(text)
            if limits.get("title_max") and n > limits["title_max"]:
                found.append(Violation("title_length", self._level("title_length"), field,
                                       f"长度 {n} 超过 {platform} 上限 {limits['title_max']}"))
            if limits.get("title_min") and n < limits["title_min"]:
                found.append(Violation("title_too_short", self._level("title_too_short"), field,
                                       f"长度 {n} 少于建议下限 {limits['title_min']}"))
            letters = [c for c in text if c.isalpha() and c.isascii()]
            if len(letters) > 10 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
                found.append(Violation("title_all_caps", self._level("title_all_caps"), field, "标题几乎全是大写"))
        return found

    @staticmethod
    def blocking(violations: list[Violation]) -> list[Violation]:
        return [v for v in violations if v.level == "block"]
