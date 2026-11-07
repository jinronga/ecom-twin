"""记忆：按动作召回（读取 / 兜底检索）、四种草稿模式、索引预算（对照数字分身的记忆设计）。"""

import json

import pytest

from ecom_twin.memory import MAX_DESCRIPTION_LENGTH, MemoryError, MemoryStore, render_memory

PRICING = """## 改价

- 改价幅度一次不超过 10%
- 大促前两周不改价

## 定价

- 目标毛利率按 30% 算"""


def store(env, tmp_path) -> MemoryStore:
    (tmp_path / "pricing.md").write_text(
        render_memory("定价经验", "提交改价提案、给新品定价前打开：幅度上限和大促前禁改", "experience", PRICING))
    (tmp_path / "promo.md").write_text(
        render_memory("大促日历", "判断预算、备货节奏前打开：各站点大促日期", "calendar", "## 中东\n\n- 斋月"))
    return MemoryStore(env.app.store, tmp_path)


def test_index_lines_are_name_dash_trigger(env, tmp_path):
    index = store(env, tmp_path).index_text()
    assert "- 定价经验 — 提交改价提案、给新品定价前打开：幅度上限和大促前禁改" in index
    assert "10%" not in index


def test_read_returns_full_body_and_logs_read(env, tmp_path):
    mem = store(env, tmp_path)
    assert "大促前两周不改价" in mem.read("定价经验", task_id="T-0001").body
    assert mem.read("不存在", task_id="T-0001") is None
    logs = env.app.store.list_recalls("T-0001")
    assert [(r["command"], r["query"], r["hits"]) for r in logs] == [
        ("read", "定价经验", ["定价经验"]), ("read", "不存在", [])]


def test_search_weights_name_over_body_and_returns_section_excerpt(env, tmp_path):
    mem = store(env, tmp_path)
    hits = mem.search("改价幅度")
    assert hits[0]["name"] == "定价经验" and hits[0]["section"] == "改价"
    assert "一次不超过 10%" in hits[0]["excerpt"] and "目标毛利率" not in hits[0]["excerpt"]
    # 空词被丢掉，不会把该命中的挤下去
    assert mem.search("怎么做 注意事项") == []


def test_description_over_limit_is_rejected(env, tmp_path):
    mem = store(env, tmp_path)
    with pytest.raises(MemoryError, match="上限"):
        mem.save("长描述", "字" * (MAX_DESCRIPTION_LENGTH + 1), "experience", "正文")


def test_check_reports_format_errors_and_soft_budget(env, tmp_path, monkeypatch):
    mem = store(env, tmp_path)
    assert mem.check() == []
    (tmp_path / "broken.md").write_text("没有头部")
    monkeypatch.setattr("ecom_twin.memory.SOFT_MAX_ENTRIES", 1)
    levels = [lv for lv, _ in mem.check()]
    assert "error" in levels and "warning" in levels


def propose(mem, **draft):
    return mem.propose({"why": "下次少走弯路", "evidence": "T-0001 现场核实", **draft}, task_id="T-0001")


def test_append_into_existing_section(env, tmp_path):
    mem = store(env, tmp_path)
    draft = propose(mem, mode="append", target_name="定价经验", body="## 改价\n\n- 改价后 3 天内不再改")
    mem.approve(draft)
    sections = dict(mem.read("定价经验").sections())
    assert "改价后 3 天内不再改" in sections["改价"] and "改价后" not in sections["定价"]


def test_append_new_section_goes_to_end(env, tmp_path):
    mem = store(env, tmp_path)
    mem.approve(propose(mem, mode="append", target_name="定价经验", body="## 清仓\n\n- 清仓款可以低于毛利下限"))
    assert [t for t, _ in mem.read("定价经验").sections()] == ["改价", "定价", "清仓"]


def test_revise_replaces_exact_text_at_approval_time(env, tmp_path):
    mem = store(env, tmp_path)
    draft = propose(mem, mode="revise", target_name="定价经验", target_section="改价",
                    old_text="- 改价幅度一次不超过 10%", body="- 改价幅度一次不超过 8%（2026-10-20 起）")
    mem.approve(draft)
    body = mem.read("定价经验").body
    assert "不超过 8%" in body and "不超过 10%" not in body


def test_revise_rejected_when_text_changed_after_proposal(env, tmp_path):
    mem = store(env, tmp_path)
    draft = propose(mem, mode="revise", target_name="定价经验", target_section="改价",
                    old_text="- 改价幅度一次不超过 10%", body="- 不超过 8%")
    memory = mem.read("定价经验")  # 人工在提草稿之后先改了这段
    mem.save(memory.name, memory.description, memory.type, memory.body.replace("10%", "12%"), slug=memory.slug)
    with pytest.raises(MemoryError, match="出现 0 次"):
        mem.approve(draft)
    assert env.app.store.get_memory_draft(draft)["status"] == "待审核"


def test_remove_deletes_text(env, tmp_path):
    mem = store(env, tmp_path)
    mem.approve(propose(mem, mode="remove", target_name="定价经验", target_section="改价",
                        old_text="- 大促前两周不改价"))
    assert "大促前两周" not in mem.read("定价经验").body


def test_create_cannot_overwrite_existing_name(env, tmp_path):
    mem = store(env, tmp_path)
    draft = propose(mem, mode="create", name="定价经验", description="定价时打开", body="x")
    with pytest.raises(MemoryError, match="已经有"):
        mem.approve(draft)


def test_human_edits_are_stored_apart_from_original(env, tmp_path):
    mem = store(env, tmp_path)
    draft = propose(mem, mode="append", target_name="定价经验", body="## 改价\n\n- 原稿")
    mem.approve(draft, edits={"body": "## 改价\n\n- 人工改过"})
    row = env.app.store.get_memory_draft(draft)
    assert "原稿" in row["body"] and json.loads(row["final"]) == {"body": "## 改价\n\n- 人工改过"}
    assert "人工改过" in mem.read("定价经验").body


def test_identical_drafts_are_not_duplicated(env, tmp_path):
    mem = store(env, tmp_path)
    a = propose(mem, mode="append", target_name="定价经验", body="## 改价\n\n- 同一条")
    b = propose(mem, mode="append", target_name="定价经验", body="## 改价\n\n- 同一条")
    assert a == b and len(env.app.store.list_memory_drafts()) == 1


@pytest.mark.parametrize("draft, message", [
    ({"mode": "revise", "target_name": "定价经验", "body": "x"}, "old_text"),
    ({"mode": "append", "target_name": "定价经验", "body": "没有主题标题"}, "## 主题"),
    ({"mode": "create", "name": "密钥", "description": "d", "body": "app_secret=abcdef123456"}, "凭证"),
    ({"mode": "rewrite", "target_name": "定价经验"}, "mode"),
])
def test_invalid_drafts_are_refused(env, tmp_path, draft, message):
    with pytest.raises(MemoryError, match=message):
        propose(store(env, tmp_path), **draft)


def test_mcp_memory_tools(env, tmp_path):
    from ecom_twin.mcp_server import ToolServer
    server = ToolServer(env.app, store(env, tmp_path), env.miaoshou, env.task.id)
    text, err = server.call("read_memory", {"name": "定价经验"})
    assert not err and "大促前两周不改价" in text
    text, err = server.call("read_memory", {"name": "没有"})
    assert err and "索引里没有" in text
    text, err = server.call("search_memory", {"query": "斋月"})
    assert not err and "大促日历" in text
    text, err = server.call("propose_memory", {"mode": "remove", "target_name": "定价经验", "why": "w",
                                               "evidence": "e"})
    assert err and "target_section" in text
