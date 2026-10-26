"""适配器公共部分：统一错误类别（DESIGN.md 6.5「错误码归一」）。

各家 ERP / 平台的错误码在各自适配器里映射成 ErrorCategory，重试和告警按类别处理。
"""

from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    AUTH = "鉴权失败"
    PERMISSION = "无权限"
    RATE_LIMIT = "限流"
    INVALID = "参数错误"
    UPSTREAM = "上游故障"
    NOT_FOUND = "不存在"
    UNKNOWN = "未知"

    @property
    def retryable(self) -> bool:
        return self in (ErrorCategory.RATE_LIMIT, ErrorCategory.UPSTREAM)


class AdapterError(Exception):
    def __init__(self, category: ErrorCategory, code: str, message: str = ""):
        super().__init__(f"[{category.value}] {code}: {message}")
        self.category = category
        self.code = code
        self.message = message
