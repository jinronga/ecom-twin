<div align="center">

# ecom-twin

### 跨境电商运营 AI 分身

让任务从一句飞书消息，走到有上下文、有审批、有证据的交付。

**Feishu · AI Agents · MCP · Miaoshou ERP · Chrome**

[快速开始](#快速开始) · [核心能力](#核心能力) · [工作原理](#工作原理) · [配置指南](#配置指南) · [开发与验证](#开发与验证)

</div>

---

## 项目简介

ecom-twin 是一个运行在本机、以飞书为入口的电商运营 Agent。它连接 AI CLI、业务工具和审批流程，帮助运营人员查询店铺数据、准备 Listing、读取仓库页面、整理截图证据，并持续跟进同一件事。

模型负责理解上下文、分析问题和组织方案；代码负责确定性计算、工具调用范围、审批校验和执行记录。负责人通过飞书交办任务，通过控制台维护规则和经验。

> 当前版本处于持续开发与真实环境验证阶段。已实现能力不等于所有平台流程均已验收；业务执行范围见[当前状态](#当前状态)。

## 核心能力

| 能力 | 可以怎样使用 |
| --- | --- |
| **自动理解上下文** | 每轮自动补充来源会话近 24 小时记录、任务原消息、引用和话题回复；最多保留 30 条，并限制总长度 |
| **连续任务协作** | 引用回复续跑原任务；明确说“继续处理”且只有一个近期未完成任务时直接续接，多个候选无法确定时询问任务号 |
| **多运行时接入** | 接入 Codex、Claude Code，另有 Cursor Agent、OpenCode 适配器；支持运行记录、重试和备选运行时 |
| **Listing 准备与审核** | 通过妙手采集货源、编辑采集箱草稿、认领商品、检查合规、比较修改前后内容并提交发布提案 |
| **业务审批** | 发布等受支持的执行动作经过提案、审批与校验，支持有效期、代理审批和高风险双人审批 |
| **浏览器与证据** | 公开网页取证、专用 Chrome 操作内部平台、截图校验和飞书图片交付 |
| **业务记忆与 SOP** | 按动作读取 Markdown 记忆、按场景加载 SOP；记忆修改经人工审核，支持历史、回滚、导入与导出 |
| **运营控制台** | 查看任务、Listing 差异、记忆草稿、召回记录、规则、成本与审计；区分负责人和运营审阅角色 |
| **日报与常驻服务** | 导入 ERP 表格计算日报指标和异常；macOS 通过 launchd 管理后台服务 |

可以直接在飞书向机器人交办：

```text
列出已授权的 TikTok 店铺。
截取这个公开商品页面，把图片回复给我。
检查这批采集箱商品的标题和 SKU，整理发布前需要确认的问题。
结合前面的要求继续处理，只改标题，不改价格。
```

以上是任务示例；涉及的账号权限、数据源和平台配置需要事先准备好。

## 工作原理

```text
飞书消息 / 定时任务
        │
        ▼
任务入口 ── 过滤、合并、引用关系、自动聊天上下文
        │
        ▼
Agent Runtime ── Codex / Claude Code / 其他适配器
        │
        ▼
MCP 工具 ── 业务查询 · 记忆 · SOP · 浏览器 · 提案
        │
        ├── 查询 / 分析 / 证据 ──────────────┐
        ├── 暂存区草稿修改 ── 合规检查与留痕 ──┤
        └── 发布等执行动作 ── 人工审批 ── 执行器 ┤
                                              ▼
                                    结果核验 → 飞书交付
```

任务状态、提案、步骤和审计记录保存在 SQLite；业务记忆保存在 Markdown 文件中。每轮输入包含任务卡和记忆索引，Agent 按需读取记忆全文。聊天记录、网页和工具结果作为背景数据处理，不能替代系统规则或审批授权。

## 快速开始

### 1. 安装与离线体验

需要 **Python 3.11+**。以下命令适用于 macOS / Linux shell；后台常驻和桌面操作章节面向 macOS。

```bash
git clone https://github.com/jinronga/ecom-twin.git
cd ecom-twin
git checkout develop

python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,evidence]'

# 使用模拟业务数据体验：提案 → 审批 → 执行 → 看板
.venv/bin/ecom-twin demo
```

离线演示不需要飞书、妙手或模型账号。真实任务需要配置并登录所选 Agent CLI；网页截图还需要安装 Google Chrome。

### 2. 配置连接

```bash
.venv/bin/ecom-twin setup
```

打开命令生成的本地页面，填写飞书应用凭证、负责人 `open_id`、运行时、店铺白名单和审批人。页面会准备缺失的飞书 SDK 和所选 Agent CLI；工具安装不包含账号登录。

在[飞书开放平台](https://open.feishu.cn/app)完成应用配置：

1. 启用机器人，配置可用范围和消息接收、读取、发送权限。
2. 选择“使用长连接接收事件”，订阅 `im.message.receive_v1`，发布应用版本。
3. 需要回复截图时，开通 `im:resource:upload` 或 `im:resource` 应用身份权限。
4. 使用同一应用下的负责人 `open_id`；多维表格看板按需配置。

登录你在配置页选择的运行时，例如：

```bash
.venv/bin/ecom-twin login codex
# 选择 Claude Code 时使用：
# .venv/bin/ecom-twin login claude_code
```

如需妙手功能，将凭证填写到本地 `.env`：

```bash
cp -n .env.example .env
# 编辑 .env，填写 MIAOSHOU_APP_KEY 和 MIAOSHOU_APP_SECRET
```

### 3. 启动与验收

```bash
.venv/bin/ecom-twin check
.venv/bin/ecom-twin serve
```

保持终端打开，在飞书私聊机器人发送“请说明你能协助哪些运营任务，以及需要我提供哪些信息”，先验证建单回执和 Agent 回复。已配置妙手时，再发送“列出已授权的 TikTok 店铺”验证真实业务查询。最后引用回复，检查能否续接原任务。

`check` 检查配置、CLI 和基础接口连接；它不代表图片上传、Agent 工具调用和所有业务操作均已通过验收。

另开终端启动控制台：

```bash
.venv/bin/ecom-twin console
```

命令会同时输出负责人和运营两种链接，打开对应的**完整链接（含 token）**。默认配置页端口为 `8765`，控制台为 `8766`；两个页面均默认监听本机。负责人链接可以修改配置，运营链接仅提供查看及记忆草稿审阅权限。

## 配置指南

### 配置和数据位置

| 路径 | 用途 |
| --- | --- |
| `.env` | 飞书和妙手凭证；配置页保存时使用 `0600` 权限 |
| `config/settings.toml` | 本机主配置，参考 [settings.example.toml](config/settings.example.toml) |
| `config/settings.console.toml` | 控制台保存的覆盖配置，优先于主配置 |
| `data/` | 数据库、日志、任务工作区、截图与运行记录 |
| `memory/` | 人工维护的 Markdown 业务记忆 |
| `skills/` | 项目自带的业务 SOP |

从源码仓库运行时，数据默认放在仓库内。非源码布局下默认数据目录为 `~/Library/Application Support/EcomTwin/`；可用 `ECOM_TWIN_HOME` 覆盖，用 `ECOM_TWIN_RESOURCES` 指定资源目录。应用安装包尚未完成。

修改连接或运行时配置后需要重启任务服务。

### 专用浏览器

公开页面取证使用本机 Chrome 的临时无头进程，不需要执行 `browser start`，目标域名需在 `[browser] allowed_domains` 中。需要交互的专用 Chrome 按下文启动；`browser.flow` 则使用独立的 Playwright 环境和平台登录状态。

在配置页开启专用 Chrome，或者合并以下设置到已有的 `[browser]` 配置节：

```toml
[browser]
enabled = true
browser_url = "http://127.0.0.1:9225"
user_data_dir = "~/.agent-chrome-ecom"
interactive = true
auto_login_domains = []
```

```bash
.venv/bin/ecom-twin browser start
```

调试端口应保持空闲，资料目录使用专用目录。启动后运行 `check` 确认连接到专用 Chrome；不要在该浏览器登录卖家中心或广告后台。

启用 `[browser] enabled = true` 后，调度器会在每轮任务（含续跑）开始前准备专用 Chrome：未运行时自动启动，已运行时复用，确认用户目录和调试接口正常后再挂载工具。无需给 Agent 开放终端权限。端口被其他进程占用、用户目录不匹配或启动失败时，会报告具体原因，不会自动切换端口或接管日常浏览器。

内部平台自动登录由负责人按**完整域名**授权，例如 `auto_login_domains = ["wms.example.com"]`。Agent 可以使用明确提供给该平台的凭证登录；该配置不包含子域名或第三方登录页。验证码、短信、MFA 和未授权平台仍交给负责人处理。

域名登录规则通过任务指令传达给 Agent，并非浏览器级网络隔离。登录许可也不等于制单、删除、改价等业务操作许可。

需要走 `browser.flow` 的内部平台流程时，另行配置 `[platforms.<key>]`，安装 Playwright，并保存该平台的登录状态：

```bash
.venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium
.venv/bin/ecom-twin browser-login warehouse
# warehouse 对应自己配置的 [platforms.warehouse]
```

### 上下文与任务续接

每轮自动读取任务来源会话，优先分析最新要求、引用关系和已有进展。失败或截断会标明；需要更早的信息时可继续调用飞书上下文工具。

其他会话需加入 `[feishu] readable_chats` 才能按需读取；以个人身份搜索聊天记录默认关闭。群聊还需配置 `[intake] listen_chats`，群内回复和通知由 `send_permissions` 单独控制。

默认运行时为 Codex 时，任务归属判断也使用 Codex。负责人私聊的明确续接有规则兜底；仍不能确定目标时，先询问任务号，避免创建重复任务。

## 操作边界

| 操作 | 当前处理方式 |
| --- | --- |
| 查询、分析、定价计算、公开页面取证 | 在工具和数据访问范围内直接执行 |
| 妙手采集箱暂存区采集、编辑、认领 | 可以直接写入；执行合规、店铺范围等校验，记录修改前后内容 |
| 发布等已实现的提案执行动作 | 负责人审批后由执行器校验并执行 |
| 内部平台网页操作 | 通过专用浏览器代理；提交单据、删除等动作要求 Agent 停在最后一步取得授权 |
| 内部平台自动登录 | 仅限负责人配置的域名和明确提供的凭证 |
| 记忆修改 | Agent 提交草稿，由人审阅采纳 |

代码强制校验与提示词约束承担不同职责：提案执行器检查店铺、限额、有效期等规则；浏览器代理过滤工具、网址和脚本并留痕，但不能准确识别所有按钮的业务后果。

Codex 使用隔离配置目录，禁用内置 Shell 和全局应用连接器，只开放本轮工具白名单。浏览器动作审计隐藏填写值及脚本正文；原始 Agent 运行记录仍保存在本机，可能包含任务输入和工具参数，不应公开上传。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `ecom-twin setup` | 配置飞书与运行时 |
| `ecom-twin check` | 基础连接和配置检查 |
| `ecom-twin runtimes` | 检查各 Agent CLI |
| `ecom-twin serve` | 前台任务服务 |
| `ecom-twin console` | 本地管理控制台 |
| `ecom-twin health` | 妙手只读连接检查 |
| `ecom-twin memory drafts` | 查看记忆草稿 |
| `ecom-twin memory recalls` | 查看召回记录 |
| `ecom-twin costs` | 查看成本统计 |
| `ecom-twin shadow report` | 查看影子模式评估 |

未激活虚拟环境时，为命令加上 `.venv/bin/`。更多选项可通过 `.venv/bin/ecom-twin --help` 查看。

本地发起任务、将飞书输出改为终端打印：

```bash
.venv/bin/ecom-twin ask "列出我的 TikTok 店铺" --console
```

`--console` 只改变消息投递方式，仍会调用真实 Agent 和业务工具，不是业务操作的 dry-run。

审批卡片下使用文本回复：

```text
批准 P-0001
拒绝 P-0001 1 成本数据不对
```

将 `P-0001` 替换为审批卡片中的提案号。拒绝格式为 `拒绝 提案号 原因编号 备注`；原因必填，编号为 `1` 数据不对、`2` 判断不对、`3` 时机不对、`4` 违反偏好、`5` 其他，也可直接填写对应的原因文字。示例中的 `1` 表示“数据不对”，备注可省略。

## 后台运行（macOS）

确认前台任务正常后，先停止前台 `serve`，再安装并启动 launchd 服务：

```bash
.venv/bin/ecom-twin start       # 登录后自启，异常退出后自动拉起
.venv/bin/ecom-twin status
.venv/bin/ecom-twin logs -n 100
.venv/bin/ecom-twin restart
.venv/bin/ecom-twin stop        # 停止本次运行，下次登录仍可能自启
.venv/bin/ecom-twin uninstall   # 移除常驻配置，保留个人数据
```

后台服务只运行任务监听与调度；控制台仍需单独启动。从 `setup` 页面启动的任务服务随配置服务退出而停止，独立运行应使用 `serve` 或 launchd。

## 当前状态

| 范围 | 验证情况与限制 |
| --- | --- |
| 飞书与 Codex 基础链路 | 已验证消息读取、工具调用、会话续跑和图片发送；不同应用仍需自行开通权限 |
| 自动上下文与任务归属 | 有回归测试及真实模型判定验证；多任务、长对话仍需结合具体业务观察 |
| Listing 暂存与发布 | 已有工具、校验及离线测试；真实写入和端到端上架需按店铺验收 |
| 仓库与内部平台 | 提供浏览器操作基础能力；具体平台登录、制单和结果回读需逐项验证 |
| 日报与异常检测 | 支持 ERP 导出表格导入和指标计算；不是所有平台的实时数据集成 |
| 部署与其他运行时 | 支持源码运行和 macOS launchd；`.app` / DMG 尚未完成，其他运行时需真实环境验证 |

详细设计见 [DESIGN.md](DESIGN.md)，阶段记录见 [PROGRESS.md](PROGRESS.md)。进度文档保留历史状态，遇到旧结论时应结合后续记录和当前代码核对。

## 项目结构

```text
src/ecom_twin/
├── intake.py             消息过滤、任务合并与归属判断
├── chat_context.py       每轮自动加载聊天上下文
├── dispatcher.py         调度、续跑、重试和证据交付
├── runtime/              Agent CLI 适配器
├── mcp_server.py         业务工具入口
├── adapters/             妙手等平台适配器
├── staging.py            采集箱暂存区工作流
├── approval.py           提案审批
├── executor.py           审批后的业务执行
├── memory.py             业务记忆与草稿
├── feishu_native.py      飞书 API 与长连接
├── browser.py            公开页面取证
├── chrome_guard.py       专用浏览器代理
├── console.py            管理控制台
└── service.py            launchd 服务管理
config/                   配置示例与合规规则
skills/                   业务 SOP
memory/                   记忆模板
tests/                    自动化测试
```

## 开发与验证

```bash
.venv/bin/python -m pytest -q
git diff --check
```

测试覆盖任务路由、上下文、审批、记忆、工具权限、飞书错误处理、截图交付和服务管理。含 Chrome 的本地取证测试；没有 Chrome 时相关测试会跳过。测试通过不替代真实平台验收。

新增业务能力时，先定义工具输入与数据来源，再实现权限校验、失败反馈和回归测试。修改任务行为时，同时检查“新建任务”“引用回复”“旧会话续跑”三条路径。

`.env`、本机配置和 `data/` 不进入版本库。`memory/` 中存在受 Git 跟踪的模板，填入真实业务经验后应检查提交差异；`settings.console.toml` 也需确保只保留在本机。提交问题反馈时附脱敏后的错误码、运行时版本和复现步骤，不上传凭证、原始聊天记录或浏览器登录资料。
