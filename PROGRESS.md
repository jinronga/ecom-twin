# 实现进度

> 对照 `DESIGN.md` 记录哪些已实现、哪些没实现。每次实现或调整后更新本文件。
> 最后更新：2026-10-29 · 当前阶段：离线补全 8 批已移植；记忆按数字分身改造；本地打包第 1 步完成；修复 Codex 继承本人插件的漏洞；操作电脑两种方式（Computer Use、AppleScript 均已用 Claude Code 真实跑通），均默认关闭；AppleScript 方式补齐数字分身的原生窗口取证；提示词吸收数字分身派单契约（分诊首行、处理流程、HITL、验证程度、汇报格式），发送出口加内容闸门；全量离线测试 322 个通过、1 个跳过

状态说明：✅ 已实现并有离线测试　🟡 已实现但依赖真实环境、尚未验证　⬜ 未实现

离线测试：`tests/` 共 322 个用例通过、1 个跳过（`.venv/bin/python -m pytest -q`）；覆盖功能 1-8、补全 8 批（上品闭环、消息入口与合并、审批增强、运行观测、看板其余表、控制台、影子模式、日报巡检）、任务队列、审批、记忆、看板、飞书消息转换、妙手适配器、定价、浏览器取证、平台操作、CLI。真实飞书应用认证及消息收发尚未验证。
**所有 🟡 项都要在第五节「统一测试清单」里过一遍，过了才能改成 ✅。**

## 一、已实现的功能

### 基础

| 功能 | 设计章节 | 状态 | 代码 |
|---|---|---|---|
| 配置加载（settings.toml + .env，密钥只走 .env） | 14.3 | ✅ | `config.py` |
| 本地状态库（任务、步骤、提案、执行记录、审计、看板队列、跟进、记忆草稿、召回记录、事件去重） | 11.5 | ✅ | `store.py` |
| 命令行：setup / login / init / check / health / propose-publish / serve / ask / tick / memory / runtimes / demo | — | ✅ | `cli.py` |
| 一次性自检 `ecom-twin check`：配置校验、底座 CLI、妙手凭据与接口、飞书原生认证或 lark-cli 授权 | — | ✅ | `config.validate_settings`、`cli.cmd_check` |
| 本地飞书认证配置页：凭证、负责人、审批人、店铺、运行时、可选看板；连接测试和任务服务启停 | 12.2 | ✅ | `setup_web.py`、`setup_config.py`、`web/setup.html`；真实应用认证待验证 |
| 缺失工具自动安装：飞书 SDK、所选 agent CLI、必要的 Node.js；后台状态和失败重试 | 12.2 | ✅ | `tooling.py`；本机飞书 SDK 安装已完成，CLI 安装/校验失败和重试逻辑有离线覆盖 |
| Agent 专用 Chrome DevTools MCP、隔离配置与证据截图收集 | 5.5、12.2 | ✅ | `config.py`、`dispatcher.py`、`evidence.py`、`tooling.py`；Chrome 操作和截图需真实授权后验证 |
| 专用 Chrome 全部放开（和数字分身一样）：跳转、点击、填写、执行脚本经 `chrome_guard` 代理，卖货 / 广告 / 妙手后台网址和脚本拦截，写类操作留审计；`interactive = false` 回到只读；控制台规则配置可开关（高风险） | 5.6 | ✅ 2026-10-30 Claude Code + 真实 Chrome 实测：后台新开标签页、点链接读回跳转结果、打开 sellercentral 被拦，审计三条齐全 | `chrome_guard.py`、`prompts/browser_interactive.md`、`tests/test_chrome_guard.py` |
| 修复：chrome-devtools-mcp 同时收到 --browserUrl 和 --userDataDir 会直接退出，配了 user_data_dir 时专用浏览器从来连不上；现在只传 --browserUrl。代理在上游退出后立刻回错误（原来 agent 只能干等 30 秒超时） | 5.5 | ✅ | `chrome_guard.upstream_command`、`computer_use.Proxy.serve` |
| 离线演示（假飞书 + 假妙手跑通整条链路） | — | ✅ | `ecom-twin demo` |

### 数据接入

| 功能 | 设计章节 | 状态 | 代码 |
|---|---|---|---|
| 妙手适配器：签名、调用、错误码统一归类、接口路径外置 | 6.5 | ✅ | `adapters/miaoshou.py`、`config/adapters/miaoshou.toml` |
| 妙手适配器：连真实接口 | 6.3 | 🟡 | `ecom-twin health`；2026-10-18 已实测店铺、采集箱、商品详情、类目树和类目属性只读接口 |
| 妙手每日健康检查，失败当天告警一次 | 6.5 | ✅ | `scheduler.health_check_job` |
| 妙手 TikTok 类目树、类目属性（只读，已开放给 agent） | 8.5 | 🟡 | `adapters/miaoshou.py`、`mcp_server.py` |

### 提案、审批、执行

| 功能 | 设计章节 | 状态 | 代码 |
|---|---|---|---|
| 数字由代码算：定价（与现有流水线公式一致） | 1.3、7.3 | ✅ | `numbers.price_sku` |
| 数字由代码算：理由里找不到出处的数字 → 拒收 | 7.3 | ✅ | `numbers.check_rationale_numbers` |
| 提案：证据、后果由代码计算；提交时先查硬上限 | 7.2、7.3 | ✅ | `proposals.py` |
| 写操作：经妙手发布到 TikTok | 8.5 | 🟡 | `actions.TiktokPublishViaMiaoshou` |
| 执行器：有效期 → 漂移 → 硬上限 → 执行 → 留痕；同店铺串行 | 7.1、7.4 | ✅ | `executor.py` |
| 审批卡片（后果说明、数字来源、有效期） | 7.3 | ✅ | `approval.render_card` |
| 审批回复：批准 / 拒绝必选原因、审批人校验、主审批人和代理审批人 | 7.5 | ✅ | `approval.py` |
| 过期前提醒（同时通知代理审批人）、过期作废 | 7.5 | ✅ | `ApprovalService.sweep` |
| 执行后回读确认上架（轮询约 1 小时，超时通知人工检查） | 7.1 | ✅ | `followups.py` |
| 执行后第 3 天、第 7 天复盘提醒（回复可并回任务） | 7.1 | ✅ | `followups.py` |
| 飞书真实发卡片、收审批回复 | 7.5 | 🟡 | `feishu_native.NativeFeishu` / `feishu.LarkCliFeishu`；原生消息转换和 SDK 回调已有离线测试，待真实凭证 |

