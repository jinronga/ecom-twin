"""配置加载：settings.toml（业务配置）+ 环境变量 / .env（密钥）。

密钥只从环境变量或 .env 读取，不写进 settings.toml，也不进入 agent 上下文。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Approver:
    primary: str
    delegate: str = ""


@dataclass
class Limits:
    allowed_shops: list[str] = field(default_factory=list)
    max_daily_writes_per_shop: int = 50
    max_publish_items_per_proposal: int = 50
    min_margin_rate: float = 0.15
    max_bid_change_pct: float = 30
    max_price_change_pct: float = 10


@dataclass
class FeishuSettings:
    owner_open_id: str = ""
    base_app_token: str = ""
    tasks_table_id: str = ""
    steps_table_id: str = ""
    proposals_table_id: str = ""
    base_max_writes_per_sec: float = 3

    @property
    def board_enabled(self) -> bool:
        return bool(self.base_app_token and self.tasks_table_id)


@dataclass
class RuntimeSettings:
    default: str = "claude_code"
    fallback: str = "codex"
    max_retries_before_switch: int = 2
    # 本机各 agent CLI 的路径，key 见 runtime/cli_check.py 的 CLI_SPECS；留空则自动查找
    cli: dict[str, str] = field(default_factory=dict)


@dataclass
class BrowserSettings:
    enabled: bool = False
    browser_url: str = "http://127.0.0.1:9222"
    chrome_path: str = ""
    user_data_dir: str = ""
    # 功能5：只读网页取证的域名白名单（含子域名）
    allowed_domains: list[str] = field(default_factory=lambda: [
        "amazon.com", "amazon.co.uk", "amazon.de", "amazon.co.jp", "amazon.ae", "amazon.sa",
        "tiktok.com", "noon.com", "1688.com", "aliexpress.com", "shopee.com", "lazada.com",
    ])
    # URL 里出现这些词一律拒绝：卖家后台、登录、结算等
    blocked_keywords: list[str] = field(default_factory=lambda: [
        "sellercentral", "seller.", "seller-", "partner", "login", "signin", "account",
        "checkout", "cart", "settle", "withdraw",
    ])
    timeout_sec: float = 45.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.browser_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError("browser.browser_url 必须是本机 HTTP 调试地址")
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("browser.browser_url 端口无效") from error
        if port is None or not 1 <= port <= 65535:
            raise ValueError("browser.browser_url 必须包含有效端口")


# 卖货店铺后台：永远不允许分身用浏览器操作（DESIGN.md 13.1 账号风控）
STORE_BACKEND_MARKERS = (
    "sellercentral", "seller.tiktok", "seller-us.tiktok", "seller-uk.tiktok", "partner.tiktokshop",
    "seller.noon", "partner.noon", "noon.partners", "sellercenter", "seller.shopee", "seller.lazada", "seller.walmart",
)


def is_store_backend(text: str) -> bool:
    """判断一段 URL 或域名是不是卖货店铺后台。"""
    text = (text or "").lower()
    return any(marker in text for marker in STORE_BACKEND_MARKERS)


@dataclass
class PlatformSettings:
    """可以由分身用浏览器操作的内部平台（海外仓、OA 审批等），不包括卖货店铺后台。"""
    key: str
    name: str
    base_url: str
    login_url: str | None = None  # 手工登录时打开的 URL；未配置时打开 base_url
    allowed_domains: list[str] = field(default_factory=list)
    # 跳转后 URL 含这些词，视为登录已失效
    login_url_keywords: list[str] = field(default_factory=lambda: ["login", "signin", "sso", "passport"])
    risk: str = "中"
    max_steps: int = 30
    # 写在审批卡片「能否撤销」里，例如「入库单提交后 2 小时内可在 WMS 里作废」
    revert_hint: str = "取决于平台，通常需要人工在平台上撤回或作废"

    def __post_init__(self) -> None:
        if is_store_backend(self.base_url):
            raise ValueError(f"平台 {self.key} 的地址看起来是卖货店铺后台，不允许配置成可操作平台")


@dataclass
class Settings:
    db_path: Path
    expire_hours: float = 24
    remind_before_hours: float = 4
    approvers: dict[str, Approver] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    feishu: FeishuSettings = field(default_factory=FeishuSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    browser: BrowserSettings = field(default_factory=BrowserSettings)
    # 功能3：场景 SOP（skill）所在目录，agent 按场景动态加载。同名 SOP 取第一个目录的。
    skill_dirs: list[Path] = field(default_factory=list)
    # 功能6：可以用浏览器操作的内部平台（海外仓、OA 审批等），不含卖货店铺后台
    platforms: dict[str, PlatformSettings] = field(default_factory=dict)

    def approver_for(self, shop_key: str) -> Approver | None:
        return self.approvers.get(shop_key)


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path) if path else PROJECT_ROOT / "config" / "settings.toml"
    raw: dict = {}
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))

    db_path = Path(raw.get("storage", {}).get("db_path", "data/ecom_twin.db"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    approval = raw.get("approval", {})
    return Settings(
        db_path=db_path,
        expire_hours=approval.get("expire_hours", 24),
        remind_before_hours=approval.get("remind_before_hours", 4),
        approvers={k: Approver(**v) for k, v in raw.get("approvers", {}).items()},
        limits=Limits(**raw.get("limits", {})),
        feishu=FeishuSettings(**raw.get("feishu", {})),
        runtime=RuntimeSettings(**raw.get("runtime", {})),
        browser=BrowserSettings(**raw.get("browser", {})),
        skill_dirs=[Path(os.path.expanduser(d)) for d in raw.get("skills", {}).get("dirs", [])],
        platforms={k: PlatformSettings(key=k, **v) for k, v in raw.get("platforms", {}).items()},
    )


def load_dotenv(path: str | Path | None = None) -> None:
    """极简 .env 解析：只补充进程里还没有的变量，进程环境变量优先。"""
    path = Path(path) if path else PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def validate_settings(settings: Settings, runtime_keys: list[str]) -> list[tuple[str, str]]:
    """配置自检。返回 [(级别, 说明)]，级别为「错误」（会导致功能不可用）或「提醒」。"""
    issues: list[tuple[str, str]] = []
    fs, lim, rt = settings.feishu, settings.limits, settings.runtime
    if not fs.owner_open_id:
        issues.append(("错误", "feishu.owner_open_id 未填：私聊建单、汇报、无审批人店铺的审批卡片都没有接收人"))
    if not lim.allowed_shops:
        issues.append(("错误", "limits.allowed_shops 为空：所有写操作都会被执行器拒绝"))
    for shop in lim.allowed_shops:
        if ":" not in shop:
            issues.append(("错误", f"店铺白名单 {shop} 格式应为「平台:店铺ID」，例如 tiktok:10001"))
        elif shop not in settings.approvers:
            issues.append(("提醒", f"店铺 {shop} 没配审批人，审批卡片会发给 owner，且没有代理审批人"))
    for shop, a in settings.approvers.items():
        if not a.primary:
            issues.append(("错误", f"店铺 {shop} 的主审批人为空"))
        if not a.delegate:
            issues.append(("提醒", f"店铺 {shop} 没有代理审批人，主审批人不在时提案只能过期"))
    if not 0 <= lim.min_margin_rate < 1:
        issues.append(("错误", f"limits.min_margin_rate={lim.min_margin_rate} 应在 0~1 之间"))
    if fs.base_app_token and not (fs.tasks_table_id and fs.steps_table_id and fs.proposals_table_id):
        issues.append(("提醒", "多维表格只配了一部分 table_id，没配的表不会同步"))
    if not fs.base_app_token:
        issues.append(("提醒", "未配置多维表格：任务看板不会同步，只写本地数据库"))
    for name, key in (("default", rt.default), ("fallback", rt.fallback)):
        if key not in runtime_keys:
            issues.append(("错误", f"runtime.{name}={key} 不是已实现的底座：{', '.join(runtime_keys)}"))
    if rt.default == rt.fallback:
        issues.append(("提醒", "默认底座和备选底座相同，失败时没有可切换的底座"))
    if settings.remind_before_hours >= settings.expire_hours:
        issues.append(("提醒", "过期提醒时间不小于有效期，提案一提交就会提醒"))
    return issues
