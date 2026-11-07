"""本地状态存储（SQLite）。

本地数据库是任务状态的真实来源；飞书多维表格只是展示和协作视图（DESIGN.md 11.5）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
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
    origin_chat_id TEXT,
    origin_chat_type TEXT,
    transcript TEXT,
    waiting TEXT
);
CREATE TABLE IF NOT EXISTS run_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT, scene TEXT, shop_key TEXT, runtime TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, created_at TEXT
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
    executed_at TEXT, result TEXT, created_at TEXT, approvals TEXT
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
    created_at TEXT,
    command TEXT
);
CREATE TABLE IF NOT EXISTS staging_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    shop_key TEXT,
    change_type TEXT NOT NULL,
    before TEXT,
    after TEXT,
    proposal_id TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS staging_links (
    common_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    PRIMARY KEY (common_id, platform)
);
CREATE TABLE IF NOT EXISTS intake_buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT, chat_type TEXT, sender_id TEXT, message_id TEXT, text TEXT, received_at TEXT,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS task_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL, message_id TEXT, chat_id TEXT, sender_id TEXT, text TEXT,
    layer TEXT, drive INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS merge_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT, message_ids TEXT, task_id TEXT, layer TEXT, drive INTEGER, reason TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS outbound_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT, chat_id TEXT, reply_to TEXT, kind TEXT, delivered_to TEXT, text TEXT, message_id TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS shadow_actuals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL, shop_key TEXT, object_key TEXT NOT NULL, actual TEXT, at TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS metrics_daily (
    day TEXT NOT NULL, shop_key TEXT NOT NULL, sku TEXT NOT NULL, metrics TEXT,
    PRIMARY KEY (day, shop_key, sku)
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
    decided_at TEXT,
    mode TEXT,
    target_name TEXT,
    target_section TEXT,
    old_text TEXT,
    why TEXT,
    evidence TEXT,
    final TEXT,
    note TEXT
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



def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """{"title": .., "sku_prices_cny": {"红色": 12}} → {"title": .., "sku_prices_cny.红色": 12}，逐字段对比用。"""
    if not isinstance(value, dict):
        return {prefix: value} if prefix else {}
    out: dict[str, Any] = {}
    for key, inner in value.items():
        out.update(_flatten(inner, f"{prefix}.{key}" if prefix else str(key)))
    return out

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
        # 逐条建表建索引：旧表缺列时建索引会失败，先跳过，补完列再建
        deferred = []
        for statement in (st.strip() for st in SCHEMA.split(";")):
            if not statement:
                continue
            try:
                self.conn.execute(statement)
            except sqlite3.OperationalError:
                deferred.append(statement)
        self._migrate()
        for statement in deferred:
            self.conn.execute(statement)
        self.conn.commit()
        self.conn.commit()

    def _migrate(self) -> None:
        """旧版本数据库升级：对照当前 SCHEMA，给已有表补上缺的列（CREATE TABLE IF NOT EXISTS 不会补列）。

        参照表结构取自一个用 SCHEMA 新建的内存库，所以以后加列只要改 SCHEMA，不用另外登记。
        补的列去掉 NOT NULL / 主键约束（ALTER TABLE ADD COLUMN 不支持），保留默认值。
        """
        ref = sqlite3.connect(":memory:")
        ref.executescript(SCHEMA)
        tables = [r[0] for r in ref.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                  if not r[0].startswith("sqlite_")]
        for table in tables:
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for _, name, col_type, _, default, _ in ref.execute(f"PRAGMA table_info({table})").fetchall():
                if name not in existing:
                    ddl = f"ALTER TABLE {table} ADD COLUMN {name} {col_type or ''}"
                    if default is not None:
                        ddl += f" DEFAULT {default}"
                    with self.conn:
                        self.conn.execute(ddl)
        ref.close()

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
    def _upsert(self, table: str, row: dict[str, Any], keep_on_update: tuple[str, ...] = ("id",)) -> None:
        """按列名写入（不依赖列顺序：旧库补列后列顺序可能和 SCHEMA 不同）。"""
        cols = list(row)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in keep_on_update)
        with self.conn:
            self.conn.execute(
                f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}", tuple(row.values()))

    def save_task(self, task: Task) -> Task:
        now = utcnow()
        task.created_at = task.created_at or now
        task.updated_at = now
        self._upsert("tasks", {
            "id": task.id, "title": task.title, "source": task.source.value, "scene": task.scene,
            "status": task.status.value, "shop_key": task.shop_key, "anchors": _j(task.anchors), "owner": task.owner,
            "current_step": task.current_step, "runtime": task.runtime, "conclusion": task.conclusion,
            "blocker": task.blocker, "created_at": _ts(task.created_at), "updated_at": _ts(task.updated_at),
            "last_heartbeat": _ts(task.last_heartbeat), "tokens_in": task.tokens_in, "tokens_out": task.tokens_out,
            "session_id": task.session_id, "switch_log": task.switch_log,
            "origin_message_id": task.origin_message_id, "origin_chat_id": task.origin_chat_id,
            "origin_chat_type": task.origin_chat_type, "transcript": task.transcript,
            "waiting": task.waiting,
        }, keep_on_update=("id", "source", "created_at"))
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
            origin_chat_id=row["origin_chat_id"] or "", origin_chat_type=row["origin_chat_type"] or "",
            transcript=row["transcript"] or "", waiting=row["waiting"] or "",
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

    def get_task_queue(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM task_queue WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def finish_task_queue(self, task_id: str, error: str = "") -> None:
        # 只结束仍在运行的这一轮：运行期间又来了新消息时，入队已把它改回 queued，不能被这里覆盖成 done
        with self.conn:
            self.conn.execute("UPDATE task_queue SET state='done', finished_at=?, error=? "
                              "WHERE task_id=? AND state='running'", (_ts(utcnow()), error[:500], task_id))

    def recover_interrupted_queue(self) -> list[dict[str, Any]]:
        """服务启动时：上次停机前还在跑（state=running）的那几轮重新排队，返回它们。只在单个 serve 启动时调用。"""
        with self.conn:
            rows = [dict(r) for r in self.conn.execute("SELECT * FROM task_queue WHERE state='running'").fetchall()]
            self.conn.execute("UPDATE task_queue SET state='queued', started_at=NULL, "
                              "error='服务重启时这一轮被中断，已重新排队' WHERE state='running'")
        return rows

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
        self._upsert("proposals", {
            "id": p.id, "task_id": p.task_id, "shop_key": p.shop_key, "action": p.action, "target": _j(p.target),
            "desired": _j(p.desired), "rationale": p.rationale, "current": _j(p.current), "evidence": _j(p.evidence),
            "consequences": _j(p.consequences), "risk": p.risk, "method": p.method.value, "status": p.status.value,
            "expires_at": _ts(p.expires_at), "approver": p.approver, "decided_at": _ts(p.decided_at),
            "reject_reason": p.reject_reason.value if p.reject_reason else None, "reject_note": p.reject_note,
            "card_message_id": p.card_message_id, "reminded": int(p.reminded), "executed_at": _ts(p.executed_at),
            "result": _j(p.result), "created_at": _ts(p.created_at), "approvals": _j(p.approvals),
        }, keep_on_update=("id", "task_id", "shop_key", "action", "rationale", "expires_at", "created_at"))
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
            approvals=json.loads(r["approvals"] or "[]"),
        )

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        row = self.conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        return self._row_to_proposal(row) if row else None

    def proposals_for_task(self, task_id: str) -> list[Proposal]:
        rows = self.conn.execute("SELECT * FROM proposals WHERE task_id=? ORDER BY id", (task_id,))
        return [self._row_to_proposal(r) for r in rows.fetchall()]

    def proposals_by_card(self, message_id: str) -> list[Proposal]:
        rows = self.conn.execute("SELECT * FROM proposals WHERE card_message_id=? ORDER BY id", (message_id,))
        return [self._row_to_proposal(r) for r in rows.fetchall()]

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

    def audit_log(self, entities: tuple[str, ...], limit: int = 200) -> list[dict]:
        """控制台「变更记录」：按实体类型取最近的审计记录，新的在前。"""
        marks = ",".join("?" * len(entities))
        rows = self.conn.execute(
            f"SELECT * FROM audit_log WHERE entity IN ({marks}) ORDER BY id DESC LIMIT ?", (*entities, limit))
        return [{**dict(r), "detail": json.loads(r["detail"] or "null")} for r in rows.fetchall()]

    def audits_by_event(self, event: str, limit: int = 100) -> list[dict]:
        """某一类审计记录，新的在前（控制台「存疑」列表用）。"""
        rows = self.conn.execute("SELECT * FROM audit_log WHERE event=? ORDER BY id DESC LIMIT ?", (event, limit))
        return [dict(r) for r in rows.fetchall()]

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

    # 进任务的方式是确定的（随任务一起创建、回复、锚点），这样的消息被人接话时可以直接落到这个任务。
    # 模型推断并进去的（等待唤醒、主题、同类）不算：推断错一次，拿它当落点就会一路错并下去（数字分身 WS-405）
    HARD_MERGE_LAYERS = ("新任务", "回复", "锚点")

    def task_for_reply(self, message_id: str) -> tuple[str, str] | None:
        """被回复的这条消息属于哪个任务、凭什么：分身发的 / 任务的触发消息 / 确定方式进任务的消息。"""
        row = self.conn.execute("SELECT task_id FROM report_links WHERE message_id=?", (message_id,)).fetchone()
        if row:
            return row[0], "分身发出的消息"
        row = self.conn.execute("SELECT id FROM tasks WHERE origin_message_id=? ORDER BY created_at DESC",
                                (message_id,)).fetchone()
        if row:
            return row[0], "任务的触发消息"
        marks = ",".join("?" * len(self.HARD_MERGE_LAYERS))
        row = self.conn.execute(f"SELECT task_id FROM task_messages WHERE message_id=? AND layer IN ({marks}) "
                                "ORDER BY id DESC", (message_id, *self.HARD_MERGE_LAYERS)).fetchone()
        if row:
            return row[0], "已经在任务里的消息"
        return None

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
    def log_recall(self, task_id: str, query: str, hits: list[str], command: str = "search") -> None:
        """command：read（按名字读全文，query 是名字）/ search（兜底检索，query 是检索词）。"""
        with self.conn:
            self.conn.execute("INSERT INTO recall_log(task_id, query, hits, created_at, command) VALUES(?,?,?,?,?)",
                              (task_id, query, _j(hits), _ts(utcnow()), command))

    def recall_summary(self, limit: int = 100) -> list[dict]:
        """按任务汇总召回：调用几次（读 / 检索）、未命中几次、最近一次。对照数字分身的「按轮次」。"""
        rows = self.conn.execute(
            "SELECT r.task_id, COUNT(*) AS calls, SUM(r.command='read') AS reads, SUM(r.command!='read') AS searches, "
            "SUM(r.hits='[]' OR r.hits IS NULL) AS misses, MAX(r.created_at) AS last_at, t.title "
            "FROM recall_log r LEFT JOIN tasks t ON t.id=r.task_id GROUP BY r.task_id ORDER BY last_at DESC LIMIT ?",
            (limit,))
        return [dict(r) for r in rows.fetchall()]

    def prune_recalls(self, days: float, now: datetime | None = None) -> int:
        """删掉超过保留期的召回记录，返回删了几条。days <= 0 不删。"""
        if days <= 0:
            return 0
        cutoff = _ts((now or utcnow()) - timedelta(days=days))
        with self.conn:
            return self.conn.execute("DELETE FROM recall_log WHERE created_at < ?", (cutoff,)).rowcount

    def list_recalls(self, task_id: str | None = None) -> list[dict]:
        if task_id:
            rows = self.conn.execute("SELECT * FROM recall_log WHERE task_id=? ORDER BY id", (task_id,))
        else:
            rows = self.conn.execute("SELECT * FROM recall_log ORDER BY id")
        return [{**dict(r), "hits": json.loads(r["hits"])} for r in rows.fetchall()]

    _DRAFT_FIELDS = ("mode", "target_name", "target_section", "old_text", "body")

    def add_memory_draft(self, draft: dict[str, Any]) -> str:
        draft_id = self.next_id("M")
        with self.conn:
            self.conn.execute(
                """INSERT INTO memory_drafts(id, task_id, name, description, type, body, reason, status, created_at,
                    mode, target_name, target_section, old_text, why, evidence)
                VALUES(?,?,?,?,?,?,?,'待审核',?,?,?,?,?,?,?)""",
                (draft_id, draft.get("task_id"), draft["name"], draft.get("description", ""),
                 draft.get("type", "experience"), draft.get("body", ""), draft.get("reason", ""), _ts(utcnow()),
                 draft.get("mode", "create"), draft.get("target_name", ""), draft.get("target_section", ""),
                 draft.get("old_text", ""), draft.get("why", draft.get("reason", "")), draft.get("evidence", "")),
            )
        return draft_id

    def find_memory_draft(self, draft: dict[str, Any]) -> str | None:
        """同一目标的相同草稿还在待审核：返回它的编号（重试的 agent 不会堆出重复草稿）。"""
        row = self.conn.execute(
            "SELECT id FROM memory_drafts WHERE status='待审核' AND COALESCE(name,'')=? AND "
            + " AND ".join(f"COALESCE({k},'')=?" for k in self._DRAFT_FIELDS),
            (draft.get("name", ""), *(draft.get(k, "") for k in self._DRAFT_FIELDS))).fetchone()
        return row["id"] if row else None

    def get_memory_draft(self, draft_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM memory_drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def list_memory_drafts(self, status: str = "待审核") -> list[dict]:
        rows = self.conn.execute("SELECT * FROM memory_drafts WHERE status=? ORDER BY id", (status,))
        return [dict(r) for r in rows.fetchall()]

    def draft_stats(self) -> dict[str, Any]:
        """草稿质量：待审、已采纳（其中改过再采纳的）、已驳回。改动率高说明 agent 写的草稿不合用，该调它的提示词。"""
        row = self.conn.execute(
            "SELECT SUM(status='待审核') AS pending, SUM(status='已采纳') AS adopted, "
            "SUM(status='已采纳' AND final IS NOT NULL) AS edited, SUM(status='已驳回') AS rejected FROM memory_drafts"
        ).fetchone()
        stats = {k: int(row[k] or 0) for k in ("pending", "adopted", "edited", "rejected")}
        decided = stats["adopted"] + stats["rejected"]
        stats["edit_rate"] = round(stats["edited"] / stats["adopted"], 3) if stats["adopted"] else None
        stats["reject_rate"] = round(stats["rejected"] / decided, 3) if decided else None
        return stats

    def decide_memory_draft(self, draft_id: str, status: str, final: dict | None = None, note: str = "") -> None:
        """final：采纳前人工改过的字段（原稿不覆盖，另存一份）；note：驳回原因。"""
        with self.conn:
            self.conn.execute("UPDATE memory_drafts SET status=?, decided_at=?, final=?, note=? WHERE id=?",
                              (status, _ts(utcnow()), _j(final) if final else None, note, draft_id))

    def rejected_proposals_since(self, since: datetime) -> list[Proposal]:
        rows = self.conn.execute(
            "SELECT * FROM proposals WHERE status=? AND decided_at>=? ORDER BY id",
            (ProposalStatus.REJECTED.value, _ts(since)),
        )
        return [self._row_to_proposal(r) for r in rows.fetchall()]

    # ---- 采集箱暂存区改动（发布时汇总进审批卡片） ----
    def add_staging_change(self, task_id: str, object_type: str, object_id: str, change_type: str,
                           before: Any = None, after: Any = None, shop_key: str = "") -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO staging_changes(task_id, object_type, object_id, shop_key, change_type, before, after,
                created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (task_id, object_type, str(object_id), shop_key, change_type, _j(before), _j(after), _ts(utcnow())),
            )

    def staging_changes_for(self, object_type: str, object_ids: list[str]) -> list[dict]:
        if not object_ids:
            return []
        marks = ",".join("?" * len(object_ids))
        rows = self.conn.execute(
            f"SELECT * FROM staging_changes WHERE object_type=? AND object_id IN ({marks}) ORDER BY id",
            (object_type, *[str(o) for o in object_ids]))
        return [{**dict(r), "before": json.loads(r["before"] or "null"), "after": json.loads(r["after"] or "null")}
                for r in rows.fetchall()]

    def listing_diffs(self, task_id: str | None = None, limit: int = 100) -> list[dict]:
        """控制台「Listing 对比」：按商品汇总暂存区改动 —— 每个字段第一次改之前的值 → 最后改成的值，外加改动时间线。"""
        sql, args = "SELECT * FROM staging_changes", []
        if task_id:
            sql += " WHERE object_id IN (SELECT object_id FROM staging_changes WHERE task_id=?)"
            args.append(task_id)
        rows = [{**dict(r), "before": json.loads(r["before"] or "null"), "after": json.loads(r["after"] or "null")}
                for r in self.conn.execute(sql + " ORDER BY id", args).fetchall()]
        items: dict[tuple, dict] = {}
        for r in rows:
            item = items.setdefault((r["object_type"], r["object_id"]), {
                "object_type": r["object_type"], "object_id": r["object_id"], "shop_key": r["shop_key"] or "",
                "tasks": [], "proposals": [], "fields": {}, "changes": [], "updated_at": r["created_at"]})
            item["shop_key"] = item["shop_key"] or r["shop_key"] or ""
            for key, value in (("tasks", r["task_id"]), ("proposals", r["proposal_id"])):
                if value and value not in item[key]:
                    item[key].append(value)
            item["changes"].append({"at": r["created_at"], "type": r["change_type"], "task_id": r["task_id"],
                                    "proposal_id": r["proposal_id"], "before": r["before"], "after": r["after"]})
            item["updated_at"] = r["created_at"]
            if r["before"] is None:  # 采集、认领、绑店只有结果没有「改前」，只进时间线，不算字段改动
                continue
            before, after = _flatten(r["before"]), _flatten(r["after"])
            for field in after.keys() | before.keys():
                slot = item["fields"].setdefault(field, {"field": field, "before": before.get(field), "after": None})
                if field in after:
                    slot["after"] = after[field]
        out = sorted(items.values(), key=lambda i: i["updated_at"], reverse=True)[:limit]
        for item in out:  # 只留真改过的字段（绕一圈改回原样的不算）
            item["fields"] = [f for f in item["fields"].values() if f["after"] is not None and f["after"] != f["before"]]
        return out

    def mark_staging_included(self, ids: list[int], proposal_id: str) -> None:
        with self.conn:
            for i in ids:
                self.conn.execute("UPDATE staging_changes SET proposal_id=? WHERE id=?", (proposal_id, i))

    def count_staging_writes(self, task_id: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM staging_changes WHERE task_id=?", (task_id,)).fetchone()[0]

    def link_staging(self, common_id: str, platform: str, platform_id: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO staging_links VALUES(?,?,?)",
                              (str(common_id), platform, str(platform_id)))

    def common_ids_for(self, platform: str, platform_ids: list[str]) -> list[str]:
        if not platform_ids:
            return []
        marks = ",".join("?" * len(platform_ids))
        rows = self.conn.execute(
            f"SELECT common_id FROM staging_links WHERE platform=? AND platform_id IN ({marks})",
            (platform, *[str(p) for p in platform_ids]))
        return [r[0] for r in rows.fetchall()]

    # ---- 消息入口：攒批缓冲 ----
    # 消息里除正文外还要带到处理阶段的字段：事件自带的 parent_id、图片附件
    _BUFFER_EXTRAS = ("parent_id", "root_id", "image_key", "image_keys", "prompt_content")

    def buffer_add(self, msg: dict[str, Any], received_at: datetime) -> None:
        extras = {k: msg[k] for k in self._BUFFER_EXTRAS if msg.get(k)}
        with self.conn:
            self.conn.execute(
                "INSERT INTO intake_buffer(chat_id, chat_type, sender_id, message_id, text, received_at, payload) "
                "VALUES(?,?,?,?,?,?,?)",
                (msg.get("chat_id", ""), msg.get("chat_type", ""), msg.get("sender_id", ""), msg.get("message_id", ""),
                 msg.get("content", ""), _ts(received_at), json.dumps(extras, ensure_ascii=False) if extras else None))

    def buffer_groups(self) -> list[list[dict]]:
        """按（会话, 发送人）分组的待处理消息，组内按收到顺序。"""
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM intake_buffer ORDER BY id").fetchall()]
        groups: dict[tuple, list[dict]] = {}
        for r in rows:
            r.update(json.loads(r.pop("payload") or "{}"))
            groups.setdefault((r["chat_id"], r["sender_id"]), []).append(r)
        return list(groups.values())

    def buffer_delete(self, ids: list[int]) -> None:
        with self.conn:
            for i in ids:
                self.conn.execute("DELETE FROM intake_buffer WHERE id=?", (i,))

    # ---- 任务消息、合并判定、代发记录 ----
    def add_task_message(self, task_id: str, msg: dict[str, Any], layer: str, drive: bool) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO task_messages(task_id, message_id, chat_id, sender_id, text, layer, drive, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, msg.get("message_id"), msg.get("chat_id"), msg.get("sender_id"), msg.get("text"), layer,
                 int(drive), _ts(utcnow())))

    def task_messages(self, task_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM task_messages WHERE task_id=? ORDER BY id", (task_id,))
        return [dict(r) for r in rows.fetchall()]

    def add_merge_decision(self, chat_id: str, message_ids: list[str], task_id: str | None, layer: str,
                           drive: bool, reason: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO merge_decisions(chat_id, message_ids, task_id, layer, drive, reason, created_at) VALUES(?,?,?,?,?,?,?)",
                (chat_id, _j(message_ids), task_id, layer, int(drive), reason, _ts(utcnow())))

    def merge_decisions(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM merge_decisions ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "message_ids": json.loads(r["message_ids"] or "[]")} for r in rows.fetchall()]

    def add_outbound(self, task_id: str, chat_id: str, reply_to: str, kind: str, delivered_to: str, text: str,
                     message_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO outbound_messages(task_id, chat_id, reply_to, kind, delivered_to, text, message_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, chat_id, reply_to, kind, delivered_to, text, message_id, _ts(utcnow())))

    def outbound_messages(self, task_id: str | None = None) -> list[dict]:
        if task_id:
            rows = self.conn.execute("SELECT * FROM outbound_messages WHERE task_id=? ORDER BY id", (task_id,))
        else:
            rows = self.conn.execute("SELECT * FROM outbound_messages ORDER BY id DESC LIMIT 500")
        return [dict(r) for r in rows.fetchall()]

    def list_tasks(self, statuses_excluded: list[str] | None = None, chat_id: str | None = None,
                   since: datetime | None = None) -> list[Task]:
        sql, args = "SELECT id FROM tasks WHERE 1=1", []
        if statuses_excluded:
            sql += f" AND status NOT IN ({','.join('?' * len(statuses_excluded))})"
            args += statuses_excluded
        if chat_id:
            sql += " AND origin_chat_id=?"
            args.append(chat_id)
        if since:
            sql += " AND updated_at>=?"
            args.append(_ts(since))
        rows = self.conn.execute(sql + " ORDER BY updated_at DESC", args).fetchall()
        return [self.get_task(r[0]) for r in rows]

    # ---- 成本 ----
    def add_usage(self, task: Task, runtime: str, input_tokens: int, output_tokens: int,
                  cost_usd: float | None, at: datetime | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO run_usage(task_id, scene, shop_key, runtime, input_tokens, output_tokens, cost_usd, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (task.id, task.scene, task.shop_key, runtime, input_tokens, output_tokens, cost_usd,
                 _ts(at or utcnow())))

    def usage_summary(self, month: str, by: str = "runtime") -> list[dict]:
        """某月（YYYY-MM）按 runtime / scene / shop_key 汇总 token 和费用。"""
        assert by in ("runtime", "scene", "shop_key")
        rows = self.conn.execute(
            f"""SELECT COALESCE({by}, '') AS key, COUNT(DISTINCT task_id) AS tasks, SUM(input_tokens) AS input_tokens,
            SUM(output_tokens) AS output_tokens, SUM(COALESCE(cost_usd, 0)) AS cost_usd,
            SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS runs_without_cost
            FROM run_usage WHERE substr(created_at, 1, 7)=? GROUP BY {by} ORDER BY cost_usd DESC""", (month,))
        return [dict(r) for r in rows.fetchall()]

    # ---- 看板：增量导出与反向同步 ----
    def rows_after(self, table: str, last_id: int, limit: int = 200) -> list[dict]:
        assert table in ("task_messages", "executions", "outbound_messages", "recall_log", "followups")
        rows = self.conn.execute(f"SELECT * FROM {table} WHERE id>? ORDER BY id LIMIT ?", (last_id, limit))
        return [dict(r) for r in rows.fetchall()]

    def board_entity_for_record(self, record_id: str) -> tuple[str, str] | None:
        row = self.conn.execute("SELECT entity, entity_id FROM board_records WHERE record_id=?", (record_id,)).fetchone()
        return (row[0], row[1]) if row else None

    # ---- 影子模式：人工实际操作 ----
    def add_shadow_actual(self, action: str, shop_key: str, object_key: str, actual: str, at: datetime,
                          source: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO shadow_actuals(action, shop_key, object_key, actual, at, source) VALUES(?,?,?,?,?,?)",
                (action, shop_key, str(object_key), actual, _ts(at), source))

    def shadow_actuals(self, action: str, shop_key: str, object_key: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM shadow_actuals WHERE action=? AND shop_key=? AND object_key=? ORDER BY at",
            (action, shop_key, str(object_key)))
        return [dict(r) for r in rows.fetchall()]

    # ---- 日报数据 ----
    def upsert_metrics(self, day: str, shop_key: str, sku: str, metrics: dict[str, float]) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO metrics_daily VALUES(?,?,?,?)", (day, shop_key, sku, _j(metrics)))

    def metrics_for(self, day: str, shop_key: str) -> list[dict]:
        rows = self.conn.execute("SELECT sku, metrics FROM metrics_daily WHERE day=? AND shop_key=? ORDER BY sku",
                                 (day, shop_key))
        return [{"sku": r[0], "metrics": json.loads(r[1])} for r in rows.fetchall()]

    def metric_shops(self, day: str) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT shop_key FROM metrics_daily WHERE day=? ORDER BY shop_key", (day,))
        return [r[0] for r in rows.fetchall()]

    def metric_value(self, day: str, shop_key: str, sku: str, key: str) -> float:
        row = self.conn.execute("SELECT metrics FROM metrics_daily WHERE day=? AND shop_key=? AND sku=?",
                                (day, shop_key, sku)).fetchone()
        return float(json.loads(row[0]).get(key, 0)) if row else 0.0