### 任务、调度、agent

| 功能 | 设计章节 | 状态 | 代码 |
|---|---|---|---|
| 任务服务：建单、状态、步骤、心跳 | 11.5 | ✅ | `tasks.py` |
| 私聊建单：负责人私聊分身 → 建任务 → 调 agent → 汇报回 thread（文本、图片附件） | 10.1、14.2 P1 | ✅ | `dispatcher.py`、`feishu_native.py` |
| 在汇报 / 提问 / 复盘提醒下回复 → 并回原任务，续跑同一会话 | 10.1 | ✅ | `dispatcher.py` |
| 审批被拒绝 → 带着原因唤醒 agent 调整 | 4.5 | ✅ | `Dispatcher.on_proposal_rejected` |
| 底座失败：可重试错误原地重试，连续失败切换备选底座，记录切换 | 5.6.5 | ✅ | `dispatcher.py` |
| agent 系统提示词（只能经提案写入、数字来源、记忆核实、防注入） | 5、13.3 | ✅ | `prompts/system.md` |
| 分诊首行（relevant / reason / waiting）：摘出存档、汇报显示等待、写了等待的任务转「等待回复」、群消息不相关不回群；waiting 喂给「等待唤醒」合并判定并开问后窗口 | 10.1、4.4 | 🟡 离线 ✅；Claude Code 真实任务已按格式输出并正确解析，群里的合并效果待真实群验证 | `dispatcher.parse_triage`、`intake.py`、`tests/test_triage.py` |
| 提示词吸收数字分身派单契约：七步处理流程、动手前和交付前看最新消息防重复操作、需要负责人介入时（只停受影响部分）、数据对不上时信谁（电商版）、验证做到什么程度（提交≠批准≠执行≠上架≠买家可见）、汇报格式、破坏性操作和注入处理 | 5、10.1 | ✅ | `prompts/system.md` |
| 发送出口内容闸门：不 @所有人、不带凭证（正则 + .env 原值）、群里单条 ≤500 字；notify_chat 不合规拒收并给改法，系统汇报自动打码、超长转私聊 | 10.1、13.3 | ✅ | `outbound.py`、`tests/test_outbound_guard.py` |
| MCP server：只读查询、定价计算、提交提案、上报步骤、召回记忆、提交记忆草稿、向负责人提问 | 5.5、5.6.3 | ✅ | `mcp_server.py` |
| MCP server：真实子进程走通 stdio 协议 | 5.6.3 | ✅ | 测试 `test_mcp_server_process_speaks_stdio` |
| 运行时：Claude Code、Codex | 5.6 | 🟡 | `runtime/claude_code.py`、`runtime/codex.py` |
| 运行时：Cursor Agent、OpenCode（参数按本机 --help 编写） | 5.6.1 | 🟡 | `runtime/cursor_agent.py`、`runtime/opencode.py` |
| 运行时超时强制结束 | 5.6 | ✅ | `runtime/base.py` |
| 本机 agent CLI 路径配置与验证（以配置为准，留空自动查找，`--version` 验证） | 5.6.1、12.2 | ✅ | `runtime/cli_check.py`、`ecom-twin runtimes` |
| 本地调试模式：飞书消息只打印不发送（MCP 子进程同样生效） | — | ✅ | `feishu.ConsoleFeishu`、`ecom-twin ask --console` |

### 看板、记忆、定时

| 功能 | 设计章节 | 状态 | 代码 |
|---|---|---|---|
| 看板回写：按记录合并、节流、失败退避重试 | 11.5 | ✅ | `board.py` |
| 看板字段：状态、当前步骤、心跳、运行时、token 用量、切换记录、结论、卡点 | 11.2 | ✅ | `board.task_fields` |
| 看板写入真实多维表格 | 11.1 | 🟡 | 需要先建表 |
| 记忆库：Markdown + frontmatter、开局只注入索引、关键词召回 | 9.2 | ✅ | `memory.py`、`memory/` |
| 调度启动自动召回任务相关记忆并注入本轮上下文 | 9.2、5.2 | ✅ | `dispatcher.py`、`memory.py`；每轮仍可用 MCP 追加召回 |
| 召回记录 | 9.2 | ✅ | `ecom-twin memory recalls` |
| 记忆草稿：agent 提交，人工采纳 / 驳回 | 9.2 | ✅ | `ecom-twin memory drafts / approve / reject` |
| 拒绝原因每周汇总成记忆草稿 | 9.2 | ✅ | `MemoryStore.rejection_digest` |
| 大促日历记忆（模板，日期待运营核对） | 9.2 | ✅ | `memory/promo-calendar.md` |
| 定时调度器（审批巡检、看板回写、执行后跟进、拒绝原因汇总、健康检查） | 4.2 | ✅ | `scheduler.py` |

### 自检修复（2026-10-16）

| 问题 | 影响 | 修复 |
|---|---|---|
| 多线程共用一个数据库连接，事务可能被其他线程的写入穿插 | serve 里消息、定时、任务线程同时写库时数据错乱 | 连接加可重入锁，事务期间持锁；文件库开启 WAL |
| 群聊里任何人发「同意」都会触发审批回执 | 群里刷屏 | 群聊只理会审批人的消息 |
| lark-cli 事件流进程退出后服务悄悄停止收消息 | 分身「失联」 | 自动重连，退避 5 秒起最长 5 分钟 |
| 同一任务连续两条回复会同时续跑同一会话 | 会话冲突、重复汇报 | 同一任务串行执行 |
| 配置缺漏（没填 owner、白名单为空等）到运行时才暴露 | 消息发不出、写操作全被拒 | `ecom-twin check` 和 serve 启动时校验配置 |
| lark-cli 错误写在 stderr，原代码只解析 stdout | 报错信息不清楚 | 两边都解析，提取错误原因 |

### 智能能力（2026-10-26 同步补充）

