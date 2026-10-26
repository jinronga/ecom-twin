"""本地状态存储（SQLite）。

本地数据库是任务状态的真实来源；飞书多维表格只是展示和协作视图（DESIGN.md 11.5）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    ExecMethod,
    Proposal,
    ProposalStatus,
    RejectReason,
    Step,
    StepStatus,
    Task,
    TaskSource,
    TaskStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    scene TEXT NOT NULL,
    status TEXT NOT NULL,
    shop_key TEXT,
    anchors TEXT,
    owner TEXT,
    current_step TEXT,
    runtime TEXT,
    conclusion TEXT,
    blocker TEXT,
    created_at TEXT,
    updated_at TEXT,
    last_heartbeat TEXT,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    session_id TEXT,
    switch_log TEXT,
    origin_message_id TEXT,
    origin_chat_id TEXT
);
CREATE TABLE IF NOT EXISTS task_queue (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    prompt_path TEXT NOT NULL,
    resume INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued',
    enqueued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_queue_order ON task_queue(state, enqueued_at);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT,
    error TEXT,
    evidence TEXT,
    started_at TEXT,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    shop_key TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT, desired TEXT, rationale TEXT,
    current TEXT, evidence TEXT, consequences TEXT,
    risk TEXT, method TEXT, status TEXT NOT NULL,
    expires_at TEXT, approver TEXT, decided_at TEXT,
    reject_reason TEXT, reject_note TEXT,
    card_message_id TEXT, reminded INTEGER DEFAULT 0,
    executed_at TEXT, result TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_card ON proposals(card_message_id);
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    shop_key TEXT NOT NULL,
    snapshot TEXT, request TEXT, response TEXT,
    verified INTEGER, rollback TEXT, outcome TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event TEXT NOT NULL,
    actor TEXT,
    detail TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS board_outbox (
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    fields TEXT NOT NULL,
    attempts INTEGER DEFAULT 0,
    next_attempt_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (entity, entity_id)
);
CREATE TABLE IF NOT EXISTS board_records (
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    PRIMARY KEY (entity, entity_id)
);
CREATE TABLE IF NOT EXISTS seen_events (
    event_id TEXT PRIMARY KEY,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS report_links (
    message_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    message_id TEXT,
    direction TEXT NOT NULL,
    sender_id TEXT,
    chat_type TEXT,
    content TEXT,
    task_id TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_message_log_created ON message_log(created_at);
CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    due_at TEXT NOT NULL,
    attempts INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS recall_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    query TEXT,
    hits TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS memory_drafts (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    name TEXT NOT NULL,
    description TEXT,
    type TEXT,
    body TEXT,
    reason TEXT,
    status TEXT NOT NULL,
    created_at TEXT,
    decided_at TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class _LockedConnection:
    """给共享连接加可重入锁：serve 里消息线程、定时线程、任务线程共用一个连接，
    `with conn:` 事务期间持锁，保证事务不被其他线程的写入穿插。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, *args):
        with self._lock:
            return self._conn.execute(*args)

    def executescript(self, script: str):
        with self._lock:
            return self._conn.executescript(script)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def __enter__(self):
        self._lock.acquire()
        return self._conn.__enter__()

    def __exit__(self, *exc):
        try:
            return self._conn.__exit__(*exc)
        finally:
            self._lock.release()


