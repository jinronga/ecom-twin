# ecom-twin · 跨境电商运营数字分身

以飞书为入口的运营分身：盯着消息和定时任务，产出操作提案，经人审批后执行，全程留痕。

- 设计：[DESIGN.md](DESIGN.md)
- 实现进度（哪些做了、哪些没做）：[PROGRESS.md](PROGRESS.md)

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/ecom-twin demo          # 离线演示：提案 → 审批卡片 → 批准 → 执行 → 看板
.venv/bin/python -m pytest -q     # 运行测试
```

## 接入真实环境

优先使用本地配置页面：

```bash
.venv/bin/ecom-twin setup
```

浏览器会打开飞书接入页，可配置 App ID / App Secret、负责人 open_id、店铺白名单、审批人、运行时和可选任务看板。密钥保存到权限为 `0600` 的 `.env`，页面和状态接口不回显。环境变量优先于文件；修改后需重启任务服务。

打开页面、保存配置或直接启动 `serve` 时，会检查并自动安装缺失的飞书 SDK、默认与备用 agent CLI。其它运行时可以在页面单独安装；已有可用版本会保留。自动下载使用官方来源：Python `lark-oapi`、npm `@anthropic-ai/claude-code` / `@openai/codex` / `opencode-ai`、Cursor 官方安装脚本。npm 工具安装在项目 `data/tools/`，缺少 Node.js 时从官方站点下载 Node.js 22 并核对 SHA-256；Cursor 安装在官方默认用户目录。支持 macOS / Linux 的 arm64 / x64，安装失败可在页面重试；手动配置的错误路径需先修正或清空。

安装不包含 agent 账号登录。首次使用时，在终端运行（支持自动安装目录）：

```bash
.venv/bin/ecom-twin login claude_code
# 或 login codex / login cursor / login opencode
```

飞书应用仍需在 [飞书开放平台](https://open.feishu.cn/app) 启用机器人、配置可用范围、申请接收单聊/读消息/发送消息权限，选择「使用长连接接收事件」，订阅 `im.message.receive_v1`，并发布版本。负责人 open_id 必须来自同一应用。

页面依次点击「保存并准备工具」「测试已保存的连接」「启动任务服务」，然后私聊机器人发送任务。连接测试只验证应用凭证和机器人信息，收到建单回执才代表消息接通。文本和图片私聊都可以建单；图片会下载到对应任务的私有附件目录，日志和面板只显示图片占位，不记录图片正文或资源键。引用回复汇报可以继续同一任务。

### Agent 浏览器与截图证据（可选）

配置页勾选「启用专用 Chrome MCP」后，先运行 `.venv/bin/ecom-twin browser start`。首次在弹出的专用 Chrome 中登录业务系统；它使用 `~/.agent-chrome`，不会复用日常 Chrome。任务运行时会自动挂载 Google `chrome-devtools-mcp`，并把文件访问限制在该任务工作目录。网页截图用 MCP 的 `take_screenshot` 写入 `evidence-image prepare` 输出的路径，随后用 `evidence-image collect` 校验完整 PNG、大小和唯一目标路径。桌面窗口可以使用 `evidence-image desktop TASK WINDOW_ID --box x,y,width,height`；标注依赖带 Pillow 的 Python，标注失败会保留原图供脱敏后交付。

调度器在每一轮任务开始前会从任务标题和指令抽取关键词，自动召回相关人工记忆并记录到本地召回日志，再把正文放入本轮 Agent 上下文；Agent 仍可用 `recall_memory` 做追加检索。浏览器中的平台写操作仍然只能通过 `submit_proposal` 进入审批链。

配置页面仅监听 `127.0.0.1`，使用每次启动生成的访问令牌；请使用命令输出的完整链接。可用 `setup --no-browser --port 8766` 指定端口。从页面启动的任务服务会随配置服务退出而停止；需独立运行时使用 `serve`。配置 App ID / Secret 后使用官方 SDK 长连接，无需安装 lark-cli；未配置时仍兼容原 lark-cli 入口。

其它命令：

```bash
cp -n .env.example .env                           # 填妙手 AppKey / AppSecret，保留已有凭证文件

.venv/bin/ecom-twin init                          # 初始化本地数据库
.venv/bin/ecom-twin health                        # 妙手只读冒烟
.venv/bin/ecom-twin runtimes                      # 验证本机 agent CLI
.venv/bin/ecom-twin ask "列出我的 TikTok 店铺" --console   # 本地发起任务，飞书消息只打印
.venv/bin/ecom-twin browser start                  # 启动给 Agent 用的隔离 Chrome（可选）
.venv/bin/ecom-twin evidence-image prepare T-0001  # 为 Chrome MCP 准备唯一截图路径
.venv/bin/ecom-twin propose-publish items.json    # 生成上品提案并推送审批卡片
.venv/bin/ecom-twin serve                         # 常驻：私聊建单、审批回复、thread 续跑、定时作业
.venv/bin/ecom-twin memory drafts                 # 审核 agent 提交的记忆草稿
```

真实环境的统一测试步骤见 [PROGRESS.md](PROGRESS.md) 第五节。

审批方式：在审批卡片下回复「批准 P-0001」或「拒绝 P-0001 原因编号 备注」。

## 代码结构

```
src/ecom_twin/
  config.py        配置（settings.toml + .env）
  models.py        任务、步骤、提案
  store.py         本地状态库（SQLite），任务状态以此为准
  numbers.py       数字由代码算：定价、理由数字校验
  actions.py       写操作类型（目前：经妙手发布到 TikTok）
  proposals.py     提案服务：证据和后果由代码计算
  approval.py      审批卡片、审批回复、过期提醒
  executor.py      执行器：唯一能调写接口的组件
  tasks.py         任务、步骤、心跳
  board.py         飞书多维表格看板回写（合并、节流、退避）
  feishu.py        飞书访问层（原生 / lark-cli；console 调试模式）
  feishu_native.py 飞书应用认证、消息与看板 API、官方 SDK 长连接事件转换
  setup_web.py     本地配置页面 API、工具安装任务、任务服务启停
  setup_config.py  配置合并保存、密钥文件与脱敏读取
  tooling.py       固定官方来源的工具检测与自动安装
  web/setup.html   飞书接入配置页面（桌面 / 手机）
  followups.py     执行后跟进：回读确认、第 3/7 天复盘
  memory.py        记忆库、召回、记忆草稿、拒绝原因汇总
  dispatcher.py    私聊建单、调 agent、重试与切换底座、汇报回 thread
  mcp_server.py    给 agent 用的工具（只读、提案、步骤、记忆、提问）
  scheduler.py     定时作业与健康检查
  prompts/         agent 系统提示词
  adapters/        ERP / 平台适配器（目前：妙手）
  runtime/         agent 运行时适配（Claude Code、Codex、Cursor Agent、OpenCode）与 CLI 验证
  app.py           组装
  cli.py           命令行
memory/            人工精炼的记忆（Markdown）
```