| 功能 | 设计章节 | 状态 | 代码 |
|---|---|---|---|
| 功能1：飞书上下文智能拉取（按需取消息 / 会话 / 话题 / 搜索），消息规范化 | 11.5 | ✅ | `mcp_server.py`、`feishu.py`、`feishu_native.py` |
| 功能2：运行时任务跟踪器（记录每次运行的时间、底座、重试、切换、结果） | 11.5 | ✅ | `task_tracker.py`、`dispatcher.py` |
| 功能3：场景 SOP 动态加载（多目录扫描、同名取优先级、agent 按需 `load_sop`） | Skill 动态载入 | ✅ | `sops.py`、`mcp_server.py`、`skills/` |
| 功能4：店铺背景查询（白名单状态、审批人、硬上限、ERP 店铺信息） | 11.5 | ✅ | `mcp_server.py` 的 `get_shop_context` |
| 功能5：只读网页取证（白名单域名公开页面截图 + 正文提取，不能点击/登录） | 13.1 | ✅ | `browser.py`、`mcp_server.py` 的 `capture_page`；真实 Chrome 抓取已验证 |
| 功能6：内部平台浏览器操作（海外仓、OA 审批等非卖货后台），可操作配置和 MCP 工具 | 13.2 | ✅ | `platform_browser.py`、`actions.BrowserFlow`、`mcp_server.py` 的 `platform_list`/`platform_open`、`ecom-twin browser-login` |
| 功能7：记忆维护控制台（本地 Web UI，记忆库增删改、草稿采纳驳回、召回记录查看） | 12.2 | ✅ | `console.py`、`web/console.html`、`ecom-twin console`；配套的 `memory.py`（增删改、先改后采纳、驳回原因、防路径穿越、审计）、`store.audit_log`、`[storage] memory_dir` 已于 2026-10-28 补齐（a517381 提交时漏了这些，导致控制台导入即报错） |
| 功能8：数据库自动迁移（自动补齐缺失列，如 `tasks.origin_chat_id`、`steps.evidence`） | 11.5 | ✅ | `store.py` |

### 离线补全 8 批（2026-10-27 本地实现，2026-10-29 移植到仓库）

采集箱暂存区写入不逐次审批、只在发布时审批一次（用户 2026-10-27 决定）；agent 的浏览器只给截图和读页面（用户决定）。

| 批次 | 内容 | 设计章节 | 状态 | 代码 |
|---|---|---|---|---|
| 1 | P1 上品闭环：按货源链接采集、编辑公共采集箱草稿、认领到 TikTok 采集箱 / 店铺、编辑店铺维度商品；暂存区改动留痕并列入发布卡片；合规检查；上品 SOP | 8.4、8.5 | 🟡 离线 ✅，真实妙手写入待验证 | `staging.py`、`compliance.py`、`config/compliance.toml`、`skills/tiktok-listing-via-miaoshou/`；8 个 `staging_` / 合规工具 |
| 2 | 消息入口：群消息过滤、静默攒批、合并判定（回复 → 锚点 → 等待唤醒 → 主题 → 同类）、驱动 / 静音、问后窗口、代发权限 | 4、5.4 | 🟡 离线 ✅，模型判定和真实群消息待验证 | `intake.py`、`outbound.py`、工具 `notify_chat`；图片附件和事件自带的 parent_id 经攒批缓冲保留 |
| 3 | 审批增强：同类提案合并一张卡逐条勾选、修改后批准并重算后果、高风险双人审批 | 7.5 | ✅ | `approval.py`、`actions.apply_edits` |
| 4 | 运行观测：运行中每 30 秒心跳、agent 原始输出存档、成本月度汇总、按站点时区算每日写次数 | 11.5、10.2、15.4、14.1 | ✅ | `dispatcher.py`、`run_usage` 表、`ecom-twin costs`、`[shop_timezones]` |
| 5 | 看板其余表（消息、执行记录、代发、召回、复盘）和表内改状态反向生效 | 10.2、11.1、11.6 | 🟡 离线 ✅，真实多维表格待验证 | `board.export_logs`、`board.pull_changes` |
| 6 | 控制台：规则配置（写 `settings.console.toml`、保存前校验、高风险二次确认、审计）、投递与合并、本机 CLI、成本、影子模式 | 12.2、12.3 | ✅ | `config_overrides.py`、`console.py`、`web/console.html` |
| 7 | 影子模式：按操作类型开关、导入人工实际操作、吻合率 | 15.1 | ✅ | `shadow.py`、`ecom-twin shadow` |
| 8 | 运营日报和异常巡检框架：导入 ERP 导出表格，确定性计算 ACOS、TACOS、销售额下滑、可售天数 | 4.2、8.1 | ✅ 框架；真实数据源未接 | `daily_report.py`、`ecom-twin report` |

### 移植时一并对齐和修复（2026-10-29）

| 问题 | 处理 |
|---|---|
| 功能 1 飞书上下文：工具缺 `feishu_context`、任意会话可读、搜索没有开关、富文本没拍平 | 改用本地实现：`feishu_context` / `feishu_chat_history` / `feishu_thread` / `feishu_search`，只读来源会话和 `readable_chats`，搜索需 `allow_user_search` |
| 功能 2 开局上下文：系统提示词里没有任务卡片 | 补任务卡片（标题、来源、状态、店铺、锚点、上轮结论和卡点、提案及拒绝原因、已完成步骤） |
| 功能 3 SOP：清单没注入提示词；`load_sop` 不提示脚本不能运行、名称不存在时不返回可用列表 | 改用本地 `SopLibrary`；项目自带 `skills/` 总是排第一 |
| 功能 4 店铺背景：按 `shop_id` 匹配，妙手实际返回 `shopId`，ERP 信息永远为空 | 改用本地实现，按 `shopId` 匹配 |
| Chrome DevTools MCP 开放全部工具（点击、填写、执行脚本），可绕过审批 | 只放行 `take_screenshot`、`take_snapshot`、`list_pages`、`select_page`、`wait_for`；打开公开网页改用有域名白名单的 `capture_page` |
| Codex、Cursor、OpenCode 没有工具白名单 | 三个底座都按同一份白名单写配置（Codex `enabled_tools`、Cursor `Mcp(server:tool)`、OpenCode `tools`）；真实 CLI 待验证 |
| `ecom-twin browser-login` 运行即崩溃 | 改用本地实现（`interactive_login` + `session_ok`） |
| 执行器只捕获 `AdapterError`，浏览器流程报错时提案卡住、服务退出 | 处理器任何异常都记为执行失败；`serve` 单条消息出错写审计后继续收消息 |
| 原生飞书凭据下搜索报 AttributeError | `NativeFeishu` 沿用 lark-cli 做用户身份搜索；新增原生图片回复（汇报附证据截图） |
| 任务队列：运行中收到的新回复被旧轮次标成已完成而丢失；排队中的两条消息互相覆盖 | 只结束仍在运行的那一轮；排队中的消息追加合并；入队和领取加锁 |
| `serve` 里创建了两个调度器 | 合并为一个（带合并判定模型） |

