"""调度：私聊建单 → 调 agent → 汇报回 thread（DESIGN.md 5、10.1、13.2 P1）。

- 负责人私聊分身的消息（不是审批指令）→ 新建任务并派给 agent
- 在汇报 / 提问消息下回复 → 并回原任务，续跑同一个底座会话
- 审批被拒绝 → 带着拒绝原因唤醒 agent 调整
- 底座失败：可重试的错误（满载、配额、限流、超时）原地重试，连续失败后切换备选底座从头重跑（5.6.5）
"""

from __future__ import annotations

import sys
import threading
import time
import hashlib
import json
import re
from datetime import timedelta
from concurrent.futures import Executor as PoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .computer_use import (ALLOWED_TOOLS as COMPUTER_TOOLS, SERVER_NAME as COMPUTER_SERVER, applescript_section,
                           find_cua, prompt_section)
from .chrome_guard import INTERACTIVE_TOOLS, READONLY_TOOLS, upstream_command, verify_dedicated
from .mcp_server import SERVER_NAME, tool_names
from . import evidence
from .memory import MemoryStore
from .models import Proposal, TaskStatus
from .sops import SopLibrary
from .store import utcnow
from .runtime import AgentRuntime, RunSpec
from .task_tracker import TaskTracker

PROMPT_FILE = Path(__file__).parent / "prompts" / "system.md"
RETRYABLE_MARKERS = ("capacity", "quota", "rate limit", "rate_limit", "overloaded", "429", "529", "timeout",
                     "timed out", "满载", "配额", "限流")
RETRY_DELAY_SEC = 30
HEARTBEAT_SEC = 30  # agent 运行期间定时更新心跳，看板据此判断是否卡住（DESIGN.md 11.4、11.5）


def is_retryable(message: str) -> bool:
    message = (message or "").lower()
    return any(m in message for m in RETRYABLE_MARKERS)


# 每轮结论第一行（对照数字分身的输出契约）：relevant / reason / waiting。全角标点、大小写都容忍
TRIAGE_LINE = re.compile(r"^\s*relevant\s*[:：]\s*(yes|no)\s*[;；]\s*reason\s*[:：]\s*(.*?)\s*[;；]\s*"
                         r"waiting\s*[:：]\s*(.*?)\s*$", re.IGNORECASE)
NOT_WAITING = {"", "无", "none", "无。", "-"}
# 汇报核对：提到的提案号，明确声称提交了提案、附了截图的说法（只认肯定句式）
PROPOSAL_ID = re.compile(r"\bP-\d+\b")
CLAIM_PROPOSAL = re.compile(r"(已经?提交|提交了)[^，。；\n]{0,6}提案|提案已(经)?提交")
CLAIM_EVIDENCE = re.compile(r"截图已(经)?(上报|附|随汇报|作为证据)|(已|已经)上报[^，。；\n]{0,4}(截图|证据)|附上了?截图")
# 判 no 必须写出属于哪一类（对照数字分身 0.2.66）。都是正面理由：说得出这条为什么和负责人的活无关。
# 「看不出对象」「没点名」不在里面 —— 那是还没认出它在接哪句；写不出类别的照样不处理，但标成存疑给人看
NO_KINDS = ("闲聊", "表态", "点了别人", "同事的活", "无关通知", "已办完")


@dataclass
class Triage:
    relevant: bool
    reason: str
    waiting: str  # 不等时为空
    kind: str = ""  # 判 no 时属于哪一类
    doubtful: bool = False  # 判了 no 却写不出类别


