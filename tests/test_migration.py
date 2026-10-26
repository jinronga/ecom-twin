"""测试数据库自动迁移功能（功能8）。"""

import sqlite3
import tempfile
from pathlib import Path

from ecom_twin.models import Step, StepStatus, Task, TaskSource, TaskStatus
from ecom_twin.store import Store


def test_migrate_old_tasks_table():
    """旧版 tasks 表缺少新字段时，能自动补齐。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    try:
        # 创建旧版 tasks 表（缺少 origin_chat_id）
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE tasks (
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
                origin_message_id TEXT
            )
        """)
        conn.commit()

        # 插入一条旧数据（使用中文枚举值）
        conn.execute("""
            INSERT INTO tasks (id, title, source, scene, status)
            VALUES ('T-0001', '测试任务', '本人私聊', 'test', '待处理')
        """)
        conn.commit()
        conn.close()

        # 用 Store 打开，应该自动迁移
        store = Store(db_path)

        # 验证新字段已添加
        columns = {r[1] for r in store.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "origin_chat_id" in columns, "origin_chat_id 列应该被自动添加"

        # 验证旧数据仍然可读
        task = store.get_task("T-0001")
        assert task is not None
        assert task.title == "测试任务"
        assert task.origin_chat_id == ""  # 新字段默认值

        # 验证可以保存带新字段的数据
        task.origin_chat_id = "oc_123456"
        store.save_task(task)

        task2 = store.get_task("T-0001")
        assert task2.origin_chat_id == "oc_123456"

    finally:
        db_path.unlink(missing_ok=True)


def test_migrate_old_steps_table():
    """旧版 steps 表缺少 evidence 字段时，能自动补齐。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    try:
        # 创建旧版结构
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source TEXT NOT NULL,
                scene TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT,
                error TEXT,
                started_at TEXT,
                ended_at TEXT
            )
        """)
        conn.execute("INSERT INTO tasks VALUES ('T-0001', '测试', '本人私聊', 'test', '待处理')")
        conn.execute("INSERT INTO steps (task_id, name, status) VALUES ('T-0001', '步骤1', '成功')")
        conn.commit()
        conn.close()

        # 用 Store 打开，应该自动迁移
        store = Store(db_path)

        # 验证新字段已添加
        columns = {r[1] for r in store.conn.execute("PRAGMA table_info(steps)").fetchall()}
        assert "evidence" in columns, "evidence 列应该被自动添加"

        # 验证旧数据仍然可读
        steps = store.list_steps("T-0001")
        assert len(steps) == 1
        assert steps[0].name == "步骤1"
        assert steps[0].evidence == ""  # 新字段默认值

        # 验证可以保存带证据的步骤
        step2 = Step(
            id=None,
            task_id="T-0001",
            name="步骤2",
            status=StepStatus.SUCCESS,
            evidence="/path/to/screenshot.png"
        )
        store.add_step(step2)

        steps = store.list_steps("T-0001")
        assert len(steps) == 2
        assert steps[1].evidence == "/path/to/screenshot.png"

    finally:
        db_path.unlink(missing_ok=True)


def test_migrate_multiple_columns():
    """测试同时补齐多个缺失列。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    try:
        # 创建非常旧的版本（缺少多个列）
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE tasks (
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
                last_heartbeat TEXT
            )
        """)
        conn.commit()
        conn.close()

        # 用 Store 打开
        store = Store(db_path)

        # 验证所有新字段都被添加
        columns = {r[1] for r in store.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "tokens_in" in columns
        assert "tokens_out" in columns
        assert "session_id" in columns
        assert "switch_log" in columns
        assert "origin_message_id" in columns
        assert "origin_chat_id" in columns

        # 验证可以正常使用
        task = Task(
            id="T-0001",
            title="新任务",
            source=TaskSource.SELF_DM,
            scene="test",
            status=TaskStatus.PENDING,
            tokens_in=1000,
            tokens_out=2000,
            session_id="sess_123",
            origin_message_id="om_456",
            origin_chat_id="oc_789",
        )
        store.save_task(task)

        loaded = store.get_task("T-0001")
        assert loaded.tokens_in == 1000
        assert loaded.tokens_out == 2000
        assert loaded.session_id == "sess_123"
        assert loaded.origin_message_id == "om_456"
        assert loaded.origin_chat_id == "oc_789"

    finally:
        db_path.unlink(missing_ok=True)


def test_migrate_is_idempotent():
    """多次迁移应该是幂等的（不会重复添加列）。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    try:
        # 创建旧版数据库
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source TEXT NOT NULL,
                scene TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        # 第一次迁移
        store1 = Store(db_path)
        columns1 = {r[1] for r in store1.conn.execute("PRAGMA table_info(tasks)").fetchall()}

        # 第二次打开（再次迁移）
        store2 = Store(db_path)
        columns2 = {r[1] for r in store2.conn.execute("PRAGMA table_info(tasks)").fetchall()}

        # 列数应该相同
        assert columns1 == columns2

    finally:
        db_path.unlink(missing_ok=True)