### 记忆改造（2026-10-29，对照本机数字分身 `/Applications/数字分身.app` 的记忆设计）

| 项 | 内容 | 状态 | 代码 |
|---|---|---|---|
| 1 按动作召回 | 索引每行「名称 — 触发动作」；agent 先列动作再比索引，比中用 `read_memory` 读全文，拿不准才 `search_memory` 兜底（名称 6 / 描述 3 / 小节 2 / 正文 1 加权，中文二字组，返回命中小节摘录）。去掉调度器每轮自动塞记忆正文 | ✅ | `memory.py`、`mcp_server.py`、`prompts/system.md`、`dispatcher.py` |
| 2 四种草稿模式 | append / revise / remove / create；改和删的原文在采纳那一刻逐字匹配（恰好一次），人工中途改过就拒绝；人工改后采纳另存，原稿保留 | ✅ | `memory.propose` / `apply_draft` / `approve`、控制台草稿页 |
| 3 召回留痕 | 每次读取、检索记进召回记录（区分读取 / 检索，任务号由服务端填）；提示词要求汇报末尾写本轮召回、没核实时逐字写固定句；记忆和现场冲突信现场并提草稿 | ✅ 记录；🟡 agent 是否照写待真实运行验证 | `store.log_recall`、`prompts/system.md` |
| 4 草稿规矩 | 默认一轮 0 条；不该提的情况；why、evidence 必填；疑似凭证拒收；相同草稿不重复入队；绝对日期；群里「记一下」不算指令 | ✅ | `memory.propose`、`prompts/system.md` |
| 5 索引预算 | description ≤120 字（硬，保存和采纳时拒绝）；100 条 / 8KB 软预算；`ecom-twin memory check`；控制台显示预算和问题 | ✅ | `memory.check` / `budget`、`cli.py`、控制台 |

### 操作电脑与 Codex 隔离（2026-10-29）

| 项 | 内容 | 状态 | 代码 |
|---|---|---|---|
| Codex 配置隔离（漏洞修复） | Codex 底座原来读本人 ~/.codex，带进 Computer Use、浏览器插件、ChatGPT 应用连接器，绕开工具白名单。改为 ecom-twin 自己的 CODEX_HOME（数据目录 `data/runtime/codex-home`，只链接 auth.json，只抄推理强度和自定义提供方）+ 命令行关掉 computer_use、browser_use、apps、js_repl。不再照抄 model：桌面端写的 gpt-6.1-sol 在 Homebrew codex 0.156 下报 model_not_found | ✅ 2026-10-29 真实 Codex 验证：隔离后可用工具里没有 cua_repl / computer use / 浏览器 / 连接器 | `runtime/codex.py`、`runtime/base.runtime_env` |
| 操作电脑（任意 Mac 软件） | 和数字分身同源：借用本机 Codex Computer Use 的 `cua_repl`。默认关闭；打开后所有底座经代理使用：只放行 js / js_reset；Computer Use 每碰一个 app 都会发授权征询（elicitation），由代理按 `allowed_apps` 白名单回答、绝不转给 agent（默认空 = 都不批，录音一律拒绝）；禁止目标（卖家后台、广告后台、密码管理器）直接拦截；代理替 cua_repl 回绝新版 Claude Code 的 `server/discover` 探测（否则 rmcp 卡死、连接超时）；每次调用和批准写审计；提示词要求对外生效 / 不可撤回的动作先 ask_owner 或提案 | ✅ 2026-10-29 真实任务（Claude Code）：打开「计算器」算 1288 × 0.85，按界面元素操作，读回 1,094.8；名单外 app 被拒、卖家后台被拦已验证。🟡 其他底座、真实业务软件待验证 | `computer_use.py`、`prompts/computer_use.md`、`[computer_use]` |
| 操作电脑：AppleScript（数字分身操作桌面软件的做法） | 核实数字分身（2026-10-30 按 301 个任务会话的实际工具调用重新统计）：网页靠 CDP（chrome-devtools MCP 2771 次 / 88 个会话，是主力），桌面软件靠 Bash 跑 open -a、osascript（含 System Events 界面脚本）、screencapture（osascript 56 次 / 13 个会话），没用过 Computer Use，也没用 CDP 连过 Electron 桌面软件。此前写的「osascript 48 次」只统计了 osascript、漏了 CDP，已更正。ecom-twin：`[computer_use] applescript`（默认关）只放行 osascript / open / cliclick，其它命令拒绝；取证照数字分身 window-capture / evidence-image：MCP 工具 desktop_windows（编译小工具读 CGWindowID）、desktop_capture（`screencapture -l` 按窗口截，不激活、被挡也完整，禁止项窗口不列不截，写审计；boxes 按屏幕坐标换算成截图像素画红框）、annotate_screenshot（按像素画框，另存 marked，原图不动），不再给整屏 screencapture；提示词补「不激活或前置任何窗口」和留证规则；Cursor、OpenCode 写了同样的放行配置；Codex 沙箱拦 AppleScript（-10827），在 AGENTS.md 注明不可用。已知缺口：AppleScript 的 do shell script 只靠提示词禁止，审批流程后续优化 | 🟡 Claude Code 命令放行已实测（osascript 通过、cat 和 && 拼接被拒）；2026-10-29 Claude Code 真实任务跑通：System Events 读 Finder 界面元素 → desktop_capture 按元素坐标截图标框 → 看图核对 → report_step 上报 → 汇报附图，全程没有 activate。Cursor / OpenCode 未验证；飞书真实发图待真实凭证 | `computer_use.applescript_section`、`prompts/applescript.md`、`desktop.py`、`mcp_server`（desktop_*）、各 runtime 的 shell_commands |

### 本地打包（2026-10-29 启动；只支持 macOS，每人一份，参照数字分身的打包方式）

| 步骤 | 内容 | 状态 |
|---|---|---|
| 1 代码和数据分开 | `paths.py` 统一决定位置：资源（skills、适配器配置、合规词库、记忆初始内容）随版本发；每个人的数据装好后在 `~/Library/Application Support/EcomTwin/`（目录 0700，首次运行用初始内容垫一份记忆库，只补缺不覆盖）；仓库直接跑时不变；`ECOM_TWIN_HOME` / `ECOM_TWIN_RESOURCES` 可覆盖 | ✅ `paths.py`、`tests/test_paths.py` |
| 2 自带 Python 运行环境 | python-build-standalone + 装好的 ecom-twin；打进包还是安装时下载待定 | ⬜ |
| 3 launchd 常驻和运维命令 | `serve` 交给 launchd（KeepAlive、登录后自启）；`ecom-twin start / stop / restart / status / doctor / logs / uninstall` | ✅ `service.py`、`tests/test_service.py`；单测用假 launchctl，plist 过了 `plutil -lint`；本机没装（真装要有可用的飞书凭证，否则 serve 会反复崩） |
| 4 轻量 .app | 双击：装服务、打开本地配置页 / 控制台；不用 Electron | ⬜ |
| 5 dmg | ad-hoc 签名；资源随包 | ⬜ |

