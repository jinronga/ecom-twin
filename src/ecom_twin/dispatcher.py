"""调度：私聊建单 → 调 agent → 汇报回 thread（DESIGN.md 5、10.1、13.2 P1）。

- 负责人私聊分身的消息（不是审批指令）→ 新建任务并派给 agent
- 在汇报 / 提问消息下回复 → 并回原任务，续跑同一个底座会话
- 审批被拒绝 → 带着拒绝原因唤醒 agent 调整
- 底座失败：可重试的错误（满载、配额、限流、超时）原地重试，连续失败后切换备选底座从头重跑（5.6.5）
"""

from __future__ import annotations

import sys
import shutil
import threading
import time
import hashlib
import json
from concurrent.futures import Executor as PoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .mcp_server import SERVER_NAME, tool_names
from . import evidence
from .memory import MemoryStore
from .models import Proposal, TaskSource, TaskStatus
from .runtime import AgentRuntime, RunSpec
from .task_tracker import TaskTracker

PROMPT_FILE = Path(__file__).parent / "prompts" / "system.md"
RETRYABLE_MARKERS = ("capacity", "quota", "rate limit", "rate_limit", "overloaded", "429", "529", "timeout",
                     "timed out", "满载", "配额", "限流")
RETRY_DELAY_SEC = 30


