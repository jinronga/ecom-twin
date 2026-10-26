"""妙手 OpenAPI 适配器。

只封装分身需要的能力；接口路径在 config/adapters/miaoshou.toml，签名和错误码在这里处理。
接口细节以妙手官方文档为准，DESIGN.md 不记录具体接口（6.5）。

写接口（认领、发布）只能由执行器调用，不暴露给 agent（DESIGN.md 5.5）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..config import PROJECT_ROOT
from .base import AdapterError, ErrorCategory

DEFAULT_BASE_URL = "https://openapi-erp.91miaoshou.com"
PATHS_FILE = PROJECT_ROOT / "config" / "adapters" / "miaoshou.toml"

# 妙手错误码 -> 统一错误类别
_ERROR_MAP: dict[str, ErrorCategory] = {
    "signMissing": ErrorCategory.AUTH,
    "signExpired": ErrorCategory.AUTH,
    "signInvalid": ErrorCategory.AUTH,
    "appNotFound": ErrorCategory.AUTH,
    "ipNotInWhitelist": ErrorCategory.PERMISSION,
    "appNoPermission": ErrorCategory.PERMISSION,
    "routeNotFound": ErrorCategory.INVALID,
    "missingRequiredField": ErrorCategory.INVALID,
    "categoryNotFound": ErrorCategory.NOT_FOUND,
    "warehouseNotFound": ErrorCategory.NOT_FOUND,
    "responsiblePersonNotFound": ErrorCategory.NOT_FOUND,
    "productNotFound": ErrorCategory.NOT_FOUND,
    "shopNotFound": ErrorCategory.NOT_FOUND,
}


def classify_error(code: str) -> ErrorCategory:
    if code in _ERROR_MAP:
        return _ERROR_MAP[code]
    if code.endswith("RateLimit"):
        return ErrorCategory.RATE_LIMIT
    if code.startswith(("upstream", "redis")) or code in ("systemError", "emptyResponse", "httpError"):
        return ErrorCategory.UPSTREAM
    return ErrorCategory.UNKNOWN


def generate_sign(app_secret: str, path: str, timestamp: int, app_key: str, body_json: str = "") -> str:
    """sign = HmacSHA256(appSecret, appSecret + path + timestamp + appKey + bodyJson + appSecret)，小写 hex。"""
    content = f"{app_secret}{path}{timestamp}{app_key}{body_json}{app_secret}"
    return hmac.new(app_secret.encode(), content.encode(), hashlib.sha256).hexdigest()


# transport(url, headers, body, timeout) -> (http_status, response_text)
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, str]]


def _urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise AdapterError(ErrorCategory.UPSTREAM, "httpError", str(e.reason)) from e


def load_paths(path: Path = PATHS_FILE) -> dict[str, str]:
    return tomllib.loads(path.read_text(encoding="utf-8"))["paths"]


class MiaoshouClient:
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30,
        paths: dict[str, str] | None = None,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if not app_key or not app_secret:
            raise AdapterError(ErrorCategory.AUTH, "missingCredentials", "缺少 MIAOSHOU_APP_KEY / MIAOSHOU_APP_SECRET")
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.paths = paths or load_paths()
        self.transport = transport or _urllib_transport
        self.clock = clock

    @classmethod
    def from_env(cls, **kwargs: Any) -> "MiaoshouClient":
        return cls(
            app_key=os.environ.get("MIAOSHOU_APP_KEY", ""),
            app_secret=os.environ.get("MIAOSHOU_APP_SECRET", ""),
            base_url=os.environ.get("MIAOSHOU_BASE_URL") or DEFAULT_BASE_URL,
            timeout=float(os.environ.get("MIAOSHOU_TIMEOUT") or 30),
            **kwargs,
        )

    def call(self, name: str, body: dict[str, Any]) -> Any:
        """按配置里的接口名调用，成功返回 data，失败抛 AdapterError（已归一化）。"""
        path = self.paths[name]
        timestamp = int(self.clock())
        body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        headers = {
            "Content-Type": "application/json",
            "x-app-key": self.app_key,
            "x-timestamp": str(timestamp),
            "x-sign": generate_sign(self.app_secret, path, timestamp, self.app_key, body_json),
        }
        status, text = self.transport(self.base_url + path, headers, body_json.encode("utf-8"), self.timeout)
        if not text.strip():
            raise AdapterError(ErrorCategory.UPSTREAM, "emptyResponse", f"HTTP {status} 空响应")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise AdapterError(ErrorCategory.UPSTREAM, "httpError", f"HTTP {status} 非 JSON 响应") from e
        if payload.get("result") != "success":
            code = str(payload.get("code") or "unknown")
            raise AdapterError(classify_error(code), code, payload.get("message") or "")
        return payload.get("data")

    # ---- Reader（只读，可暴露给 agent） ----
    def list_shops(self, platform: str, site: str = "", page: int = 1, size: int = 100) -> list[dict]:
        data = self.call("shop_list", {"platform": platform, "site": site, "pageNo": page, "pageSize": size})
        return (data or {}).get("shopList", [])

    def tiktok_search_collect_box(self, status: str = "notPublished", page: int = 1, size: int = 20) -> Any:
        return self.call(
            "tiktok_search_collect_box",
            {"pageNo": page, "pageSize": size, "filter": {"status": status, "sourceItemIdKeyword": ""}},
        )

    def tiktok_shop_item_info(self, detail_id: int, shop_id: int) -> dict:
        return self.call("tiktok_shop_item_info", {"detailId": detail_id, "shopId": shop_id}) or {}

    def tiktok_category_tree(self, site: str) -> Any:
        return self.call("tiktok_category_tree", {"site": site})

    def tiktok_category_metadata(self, site: str, cid: int, shop_ids: list[int]) -> Any:
        return self.call("tiktok_category_metadata", {"site": site, "cid": cid, "shopIds": shop_ids})

    def common_collect_box_detail(self, detail_id: int) -> dict:
        """读取公共采集箱详情及编辑所需的 ``ossMd5``。"""
        return self.call("common_collect_box_detail", {"commonCollectBoxDetailId": int(detail_id)}) or {}

    # ---- Writer（只给执行器用） ----
    def tiktok_claim_to_shop(self, detail_ids: list[int], shop_ids: list[int]) -> Any:
        return self.call("tiktok_claim_to_shop", {"detailIds": detail_ids, "shopIds": shop_ids})

    def tiktok_publish(self, detail_ids: list[int], shop_ids: list[int]) -> Any:
        return self.call("tiktok_publish", {"detailIds": detail_ids, "shopIds": shop_ids})

    def common_collect_box_edit(self, detail_id: int, detail: dict[str, Any], oss_md5: str) -> Any:
        """编辑公共采集箱详情；调用方必须先读取详情并保留未修改字段。"""
        return self.call(
            "common_collect_box_edit",
            {
                "commonCollectBoxDetailId": int(detail_id),
                "editCommonCollectBoxDetail": detail,
                "ossMd5": oss_md5,
            },
        )