## 二、与设计的偏差（需要时回写 DESIGN.md）

| 偏差 | 原因 | 后续 |
|---|---|---|
| 审批用「回复文本指令」，不用卡片按钮 | 原生 SDK 当前仅注册消息事件，按钮回调尚未实现 | 原生消息直接使用 parent_id；兼容 CLI 时仍支持调接口反查卡片 |
| 配置暂放 `config/settings.toml`，没放多维表格 | 先跑通链路 | 多维表格建好后迁移店铺登记、负责对象、硬上限 |
| 「单店每日写次数」按 UTC 自然日 | 还没有店铺时区数据 | 店铺登记补时区后按站点本地日期 |
| 漂移、超限导致的失败用「执行失败」+ 原因区分 | 设计里没有「已失效」状态 | 视看板体验决定是否加状态 |
| 任务场景先标「待定」，由 agent 加载 SOP 时判断 | 不在入口做硬编码意图分流（DESIGN 5.2） | — |
| 第 3 / 7 天复盘目前是提醒，不是数据复盘 | 效果数据还没接入 | P2/P3 接入领星和广告数据后改成数据复盘 |
| 没有效果数据时，复盘回复由 agent 处理后提交记忆草稿 | 复盘提醒的回复会并回原任务 | — |

## 三、还没做的（P0、P1 范围内）

- [ ] 发布前漂移校验目前只比对标题；售价、SKU 比对要等真实接口结构
- [ ] 卡片按钮审批（已接入 SDK 长连接，按钮事件仍未实现）；群里按真实 @ 名单判断是否点名
- [ ] TOPWMS 等仓库平台的具体接入（商品建档、备货单、条码下载）：通用浏览器流程已有，具体平台配置、Playwright 依赖、单号回读和条码交付未做（见 `功能缺口清单-20261028.md`）
- [ ] 按场景产物判断业务完成（例如仓库制单要回读到单号）：通用的「汇报说法对系统记录」已于 2026-11-07 完成，各场景的专用产物要等对应场景接入
- [ ] AppleScript 方式的对外生效动作（提交、删除等）仍只靠提示词停在最后一步（do shell script 绕过已于 2026-11-06 用 hook 拦截）
- [ ] Codex 自带的联网（web__run）和多 agent 协作工具仍可用，是否关闭待定
- [ ] Claude Code 自动记忆目录（`autoMemoryDirectory`）未配置；提示词已声明与人工记忆库冲突时以人工记忆为准

## 四、未开始（按 DESIGN.md 14.2 分期）

| 阶段 | 内容 |
|---|---|
| P2 | 领星只读接入（日报和异常巡检框架已有，目前用 ERP 导出表格） |
| P3 | 广告巡检和调价、否词（影子模式框架已有）；Amazon Ads API |
| P4 | Listing 优化、关键词周报；控制台 Listing 对比页 |
| P5 | 已完成离线部分（群消息入口、四层合并、问后窗口、代发权限）；待真实群验证 |
| P6 | 选品；Amazon、Noon 扩展 |
| — | 多 agent（队长 + 小队）、能力矩阵、记忆召回改进（按轮次归组、记录实际依据的记忆） |

## 五、统一测试清单（真实环境）

按顺序测，每一步通过后把对应 🟡 改成 ✅，失败的记到「测试记录」。

**准备**
1. `.env` 填妙手 AppKey / AppSecret；如开了 IP 白名单，把本机出口 IP 加进去
2. 运行 `ecom-twin setup`，填写飞书自建应用 App ID / Secret；后台启用机器人、开通单聊收发和消息读取权限、订阅 `im.message.receive_v1` 长连接事件，发布应用版本
3. 在配置页填写 `owner_open_id`、审批人、店铺白名单（`tiktok:店铺ID`），选择运行时；保存后自动准备缺失工具，首次使用通过 `ecom-twin login 底座名` 登录
4. 多维表格：建任务表、步骤表、提案表，列名与 `board.py` 一致，填 app_token 和 table_id
5. `ecom-twin init`

先在配置页测试已保存的连接，再启动任务服务并私聊测试；也可运行 `ecom-twin check`。2026-10-21 当前机器未填写真实飞书凭证和负责人；Claude Code / Codex 可用，飞书 SDK 已安装，Cursor / OpenCode 可按选择自动安装。店铺白名单为空时可以查询，写操作会被拒绝。

**逐项测试**

