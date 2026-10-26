"""定时任务调度（DESIGN.md 4.2）。

进程内的简单调度器：每个作业有固定间隔，上次运行时间存在本地库（重启后不会立刻重跑）。
作业本身尽量是确定性代码，只在需要判断时才唤醒 agent。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .adapters.base import AdapterError
from .store import Store, utcnow

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    interval: timedelta
    fn: Callable[[datetime], Any]


class Scheduler:
    def __init__(self, store: Store, jobs: list[Job]):
        self.store = store
        self.jobs = jobs

    def tick(self, now: datetime | None = None) -> list[str]:
        """运行所有到期的作业，返回本次运行的作业名。单个作业失败不影响其他作业。"""
        now = now or utcnow()
        ran = []
        for job in self.jobs:
            last = self.store.kv_get(f"job:{job.name}")
            if last and datetime.fromisoformat(last) + job.interval > now:
                continue
            try:
                job.fn(now)
            except Exception:  # noqa: BLE001 —— 调度器要兜住所有作业异常
                log.exception("定时作业 %s 失败", job.name)
            self.store.kv_set(f"job:{job.name}", now.isoformat())
            ran.append(job.name)
        return ran


def health_check_job(store: Store, feishu: Any, owner_open_id: str, miaoshou: Any) -> Callable[[datetime], None]:
    """适配器每日健康检查（DESIGN.md 6.5）：只读冒烟，失败当天告警一次。"""

    def run(now: datetime) -> None:
        try:
            shops = miaoshou.list_shops("tiktok")
            store.kv_set("health:miaoshou", f"ok {now.isoformat()} shops={len(shops)}")
        except AdapterError as e:
            store.kv_set("health:miaoshou", f"fail {now.isoformat()} {e}")
            day = now.strftime("%Y-%m-%d")
            if store.kv_get("health:miaoshou:alerted") != day:
                text = f"妙手接口健康检查失败（{e.category.value}）：{e}。相关写操作会失败，请检查凭据、IP 白名单或接口变更。"
                msg_id = feishu.send_text(owner_open_id, text)
                store.log_message({"message_id": msg_id, "chat_type": "p2p", "content": text}, "outbound")
                store.kv_set("health:miaoshou:alerted", day)

    return run


def build_jobs(app: Any, memory: Any, miaoshou: Any | None) -> list[Job]:
    jobs = [
        Job("approval_sweep", timedelta(minutes=1), lambda now: app.approvals.sweep(now)),
        Job("board_flush", timedelta(seconds=5), lambda now: app.board.flush(now, seconds=5)),
        Job("followups", timedelta(minutes=1), lambda now: app.followups.run_due(now)),
        Job("rejection_digest", timedelta(days=7), lambda now: memory.rejection_digest(days=7, now=now)),
    ]
    if miaoshou is not None:
        jobs.append(Job("health_miaoshou", timedelta(days=1),
                        health_check_job(app.store, app.feishu, app.settings.feishu.owner_open_id, miaoshou)))
    return jobs
