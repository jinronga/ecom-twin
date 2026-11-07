"""记忆库：版本历史与回滚、防覆盖、[[链接]]、记忆包导入导出（对照数字分身 memory-history / memory-bundle）。"""

import pytest

from ecom_twin.memory import BUNDLE_PREFIX, MAX_SNAPSHOTS, MemoryError, MemoryStore, file_sha256
from ecom_twin.store import Store


def mem(tmp_path, name="lib"):
    return MemoryStore(Store(":memory:"), tmp_path / name)


def test_every_write_snapshots_and_rollback_restores(tmp_path):
    m = mem(tmp_path)
    first = m.save("大促日历", "判断备货节奏时打开", "experience", "## 黑五\n提前 6 周备货")
    m.save("大促日历", "判断备货节奏时打开", "experience", "## 黑五\n提前 4 周备货", slug=first.slug)
    history = m.history(first.slug)
    assert len(history) == 1 and "提前 6 周" in m.read_snapshot(first.slug, history[0]["file"])
    restored = m.rollback(first.slug, history[0]["file"])
    assert "提前 6 周" in restored.body
    assert len(m.history(first.slug)) == 2                       # 回滚前的「4 周」也留了一份，回滚错了还能回来
    assert "rolled_back" in m.store.audit_events(first.slug)


def test_history_is_capped_and_snapshot_names_are_safe(tmp_path):
    m = mem(tmp_path)
    s = m.save("改价节奏", "要调价时打开", "experience", "v0").slug
    for i in range(1, MAX_SNAPSHOTS + 6):
        m.save("改价节奏", "要调价时打开", "experience", f"v{i}", slug=s)
    assert len(m.history(s)) == MAX_SNAPSHOTS
    with pytest.raises(MemoryError):
        m.read_snapshot(s, "../../etc/passwd")
    assert all(x.name != ".history" for x in m.all())            # 历史目录不会被当成记忆读进索引


def test_delete_keeps_a_snapshot(tmp_path):
    m = mem(tmp_path)
    s = m.save("临时", "测试时打开", "experience", "内容").slug
    m.delete(s)
    assert m.get(s) is None and len(m.history(s)) == 1         # 误删能从历史里恢复


def test_save_refuses_when_file_changed_since_opened(tmp_path):
    m = mem(tmp_path)
    s = m.save("大促日历", "判断备货节奏时打开", "experience", "原文").slug
    opened = file_sha256(m.get(s).path)
    m.save("大促日历", "判断备货节奏时打开", "experience", "采纳草稿后的新内容", slug=s)   # 期间别处改过
    with pytest.raises(MemoryError, match="被改过"):
        m.save("大促日历", "判断备货节奏时打开", "experience", "我在旧页面上改的", slug=s, expected_sha=opened)
    assert "采纳草稿后的新内容" in m.get(s).body


def test_links_must_resolve_and_protect_targets(tmp_path):
    m = mem(tmp_path)
    m.save("业务信息去哪找", "要查业务背景时打开", "experience", "看 ERP")
    with pytest.raises(MemoryError, match="指向不存在"):
        m.save("改价节奏", "要调价时打开", "experience", "先读 [[不存在的记忆]]")
    linker = m.save("改价节奏", "要调价时打开", "experience", "动手前先读 [[业务信息去哪找]]")
    target = m.by_name("业务信息去哪找")
    with pytest.raises(MemoryError, match="还链着"):
        m.delete(target.slug)
    with pytest.raises(MemoryError, match="改名"):
        m.save("业务资料", "要查业务背景时打开", "experience", "看 ERP", slug=target.slug)
    graph = m.graph()
    assert graph["改价节奏"]["links"] == ["业务信息去哪找"] and graph["业务信息去哪找"]["linked_by"] == ["改价节奏"]
    m.save("孤立的", "测试时打开", "experience", "没有链接")
    assert m.graph()["孤立的"]["orphan"] and not graph["改价节奏"]["orphan"]
    assert linker


def test_bundle_roundtrip_and_overwrite_with_snapshot(tmp_path):
    src = mem(tmp_path, "a")
    src.save("业务信息去哪找", "要查业务背景时打开", "experience", "看 ERP")
    src.save("改价节奏", "要调价时打开", "experience", "## 规则\n先读 [[业务信息去哪找]]，每周最多调两次")
    text = src.export_bundle()
    assert text.count(BUNDLE_PREFIX) == 2 and "每周最多调两次" in text      # 纯文本，聊天窗口里能直接看

    dst = mem(tmp_path, "b")
    dst.save("改价节奏", "要调价时打开", "experience", "旧的说法")
    r = dst.import_bundle(text)
    assert r == {"created": ["业务信息去哪找"], "updated": ["改价节奏"], "unchanged": []}
    assert "每周最多调两次" in dst.by_name("改价节奏").body
    assert len(dst.history(dst.by_name("改价节奏").slug)) == 1               # 覆盖前留了一份
    assert dst.import_bundle(text)["unchanged"] == ["业务信息去哪找", "改价节奏"]


