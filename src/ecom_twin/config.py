"""配置加载：settings.toml（业务配置）+ 环境变量 / .env（密钥）。

密钥只从环境变量或 .env 读取，不写进 settings.toml，也不进入 agent 上下文。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .paths import DATA_HOME, ENV_FILE, MEMORY_DIR, SETTINGS_FILE, SKILLS_DIR


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
    # 其余看板表（DESIGN.md 10.2、11.1），不填就不同步
    messages_table_id: str = ""
    executions_table_id: str = ""
    outbound_table_id: str = ""
    recalls_table_id: str = ""
    reviews_table_id: str = ""
    base_max_writes_per_sec: float = 3
    # agent 拉上下文时，除了任务来源会话，还允许读这些会话（chat_id）
    readable_chats: list[str] = field(default_factory=list)
    # 允许用负责人本人身份搜索全部聊天记录（需要 lark-cli auth login）。默认关闭
    allow_user_search: bool = False

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
    # 专用 Chrome 里 agent 能不能跳转、点击、填写、执行脚本（2026-10-30 负责人决定和数字分身一样放开）。
    # false = 只能读页面和截图。放开时经 chrome_guard 代理：下面这些词出现在网址或脚本里一律拒绝
    interactive: bool = True
    interactive_blocked: list[str] = field(default_factory=lambda: [
        "sellercentral", "seller.tiktok", "seller-us.tiktok", "seller-uk.tiktok", "seller.tiktokglobalshop",
        "seller.noon", "noon.partners", "advertising.amazon", "ads.tiktok", "business.tiktok",
        "partner.tiktokshop", "91miaoshou", "erp.miaoshou",
    ])

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
class ComputerUseSettings:
    """操作电脑（任意 Mac 软件），对照数字分身：借用本机 ChatGPT / Codex 装好的 Computer Use（cua_repl）。

    默认关闭。打开后所有底座都经 ecom-twin 的代理（computer_use.py）使用，代理只放行 js / js_reset，
    每次调用写审计；代码里出现 blocked_keywords 的直接拒绝（卖货店铺后台、密码管理器等）。
    这道拦截只是按文本匹配的兜底，主要约束在提示词：对外生效、不可撤回的动作照样先提案或先问负责人。
    """
    enabled: bool = False
    # 允许操作的软件（名称或 bundle ID，例如 "Calculator"、"com.apple.calculator"）。Computer Use 每碰一个 app
    # 都会先问一次，代理只批准这里列出的；默认空 = 一个都不批
    allowed_apps: list[str] = field(default_factory=list)
    # cua_repl 的 MCP 定义文件；留空自动找 ~/.codex/plugins/cache/openai-bundled/unified-computer-use/*/.mcp.json
    mcp_json: str = ""
    blocked_keywords: list[str] = field(default_factory=lambda: [
        "sellercentral", "seller.tiktok", "seller-", "sellercenter", "seller.shopee", "seller.lazada",
        "ads.tiktok", "advertising.amazon", "business.tiktok",
        "keychain", "钥匙串", "1password", "bitwarden", "lastpass", "passwords",
    ])
    timeout_ms: int = 120_000  # 单次 js 调用的上限
    # 数字分身的做法：让 agent 用 Bash 跑 AppleScript 等命令操作 Mac（打开软件、读界面、点按钮、截图）。默认关闭。
    # 只放开下面这几个命令，其它 shell 命令一律拒绝（Claude Code 按命令前缀校验，连 && 拼接的也会拆开查）。
    # 已知缺口：AppleScript 的 do shell script 能执行任意命令，目前只靠提示词禁止，审批流程后续再优化
    applescript: bool = False
    # 不放 screencapture：整屏截图会带进无关内容，取证改用 MCP 的 desktop_capture（按窗口截、不抢焦点）
    shell_commands: list[str] = field(default_factory=lambda: ["osascript", "open", "cliclick"])


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
class IntakeSettings:
    """消息入口与驱动（DESIGN.md 4）。"""
    listen_chats: list[str] = field(default_factory=list)   # 监听的群（chat_id）；私聊负责人总是监听
    bot_name: str = "分身"                                   # 群里 @ 这个名字视为点名分身
    quiet_seconds: float = 60        # 群消息静默攒批：同一人在同一会话停顿这么久后才处理
    p2p_quiet_seconds: float = 0     # 私聊默认立即处理
    ask_window_hours: float = 2      # 分身在会话里提问后，这段时间内的顶层消息也收进来判断归属
    topic_window_hours: float = 24   # 主题归并的候选：本会话这段时间内碰过的未结束任务
    same_class_whitelist: list[str] = field(default_factory=list)  # 允许同类合并的类别，例如「新增运输商」
    same_class_window_days: float = 5
    closed_statuses: list[str] = field(default_factory=lambda: ["已关闭"])  # 已告一段落：不再合并、不再唤醒
    # 锚点（工单号、提案号、任务号、ASIN 等），命中同一锚点的消息并入同一任务；可追加自己的单号格式
    anchor_patterns: list[str] = field(default_factory=lambda: [
        r"\bP-\d{4,}\b", r"\bT-\d{4,}\b", r"\bB0[A-Z0-9]{8}\b"])
    ack_words: list[str] = field(default_factory=lambda: [
        "好的", "好", "收到", "ok", "OK", "嗯", "👌", "谢谢", "辛苦", "了解", "知道了"])
    judge_enabled: bool = True       # 等待唤醒 / 主题 / 同类判定用廉价模型；关闭后只用规则（回复、锚点）
    judge_model: str = "haiku"


@dataclass
class SendPermission:
    """分身在某个群里代发的权限，默认全关（DESIGN.md 5.4、13.3）。"""
    reply: bool = False   # 在消息 thread 里回执、汇报
    ask: bool = False     # 在群里向人提问
    notify: bool = False  # 主动发起新消息知会同事


@dataclass
class ReportSettings:
    send_hour: int = 9               # 本地时间几点发日报
    timezone: str = "Asia/Shanghai"
    # ERP 导出表格的列名映射：统一字段名 -> 表格里的列名
    columns: dict[str, str] = field(default_factory=lambda: {
        "date": "日期", "shop": "店铺", "sku": "SKU", "sales_qty": "销量", "revenue": "销售额",
        "ad_spend": "广告花费", "ad_sales": "广告销售额", "inventory": "可售库存"})
    acos_max: float = 0.35           # 广告花费 / 广告销售额
    tacos_max: float = 0.15          # 广告花费 / 总销售额
    revenue_drop_pct: float = 30     # 比前 7 天日均下降超过这个百分比
    inventory_days_min: float = 14   # 可售天数（库存 / 近 7 天日均销量）低于这个值

@dataclass
class Settings:
    db_path: Path
    # 功能7：记忆库目录（控制台维护的 Markdown 记忆文件）
    memory_dir: Path = MEMORY_DIR
    # 召回记录留多久（天），0 = 不删。这份数据只用来事后核对某一轮召回了什么，排查都在几小时内（对照数字分身，默认 90 天）
    recall_retention_days: float = 90
    expire_hours: float = 24
    remind_before_hours: float = 4
    batch_window_seconds: float = 0            # >0 时，同一审批人、同一类提案在窗口内合并成一张卡
    double_approval_risks: list[str] = field(default_factory=lambda: ["高"])  # 这些风险等级要两个人批准
    approvers: dict[str, Approver] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    feishu: FeishuSettings = field(default_factory=FeishuSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    browser: BrowserSettings = field(default_factory=BrowserSettings)
    computer_use: ComputerUseSettings = field(default_factory=ComputerUseSettings)
    platforms: dict[str, PlatformSettings] = field(default_factory=dict)
    intake: IntakeSettings = field(default_factory=IntakeSettings)
    # 店铺所在站点的时区（IANA 名称），「单店每日写操作次数」按站点本地自然日计算；没配按 UTC
    shop_timezones: dict[str, str] = field(default_factory=dict)
    # 影子模式：这些操作类型的提案只记录、不推送审批，用来和人工实际操作比对吻合率（DESIGN.md 15.1）
    shadow_actions: list[str] = field(default_factory=list)
    daily_report: ReportSettings = field(default_factory=ReportSettings)
    send_permissions: dict[str, SendPermission] = field(default_factory=dict)
    # 场景 SOP（skill）所在目录，agent 按场景动态加载（DESIGN.md「Skill 动态载入」）
    # 项目自带的 skills/ 总是第一个（同名时优先），再加配置的目录
    skill_dirs: list[Path] = field(default_factory=lambda: [SKILLS_DIR])

    def approver_for(self, shop_key: str) -> Approver | None:
        return self.approvers.get(shop_key)


def load_raw(path: str | Path | None = None) -> dict:
    """settings.toml 叠加控制台改过的 settings.console.toml（同目录）。"""
    from .config_overrides import deep_merge, load_overrides
    path = Path(path) if path else SETTINGS_FILE
    raw: dict = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return deep_merge(raw, load_overrides(path.with_name("settings.console.toml")))


def load_settings(path: str | Path | None = None) -> Settings:
    return settings_from_raw(load_raw(path))


def settings_from_raw(raw: dict) -> Settings:

    db_path = Path(raw.get("storage", {}).get("db_path", "data/ecom_twin.db"))
    if not db_path.is_absolute():
        db_path = DATA_HOME / db_path

    memory_dir = Path(os.path.expanduser(raw.get("storage", {}).get("memory_dir", "memory")))
    if not memory_dir.is_absolute():
        memory_dir = DATA_HOME / memory_dir

    approval = raw.get("approval", {})
    return Settings(
        db_path=db_path,
        memory_dir=memory_dir,
        recall_retention_days=raw.get("storage", {}).get("recall_retention_days", 90),
        expire_hours=approval.get("expire_hours", 24),
        remind_before_hours=approval.get("remind_before_hours", 4),
        batch_window_seconds=approval.get("batch_window_seconds", 0),
        double_approval_risks=approval.get("double_approval_risks", ["高"]),
        approvers={k: Approver(**v) for k, v in raw.get("approvers", {}).items()},
        limits=Limits(**raw.get("limits", {})),
        feishu=FeishuSettings(**raw.get("feishu", {})),
        runtime=RuntimeSettings(**raw.get("runtime", {})),
        browser=BrowserSettings(**raw.get("browser", {})),
        computer_use=ComputerUseSettings(**raw.get("computer_use", {})),
        platforms={k: PlatformSettings(key=k, **v) for k, v in raw.get("platforms", {}).items()},
        intake=IntakeSettings(**raw.get("intake", {})),
        shop_timezones=dict(raw.get("shop_timezones", {})),
        send_permissions={k: SendPermission(**v) for k, v in raw.get("send_permissions", {}).items()},
        shadow_actions=list(raw.get("shadow", {}).get("actions", [])),
        daily_report=ReportSettings(**{**raw.get("daily_report", {}),
                                       **({"columns": {**ReportSettings().columns,
                                                       **raw["daily_report"]["columns"]}}
                                          if "columns" in raw.get("daily_report", {}) else {})}),
        skill_dirs=[SKILLS_DIR] + [Path(os.path.expanduser(d)) for d in raw.get("skills", {}).get("dirs", [])],
    )


# 底座 CLI 自己登录要用的变量：.env 里就算写了也不能从 agent 环境里拿掉，否则底座起不来
_RUNTIME_AUTH_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "OPENAI_", "CODEX_", "CURSOR_", "OPENCODE_")
# ecom-twin 自己的密钥，不在 .env 里（比如直接 export 的）也要拿掉
_KNOWN_SECRETS = ("MIAOSHOU_APP_KEY", "MIAOSHOU_APP_SECRET", "FEISHU_APP_ID", "FEISHU_APP_SECRET")


def agent_hidden_env(path: str | Path | None = None) -> set[str]:
    """不能进 agent 进程环境的变量名：.env 里的（妙手、飞书凭证等）加上已知的 ecom-twin 密钥。

    agent 进程原本复制 ecom-twin 的整个环境，.env 载入后密钥就在里面 —— 打开 AppleScript 方式时一句
    do shell script "env" 就能读到。拿掉不影响工具：ecom-twin 的 MCP 服务、代理启动时自己重新读 .env。"""
    path = Path(path) if path else ENV_FILE
    keys = set(_KNOWN_SECRETS)
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                keys.add(line.partition("=")[0].strip())
    return {k for k in keys if not k.startswith(_RUNTIME_AUTH_PREFIXES)}


def load_dotenv(path: str | Path | None = None) -> None:
    """极简 .env 解析：只补充进程里还没有的变量，进程环境变量优先。"""
    path = Path(path) if path else ENV_FILE
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
