"""影子模式：只记录不推送审批、人工实际操作比对、吻合率。"""

from datetime import timedelta

from conftest import NOW, SHOP
from ecom_twin.models import ProposalStatus


def shadow_on(env):
    env.app.settings.shadow_actions = ["miaoshou.tiktok.publish"]


def test_shadow_proposal_is_recorded_not_sent(env):
    shadow_on(env)
    p = env.app.approvals.request(env.submit())
    assert p.status == ProposalStatus.SHADOW and env.feishu.sent == []
    assert env.app.executor.execute(p.id, now=NOW).reason == "not_approved"   # 永远不会执行
    assert env.app.store.get_task(env.task.id).status.value == "已关闭"


def test_match_rate_from_actuals_and_timeout(env):
    shadow_on(env)
    p1 = env.app.approvals.request(env.submit())        # 901、902
    shadow = env.app.shadow
    shadow.add_actual(p1.action, SHOP, "901", "上架", NOW + timedelta(hours=5))
    report = shadow.report(now=NOW + timedelta(days=1))
    s = report["summary"][0]
    assert (s["proposals"], s["compared"], s["matched"]) == (1, 1, 1)     # 902 还在窗口期
    report = shadow.report(now=NOW + timedelta(days=4))
    s = report["summary"][0]
    assert (s["compared"], s["matched"], round(s["match_rate"], 2)) == (2, 1, 0.5)
    assert report["mismatches"][0]["actual"] == "未操作"


def test_collect_publish_from_miaoshou(env):
    shadow_on(env)
    p = env.app.approvals.request(env.submit())
    env.miaoshou.published_list = [{"collectBoxDetailId": "902"}]
    assert env.app.shadow.collect_publish(env.miaoshou, NOW + timedelta(hours=1)) == 1
    assert env.app.shadow.collect_publish(env.miaoshou, NOW + timedelta(hours=2)) == 0   # 不重复记录
    assert env.app.store.shadow_actuals(p.action, SHOP, "902")[0]["source"] == "miaoshou:published"


def test_import_csv(env, tmp_path):
    f = tmp_path / "ops.csv"
    f.write_text("action,shop_key,object,actual,at\nmiaoshou.tiktok.publish,tiktok:10001,901,上架,2026-10-16T09:00:00+00:00\n")
    assert env.app.shadow.import_csv(f) == 1
