"""命令行入口。

  ecom-twin init                       初始化本地数据库
  ecom-twin check                      一次性自检：配置、底座 CLI、妙手凭据与接口、飞书 lark-cli 授权
  ecom-twin health                     妙手只读冒烟调用（拉 TikTok 店铺列表）
  ecom-twin propose-publish FILE       用流水线产出的 JSON 生成上品提案并推送审批卡片
  ecom-twin serve                      常驻服务：监听飞书消息（审批指令、私聊建单、thread 回复），跑定时作业
  ecom-twin ask TEXT                   本地发起一个任务并调 agent 处理（--console 时只打印、不发飞书）
  ecom-twin tick                       手动运行一次到期的定时作业
  ecom-twin memory ...                 记忆：drafts / approve ID / reject ID / recalls / digest
  ecom-twin runtimes                   验证本机 agent CLI（Claude Code、Codex、Cursor、OpenCode）路径和版本
  ecom-twin demo                       离线演示完整链路（假飞书 + 假妙手，不发任何真实请求）
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .adapters.base import AdapterError
from .adapters.miaoshou import MiaoshouClient
from .app import build_app
from .config import load_dotenv, load_settings, validate_settings
from .config import PROJECT_ROOT
from .dispatcher import Dispatcher
from .feishu import FakeFeishu, task_messages, make_feishu
from .memory import MemoryStore
from .models import TaskSource
from .proposals import ProposalRejected
from .runtime import RUNTIMES, build_runtime, check_all
from .scheduler import Scheduler, build_jobs
from .store import Store


def cmd_init(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    Store(settings.db_path)
    print(f"数据库已就绪：{settings.db_path}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from .setup_web import run_setup
    try:
        run_setup(args.config, args.port, open_browser=not args.no_browser)
    except OSError:
        print("配置页启动失败，端口可能被占用；可用 --port 指定其它端口", file=sys.stderr)
        return 1
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """登录所选 agent，兼容项目本地安装的 CLI 路径。"""
    import os
    import subprocess
    from .tooling import install_tool
    settings = load_settings(args.config)
    key = args.runtime
    try:
        status = install_tool(key, settings.runtime.cli.get(key, ''))
        login_args = {'claude_code': ['auth', 'login'], 'codex': ['login'],
                      'cursor': ['login'], 'opencode': ['auth', 'login']}[key]
        env = {k: v for k, v in os.environ.items() if not k.startswith(('FEISHU_', 'MIAOSHOU_'))}
        return subprocess.run([status['path'], *login_args], env=env).returncode
    except (ValueError, RuntimeError, OSError):
        print('登录工具启动失败，请检查工具路径或在配置页重新安装', file=sys.stderr)
        return 1


def _lark_cli_ok() -> tuple[bool, str]:
    """用只读接口探测 lark-cli 的 bot 身份是否可用。"""
    import subprocess
    try:
        proc = subprocess.run(["lark-cli", "api", "GET", "/open-apis/im/v1/chats", "--as", "bot",
                               "--params", '{"page_size":1}', "--format", "json"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    payload = None
    for text in (proc.stdout, proc.stderr):  # lark-cli 出错时把 JSON 写到 stderr
        try:
            payload = json.loads(text)
            break
        except json.JSONDecodeError:
            continue
    if payload is None:
        return False, (proc.stderr or proc.stdout)[:300]
    if payload.get("ok") is False:
        return False, (payload.get("error") or {}).get("message", "")
    if payload.get("code", 0) != 0:
        return False, f"{payload.get('code')}: {payload.get('msg')}"
    return True, "bot 身份可用"


def cmd_check(args: argparse.Namespace) -> int:
    load_dotenv()
    settings = load_settings(args.config)
    errors = 0
    print("== 配置 ==")
    issues = validate_settings(settings, list(RUNTIMES))
    for level, text in issues:
        errors += level == "错误"
        print(f"  [{level}] {text}")
    if not issues:
        print("  通过")
    print("\n== 底座 CLI ==")
    for st in check_all(settings.runtime.cli):
        role = "（默认）" if st.key == settings.runtime.default else "（备选）" if st.key == settings.runtime.fallback else ""
        if st.key in (settings.runtime.default, settings.runtime.fallback) and not st.usable:
            errors += 1
        print(f"  {st.label}{role}：{'验证通过 ' + st.version if st.ok else '不可用 ' + st.error[:120]}")
    print("\n== 妙手 ==")
    try:
        shops = MiaoshouClient.from_env().list_shops("tiktok")
        print(f"  接口可用，TikTok 店铺 {len(shops)} 个")
        known = {f"tiktok:{s.get('shopId')}" for s in shops}
        for shop in settings.limits.allowed_shops:
            if shop.startswith("tiktok:") and shop not in known:
                print(f"  [提醒] 白名单店铺 {shop} 不在妙手已授权的 TikTok 店铺里")
    except AdapterError as e:
        errors += 1
        print(f"  不可用：{e}")
    print("\n== 飞书 ==")
    from .setup_config import feishu_credentials
    if all(feishu_credentials()):
        from .feishu import FeishuError
        try:
            result = make_feishu().check_connection()
            ok, detail = True, result['message']
        except FeishuError as error:
            ok, detail = False, str(error)
    else:
        ok, detail = _lark_cli_ok()
    errors += not ok
    print(f"  {'可用' if ok else '不可用'}：{detail}")
    print(f"\n自检结果：{'全部通过' if not errors else f'{errors} 项需要处理'}")
    return 0 if not errors else 1


def cmd_health(args: argparse.Namespace) -> int:
    load_dotenv()
    try:
        shops = MiaoshouClient.from_env().list_shops(args.platform, args.site)
    except AdapterError as e:
        print(f"妙手冒烟失败：{e}（类别：{e.category.value}）")
        return 1
    print(f"妙手冒烟通过：{args.platform} 店铺 {len(shops)} 个")
    for s in shops:
        print(f"  {s.get('platform')}:{s.get('shopId')}  {s.get('shopNick')}  {s.get('site')}  {s.get('status')}")
    return 0


def _miaoshou_or_none():
    try:
        return MiaoshouClient.from_env()
    except AdapterError as e:
        print(f"提示：妙手不可用（{e}），妙手相关能力暂停", file=sys.stderr)
        return None


def _real_app(config: str | None):
    load_dotenv()
    settings = load_settings(config)
    return build_app(settings, Store(settings.db_path), make_feishu(), _miaoshou_or_none())


def _dispatcher(app, config: str | None, pool=None) -> Dispatcher:
    return Dispatcher(app, MemoryStore(app.store), lambda key: build_runtime(key, app.settings.runtime.cli),
                      workroot=PROJECT_ROOT / "data" / "workspaces", config_path=config, pool=pool)


def cmd_propose_publish(args: argparse.Namespace) -> int:
    """FILE 格式：{"shop_key": "tiktok:10001", "title": "...", "rationale": "...", "target": {...}}"""
    spec = json.loads(Path(args.file).read_text(encoding="utf-8"))
    app = _real_app(args.config)
    task = app.tasks.create(spec.get("title", "TikTok 上品"), TaskSource.SELF_DM, "上品",
                            shop_key=spec["shop_key"], owner=app.settings.feishu.owner_open_id)
    try:
        p = app.proposals.submit(task.id, spec["shop_key"], "miaoshou.tiktok.publish",
                                 spec["target"], {"publish": True}, spec.get("rationale", ""))
    except ProposalRejected as e:
        print(f"提案被拒收：{e}")
        return 1
    app.approvals.request(p)
    print(f"已推送审批卡片：{p.id}（任务 {task.id}）")
    return 0


def _print_cli_status(statuses) -> None:
    for st in statuses:
        mark = "验证通过" if st.ok else "跑不起来" if st.path else "未找到"
        adapter = "适配器已实现" if st.adapter_ready else "适配器未实现"
        print(f"{st.label}（{st.key}）")
        print(f"  路径：{st.path or '-'}（{st.source}）")
        print(f"  状态：{mark}" + (f"，{st.version}" if st.ok else "") + f"；{adapter}")
        if st.error:
            print(f"  错误：{st.error}")
        if st.hint:
            print(f"  建议：{st.hint}")


def cmd_runtimes(args: argparse.Namespace) -> int:
    """以配置为准验证各 agent CLI；--path key=路径 可临时试一个路径（不写回配置）。"""
    settings = load_settings(args.config)
    configured = dict(settings.runtime.cli)
    for item in args.path or []:
        key, _, path = item.partition("=")
        configured[key] = path
    statuses = check_all(configured)
    if args.json:
        print(json.dumps([vars(s) | {"usable": s.usable} for s in statuses], ensure_ascii=False, indent=2))
    else:
        _print_cli_status(statuses)
    by_key = {s.key: s for s in statuses}
    default = by_key.get(settings.runtime.default)
    if not args.json:
        print(f"\n默认底座：{settings.runtime.default}（{'可用' if default and default.usable else '不可用'}）；"
              f"备选：{settings.runtime.fallback}")
    return 0 if default and default.usable else 1


def cmd_serve(args: argparse.Namespace) -> int:
    from .tooling import ensure_task_tools
    try:
        ensure_task_tools(load_settings(args.config))
    except (ValueError, RuntimeError, OSError) as error:
        print(f"工具准备失败：{error}", file=sys.stderr)
        return 1
    app = _real_app(args.config)
    rt = app.settings.runtime
    for level, text in validate_settings(app.settings, list(RUNTIMES)):
        print(f"配置[{level}]：{text}")
    for st in check_all(rt.cli):
        if st.key in (rt.default, rt.fallback) and not st.usable:
            print(f"警告：底座 {st.label} 不可用（{st.error or '适配器未实现'}），运行 `ecom-twin runtimes` 查看")
    pool = ThreadPoolExecutor(max_workers=args.workers)
    dispatcher = _dispatcher(app, args.config, pool)
    # 恢复服务重启前尚未触发的任务；worker 自身保证严格按入队顺序串行执行。
    dispatcher.start_queue_worker()
    app.approvals.on_rejected = dispatcher.on_proposal_rejected
    scheduler = Scheduler(app.store, build_jobs(app, dispatcher.memory, app.miaoshou))
    stop = threading.Event()

    def housekeeping() -> None:
        while not stop.is_set():
            scheduler.tick()
            dispatcher.start_queue_worker()
            # 队列控制（暂停/继续/面板手动触发）需要快速响应；定时作业仍由自身到期时间节流。
            stop.wait(min(args.interval, 1))

    threading.Thread(target=housekeeping, daemon=True).start()
    print("分身已启动：监听飞书消息（审批指令、私聊建单、thread 回复），运行定时作业。Ctrl+C 退出。")
    try:
        for msg in task_messages():
            if not app.store.mark_event_seen(msg.get("event_id") or msg.get("message_id", "")):
                continue
            app.store.log_message(msg, "inbound")
            receipt = app.approvals.handle_message(msg) or dispatcher.handle_message(msg)
            if receipt:
                # Sender IDs and message bodies are private; keep the service log useful
                # without copying either into a terminal, supervisor, or log collector.
                print(f"[消息] 已处理一条飞书消息 -> {receipt}")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        pool.shutdown(wait=False)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """本地发起任务：不经过飞书消息入口，直接建单并同步调 agent 处理。"""
    import os
    if args.console:
        os.environ["ECOM_TWIN_FEISHU"] = "console"  # MCP server 子进程继承，同样只打印
    app = _real_app(args.config)
    if app.settings.browser.enabled:
        from .tooling import ensure_browser_tools
        try:
            ensure_browser_tools()
        except (ValueError, RuntimeError, OSError) as error:
            print(f"浏览器工具准备失败：{error}", file=sys.stderr)
            return 1
    if args.runtime:
        app.settings.runtime.default = args.runtime
    dispatcher = _dispatcher(app, args.config)
    task = app.tasks.create(args.text[:40], TaskSource.SELF_DM, "待定", owner=app.settings.feishu.owner_open_id)
    print(f"已建任务 {task.id}，底座：{app.settings.runtime.default}")
    result = dispatcher.run_task(task.id, f"负责人交代的任务：\n{args.text}", resume=False)
    task = app.store.get_task(task.id)
    print(f"\n结果：{'成功' if result.ok else '失败'}；状态：{task.status.value}；底座：{task.runtime}；"
          f"token：{task.tokens_in}+{task.tokens_out}")
    print(result.text or result.error)
    return 0 if result.ok else 1


def cmd_tick(args: argparse.Namespace) -> int:
    app = _real_app(args.config)
    ran = Scheduler(app.store, build_jobs(app, MemoryStore(app.store), app.miaoshou)).tick()
    print("本次运行：" + ("、".join(ran) if ran else "没有到期的作业"))
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    store = Store(settings.db_path)
    memory = MemoryStore(store)
    if args.action == "drafts":
        drafts = store.list_memory_drafts()
        for d in drafts:
            print(f"{d['id']}  {d['name']}  （来自 {d['task_id'] or '系统'}）\n  描述：{d['description']}\n"
                  f"  理由：{d['reason']}\n  正文：{d['body'][:300]}\n")
        print(f"共 {len(drafts)} 条待审核")
    elif args.action == "approve":
        print(f"已采纳，写入 {memory.approve(args.id)}")
    elif args.action == "reject":
        memory.reject(args.id)
        print(f"已驳回 {args.id}")
    elif args.action == "recalls":
        for r in store.list_recalls(args.id):
            print(f"{r['created_at']}  {r['task_id'] or '-'}  查询「{r['query']}」 -> {', '.join(r['hits']) or '无命中'}")
    elif args.action == "digest":
        draft_id = memory.rejection_digest(days=7)
        print(f"已生成记忆草稿 {draft_id}" if draft_id else "最近 7 天没有被拒绝的提案")
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    """准备、收集和标注任务证据截图。"""
    from . import evidence
    if args.evidence_action == "prepare":
        staging = Path(args.staging_root) if args.staging_root else PROJECT_ROOT / "data" / "workspaces"
        print(evidence.prepare(args.task_id, args.name, root=staging / args.task_id / "evidence"))
    elif args.evidence_action == "collect":
        task_root = (PROJECT_ROOT / "data" / "workspaces" / args.task_id).resolve()
        source = Path(args.source).resolve()
        if source != task_root and task_root not in source.parents:
            raise ValueError("截图来源必须位于该任务的工作目录")
        destination = Path(args.destination) if args.destination else evidence.final_path(args.task_id, args.name)
        root = (PROJECT_ROOT / "data" / "evidence" / args.task_id).resolve()
        if destination.resolve().parent != root:
            raise ValueError("证据目标必须位于该任务的统一证据目录")
        print(evidence.collect(source, destination))
    elif args.evidence_action == "desktop":
        box = None
        if args.box:
            values = tuple(int(x) for x in args.box.split(","))
            if len(values) != 4:
                raise ValueError("--box 格式应为 x,y,width,height")
            box = values
        destination = Path(args.destination) if args.destination else evidence.prepare(args.task_id, args.name)
        if box:
            from .tooling import ensure_pillow
            try:
                ensure_pillow()
            except (RuntimeError, OSError) as error:
                print(f"Pillow 未准备好，将交付未标注原图：{error}", file=sys.stderr)
        print(evidence.desktop(args.window_id, destination, box))
    return 0


def cmd_browser(args: argparse.Namespace) -> int:
    from .evidence import start_chrome
    port = start_chrome(args.chrome_path, args.user_data_dir, args.port, args.url)
    print(f"专用 Chrome 已启动：调试端口 {port}；用户目录 {args.user_data_dir or '~/.agent-chrome'}")
    print("MCP：npx -y chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:%d" % port)
    return 0


class _FakeMiaoshou:
    def __init__(self):
        self.titles = {"901": "Pet Hair Remover Roller", "902": "Silicone Pet Feeding Mat"}
        self.published: list = []

    def tiktok_shop_item_info(self, detail_id, shop_id):
        return {"shopCollectItemInfo": {"title": self.titles[str(detail_id)]}}

    def tiktok_publish(self, detail_ids, shop_ids):
        self.published.append((detail_ids, shop_ids))
        return None


def cmd_browser_login(args: argparse.Namespace) -> int:
    """启动内部平台登录浏览器并等待用户手工登录（保存登录状态）。"""
    from .platform_browser import PlatformBrowser

    settings = load_settings()
    platform = settings.platforms.get(args.platform)
    if platform is None:
        print(f"错误：没有名为 {args.platform} 的平台，可用：{', '.join(settings.platforms) or '（无）'}")
        return 1

    browser = PlatformBrowser(platform, chrome_path=settings.browser.chrome_path)
    print(f"平台：{platform.name} ({platform.key})")
    print(f"用户目录：{browser.user_dir}")
    print(f"打开 {platform.base_url}，手工登录完成后按 Ctrl+C 退出，登录状态会自动保存")
    print("")
    print(f"启动浏览器中...")
    browser.launch()
    url = platform.login_url if platform.login_url else platform.base_url
    try:
        import playwright.sync_api
        browser.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print(f"浏览器已打开：{url}（会话不超时）")
        import time
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n登录会话已保存")
    finally:
        browser.close()
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    import os
    from .console import make_server
    settings = load_settings(args.config)
    store = Store(settings.db_path)
    if args.host not in ("127.0.0.1", "localhost"):
        print(f"Warning: console will listen on {args.host}. Anyone with the link can modify memories. "
              f"Please confirm your network environment is secure.")
    server, token = make_server(store, MemoryStore(store, settings.memory_dir), args.host, args.port,
                                token=os.environ.get("ECOM_TWIN_CONSOLE_TOKEN"))
    print(f"Console started: http://{args.host}:{args.port}/?token={token}")
    print("Open the link above (includes login token, do not share). Press Ctrl+C to exit.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .config import Approver, Limits, Settings

    settings = Settings(
        db_path=Path(":memory:"),
        approvers={"tiktok:10001": Approver(primary="ou_boss", delegate="ou_backup")},
        limits=Limits(allowed_shops=["tiktok:10001"]),
    )
    settings.feishu.owner_open_id = "ou_boss"
    settings.feishu.base_app_token, settings.feishu.tasks_table_id = "app_demo", "tbl_tasks"
    settings.feishu.proposals_table_id = "tbl_proposals"
    feishu, miaoshou = FakeFeishu(), _FakeMiaoshou()
    app = build_app(settings, Store(":memory:"), feishu, miaoshou)

    task = app.tasks.create("宠物用品 2 款上 TikTok US", TaskSource.SELF_DM, "上品", shop_key="tiktok:10001")
    target = {
        "detail_ids": [901, 902],
        "pricing_params": {"commission_rate": 0.08, "target_margin_rate": 0.30, "exchange_rate": 0.14,
                           "rounding": "0.99", "currency": "USD"},
        "skus": [
            {"detail_id": 901, "sku": "901-Blue", "source_price": 25, "shipping": 3.5, "other_cost": 0.5},
            {"detail_id": 902, "sku": "902-Grey", "source_price": 40, "shipping": 4.0, "other_cost": 0.5},
        ],
    }
    p = app.proposals.submit(task.id, "tiktok:10001", "miaoshou.tiktok.publish", target, {"publish": True},
                             "按 US 站规则定价，目标毛利率 30%，两款都是测品款")
    app.approvals.request(p)
    card = feishu.sent[-1]["card"]
    print("== 审批卡片 ==")
    for el in card["elements"]:
        if el["tag"] == "div":
            print(el["text"]["content"])
        elif el["tag"] == "note":
            print(el["elements"][0]["content"])
    print("\n== 审批人回复「批准」 ==")
    print(app.approvals.handle_message({"message_id": "om_reply", "sender_id": "ou_boss",
                                        "content": f"批准 {p.id}"}))
    print("妙手发布调用：", miaoshou.published)
    print("执行回报：", feishu.sent[-1]["text"])
    app.board.flush(seconds=10)
    print("\n== 看板记录 ==")
    for rid, rec in feishu.records.items():
        print(rid, json.dumps(rec, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    from .tooling import activate_tools
    activate_tools()
    parser = argparse.ArgumentParser(prog="ecom-twin", description="跨境电商运营数字分身")
    parser.add_argument("--config", help="settings.toml 路径，默认 config/settings.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    setup = sub.add_parser("setup", help="打开本地飞书认证与工具配置页面")
    setup.add_argument("--port", type=int, default=8765)
    setup.add_argument("--no-browser", action="store_true")
    setup.set_defaults(func=cmd_setup)
    login = sub.add_parser("login", help="登录 agent 账号（缺少 CLI 时自动安装）")
    login.add_argument("runtime", choices=list(RUNTIMES))
    login.set_defaults(func=cmd_login)
    sub.add_parser("check").set_defaults(func=cmd_check)
    h = sub.add_parser("health")
    h.add_argument("--platform", default="tiktok")
    h.add_argument("--site", default="")
    h.set_defaults(func=cmd_health)
    pp = sub.add_parser("propose-publish")
    pp.add_argument("file")
    pp.set_defaults(func=cmd_propose_publish)
    s = sub.add_parser("serve")
    s.add_argument("--interval", type=float, default=5.0)
    s.add_argument("--workers", type=int, default=2, help="同时处理的任务数")
    s.set_defaults(func=cmd_serve)
    a = sub.add_parser("ask")
    a.add_argument("text")
    a.add_argument("--runtime", help="指定底座：claude_code / codex / cursor / opencode")
    a.add_argument("--console", action="store_true", help="飞书消息只打印，不真实发送")
    a.set_defaults(func=cmd_ask)
    sub.add_parser("tick").set_defaults(func=cmd_tick)
    m = sub.add_parser("memory")
    m.add_argument("action", choices=["drafts", "approve", "reject", "recalls", "digest"])
    m.add_argument("id", nargs="?")
    m.set_defaults(func=cmd_memory)
    e = sub.add_parser("evidence-image", help="准备、收集或标注任务截图证据")
    es = e.add_subparsers(dest="evidence_action", required=True)
    ep = es.add_parser("prepare", help="输出给浏览器 MCP 使用的唯一 PNG 路径")
    ep.add_argument("task_id")
    ep.add_argument("--name", default="screenshot.png")
    ep.add_argument("--staging-root", help=argparse.SUPPRESS)
    ep.set_defaults(func=cmd_evidence)
    ec = es.add_parser("collect", help="校验 MCP 生成的 PNG 并复制到证据目录")
    ec.add_argument("task_id")
    ec.add_argument("source")
    ec.add_argument("--name", default="screenshot.png")
    ec.add_argument("--destination")
    ec.set_defaults(func=cmd_evidence)
    ed = es.add_parser("desktop", help="截取 macOS 窗口，可用 --box x,y,width,height 标注")
    ed.add_argument("task_id")
    ed.add_argument("window_id")
    ed.add_argument("--name", default="desktop.png")
    ed.add_argument("--destination")
    ed.add_argument("--box", help="红框坐标：x,y,width,height")
    ed.set_defaults(func=cmd_evidence)
    b = sub.add_parser("browser", help="启动给 Agent 使用的隔离 Chrome")
    bs = b.add_subparsers(dest="browser_action", required=True)
    start = bs.add_parser("start")
    start.add_argument("--port", type=int, default=9222)
    start.add_argument("--chrome-path", default="")
    start.add_argument("--user-data-dir", default="")
    start.add_argument("--url", default="about:blank")
    start.set_defaults(func=cmd_browser)
    bl = sub.add_parser("browser-login", help="启动内部平台浏览器并打开登录页，保存登录状态后按 Ctrl+C 退出")
    bl.add_argument("platform", help="平台 key，对应 settings.toml [platforms.<key>]")
    bl.set_defaults(func=cmd_browser_login)
    c = sub.add_parser("console", help="Launch admin console web UI for memory maintenance and recall records")
    c.add_argument("--host", default="127.0.0.1", help="Host to listen on (default: 127.0.0.1)")
    c.add_argument("--port", type=int, default=8766, help="Port to listen on (default: 8766)")
    c.set_defaults(func=cmd_console)
    r = sub.add_parser("runtimes")
    r.add_argument("--path", action="append", help="临时指定路径试验证，例如 --path cursor=/path/to/cursor-agent")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_runtimes)
    sub.add_parser("demo").set_defaults(func=cmd_demo)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
