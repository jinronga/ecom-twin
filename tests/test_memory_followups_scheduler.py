from datetime import timedelta

from conftest import NOW
from ecom_twin.adapters.base import AdapterError, ErrorCategory
from ecom_twin.memory import MemoryStore, render_memory
from ecom_twin.scheduler import Job, Scheduler, health_check_job


def memory_store(env, tmp_path):
    (tmp_path / "ads.md").write_text(render_memory("广告健康线", "某类目 ACOS 30% 以内算健康", "experience",
                                                   "宠物类目 ACOS 30% 以内算健康，超过 40% 才考虑降价"))
    (tmp_path / "promo.md").write_text(render_memory("大促日历", "各站点大促节点", "calendar", "斋月、黑五"))
    return MemoryStore(env.app.store, tmp_path)


def test_index_only_contains_names_and_descriptions(env, tmp_path):
    index = memory_store(env, tmp_path).index_text()
    assert "广告健康线" in index and "超过 40%" not in index


def test_recall_ranks_by_keywords_and_logs(env, tmp_path):
    mem = memory_store(env, tmp_path)
    hits = mem.recall(["ACOS", "广告"], task_id="T-0001")
    assert [m.name for m in hits] == ["广告健康线"]
    assert env.app.store.list_recalls("T-0001")[0]["hits"] == ["广告健康线"]
    assert mem.recall(["不存在的词"]) == []


def test_drafts_need_human_approval(env, tmp_path):
    mem = memory_store(env, tmp_path)
    draft_id = mem.propose("测品经验", "测品款先小量上架", "先上 2 款看 7 天数据", "任务中发现缺失", task_id="T-0001")
    assert len(mem.all()) == 2  # 草稿不会直接生效
    path = mem.approve(draft_id)
    assert path.exists() and len(mem.all()) == 3
    assert env.app.store.get_memory_draft(draft_id)["status"] == "已采纳"


def test_rejection_digest_turns_reasons_into_draft(env, tmp_path):
    mem = memory_store(env, tmp_path)
    assert mem.rejection_digest(now=NOW) is None
    p = env.app.approvals.request(env.submit())
    env.app.approvals.handle_message({"message_id": "om_x", "sender_id": "ou_boss",
                                      "content": f"拒绝 {p.id} 2 类目选错了"}, now=NOW)
    draft = env.app.store.get_memory_draft(mem.rejection_digest(now=NOW + timedelta(minutes=1)))
    assert "判断不对：1 次" in draft["body"] and "类目选错了" in draft["body"]


def approve_and_execute(env):
    p = env.app.approvals.request(env.submit())
    env.app.approvals.handle_message({"message_id": "om_a", "sender_id": "ou_boss", "content": f"批准 {p.id}"},
                                     now=NOW)
    return p


def test_execution_schedules_confirm_and_reviews(env):
    p = approve_and_execute(env)
    kinds = [f["kind"] for f in env.app.store.list_followups(p.id)]
    assert kinds == ["confirm_publish", "review_3d", "review_7d"]


def test_publish_confirmed_by_readback(env):
    p = approve_and_execute(env)
    env.miaoshou.published_list = [{"collectBoxDetailId": "901"}, {"collectBoxDetailId": "902"}]
    env.app.followups.run_due(NOW + timedelta(minutes=3))
    assert env.app.store.get_proposal(p.id).result["回读确认"] == "全部已发布"
    assert "回读确认" in env.feishu.sent[-1]["text"]


def test_publish_confirmed_by_miaoshou_detail_list_wrapper(env):
    """妙手真实接口把已发布列表放在 detailList，而不是裸数组。"""
    p = approve_and_execute(env)
    env.miaoshou.published_list = {"detailList": [
        {"collectBoxDetailId": "901"}, {"collectBoxDetailId": "902"}
    ]}
    env.app.followups.run_due(NOW + timedelta(minutes=3))
    assert env.app.store.get_proposal(p.id).result["回读确认"] == "全部已发布"


def test_publish_confirm_retries_then_alerts(env):
    p = approve_and_execute(env)
    env.miaoshou.published_list = [{"collectBoxDetailId": "901"}]
    t = NOW + timedelta(minutes=3)
    for _ in range(12):
        env.app.followups.run_due(t)
        t += timedelta(minutes=6)
    assert "超时未确认" in env.app.store.get_proposal(p.id).result["回读确认"]
    assert "人工检查" in env.feishu.sent[-1]["text"]


def test_review_reminder_links_reply_to_task(env):
    approve_and_execute(env)
    env.miaoshou.published_list = [{"collectBoxDetailId": "901"}, {"collectBoxDetailId": "902"}]
    env.app.followups.run_due(NOW + timedelta(days=3, minutes=1))
    reminder = next(m for m in env.feishu.sent if "复盘提醒" in m.get("text", ""))
    assert env.app.store.task_for_message(reminder["message_id"]) == env.task.id


def test_scheduler_runs_due_jobs_and_survives_failures(env):
    calls = []

    def boom(now):
        raise RuntimeError("x")

    sched = Scheduler(env.app.store, [Job("a", timedelta(minutes=1), calls.append), Job("b", timedelta(minutes=1), boom)])
    assert sched.tick(NOW) == ["a", "b"]
    assert sched.tick(NOW + timedelta(seconds=30)) == []
    assert sched.tick(NOW + timedelta(minutes=1)) == ["a", "b"]
    assert len(calls) == 2


def test_health_check_alerts_once_per_day(env):
    class Broken:
        def list_shops(self, platform):
            raise AdapterError(ErrorCategory.PERMISSION, "ipNotInWhitelist")

    job = health_check_job(env.app.store, env.feishu, "ou_boss", Broken())
    job(NOW)
    job(NOW + timedelta(hours=1))
    alerts = [m for m in env.feishu.sent if "健康检查失败" in m.get("text", "")]
    assert len(alerts) == 1
    assert env.app.store.kv_get("health:miaoshou").startswith("fail")