| # | 测什么 | 怎么测 | 通过标准 |
|---|---|---|---|
| 1 | 本机 CLI | `ecom-twin runtimes` | 默认和备选底座都「验证通过」 |
| 2 | 妙手只读 | `ecom-twin health` | 列出 TikTok 店铺 |
| 3 | 飞书发消息 | `ecom-twin ask "列出我的 TikTok 店铺"`（不加 --console） | 私聊收到汇报 |
| 4 | Claude Code 真实调用 + MCP | 同上，看汇报内容 | agent 调用了 `miaoshou_list_shops`，汇报里的店铺和妙手一致；看板有步骤 |
| 5 | Codex / Cursor / OpenCode | `ecom-twin ask "..." --runtime codex`（cursor、opencode 同理） | 各底座都能完成并汇报；记录事件格式与代码不一致的地方 |
| 6 | 私聊建单 | `ecom-twin serve`，在飞书私聊分身发一句任务 | 收到「已建任务」回执，随后收到汇报 |
| 7 | thread 续跑 | 在汇报下回复追加指令 | 收到「已并回」，agent 续跑同一会话 |
| 8 | 上品提案 + 审批卡片 | 私聊让分身把采集箱里某个商品按规则上架到测试店铺 | 收到审批卡片，售价和毛利由代码计算 |
| 9 | 拒绝 → 唤醒 | 回复「拒绝 P-xxxx 2 备注」 | agent 被唤醒并调整 |
| 10 | 批准 → 执行 → 回读 | 回复「批准 P-xxxx」 | 妙手发布成功；约数分钟后收到回读确认 |
| 11 | 过期提醒 | 把 `expire_hours` 调小重新提案 | 提醒主审批人和代理人，过期后作废 |
| 12 | 看板 | 打开多维表格 | 任务、步骤、提案状态正确，运行时和 token 有值 |
| 13 | 记忆 | agent 提交记忆草稿后 `ecom-twin memory drafts` / `approve` | 草稿可见，采纳后写入 `memory/` |
| 14 | 健康检查告警 | 故意填错 AppSecret 后 `ecom-twin tick` | 当天收到一次告警 |
| 2 | 入口与驱动：群消息入口、静默攒批、四层合并、问后窗口、驱动 / 静音、代发权限、「已告一段落」 | 4、5.4 | ✅ `intake.py`（过滤、攒批、回复 / 锚点 / 等待唤醒 / 主题 / 同类五层判定、静音）、`outbound.py`（代发闸门和记录）、工具 `notify_chat`；模型判定用本机 Claude Code + haiku，真实效果待验证 |
| 2 | 妙手只读 | `ecom-twin health` | 列出 TikTok 店铺 |
| 15 | 上品闭环（按链接） | 私聊发一个 1688 链接和上品规则 | agent 采集 → 改标题（过合规）→ 认领 → 写店铺信息 → 提交发布提案；卡片里「暂存区改动」逐条列出 |
| 16 | 群消息入口与合并 | `[intake] listen_chats` 配一个测试群；群里先说两句、再 @分身；再发带同一单号的消息 | 攒批后只建一个任务；带单号的消息并回同一任务；「投递与合并」页能看到判定层 |
| 17 | 代发权限 | 群没开权限 / 开了 reply 各试一次 | 没权限时回执和汇报改发负责人私聊；开了在群 thread 里回 |
| 18 | 问后窗口 | agent 在群里提问（开 ask 权限）后，有人直接在群里回答（不回复 thread） | 答复被并回任务并唤醒 |
| 19 | 审批增强 | `batch_window_seconds = 60`，连续提交两个提案；把一个提案风险改成「高」 | 合并成一张卡，「批准 1」只执行第一个；「修改 P-x 毛利率=35%」重算后执行；高风险要两个人批 |
| 20 | 看板反向 | 在多维表格把任务状态改成「已关闭」「重试」 | 一分钟内生效；审计里有 board_reverse |
| 21 | 控制台规则配置 | `ecom-twin console` → 规则配置改一个高风险项 | 弹二次确认；`settings.console.toml` 有改动，`settings.toml` 不变；变更记录有记录 |
| 22 | 日报 | 用 ERP 导出表格 `ecom-twin report import`，再 `report run --agent` | 日报数字和表格一致；有异常时建「异常巡检」任务 |
| 23 | 影子模式 | `[shadow] actions = ["miaoshou.tiktok.publish"]` 后让分身上品；人工在妙手里上架其中一款 | 不推审批；`ecom-twin shadow report` 显示吻合率 |
| 24 | 成本、心跳、存档 | 跑几个任务后 `ecom-twin costs`；看 `data/transcripts/` | 有按底座、场景、店铺的汇总；每轮有原始记录文件；任务运行中心跳持续更新 |
| 2026-10-27 | 补全所有能离线做的功能（8 批）：P1 上品闭环（暂存区工具、合规检查、上品 SOP）、消息入口与驱动（群入口、攒批、五层合并、静音、代发闸门、问后窗口）、审批增强（合并卡片、修改后批准、双人审批）、运行观测（心跳、完整记录存档、成本汇总、站点时区）、看板其余表与反向操作、控制台（规则配置、投递与合并、本机 CLI、成本、影子）、影子模式、运营日报与异常巡检；顺带修复：提案保存不更新风险等级、按位置插入依赖列顺序、迁移改为对照 SCHEMA 自动补列、提案创建时间用了真实时钟；测试 110 → 159 |
| 2026-10-26 | 新增 `功能同步说明.md`：同事已按自己风格改造代码，改为同步需求（8 个功能的要求、边界、验收用例），不同步代码 |
| 2026-10-26 | 飞书上下文拉取：4 个工具，任务记录来源会话；`check` 增加本人身份登录检查；测试 110 个 |
| 2026-10-26 | 旧版本数据库启动时自动补列；新增 `UPGRADE.md`（给在用 10-16 包的同事：新增内容、升级步骤、扩展指南）；打包 10-26 版；测试 102 个 |
| 2026-10-26 | 内部平台浏览器操作（海外仓、OA 等，卖家后台仍禁止）：Playwright 驱动本机 Chrome；审批后回放步骤；修复会话 cookie 在浏览器关闭后丢失导致每次都「未登录」的问题；测试 101 个 |
| 2026-10-26 | agent 工具补齐：场景 SOP 动态加载、店铺背景、只读网页取证（真实 Chrome 测试通过）、证据截图附在汇报里；开局上下文分层；`check` 增加 Chrome 和 SOP 目录检查；测试 95 个 |
| 2026-10-26 | 控制台记忆维护（参考数字分身后台）：记忆库增删改、草稿先改后采纳、召回记录、变更记录；已在真实浏览器里检查过页面（无报错）；修复两个依赖真实时钟的测试；测试 84 个 |
| 2026-10-16 | 自检：修复多线程共用连接、群聊审批刷屏、事件流断开不重连、同任务并发续跑、配置缺漏晚暴露、lark-cli 错误解析 6 个问题；新增 `ecom-twin check`；开放只读类目工具；测试 78 个 |
| 2026-10-16 | P1 代码：MCP server、私聊建单与 thread 续跑、拒绝唤醒、底座重试与切换、记忆库与草稿审核、拒绝原因汇总、执行后回读确认与复盘提醒、定时调度与健康检查、Cursor Agent / OpenCode 适配器、本地调试模式；测试 72 个 |
| 2026-10-16 | 本机 agent CLI 路径配置与验证（`ecom-twin runtimes`）；本机实测 Claude Code 2.1.301、Codex 0.156.0、Cursor Agent 2026.09.02、OpenCode 1.2.13 均验证通过 |
| 2026-10-16 | P0 骨架：配置、存储、妙手适配器、定价与数字校验、提案、执行器、审批、看板回写、运行时适配、CLI、离线演示 |
| 25 | 浏览器 MCP 只读 + 各底座白名单 | 开 `[browser] enabled`，分别用 Codex、Cursor、OpenCode 跑一个让它「打开某网址并点击」的任务 | agent 只能截图和读已打开页面，拿不到跳转、点击工具；Codex `enabled_tools` 和 OpenCode `tools` 配置被 CLI 接受 |