class Store:
    def __init__(self, db_path: str | Path):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # MCP server 是独立进程，和主进程共用同一个库，写冲突时等待而不是立刻报错
        conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        if str(db_path) != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")  # 读写不互相阻塞
        self.conn = _LockedConnection(conn)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()
        self._backfill_message_log()

    # 旧版本数据库升级：CREATE TABLE IF NOT EXISTS 不会给已有表加列，这里补齐后加的列
    _ADDED_COLUMNS = {
        "tasks": {
            "tokens_in": "INTEGER DEFAULT 0",
            "tokens_out": "INTEGER DEFAULT 0",
            "session_id": "TEXT",
            "switch_log": "TEXT",
            "origin_message_id": "TEXT",
            "origin_chat_id": "TEXT",
        },
        "steps": {"evidence": "TEXT"},
    }

    def _migrate(self) -> None:
        """检查并补齐旧数据库缺失的列。"""
        for table, columns in self._ADDED_COLUMNS.items():
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in columns.items():
                if name not in existing:
                    with self.conn:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ---- id ----
    def next_id(self, prefix: str) -> str:
        with self.conn:
            self.conn.execute(
                "INSERT INTO counters(name, value) VALUES(?, 1) "
                "ON CONFLICT(name) DO UPDATE SET value = value + 1",
                (prefix,),
            )
            value = self.conn.execute("SELECT value FROM counters WHERE name=?", (prefix,)).fetchone()[0]
        return f"{prefix}-{value:04d}"

    # ---- tasks ----
    def save_task(self, task: Task) -> Task:
        now = utcnow()
        task.created_at = task.created_at or now
        task.updated_at = now
        with self.conn:
            self.conn.execute(
                """INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, status=excluded.status,
                scene=excluded.scene, shop_key=excluded.shop_key, anchors=excluded.anchors,
                owner=excluded.owner, current_step=excluded.current_step, runtime=excluded.runtime,
                conclusion=excluded.conclusion, blocker=excluded.blocker,
                updated_at=excluded.updated_at, last_heartbeat=excluded.last_heartbeat,
                tokens_in=excluded.tokens_in, tokens_out=excluded.tokens_out,
                session_id=excluded.session_id, switch_log=excluded.switch_log,
                origin_message_id=excluded.origin_message_id, origin_chat_id=excluded.origin_chat_id""",
                (
                    task.id, task.title, task.source.value, task.scene, task.status.value,
                    task.shop_key, _j(task.anchors), task.owner, task.current_step, task.runtime,
                    task.conclusion, task.blocker, _ts(task.created_at), _ts(task.updated_at),
                    _ts(task.last_heartbeat), task.tokens_in, task.tokens_out, task.session_id,
                    task.switch_log, task.origin_message_id, task.origin_chat_id,
                ),
            )
        return task

    def get_task(self, task_id: str) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        return Task(
            id=row["id"], title=row["title"], source=TaskSource(row["source"]), scene=row["scene"],
            status=TaskStatus(row["status"]), shop_key=row["shop_key"] or "",
            anchors=json.loads(row["anchors"] or "[]"), owner=row["owner"] or "",
            current_step=row["current_step"] or "", runtime=row["runtime"] or "",
            conclusion=row["conclusion"] or "", blocker=row["blocker"] or "",
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
            last_heartbeat=_dt(row["last_heartbeat"]), tokens_in=row["tokens_in"] or 0,
            tokens_out=row["tokens_out"] or 0, session_id=row["session_id"] or "",
            switch_log=row["switch_log"] or "", origin_message_id=row["origin_message_id"] or "",
            origin_chat_id=row["origin_chat_id"] or "",
        )

    # ---- 任务队列 ----
    def enqueue_task(self, task_id: str, prompt_path: str, resume: bool = False) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO task_queue(task_id,prompt_path,resume,state,enqueued_at,started_at,finished_at,error)
                VALUES(?,?,?,'queued',?,NULL,NULL,NULL)
                ON CONFLICT(task_id) DO UPDATE SET prompt_path=excluded.prompt_path,
                    resume=excluded.resume, state='queued', enqueued_at=excluded.enqueued_at,
                    started_at=NULL, finished_at=NULL, error=NULL""",
                (task_id, prompt_path, int(resume), _ts(utcnow())),
            )

    def claim_next_task(self) -> dict[str, Any] | None:
        """原子领取最早排队任务；队列 worker 以此保证单次只触发一个任务。"""
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM task_queue WHERE state='queued' ORDER BY enqueued_at, rowid LIMIT 1"
            ).fetchone()
            if not row:
                return None
            now = _ts(utcnow())
            self.conn.execute("UPDATE task_queue SET state='running', started_at=? WHERE task_id=?",
                              (now, row['task_id']))
            result = dict(row)
            result['state'], result['started_at'] = 'running', now
            return result

    def finish_task_queue(self, task_id: str, error: str = "") -> None:
        with self.conn:
            self.conn.execute("UPDATE task_queue SET state='done', finished_at=?, error=? WHERE task_id=?",
                              (_ts(utcnow()), error[:500], task_id))

    def cancel_task_queue(self, task_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE task_queue SET state='cancelled', finished_at=? WHERE task_id=? AND state='queued'",
                (_ts(utcnow()), task_id),
            )
        return cur.rowcount > 0

    def requeue_task(self, task_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE task_queue SET state='queued', enqueued_at=?, started_at=NULL, finished_at=NULL, error=NULL "
                "WHERE task_id=? AND state IN ('done','cancelled','failed')",
                (_ts(utcnow()), task_id),
            )
        return cur.rowcount > 0

    def list_task_queue(self, states: tuple[str, ...] = ('queued', 'running')) -> list[dict[str, Any]]:
        marks = ','.join('?' for _ in states)
        rows = self.conn.execute(
            f"SELECT q.*, t.title, t.status AS task_status FROM task_queue q "
            f"JOIN tasks t ON t.id=q.task_id WHERE q.state IN ({marks}) ORDER BY q.enqueued_at, q.rowid", states)
        return [dict(row) for row in rows.fetchall()]

    def queue_paused(self) -> bool:
        return self.kv_get('queue:paused') == '1'

    def set_queue_paused(self, paused: bool) -> None:
        self.kv_set('queue:paused', '1' if paused else '0')

    # ---- steps ----
    def add_step(self, step: Step) -> Step:
        step.started_at = step.started_at or utcnow()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO steps(task_id,name,status,summary,error,evidence,started_at,ended_at) VALUES(?,?,?,?,?,?,?,?)",
                (step.task_id, step.name, step.status.value, step.summary, step.error, step.evidence,
                 _ts(step.started_at), _ts(step.ended_at)),
            )
        step.id = cur.lastrowid
        return step

    def update_step(self, step: Step) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE steps SET status=?, summary=?, error=?, evidence=?, ended_at=? WHERE id=?",
                (step.status.value, step.summary, step.error, step.evidence, _ts(step.ended_at), step.id),
            )

    def list_steps(self, task_id: str) -> list[Step]:
        rows = self.conn.execute("SELECT * FROM steps WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return [
            Step(id=r["id"], task_id=r["task_id"], name=r["name"], status=StepStatus(r["status"]),
                 summary=r["summary"] or "", error=r["error"] or "", evidence=r["evidence"] or "",
                 started_at=_dt(r["started_at"]), ended_at=_dt(r["ended_at"]))
            for r in rows
        ]

    # ---- proposals ----
    def save_proposal(self, p: Proposal) -> Proposal:
        p.created_at = p.created_at or utcnow()
        with self.conn:
            self.conn.execute(
                """INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET current=excluded.current, evidence=excluded.evidence,
                consequences=excluded.consequences, status=excluded.status, approver=excluded.approver,
                decided_at=excluded.decided_at, reject_reason=excluded.reject_reason,
                reject_note=excluded.reject_note, card_message_id=excluded.card_message_id,
                reminded=excluded.reminded, executed_at=excluded.executed_at, result=excluded.result""",
                (
                    p.id, p.task_id, p.shop_key, p.action, _j(p.target), _j(p.desired), p.rationale,
                    _j(p.current), _j(p.evidence), _j(p.consequences), p.risk, p.method.value,
                    p.status.value, _ts(p.expires_at), p.approver, _ts(p.decided_at),
                    p.reject_reason.value if p.reject_reason else None, p.reject_note,
                    p.card_message_id, int(p.reminded), _ts(p.executed_at), _j(p.result),
                    _ts(p.created_at),
                ),
            )
        return p

    def _row_to_proposal(self, r: sqlite3.Row) -> Proposal:
        return Proposal(
            id=r["id"], task_id=r["task_id"], shop_key=r["shop_key"], action=r["action"],
            target=json.loads(r["target"]), desired=json.loads(r["desired"]), rationale=r["rationale"] or "",
            current=json.loads(r["current"] or "{}"), evidence=json.loads(r["evidence"] or "{}"),
            consequences=json.loads(r["consequences"] or "{}"), risk=r["risk"],
            method=ExecMethod(r["method"]), status=ProposalStatus(r["status"]),
            expires_at=_dt(r["expires_at"]), approver=r["approver"] or "", decided_at=_dt(r["decided_at"]),
            reject_reason=RejectReason(r["reject_reason"]) if r["reject_reason"] else None,
            reject_note=r["reject_note"] or "", card_message_id=r["card_message_id"] or "",
            reminded=bool(r["reminded"]), executed_at=_dt(r["executed_at"]),
            result=json.loads(r["result"] or "{}"), created_at=_dt(r["created_at"]),
        )

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        row = self.conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        return self._row_to_proposal(row) if row else None

    def find_proposal_by_card(self, message_id: str) -> Proposal | None:
        row = self.conn.execute("SELECT * FROM proposals WHERE card_message_id=?", (message_id,)).fetchone()
        return self._row_to_proposal(row) if row else None

    def list_proposals(self, status: ProposalStatus | None = None) -> list[Proposal]:
        if status:
            rows = self.conn.execute("SELECT * FROM proposals WHERE status=? ORDER BY id", (status.value,))
        else:
            rows = self.conn.execute("SELECT * FROM proposals ORDER BY id")
        return [self._row_to_proposal(r) for r in rows.fetchall()]

    # ---- executions ----
    def add_execution(self, proposal_id: str, shop_key: str, **fields: Any) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO executions(proposal_id, shop_key, snapshot, request, response, verified,
                rollback, outcome, created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    proposal_id, shop_key, _j(fields.get("snapshot")), _j(fields.get("request")),
                    _j(fields.get("response")),
                    None if fields.get("verified") is None else int(fields["verified"]),
                    _j(fields.get("rollback")), fields.get("outcome"), _ts(utcnow()),
                ),
            )
        return cur.lastrowid

    def count_writes_since(self, shop_key: str, since: datetime) -> int:
        """统计某店铺自某时间起真正发出过写请求的执行次数（用于每日写操作上限）。"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM executions WHERE shop_key=? AND created_at>=? AND request != 'null'",
            (shop_key, _ts(since)),
        ).fetchone()
        return row[0]

    def list_executions(self, proposal_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM executions WHERE proposal_id=? ORDER BY id", (proposal_id,))
        return [dict(r) for r in rows.fetchall()]

    # ---- audit ----
    def audit(self, entity: str, entity_id: str, event: str, actor: str = "", detail: Any = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO audit_log(entity, entity_id, event, actor, detail, created_at) VALUES(?,?,?,?,?,?)",
                (entity, entity_id, event, actor, _j(detail), _ts(utcnow())),
            )

    def audit_events(self, entity_id: str) -> list[str]:
        rows = self.conn.execute("SELECT event FROM audit_log WHERE entity_id=? ORDER BY id", (entity_id,))
        return [r[0] for r in rows.fetchall()]

    # ---- board outbox（看板回写队列，按记录合并） ----
    def enqueue_board(self, entity: str, entity_id: str, fields: dict[str, Any]) -> None:
        with self.conn:
            row = self.conn.execute(
                "SELECT fields FROM board_outbox WHERE entity=? AND entity_id=?", (entity, entity_id)
            ).fetchone()
            merged = {**json.loads(row["fields"]), **fields} if row else fields
            self.conn.execute(
                """INSERT INTO board_outbox(entity, entity_id, fields, attempts, next_attempt_at, updated_at)
                VALUES(?,?,?,0,NULL,?) ON CONFLICT(entity, entity_id) DO UPDATE SET
                fields=excluded.fields, updated_at=excluded.updated_at""",
                (entity, entity_id, _j(merged), _ts(utcnow())),
            )

    def due_board_items(self, now: datetime, limit: int) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM board_outbox WHERE next_attempt_at IS NULL OR next_attempt_at <= ?
            ORDER BY updated_at LIMIT ?""",
            (_ts(now), limit),
        ).fetchall()
        return [{**dict(r), "fields": json.loads(r["fields"])} for r in rows]

    def board_done(self, entity: str, entity_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM board_outbox WHERE entity=? AND entity_id=?", (entity, entity_id))

    def board_retry_later(self, entity: str, entity_id: str, next_at: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE board_outbox SET attempts = attempts + 1, next_attempt_at=? WHERE entity=? AND entity_id=?",
                (_ts(next_at), entity, entity_id),
            )

    def board_record_id(self, entity: str, entity_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT record_id FROM board_records WHERE entity=? AND entity_id=?", (entity, entity_id)
        ).fetchone()
        return row[0] if row else None

    def set_board_record_id(self, entity: str, entity_id: str, record_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO board_records(entity, entity_id, record_id) VALUES(?,?,?)",
                (entity, entity_id, record_id),
            )

    # ---- 事件去重 ----
    def mark_event_seen(self, event_id: str) -> bool:
        """返回 True 表示第一次见到；False 表示重复事件。"""
        try:
            with self.conn:
                self.conn.execute("INSERT INTO seen_events VALUES(?,?)", (event_id, _ts(utcnow())))
            return True
        except sqlite3.IntegrityError:
            return False

    # ---- kv（调度器上次运行时间等） ----
    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?,?)", (key, value))

    # ---- 汇报消息 -> 任务（在汇报 thread 里回复会并回原任务） ----
    def link_report(self, message_id: str, task_id: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO report_links VALUES(?,?)", (message_id, task_id))

    def task_for_message(self, message_id: str) -> str | None:
        row = self.conn.execute("SELECT task_id FROM report_links WHERE message_id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def task_for_origin_message(self, message_id: str) -> str | None:
        row = self.conn.execute("SELECT id FROM tasks WHERE origin_message_id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def log_message(self, message: dict[str, Any], direction: str = "inbound",
                    task_id: str | None = None) -> int:
        """保存本地对话索引；凭据不会进入消息内容，界面也不展示 sender_id。"""
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO message_log(event_id,message_id,direction,sender_id,chat_type,content,task_id,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (message.get("event_id", ""), message.get("message_id", ""), direction,
                 message.get("sender_id", ""), message.get("chat_type", ""),
                 str(message.get("log_content", message.get("content", "")))[:10000], task_id, _ts(utcnow())),
            )
        return cur.lastrowid

    def link_message_task(self, message_id: str, task_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE message_log SET task_id=? WHERE message_id=?", (task_id, message_id))

    def list_message_log(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id,event_id,message_id,direction,chat_type,content,task_id,created_at "
            "FROM message_log ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),)
        ).fetchall()
        return [dict(row) for row in rows]

    def _backfill_message_log(self) -> None:
        """给升级前已存在的任务补一条本地对话摘要，避免新面板看起来是空的。"""
        if self.conn.execute("SELECT COUNT(*) FROM message_log").fetchone()[0]:
            return

        # 检查 tasks 表是否有必要的列（兼容旧版本）
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "origin_message_id" not in columns or "conclusion" not in columns:
            return  # 旧版本还没迁移这些列，跳过回填

        tasks = self.conn.execute(
            "SELECT id,title,origin_message_id,conclusion,created_at FROM tasks ORDER BY created_at"
        ).fetchall()
        if not tasks:
            return
        with self.conn:
            for task in tasks:
                if task["origin_message_id"]:
                    self.conn.execute(
                        """INSERT INTO message_log(message_id,direction,chat_type,content,task_id,created_at)
                        VALUES(?,?,?,?,?,?)""",
                        (task["origin_message_id"], "inbound", "p2p", task["title"], task["id"], task["created_at"]),
                    )
                if task["conclusion"]:
                    report = self.conn.execute(
                        "SELECT message_id FROM report_links WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                        (task["id"],),
                    ).fetchone()
                    self.conn.execute(
                        """INSERT INTO message_log(message_id,direction,chat_type,content,task_id,created_at)
                        VALUES(?,?,?,?,?,?)""",
                        (report[0] if report else "", "outbound", "p2p", task["conclusion"], task["id"], task["created_at"]),
                    )

    # ---- 执行后跟进（上架确认、复盘） ----
    def add_followup(self, proposal_id: str, kind: str, due_at: datetime) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO followups(proposal_id, kind, due_at) VALUES(?,?,?)",
                              (proposal_id, kind, _ts(due_at)))

    def due_followups(self, now: datetime) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM followups WHERE done=0 AND due_at<=? ORDER BY id", (_ts(now),))
        return [dict(r) for r in rows.fetchall()]

    def update_followup(self, fid: int, *, due_at: datetime | None = None, done: bool = False,
                        note: str = "") -> None:
        with self.conn:
            if due_at:
                self.conn.execute("UPDATE followups SET attempts=attempts+1, due_at=?, note=? WHERE id=?",
                                  (_ts(due_at), note, fid))
            else:
                self.conn.execute("UPDATE followups SET attempts=attempts+1, done=?, note=? WHERE id=?",
                                  (int(done), note, fid))

    def list_followups(self, proposal_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM followups WHERE proposal_id=? ORDER BY id", (proposal_id,))
        return [dict(r) for r in rows.fetchall()]

    # ---- 记忆：召回记录、记忆草稿 ----
    def log_recall(self, task_id: str, query: str, hits: list[str]) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO recall_log(task_id, query, hits, created_at) VALUES(?,?,?,?)",
                              (task_id, query, _j(hits), _ts(utcnow())))

    def list_recalls(self, task_id: str | None = None) -> list[dict]:
        if task_id:
            rows = self.conn.execute("SELECT * FROM recall_log WHERE task_id=? ORDER BY id", (task_id,))
        else:
            rows = self.conn.execute("SELECT * FROM recall_log ORDER BY id")
        return [{**dict(r), "hits": json.loads(r["hits"])} for r in rows.fetchall()]

    def add_memory_draft(self, draft: dict[str, Any]) -> str:
        draft_id = self.next_id("M")
        with self.conn:
            self.conn.execute(
                """INSERT INTO memory_drafts(id, task_id, name, description, type, body, reason, status, created_at)
                VALUES(?,?,?,?,?,?,?,'待审核',?)""",
                (draft_id, draft.get("task_id"), draft["name"], draft.get("description", ""),
                 draft.get("type", "experience"), draft.get("body", ""), draft.get("reason", ""), _ts(utcnow())),
            )
        return draft_id

    def get_memory_draft(self, draft_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM memory_drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def list_memory_drafts(self, status: str = "待审核") -> list[dict]:
        rows = self.conn.execute("SELECT * FROM memory_drafts WHERE status=? ORDER BY id", (status,))
        return [dict(r) for r in rows.fetchall()]

    def decide_memory_draft(self, draft_id: str, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE memory_drafts SET status=?, decided_at=? WHERE id=?",
                              (status, _ts(utcnow()), draft_id))

    def rejected_proposals_since(self, since: datetime) -> list[Proposal]:
        rows = self.conn.execute(
            "SELECT * FROM proposals WHERE status=? AND decided_at>=? ORDER BY id",
            (ProposalStatus.REJECTED.value, _ts(since)),
        )
        return [self._row_to_proposal(r) for r in rows.fetchall()]