def test_bundle_rejects_bad_input_as_a_whole(tmp_path):
    src = mem(tmp_path, "a")
    src.save("改价节奏", "要调价时打开", "experience", "内容")
    good = src.export_bundle()
    dst = mem(tmp_path, "b")
    with pytest.raises(MemoryError, match="分隔行"):
        dst.import_bundle("随便一段文字")
    with pytest.raises(MemoryError, match="两条都叫"):
        dst.import_bundle(good + "\n" + good.split("\n\n", 1)[1])
    with pytest.raises(MemoryError, match="被手改过"):
        dst.import_bundle(good.replace(f"{BUNDLE_PREFIX} 改价节奏 ===", f"{BUNDLE_PREFIX} 别的名字 ==="))
    with pytest.raises(MemoryError, match="指向不存在"):
        dst.import_bundle(good.replace("内容", "先读 [[包外的记忆]]"))
    with pytest.raises(MemoryError, match="凭证|密钥"):
        dst.import_bundle(good.replace("内容", "app_secret=abcdef123456"))
    assert dst.all() == []                                                   # 整包拒绝，一条都没写进去


def test_console_routes(tmp_path):
    from ecom_twin.console import ConsoleApp
    m = mem(tmp_path)
    app = ConsoleApp(m.store, m)
    status, created = app.route("POST", "/api/memories", {}, {"name": "大促日历", "description": "判断备货节奏时打开",
                                                              "type": "experience", "body": "v1"})
    slug, sha = created["slug"], created["sha256"]
    app.route("PUT", f"/api/memories/{slug}", {}, {"name": "大促日历", "description": "判断备货节奏时打开",
                                                    "type": "experience", "body": "v2", "sha256": sha})
    status, data = app.route("PUT", f"/api/memories/{slug}", {}, {"name": "大促日历", "description": "判断备货节奏时打开",
                                                                   "type": "experience", "body": "v3", "sha256": sha})
    assert status == 400 and "被改过" in data["error"]                        # 拿着旧的 sha 再存：拒绝
    status, h = app.route("GET", f"/api/memories/{slug}/history", {}, {})
    status, back = app.route("POST", f"/api/memories/{slug}/rollback", {}, {"file": h["history"][0]["file"]})
    assert status == 200 and back["body"] == "v1"
    status, bundle = app.route("GET", "/api/memory-bundle", {}, {})
    status, result = app.route("POST", "/api/memory-bundle", {}, {"text": bundle["text"]})
    assert status == 200 and result["unchanged"] == ["大促日历"]


def test_draft_stats_edit_and_reject_rates(tmp_path):
    m = mem(tmp_path)
    m.save("改价节奏", "要调价时打开", "experience", "## 规则\n每周最多调两次")
    def draft(body):
        return m.propose({"mode": "append", "target_name": "改价节奏", "body": f"## 补充\n{body}",
                          "why": "实际碰到了", "evidence": "任务 T-1"}, task_id="T-1")
    a, b, c, d = draft("一"), draft("二"), draft("三"), draft("四")
    m.approve(a)
    m.approve(b, edits={"body": "## 补充\n人工改过的二"})
    m.reject(c, note="不值得记")
    s = m.store.draft_stats()
    assert (s["pending"], s["adopted"], s["edited"], s["rejected"]) == (1, 2, 1, 1)
    assert s["edit_rate"] == 0.5 and s["reject_rate"] == round(1 / 3, 3)
    assert d


def test_suggest_description_only_returns_a_validated_suggestion(tmp_path, monkeypatch):
    import subprocess
    m = mem(tmp_path)
    s = m.save("改价节奏", "调价规则、预算上限", "experience", "## 调价频率\n每周最多两次").slug
    monkeypatch.setenv("MIAOSHOU_APP_SECRET", "FAKE")
    seen = {}

    def fake_cli(cmd, **kwargs):
        seen.update(cmd=cmd, env=kwargs["env"])
        return subprocess.CompletedProcess(cmd, 0, "<description>要给 SKU 改价时打开：每周调价次数上限</description>", "")
    r = m.suggest_description(s, binary="/x/claude", run=fake_cli)
    assert r == {"before": "调价规则、预算上限", "suggestion": "要给 SKU 改价时打开：每周调价次数上限"}
    assert m.get(s).description == "调价规则、预算上限"                      # 只给建议，不落盘
    assert "Bash,Read,Write" in ",".join(seen["cmd"]) and "MIAOSHOU_APP_SECRET" not in seen["env"]

    too_long = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, f"<description>{'长' * 130}</description>", "")
    with pytest.raises(MemoryError, match="超过上限"):
        m.suggest_description(s, binary="/x/claude", run=too_long)
    nothing = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "not logged in")
    with pytest.raises(MemoryError, match="没给出建议"):
        m.suggest_description(s, binary="/x/claude", run=nothing)