**测试记录**

| 日期 | # | 结果 | 问题 |
|---|---|---|---|
| 2026-10-21 | 配置页和原生飞书接入 | 离线 102 个测试；桌面/手机浏览器检查通过 | 覆盖配置保存保留、密钥不回显、访问鉴权、原始事件转换、SDK 回调、工具安装与失败、服务启停；认证浏览器测试使用模拟返回，待真实飞书凭证和事件授权 |
| 2026-10-18 | 离线回归 | 通过（80 passed） | 修复审批测试使用过期固定时间；发布回读兼容妙手真实 `detailList` 包装 |
| 2026-10-18 | 妙手只读 | 通过 | 1 家 TikTok 店铺；采集箱列表、商品详情（含 `ossMd5` / `skuMap` / `imgUrls`）、MY 类目树和类目属性均成功返回 |
| 2026-10-18 | 妙手写入 | 未执行 | 按安全边界未调用认领、编辑、发布等写接口 |

## 六、变更记录

| 日期 | 内容 |
|---|---|
| 2026-11-07 | 记忆库最后两项（对照数字分身）：① 草稿改动率、驳回率，控制台「记忆草稿」页显示 ② 让本机 Claude Code 按索引写法标准改写描述：工具全关、不挂 MCP、密钥不进环境，只输出一行建议，校验 ≤120 字后填进控制台，人看过再保存；`ecom-twin memory suggest`。真实 Claude Code 实测给出 102 字、动作开头的建议，文件未被改动；测试 320 → 322 |
| 2026-11-07 | 汇报核对：发汇报前拿 agent 的说法对系统记录 —— 提到的提案号要存在且属于本任务、声称提交了提案这一轮要有新提案、声称附了截图这一轮要有证据；对不上在汇报末尾写「系统核对：…」并留审计 claim_mismatch；只认肯定句式，「没有提交提案」不算声称；测试 316 → 320 |
| 2026-11-07 | 控制台按角色区分权限（DESIGN.md 12）：启动时给出负责人和运营两个链接；运营所有页面都能看、能采纳 / 驳回记忆草稿，改规则配置（含硬上限、代发权限）、记忆库、导入、回滚一律 403 并留审计；页面顶部提示、改动按钮隐藏、表单只读；真实浏览器检查过；测试 313 → 316 |
| 2026-11-07 | 控制台「Listing 对比」：按商品汇总妙手暂存区改动，每个字段第一次改之前 → 最后改成的值，标题、描述逐字标出差别（几乎整段重写时直接显示整段删 + 整段加），价格显示涨跌，附任务、提案和改动时间线，可按任务筛选；真实浏览器渲染检查过；记忆库导入改成真按钮；测试 310 → 313 |
| 2026-11-07 | 记忆库对照数字分身补齐：① 版本历史：保存、采纳草稿、删除、回滚、导入前自动快照到 .history/，每条 20 份，控制台和 `memory history / rollback` 可回滚（回滚前也留一份） ② 防覆盖：控制台带打开时的 sha256 保存，期间被改过就拒绝 ③ [[链接]]：断链拒绝保存，被链着的不能删、不能改名，check 报断链，控制台标孤立和链接关系 ④ 记忆包：纯文本分隔行格式，`memory export / import` 和控制台导出导入；格式不对、名字对不上、重名、断链、疑似密钥整包拒绝，覆盖前留快照；测试 302 → 310 |
| 2026-11-06 | AppleScript 方式的命令闸门：Claude Code 每条 Bash 执行前先过 `shell_guard`（PreToolUse hook，经 --settings 挂上，只在开 AppleScript 时），拦 do shell script、JXA doShellScript、run script、ObjC 桥接和禁止目标，退出码 2 不执行、原因交给 agent、审计 shell_blocked。真实 Claude Code 实测：模型照做时被 hook 拦下，命令没执行；测试 298 → 302 |
| 2026-11-06 | 打包第 3 步：常驻服务交给 launchd —— `ecom-twin start / stop / restart / status / logs / doctor / uninstall`；当前用户的 LaunchAgent，登录后自启、只在异常退出时拉起（30 秒节流），PATH 补上底座 CLI 常见位置，日志在数据目录 logs/；uninstall 只移除常驻、数据保留；测试 295 → 298 |
| 2026-11-06 | 召回记录对照数字分身补齐：按任务汇总（调用、读全文、检索、未命中、最近一次），控制台「召回记录」分「按任务 / 调用明细」，`ecom-twin memory recalls` 不带任务号先给汇总；`[storage] recall_retention_days`（默认 90 天，0 = 不删）由定时任务每天清理；测试 291 → 295 |
| 2026-11-06 | 对照数字分身 0.2.66 的方案 B、C：① 第三层改成「接话 / 主题」：廉价模型先认出新消息接的是会话历史里哪一句（编号），那句确定属于某个任务就由代码落到那里（层名「接话」，不算确定落点），认不出再按主题从候选任务里选；拉不到群历史时退回只看候选任务 ② 判 no 必须以类别开头（闲聊、表态、点了别人、同事的活、无关通知、已办完），写不出的照样不处理但标成存疑，控制台「投递与合并」单独列出；测试 286 → 291 |
| 2026-11-06 | 吸收数字分身派单契约剩下的几条：提示词加「现状不合理就提出来」（电商例子：负毛利、零库存却在出单）和「要找同事又发不出去时」给负责人一段能原样复制发出的话；annotate_screenshot 对所有截图开放（原来只在开 AppleScript 时有）；交付前证据自检：附图前逐张校验 PNG，坏的在汇报里写明哪张、为什么，留审计 evidence_invalid |
| 2026-11-06 | 可靠性：agent 放进独立进程组，超时或结束时整组清理（先 TERM 再 KILL），孙子进程不再占着输出管道卡住；serve 启动时把停机时还在跑的任务重新排队，提示词前加一句「上一轮被中断，已提交的提案不要重复提交」，留审计；真实 Claude Code 跑完不留 MCP 进程；测试 284 → 286 |
| 2026-11-06 | 修复 agent 能拿到 ecom-twin 密钥：① agent 进程不再继承 .env 里的变量和已知密钥（妙手、飞书），底座登录用的 ANTHROPIC_ / OPENAI_ 等保留，MCP 服务自己读 .env ② Codex 关掉 shell_tool、unified_exec（只读沙箱仍能读整个磁盘）。真实 CLI 实测：Claude Code 读工作目录外的 .env、搜索都被权限拒绝，也没有读环境变量的工具；Codex 关掉 shell 后没有任何读文件或环境变量的工具；两者都没输出假密钥；测试 281 → 284 |
| 2026-11-06 | 对照数字分身 0.2.66 扩展入口合并的「回复」层：同事回复任务的触发消息、回复确定进任务的消息（新任务 / 回复 / 锚点），或在这类消息的 thread 里接话（root_id），不 @分身 也收，由代码直接落到那个任务，不交给模型猜；模型推断并进去的消息不当落点；已结束任务只在接分身的话时重新打开。飞书消息带上 root_id；测试 277 → 281。群里实际效果待真实群验证 |
| 2026-11-02 | 真实调试（打开 Google 截图、Notion 登录流程、操作本机数字分身 10 个页面）发现并修复：① 专用浏览器可能连到负责人的日常 Chrome —— 端口被占时 browser start 换了端口但配置不变，本机 9222 就是日常 Chrome；现在挂浏览器前核对端口上那个 Chrome 的用户目录是不是配置的专用目录，不是就不挂、提示词和审计写明原因，check 也会报；browser start 默认读配置，端口变了报错并给出要改的配置 ② 全新 profile 弹「登录 Chrome」引导页：加 --no-first-run ③ 配了 chrome_path 时起不来：直接启动可执行文件 ④ 截图失败只说缺权限会带偏：自动重试一次，再按窗口还在不在说原因 ⑤ Electron 软件（数字分身、Notion、Slack）按元素操作不可靠、被完全遮挡时新页面不绘制（后台切页截到空白）：写进 AppleScript 提示词，按坐标点要先 ask_owner 同意前置，有网页版优先走专用浏览器；测试 273 → 277 |
| 2026-10-30 | 专用 Chrome 按负责人决定全部放开（chrome_guard 代理拦卖货后台、留审计）；修复 MCP 参数冲突导致专用浏览器连不上；真实 Chrome 端到端验证；测试 269 → 273 通过 |
| 2026-10-30 | 更正对数字分身的描述：网页靠 CDP（主力），桌面软件靠 AppleScript（少量），CDP 没用于桌面软件（DESIGN.md 5.6 操作电脑一节） |
| 2026-10-29 | 吸收数字分身派单契约：分诊首行（waiting 驱动等待唤醒合并）、处理流程、防重复操作、HITL、数据冲突、验证程度、汇报格式写进提示词；发送出口内容闸门；Claude Code 真实任务验证分诊首行；测试 260 → 270 |
| 2026-10-29 | AppleScript 方式补齐数字分身的原生窗口取证：desktop_windows / desktop_capture / annotate_screenshot（按窗口截、不抢焦点、红框另存），开 applescript 才挂；放行命令去掉整屏 screencapture；Claude Code 真实任务端到端跑通；测试 253 → 260 |
| 2026-10-29 | 加入 AppleScript 方式操作电脑（数字分身的做法，只放行 osascript / open / screencapture / cliclick，默认关）；更正设计文档：数字分身不是靠 Codex Computer Use；测试 251 → 253 |
| 2026-10-29 | 操作电脑真实跑通：代理回答 Computer Use 的按 app 授权征询（allowed_apps 白名单），回绝 Claude Code 的 server/discover 探测；Claude Code 打开计算器读回结果；测试 250 → 251 |
| 2026-10-29 | 修复 Codex 底座继承本人 ~/.codex 插件（Computer Use、浏览器、应用连接器）的漏洞：隔离 CODEX_HOME + 关掉内置功能，真实 Codex 验证；加入操作电脑（借用本机 Codex Computer Use，经代理放行、拦截、审计，默认关闭）；测试 245 → 250 |
| 2026-10-29 | 本地打包第 1 步：代码和数据分开（`paths.py`），装好的版本数据在 `~/Library/Application Support/EcomTwin/`；部署定位改为每人一份的 macOS 本地软件；测试 242 → 245 |
| 2026-10-29 | 记忆按数字分身设计改造：按动作召回（`read_memory` / `search_memory`，去掉每轮自动塞正文）、四种草稿模式、召回留痕、草稿规矩、索引预算；测试 224 → 242 |
| 2026-10-29 | 移植本地离线补全 8 批（三方合并，冲突逐处手工处理）；功能 1-4 对齐功能同步说明；修复仓库核对的严重问题（浏览器 MCP 只读、各底座工具白名单、执行器异常、serve 容错、原生飞书搜索）和任务队列丢消息；测试 166 → 224 |
| 2026-10-21 | 新增 `setup` 配置页和 `login` 登录入口；飞书自建应用认证、官方 SDK 长连接与事件标准化；自动安装缺失工具；支持本机页面启动/停止任务服务；22 个新增离线用例与浏览器交互验证 |
| 2026-10-16 | 自检：修复多线程共用连接、群聊审批刷屏、事件流断开不重连、同任务并发续跑、配置缺漏晚暴露、lark-cli 错误解析 6 个问题；新增 `ecom-twin check`；开放只读类目工具；测试 78 个 |
| 2026-10-16 | P1 代码：MCP server、私聊建单与 thread 续跑、拒绝唤醒、底座重试与切换、记忆库与草稿审核、拒绝原因汇总、执行后回读确认与复盘提醒、定时调度与健康检查、Cursor Agent / OpenCode 适配器、本地调试模式；测试 72 个 |
| 2026-10-16 | 本机 agent CLI 路径配置与验证（`ecom-twin runtimes`）；本机实测 Claude Code 2.1.301、Codex 0.156.0、Cursor Agent 2026.09.02、OpenCode 1.2.13 均验证通过 |
| 2026-10-16 | P0 骨架：配置、存储、妙手适配器、定价与数字校验、提案、执行器、审批、看板回写、运行时适配、CLI、离线演示 |
- 2026-10-26 同步补充功能 1/2/3/4/5/8：飞书上下文智能拉取、运行时任务跟踪器、场景 SOP 动态加载、店铺背景查询、只读网页取证、数据库自动迁移；测试 136 → 150，全部通过
- 2026-10-28 修复功能 7：a517381 只提交了控制台，配套的 `memory.py`、`store.py`、`config.py` 改动漏提交，`ecom-twin console` 和 `tests/test_console.py` 导入即报 ImportError。补齐 `MemoryError`、`save` / `delete`、`approve(edits)`、`reject(note)`、`store.audit_log`、`memory_dir` 配置；测试 160 → 166（另 1 个跳过），控制台实际启动验证通过