def is_retryable(message: str) -> bool:
    message = (message or "").lower()
    return any(m in message for m in RETRYABLE_MARKERS)


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
    ):
        self.app = app
        self.memory = memory
        self.runtime_factory = runtime_factory
        self.workroot = Path(workroot)
        self.config_path = config_path
        self.pool = pool  # None 时同步执行（测试用）
        self.sleep = sleep
        self._task_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._queue_active = False
        self.task_tracker = TaskTracker()  # 功能2：运行时任务跟踪器

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
        """处理负责人私聊分身的非审批消息。返回回执文本；不归分身处理时返回 None。"""
        owner = self.app.settings.feishu.owner_open_id
        if msg.get("chat_type") != "p2p" or msg.get("sender_id") != owner:
            return None
        text = (msg.get("content") or "").strip()
        if not text:
            return None
        parent = msg.get("parent_id") or self.app.feishu.get_parent_id(msg["message_id"])
        task_id = self.app.store.task_for_message(parent) if parent else None
        if task_id:
            receipt = f"已并回 {task_id}，继续处理。"
            out_id = self.app.feishu.reply_text(msg["message_id"], receipt)
            self.app.store.log_message({"message_id": out_id, "chat_type": "p2p", "content": receipt},
                                       "outbound", task_id)
            self._submit(task_id, f"负责人回复：{self._message_prompt(msg, task_id)}", resume=True)
            return receipt
        task = self.app.tasks.create(text[:40], TaskSource.SELF_DM, "待定", owner=owner)
        task.origin_message_id = msg["message_id"]
        self.app.store.save_task(task)
        receipt = f"收到，已建任务 {task.id}。"
        out_id = self.app.feishu.reply_text(msg["message_id"], receipt)
        self.app.store.log_message({"message_id": out_id, "chat_type": "p2p", "content": receipt},
                                   "outbound", task.id)
        self.app.store.link_message_task(msg["message_id"], task.id)
        self._submit(task.id, f"负责人在飞书私聊里交代的任务：\n{self._message_prompt(msg, task.id)}", resume=False)
        return receipt

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
        path.write_text(json.dumps({'prompt': prompt, 'resume': resume}, ensure_ascii=False), encoding='utf-8')
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self.app.store.enqueue_task(task_id, str(path), resume)
        self.app.tasks.set_status(task_id, TaskStatus.QUEUED)
        self.start_queue_worker()

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
                item = self.app.store.claim_next_task()
                if not item:
                    break
                try:
                    payload = json.loads(Path(item['prompt_path']).read_text(encoding='utf-8'))
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
    def _spec(self, task_id: str, prompt: str, recalled_memory: str = "") -> RunSpec:
        evidence_dir = self.workroot / task_id / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        browser = getattr(self.app.settings, "browser", None)
        system = PROMPT_FILE.read_text(encoding="utf-8").format(task_id=task_id,
                                                                memory_index=self.memory.index_text(),
                                                                recalled_memory=recalled_memory or "（本轮尚未召回正文）",
                                                                evidence_dir=str(evidence_dir))
        args = ["-m", "ecom_twin.mcp_server", "--task", task_id]
        if self.config_path:
            args += ["--config", self.config_path]
        mcp_servers = {SERVER_NAME: {"command": sys.executable, "args": args}}
        allowed_tools = [f"mcp__{SERVER_NAME}__{name}" for name in tool_names()]
        if browser and browser.enabled:
            chrome_binary = shutil.which("chrome-devtools-mcp")
            chrome_command = chrome_binary or "npx"
            chrome_args = ([] if chrome_binary else ["-y", "chrome-devtools-mcp@latest"]) + [
                           "--browserUrl", browser.browser_url, "--workspace", str(self.workroot / task_id),
                           "--no-usage-statistics"]
            if browser.chrome_path:
                chrome_args += ["--executablePath", browser.chrome_path]
            if browser.user_data_dir:
                chrome_args += ["--userDataDir", browser.user_data_dir]
            mcp_servers["chrome-devtools"] = {"command": chrome_command, "args": chrome_args}
            allowed_tools += ["mcp__chrome-devtools__*"]
        return RunSpec(
            prompt=prompt, system_prompt=system, workdir=self.workroot / task_id,
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
        )

    def _run_once(self, runtime: AgentRuntime, key: str, task_id: str, spec: RunSpec,
                  resume_id: str | None) -> RunResult:
        texts, errors, ok = [], [], False
        for event in runtime.run(spec, resume_id=resume_id):
            task = self.app.store.get_task(task_id)
            if event.type == "session":
                task.session_id, task.runtime = event.data.get("session_id") or "", key
                self.app.store.save_task(task)
            elif event.type == "usage":
                task.tokens_in += int(event.data.get("input_tokens") or 0)
                task.tokens_out += int(event.data.get("output_tokens") or 0)
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
        error = "；".join(e for e in errors if e)
        self._collect_evidence(task_id, spec.workdir)
        return RunResult(ok, texts[-1] if texts else "", error, is_retryable(error))

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
        settings = self.app.settings.runtime
        task = self.app.store.get_task(task_id)
        self.app.tasks.set_status(task_id, TaskStatus.RUNNING)
        # 每轮启动都自动召回，保证 Multica/其他编排器续跑时也拿到本地经验。
        recalled = self.memory.recall_for_task(f"{task.title}\n{prompt}", task_id=task_id)
        recalled_memory = "\n\n".join(
            f"### {m.name}\n{m.body}" for m in recalled
        ) or "（没有命中的记忆；需要时仍可调用 recall_memory）"
        primary = task.runtime if (resume and task.runtime) else settings.default
        order = [primary] + [k for k in (settings.default, settings.fallback) if k != primary]
        spec = self._spec(task_id, prompt, recalled_memory)
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
                    spec = self._spec(task_id, f"（接续任务 {task_id}，此前由其他底座处理）\n{prompt}", recalled_memory)

        self._finish(task_id, result, run)
        return result

    def _finish(self, task_id: str, result: RunResult, run: Any = None) -> None:
        task = self.app.store.get_task(task_id)
        if result.ok:
            # 提案待审批、等负责人回复时保持原状态；否则本轮结束
            if task.status == TaskStatus.RUNNING:
                self.app.tasks.set_status(task_id, TaskStatus.CLOSED, conclusion=result.text[:500])
            else:
                self.app.tasks.set_status(task_id, task.status, conclusion=result.text[:500])
            body = result.text or "（本轮没有输出结论）"
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
        text = f"[{task_id}] {task.title}\n状态：{task.status.value}\n\n{body}\n\n（在本消息下回复可追加指令）"
        if task.origin_message_id:
            msg_id = self.app.feishu.reply_text(task.origin_message_id, text)
        else:
            msg_id = self.app.feishu.send_text(task.owner or self.app.settings.feishu.owner_open_id, text)
        self.app.store.log_message({"message_id": msg_id, "chat_type": "p2p", "content": text},
                                   "outbound", task_id)
        self.app.store.link_report(msg_id, task_id)