def parse_triage(text: str) -> tuple[Triage | None, str]:
    """拆出第一行的分诊结果和后面的汇报正文。没按格式写就返回 None 和原文（不因格式问题丢汇报）。"""
    lines = (text or "").strip().splitlines()
    match = TRIAGE_LINE.match(lines[0]) if lines else None
    if not match:
        return None, text
    reason = match.group(2).strip()
    waiting = match.group(3).strip()
    if waiting.startswith("「") and waiting.endswith("」") and waiting.count("「") == 1:
        waiting = waiting[1:-1]  # 整句被一对书名号包住才去掉；句中的「好了」要保留
    relevant = match.group(1).lower() == "yes"
    head = reason[:8]  # 类别写在开头附近就算（「群里闲聊」也认），整句里某处碰巧出现不算
    kind = "" if relevant else next((k for k in NO_KINDS if k in head), "")
    triage = Triage(relevant, reason, "" if waiting.lower() in NOT_WAITING else waiting,
                    kind=kind, doubtful=not relevant and not kind)
    return triage, "\n".join(lines[1:]).strip()


@dataclass
class RunResult:
    ok: bool
    text: str = ""
    error: str = ""
    retryable: bool = False


class Dispatcher:
    def __init__(
        self,
        app: Any,
        memory: MemoryStore,
        runtime_factory: Callable[[str], AgentRuntime],
        workroot: Path,
        config_path: str | None = None,
        pool: PoolExecutor | None = None,
        sleep: Callable[[float], None] = time.sleep,
        judge: Any = None,
        heartbeat_sec: float = HEARTBEAT_SEC,
    ):
        self.app = app
        self.memory = memory
        self.runtime_factory = runtime_factory
        self.workroot = Path(workroot)
        self.config_path = config_path
        self.pool = pool  # None 时同步执行（测试用）
        self.heartbeat_sec = heartbeat_sec
        self.chrome_check = verify_dedicated  # 挂浏览器前确认端口上是专用 Chrome，不是负责人的日常 Chrome
        self.sleep = sleep
        self._task_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._queue_active = False
        # 入队（读状态、写提示词、入库）和领取（领取、读提示词）互斥，避免追加的消息被读漏或重复执行
        self._queue_lock = threading.Lock()
        self.task_tracker = TaskTracker()  # 功能2：运行时任务跟踪器
        from .intake import Intake
        from .outbound import Outbound
        self.outbound = Outbound(app)
        self.intake = Intake(app, self, judge)

    def _task_lock(self, task_id: str) -> threading.Lock:
        with self._guard:
            return self._task_locks.setdefault(task_id, threading.Lock())

    # ---- 入口 ----
    def _message_prompt(self, msg: dict[str, Any], task_id: str) -> str:
        """把飞书消息转换为任务提示；图片只传本地附件路径，不把图片内容写日志。"""
        text = (msg.get("prompt_content") or msg.get("content") or "").strip()
        if not (msg.get("image_keys") or msg.get("image_key")):
            return text
        image_keys = list(msg.get("image_keys") or [])
        if msg.get("image_key"):
            image_keys.append(msg["image_key"])
        downloader = getattr(self.app.feishu, "download_image", None)
        if not image_keys or not callable(downloader):
            return "负责人在飞书发送了一张图片，但当前无法下载图片附件，请告知负责人图片暂不可读取。"
        digest = hashlib.sha256(str(msg.get("message_id", "")).encode()).hexdigest()[:12]
        paths = []
        for index, image_key in enumerate(image_keys, 1):
            destination = self.workroot / task_id / "attachments" / f"feishu-image-{digest}-{index}"
            try:
                paths.append(downloader(msg.get("message_id", ""), image_key, destination))
            except Exception:
                return "负责人在飞书发送了一张图片，但图片附件下载失败，请告知负责人稍后重试。"
        return (f"负责人在飞书发送了 {len(paths)} 张图片。请读取任务工作目录中的图片附件，先脱敏再处理。"
                f"附带文字说明：{text}\n附件路径：" + ", ".join(str(path) for path in paths))

    def handle_message(self, msg: dict[str, Any]) -> str | None:
        """处理一条非审批消息：交给消息入口（过滤、攒批、合并、驱动）。立即处理时返回回执。"""
        receipts = self.intake.accept(msg)
        return receipts[0] if receipts else None

    def on_proposal_rejected(self, p: Proposal) -> None:
        reason = p.reject_reason.value if p.reject_reason else "未说明"
        self._submit(p.task_id, f"提案 {p.id} 被负责人拒绝。原因：{reason}；备注：{p.reject_note or '无'}。"
                                f"请根据原因调整：需要的话重新提交提案，或者说明为什么结束任务。", resume=True)

    def _submit(self, task_id: str, prompt: str, resume: bool) -> None:
        if self.pool is None:
            self.run_task(task_id, prompt, resume)
            return
        path = self.workroot / task_id / 'queue_prompt.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._queue_lock:
            payload = {'prompt': prompt, 'resume': resume}
            waiting = self.app.store.get_task_queue(task_id)
            if waiting and waiting['state'] == 'queued' and path.exists():
                # 还没开跑又来一条：并进同一轮，前面的消息不能被覆盖掉
                earlier = json.loads(path.read_text(encoding='utf-8'))
                payload = {'prompt': f"{earlier['prompt']}\n\n（随后又补充）\n{prompt}", 'resume': earlier['resume']}
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            try:
                path.chmod(0o600)
            except OSError:
                pass
            self.app.store.enqueue_task(task_id, str(path), payload['resume'])
        self.app.tasks.set_status(task_id, TaskStatus.QUEUED)
        self.start_queue_worker()

    INTERRUPTED_NOTE = ("（上一轮做到一半时服务重启被中断了。先看任务卡里的提案状态和看板上的步骤，"
                        "已经提交过的提案不要重复提交，从中断的地方接着做。）\n")

    def recover_interrupted(self) -> list[str]:
        """服务启动时恢复上次被中断的任务：重新排队，提示词前面加一句中断说明，防止重复提交提案。"""
        recovered = []
        for item in self.app.store.recover_interrupted_queue():
            task_id = item["task_id"]
            path = Path(item["prompt_path"])
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not str(payload.get("prompt", "")).startswith(self.INTERRUPTED_NOTE):
                    payload["prompt"] = self.INTERRUPTED_NOTE + payload.get("prompt", "")
                    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except (OSError, ValueError):
                pass  # 读不到提示词文件：照样重新排队，领取时会按坏文件处理并记错
            task = self.app.store.get_task(task_id)
            if task and task.status == TaskStatus.RUNNING:
                self.app.tasks.set_status(task_id, TaskStatus.QUEUED)
            self.app.store.audit("task", task_id, "queue_recovered", "system", {"started_at": item.get("started_at")})
            recovered.append(task_id)
        return recovered

    def start_queue_worker(self) -> None:
        if self.pool is None:
            return
        with self._guard:
            if self._queue_active:
                return
            self._queue_active = True
        self.pool.submit(self._drain_queue)

    def _drain_queue(self) -> None:
        try:
            while not self.app.store.queue_paused():
                with self._queue_lock:
                    item = self.app.store.claim_next_task()
                    if not item:
                        break
                    try:
                        payload = json.loads(Path(item['prompt_path']).read_text(encoding='utf-8'))
                    except (OSError, ValueError) as error:
                        self.app.store.finish_task_queue(item['task_id'], str(error))
                        continue
                try:
                    self.run_task(item['task_id'], payload['prompt'], bool(payload.get('resume')))
                    self.app.store.finish_task_queue(item['task_id'])
                except Exception as error:  # keep the queue moving after one bad task
                    self.app.store.finish_task_queue(item['task_id'], str(error))
        finally:
            with self._guard:
                self._queue_active = False
            # A message may have arrived between the last claim and release.
            if not self.app.store.queue_paused() and self.app.store.list_task_queue(('queued',)):
                self.start_queue_worker()

    # ---- 执行 ----
    def task_card(self, task_id: str) -> str:
        """本任务的背景卡片：每次开局按任务现状生成，续跑时也带上最新的提案状态和拒绝原因。"""
        task = self.app.store.get_task(task_id)
        lines = [f"- 任务号：{task.id}；标题：{task.title}",
                 f"- 来源：{task.source.value}；场景：{task.scene}；当前状态：{task.status.value}"]
        if task.shop_key:
            lines.append(f"- 店铺：{task.shop_key}（动手前用 get_shop_context 查背景）")
        if task.anchors:
            lines.append(f"- 锚点：{', '.join(task.anchors)}")
        if task.conclusion:
            lines.append(f"- 上一轮结论：{task.conclusion[:300]}")
        if task.blocker:
            lines.append(f"- 当前卡点：{task.blocker[:300]}")
        if task.switch_log:
            lines.append("- 此前换过底座，以下是本任务已有的进展，不要重复已完成的步骤")
        for p in self.app.store.proposals_for_task(task_id):
            line = f"- 提案 {p.id}（{p.action}）：{p.status.value}"
            if p.reject_reason:
                line += f"，拒绝原因：{p.reject_reason.value}，备注：{p.reject_note or '无'}"
            lines.append(line)
        done = [s for s in self.app.store.list_steps(task_id) if s.status.value == "成功"][-8:]
        if done:
            lines.append("- 已完成的步骤：" + "；".join(s.name for s in done))
        return "\n".join(lines)

    def _computer_use_server(self, task_id: str) -> dict | None:
        """开启了操作电脑、且本机装了 Computer Use 时，返回经 ecom-twin 代理的 computer MCP 定义。"""
        cfg = self.app.settings.computer_use
        if not (cfg.enabled and find_cua(cfg)):
            return None
        args = ["-m", "ecom_twin.computer_use", "--task", task_id]
        if self.config_path:
            args += ["--config", self.config_path]
        return {"command": sys.executable, "args": args}

    def _browser_mode(self, unavailable: str | None) -> str:
        if unavailable:
            return (f"- 专用浏览器这一轮不可用：{unavailable}。需要浏览器的步骤不要另找办法打开网页，"
                    "在汇报里写明卡点，请负责人运行 ecom-twin browser start 后再派。\n")
        browser = getattr(self.app.settings, "browser", None)
        name = "browser_interactive.md" if browser and browser.interactive else "browser_readonly.md"
        text = (PROMPT_FILE.parent / name).read_text(encoding="utf-8")
        domains = browser.auto_login_domains if browser and browser.enabled and browser.interactive else []
        text += "\n### 本轮自动登录权限\n"
        if domains:
            text += "负责人已允许自动登录的完整域名：" + "、".join(domains) + "。\n"
            text += ("只在专用 Chrome 中操作：先用页面快照核对当前 URL 是 HTTPS 且主机名与名单完全一致，"
                     "然后可使用负责人为该平台明确提供的账号密码填写登录表单并点击登录，不必再次询问。"
                     "名单不包含其子域名，也不授权跳转后的第三方登录域名。缺少凭证、跳到名单外、"
                     "遇到验证码/短信/MFA或登录失败时停止登录并请负责人协助，不猜密码、不反复尝试。"
                     "不得在汇报、记忆、提案中复述凭证，不截取明文密码。登录后先核实成功，再执行本次查询。"
                     "此授权只允许登录，不包含创建单据、审批、删除、改价等业务操作。\n")
        else:
            text += "当前没有已授权自动登录的域名；需要登录时请负责人完成。\n"
        return text

    def _spec(self, task_id: str, prompt: str) -> RunSpec:
        from .chat_context import automatic_context
        context = automatic_context(self.app, task_id)
        evidence_dir = self.workroot / task_id / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        browser = getattr(self.app.settings, "browser", None)
        computer = self._computer_use_server(task_id)
        cu = self.app.settings.computer_use
        unavailable = self.chrome_check(browser) if browser and browser.enabled else None
        if unavailable:
            self.app.store.audit("task", task_id, "browser_unavailable", "system", {"reason": unavailable})
        sections = ([prompt_section(cu)] if computer else []) + \
            ([applescript_section(cu)] if cu.applescript else [])
        system = PROMPT_FILE.read_text(encoding="utf-8").format(task_id=task_id,
                                                                computer_use="".join(s + "\n" for s in sections),
                                                                task_card=self.task_card(task_id),
                                                                sop_index=SopLibrary(self.app.settings.skill_dirs).index_text(),
                                                                memory_index=self.memory.index_text(),
                                                                evidence_dir=str(evidence_dir),
                                                                browser_mode=self._browser_mode(unavailable))
        args = ["-m", "ecom_twin.mcp_server", "--task", task_id]
        if self.config_path:
            args += ["--config", self.config_path]
        mcp_servers = {SERVER_NAME: {"command": sys.executable, "args": args}}
        allowed_tools = [f"mcp__{SERVER_NAME}__{name}" for name in tool_names(desktop=cu.applescript)]
        if browser and browser.enabled and not unavailable:
            # interactive（默认，2026-10-30 负责人决定和数字分身一样）经 chrome_guard 代理全部放开；
            # 关掉时只能截图和读页面。公开网页另有 capture_page（有域名白名单）
            if browser.interactive:
                guard = ["-m", "ecom_twin.chrome_guard", "--task", task_id, "--workdir", str(self.workroot / task_id)]
                if self.config_path:
                    guard += ["--config", self.config_path]
                mcp_servers["chrome-devtools"] = {"command": sys.executable, "args": guard}
                chrome_tools = INTERACTIVE_TOOLS
            else:
                upstream = upstream_command(browser, self.workroot / task_id)
                mcp_servers["chrome-devtools"] = {"command": upstream[0], "args": upstream[1:]}
                chrome_tools = READONLY_TOOLS
            allowed_tools += [f"mcp__chrome-devtools__{name}" for name in chrome_tools]
        if computer:
            mcp_servers[COMPUTER_SERVER] = computer
            allowed_tools += [f"mcp__{COMPUTER_SERVER}__{name}" for name in COMPUTER_TOOLS]
        return RunSpec(
            prompt=context + prompt, system_prompt=system, workdir=self.workroot / task_id,
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
            shell_commands=list(cu.shell_commands) if cu.applescript else [],
            shell_guard=([sys.executable, "-m", "ecom_twin.shell_guard", "--task", task_id]
                         + (["--config", self.config_path] if self.config_path else [])) if cu.applescript else [],
        )

    def _run_once(self, runtime: AgentRuntime, key: str, task_id: str, spec: RunSpec,
                  resume_id: str | None) -> RunResult:
        texts, errors, ok = [], [], False
        usage = {"in": 0, "out": 0, "cost": None}
        spec.transcript_path = self.workroot.parent / "transcripts" / task_id / \
            f"{utcnow().strftime('%Y%m%dT%H%M%S')}-{key}.jsonl"
        task = self.app.store.get_task(task_id)
        task.transcript = str(spec.transcript_path)
        self.app.store.save_task(task)
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.heartbeat_sec):
                self.app.tasks.heartbeat(task_id)

        heartbeat = threading.Thread(target=beat, daemon=True)
        heartbeat.start()
        try:
            self._consume(runtime, key, task_id, spec, resume_id, texts, errors, usage)
        finally:
            stop.set()
            heartbeat.join(timeout=1)
            self._collect_evidence(task_id, spec.workdir)
        ok = usage.pop("ok", False)
        if usage["in"] or usage["out"] or usage["cost"] is not None:
            self.app.store.add_usage(self.app.store.get_task(task_id), key, usage["in"], usage["out"], usage["cost"])
        error = "；".join(e for e in errors if e)
        return RunResult(ok, texts[-1] if texts else "", error, is_retryable(error))

    def _consume(self, runtime: AgentRuntime, key: str, task_id: str, spec: RunSpec, resume_id: str | None,
                 texts: list, errors: list, usage: dict) -> None:
        ok = False
        for event in runtime.run(spec, resume_id=resume_id):
            task = self.app.store.get_task(task_id)
            if event.type == "session":
                task.session_id, task.runtime = event.data.get("session_id") or "", key
                self.app.store.save_task(task)
            elif event.type == "usage":
                task.tokens_in += int(event.data.get("input_tokens") or 0)
                task.tokens_out += int(event.data.get("output_tokens") or 0)
                usage["in"] += int(event.data.get("input_tokens") or 0)
                usage["out"] += int(event.data.get("output_tokens") or 0)
                if event.data.get("cost_usd") is not None:
                    usage["cost"] = (usage["cost"] or 0) + float(event.data["cost_usd"])
                self.app.store.save_task(task)
                self.app.board.enqueue_task(task)
            elif event.type == "tool_call":
                self.app.tasks.heartbeat(task_id)
            elif event.type == "message":
                texts.append(event.data.get("text", ""))
            elif event.type == "error":
                errors.append(str(event.data.get("message") or ""))
            elif event.type == "done":
                ok = bool(event.data.get("ok")) and not errors
                if event.data.get("result"):
                    texts.append(event.data["result"])
        usage["ok"] = ok

    def _collect_evidence(self, task_id: str, workdir: Path) -> None:
        """运行结束后自动收集 MCP 写入任务工作目录的 PNG，避免 Agent 需要 shell 权限。"""
        staging = workdir / "evidence"
        if not staging.exists():
            return
        for source in sorted(staging.rglob("*.png")):
            try:
                destination = evidence.final_path(task_id, source.name)
                evidence.collect(source, destination)
                self.app.store.audit("task", task_id, "evidence_collected", "system",
                                     {"path": str(destination.relative_to(evidence.EVIDENCE_ROOT))})
            except (ValueError, FileExistsError, OSError):
                # 坏图不阻塞业务任务；仍由人工从任务目录排查。
                continue

    def run_task(self, task_id: str, prompt: str, resume: bool) -> RunResult:
        # 同一任务串行：连续两条回复不会同时续跑同一个会话
        with self._task_lock(task_id):
            return self._run_task(task_id, prompt, resume)

    def _run_task(self, task_id: str, prompt: str, resume: bool) -> RunResult:
        started = utcnow()
        settings = self.app.settings.runtime
        task = self.app.store.get_task(task_id)
        self.app.tasks.set_status(task_id, TaskStatus.RUNNING)
        # 记忆只注入索引，正文由 agent 按这一轮要做的动作自己读（对照数字分身：强制召回试过又撤回了，
        # 按话题塞进来的正文常常不是这一轮要用的，反而挤掉上下文）
        primary = task.runtime if (resume and task.runtime) else settings.default
        order = [primary] + [k for k in (settings.default, settings.fallback) if k != primary]
        spec = self._spec(task_id, prompt)
        result = RunResult(False, error="没有可用的底座")

        # 功能2：开始跟踪任务运行
        run = self.task_tracker.start(task_id, prompt, resume, primary)

        for i, key in enumerate(order):
            try:
                runtime = self.runtime_factory(key)
            except ValueError as e:
                result = RunResult(False, error=str(e))
                continue
            for attempt in range(max(1, settings.max_retries_before_switch)):
                task = self.app.store.get_task(task_id)
                resume_id = task.session_id if (resume and i == 0 and task.runtime == key) else None
                result = self._run_once(runtime, key, task_id, spec, resume_id)

                # 功能2：记录重试和底座信息
                if attempt > 0:
                    run.retries += 1
                if i == 0 and result.ok:  # 首次成功时记录会话信息
                    run.runtime_used = key
                    task = self.app.store.get_task(task_id)
                    run.run_id = task.session_id or ""

                if result.ok or not result.retryable:
                    break
                if attempt + 1 < settings.max_retries_before_switch:
                    self.sleep(RETRY_DELAY_SEC)
            if result.ok:
                break
            if i + 1 < len(order):
                # 功能2：记录切底座次数
                run.switches += 1
                run.runtime_used = order[i + 1]

                task = self.app.store.get_task(task_id)
                task.switch_log = (task.switch_log + "\n" if task.switch_log else "") + \
                    f"{key} 失败（{result.error[:200]}），切换到 {order[i + 1]}"
                task.session_id = ""
                self.app.store.save_task(task)
                if resume:  # 换底座后拿不到原会话，带上任务背景从头处理
                    spec = self._spec(task_id, f"（接续任务 {task_id}，此前由其他底座处理）\n{prompt}")

        self._finish(task_id, result, run, started)
        return result

    def _finish(self, task_id: str, result: RunResult, run: Any = None, started=None) -> None:
        task = self.app.store.get_task(task_id)
        triage = None
        if result.ok:
            triage, report = parse_triage(result.text)
            self._record_triage(task_id, triage)
            if triage and not triage.relevant:
                report = report or f"判断无需处理：{triage.reason}"
            # 提案待审批、等负责人回复时保持原状态；写了在等谁的转为等待回复（关闭的任务不参与合并，
            # 等的那句回复就认不回来了）；否则本轮结束
            if task.status == TaskStatus.RUNNING and triage and triage.relevant and triage.waiting:
                self.app.tasks.set_status(task_id, TaskStatus.WAITING, conclusion=report[:500],
                                          blocker=task.blocker or triage.waiting)
            elif task.status == TaskStatus.RUNNING:
                self.app.tasks.set_status(task_id, TaskStatus.CLOSED, conclusion=report[:500])
            else:
                self.app.tasks.set_status(task_id, task.status, conclusion=report[:500])
            body = report or "（本轮没有输出结论）"
        else:
            self.app.tasks.set_status(task_id, TaskStatus.FAILED, blocker=result.error[:500])
            body = f"处理失败：{result.error or '未知错误'}"

        # 功能2：结束任务跟踪
        if run:
            outcome_map = {
                TaskStatus.CLOSED: "success",
                TaskStatus.FAILED: "failed",
                TaskStatus.AWAITING_APPROVAL: "awaiting_approval",
                TaskStatus.WAITING: "waiting_reply",
            }
            outcome = outcome_map.get(task.status, "unknown")
            self.task_tracker.finish(
                run,
                outcome=outcome,
                error=result.error if not result.ok else None,
                conclusion=result.text[:500] if result.ok else None,
            )

        task = self.app.store.get_task(task_id)
        if triage and not triage.relevant and task.origin_chat_type == "group":
            # 群消息判为不相关：不在群里回一句「无需处理」打扰大家，留痕已在审计里
            return
        waiting = f"\n等待：{task.waiting}" if task.waiting else ""
        images, broken = self._check_evidence(task_id, started)
        if broken:  # 对照数字分身的 evidence-check：交付前查一遍附图，坏的写明，不悄悄少一张
            body += "\n\n（有 " + str(len(broken)) + " 张证据图没能附上：" + "；".join(broken) + "）"
        mismatches = self._verify_claims(task_id, body, started, bool(images or broken)) if result.ok else []
        if mismatches:  # 汇报里说做了、系统里却没有对应记录：写明，不让「说做完了」代替「真做完了」
            body += "\n\n（系统核对：" + "；".join(mismatches) + "）"
        text = f"[{task_id}] {task.title}\n状态：{task.status.value}{waiting}\n\n{body}\n\n（在本消息下回复可追加指令）"
        msg_id = self.outbound.to_task(task_id, text, "report")
        self.app.store.link_report(msg_id, task_id)
        self._attach_evidence(task_id, msg_id, images)

    def _record_triage(self, task_id: str, triage: Triage | None) -> None:
        """第一行存档：waiting 写进任务（入口合并判断用），整行进审计。没按格式写也记一笔，方便复盘提示词。"""
        task = self.app.store.get_task(task_id)
        task.waiting = triage.waiting if triage else ""
        self.app.store.save_task(task)
        if task.waiting and task.origin_chat_type == "group":
            # 等的人多半不会 @分身：和 ask_owner 一样开问后窗口，这段时间群里的顶层消息也收进来判断是不是回应
            until = utcnow() + timedelta(hours=self.app.settings.intake.ask_window_hours)
            self.app.store.kv_set(f"ask_window:{task.origin_chat_id}", until.isoformat())
        if triage:
            self.app.store.audit("task", task_id, "triage", "agent", {"relevant": triage.relevant,
                                                                      "reason": triage.reason,
                                                                      "waiting": triage.waiting,
                                                                      "kind": triage.kind})
            if triage.doubtful:  # 照样不处理，但单独列到控制台「投递与合并」给人看
                self.app.store.audit("task", task_id, "triage_doubtful", "agent", {"reason": triage.reason})
        else:
            self.app.store.audit("task", task_id, "triage_missing", "agent", {})

    def _verify_claims(self, task_id: str, report: str, started, has_evidence: bool) -> list[str]:
        """拿汇报里的说法对系统记录：提到的提案存在且属于本任务；说提交了提案，这一轮确实有新提案；
        说附了截图，这一轮确实上报了证据。只认明确的说法，免得把「没有提交提案」误当成声称提交了。"""
        problems = []
        for pid in dict.fromkeys(PROPOSAL_ID.findall(report)):
            proposal = self.app.store.get_proposal(pid)
            if proposal is None:
                problems.append(f"汇报提到的 {pid} 在系统里不存在")
            elif proposal.task_id != task_id:
                problems.append(f"汇报提到的 {pid} 属于 {proposal.task_id}，不是本任务的")
        new_proposals = [p for p in self.app.store.proposals_for_task(task_id)
                         if started is None or (p.created_at and p.created_at >= started)]
        if CLAIM_PROPOSAL.search(report) and not new_proposals and not PROPOSAL_ID.search(report):
            problems.append("汇报说提交了提案，但这一轮没有新建任何提案")
        if CLAIM_EVIDENCE.search(report) and not has_evidence:
            problems.append("汇报说附了截图或证据，但这一轮没有上报任何证据")
        if problems:
            self.app.store.audit("task", task_id, "claim_mismatch", "system", {"problems": problems})
        return problems

    def _check_evidence(self, task_id: str, started, limit: int = 3) -> tuple[list[str], list[str]]:
        """本轮上报的证据图（最新 limit 张）逐张校验：返回（能附的，附不了的说明）。附不了的写审计。"""
        steps = [s for s in self.app.store.list_steps(task_id)
                 if s.evidence and (started is None or (s.ended_at and s.ended_at >= started))]
        images, broken = [], []
        for step in steps[-limit:]:
            path = Path(step.evidence)
            try:
                evidence.validate_png(path)
                images.append(str(path))
            except (OSError, ValueError) as e:
                reason = "文件不存在" if not path.exists() else f"不是有效的 PNG（{e}）"
                broken.append(f"「{step.name}」的 {path.name} {reason}")
                self.app.store.audit("task", task_id, "evidence_invalid", "system",
                                     {"step": step.name, "path": str(path), "reason": reason})
        return images, broken

    def _attach_evidence(self, task_id: str, report_id: str, images: list[str]) -> None:
        """把校验过的证据截图附在汇报下面；交付失败必须反馈，不能只留内部日志。"""
        failures = []
        for image in images:
            try:
                self.outbound.image_to(task_id, report_id, image)
            except Exception as e:  # noqa: BLE001 —— 一张上传失败仍尝试其余图片
                self.app.store.audit("task", task_id, "evidence_upload_failed", "system", {"error": str(e)})
                failures.append(f"{Path(image).name}：{str(e)[:400]}")
        if failures:
            warning = f"[{task_id}] 有 {len(failures)} 张截图未发送成功：" + "；".join(failures)
            warning += "。上一条汇报中的附图尚未全部交付，修复后请回复重试。"
            task = self.app.store.get_task(task_id)
            if task and task.status == TaskStatus.CLOSED:
                self.app.tasks.set_status(task_id, TaskStatus.FAILED, blocker="截图交付失败，请查看发送错误")
            try:
                message_id = self.outbound.to_task(task_id, warning, "report", reply_to=report_id)
                self.app.store.link_report(message_id, task_id)
            except Exception as error:
                self.app.store.audit("task", task_id, "evidence_notice_failed", "system", {"error": str(error)})
