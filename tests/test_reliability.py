"""可靠性：超时清理整个进程树；服务重启时恢复上次被中断的任务。"""

import json
import os
import sys
import time

from ecom_twin.models import TaskStatus
from ecom_twin.runtime.base import AgentRuntime, RunSpec

# 底座 CLI 的替身：起一个孙子进程（继承输出管道），自己也卡住不退
HANG = '''
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(f"pid={child.pid}", flush=True)
time.sleep(60)
'''


class Hanging(AgentRuntime):
    name = "hanging"

    def build_command(self, spec, resume_id=None):
        return [sys.executable, "-c", HANG]

    def parse_line(self, obj):
        return []


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_timeout_kills_the_whole_process_tree(tmp_path):
    spec = RunSpec(prompt="p", system_prompt="s", workdir=tmp_path, timeout_sec=1)
    started = time.monotonic()
    events = list(Hanging(binary=sys.executable).run(spec))
    # 以前只杀 CLI 本身，孙子进程占着输出管道，这里会卡到孙子 60 秒睡完
    assert time.monotonic() - started < 15
    grandchild = int(next(e.data["text"] for e in events if e.type == "message").split("=")[1])
    deadline = time.monotonic() + 5
    while alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(grandchild)
    assert any(e.type == "done" and not e.data.get("ok") for e in events)


def test_interrupted_runs_are_requeued_with_a_note(env, tmp_path):
    from test_mcp_and_dispatcher import dispatcher
    d, _ = dispatcher(env, tmp_path, {})
    prompt_file = tmp_path / "p.json"
    prompt_file.write_text(json.dumps({"prompt": "把采集箱第一款上到测试店", "resume": True}), encoding="utf-8")
    env.app.store.enqueue_task(env.task.id, str(prompt_file), resume=True)
    assert env.app.store.claim_next_task()["task_id"] == env.task.id      # 领取后服务崩了，停在 running
    env.app.tasks.set_status(env.task.id, TaskStatus.RUNNING)

    assert d.recover_interrupted() == [env.task.id]
    item = env.app.store.get_task_queue(env.task.id)
    assert item["state"] == "queued" and "被中断" in item["error"]
    assert env.app.store.get_task(env.task.id).status == TaskStatus.QUEUED
    prompt = json.loads(prompt_file.read_text(encoding="utf-8"))["prompt"]
    assert prompt.startswith(d.INTERRUPTED_NOTE) and prompt.endswith("把采集箱第一款上到测试店")
    assert "queue_recovered" in env.app.store.audit_events(env.task.id)
    # 再重启一次不会叠加说明
    env.app.store.claim_next_task()
    d.recover_interrupted()
    assert json.loads(prompt_file.read_text(encoding="utf-8"))["prompt"].count("被中断") == 1
