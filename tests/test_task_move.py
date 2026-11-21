"""控制台看板拖拽（照 Multica）：任意卡片拖到任意一列改状态，同一列里拖动排序；排队中的拖走就取消排队；正在跑的不能拖。"""

from ecom_twin.console import ROLE_REVIEWER, ConsoleApp, allowed
from ecom_twin.memory import MemoryStore
from ecom_twin.models import TaskSource, TaskStatus


def setup(env, tmp_path, monkeypatch):
    import ecom_twin.paths as paths
    monkeypatch.setattr(paths, "WORKSPACES_DIR", tmp_path / "ws")
    return ConsoleApp(env.app.store, MemoryStore(env.app.store, tmp_path), tmp_path / "settings.toml")


def move(app, tid, to, frm="", order=None):
    return app.route("POST", f"/api/tasks/{tid}/move", {}, {"to": to, "from": frm, "order": order or []})


def test_drag_to_every_column(env, tmp_path, monkeypatch):
    app = setup(env, tmp_path, monkeypatch)
    task = env.app.tasks.create("分析美国市场", TaskSource.CHAT, "选品")
    for to, status in [("waiting", TaskStatus.WAITING), ("failed", TaskStatus.FAILED), ("closed", TaskStatus.CLOSED),
                       ("pending", TaskStatus.PENDING)]:
        code, r = move(app, task.id, to)
        assert code == 200 and env.app.store.get_task(task.id).status == status, to
    code, r = move(app, task.id, "running")
    q = env.app.store.get_task_queue(task.id)
    assert code == 200 and q["state"] == "queued" and q["resume"] and "没在运行" in r["note"]
    # 排队中（还没开跑）的也能拖：拖走就取消这次排队
    code, r = move(app, task.id, "closed", "pending")
    assert code == 200 and "排队已取消" in r["note"] and env.app.store.get_task_queue(task.id)["state"] == "cancelled"
    # 分身正在跑的不能拖到别的列
    env.app.tasks.set_status(task.id, TaskStatus.RUNNING)
    code, r = move(app, task.id, "closed", "running")
    assert code == 400 and "正在跑" in r["error"]
    env.app.tasks.set_status(task.id, TaskStatus.WAITING)
    assert move(app, task.id, "nowhere")[0] == 400
    assert move(app, "T-9999", "closed")[0] == 404
    actions = {a["event"] for a in env.app.store.audit_log(("task",))}
    assert {"moved:waiting", "moved:failed", "moved:closed", "moved:pending", "moved:running"} <= actions
    assert not allowed(ROLE_REVIEWER, "POST", f"/api/tasks/{task.id}/move")


def test_reorder_within_column_is_saved(env, tmp_path, monkeypatch):
    app = setup(env, tmp_path, monkeypatch)
    a, b, c = (env.app.tasks.create(n, TaskSource.CHAT, "选品") for n in "abc")
    for t in (a, b, c):
        env.app.tasks.set_status(t.id, TaskStatus.WAITING)
    code, r = move(app, c.id, "waiting", "waiting", [c.id, a.id, b.id])           # 同一列：只排序，不改状态
    assert code == 200 and r["note"] == "已调整顺序" and env.app.store.get_task(c.id).status == TaskStatus.WAITING
    ranks = {t["id"]: t["rank"] for t in app.route("GET", "/api/tasks", {}, {})[1]["tasks"]}
    assert ranks[c.id] < ranks[a.id] < ranks[b.id]
    env.app.tasks.set_status(b.id, TaskStatus.RUNNING)
    assert move(app, b.id, "waiting", "running", [b.id])[0] == 400               # 跑着的换列不行
    code, _ = move(app, b.id, "running", "running", [b.id])                         # 跑着的在本列排序可以
    assert code == 200
