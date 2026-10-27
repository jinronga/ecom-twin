import json
import threading
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest

from ecom_twin.console import make_server
from ecom_twin.memory import MemoryStore, render_memory
from ecom_twin.store import Store


@pytest.fixture
def console(tmp_path):
    store = Store(":memory:")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "ads.md").write_text(render_memory("广告健康线", "ACOS 判断", "experience", "30% 以内健康"))
    server, token = make_server(store, MemoryStore(store, mem_dir), port=0, token="tok-123")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None, token=token):
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-Console-Token": token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    yield type("C", (), {"call": staticmethod(call), "store": store, "dir": mem_dir, "base": base})
    server.shutdown()


def test_requires_token(console):
    assert console.call("GET", "/api/memories", token="wrong")[0] == 401
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(console.base + "/")
    assert e.value.code == 401


def test_token_link_sets_cookie_and_serves_page(console):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(console.base + "/?token=tok-123")
    assert e.value.code == 302
    cookie = e.value.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    req = urllib.request.Request(console.base + "/", headers={"Cookie": cookie.split(";")[0]})
    with urllib.request.urlopen(req) as r:
        assert "运营分身控制台" in r.read().decode()


def test_memory_crud_with_audit(console):
    status, data = console.call("GET", "/api/memories")
    assert status == 200 and data["memories"][0]["name"] == "广告健康线" and "ACOS 判断" in data["index"]
    status, created = console.call("POST", "/api/memories", {"name": "测品节奏", "description": "新品先小量",
                                                            "type": "experience", "body": "先上 2 款"})
    assert status == 201 and (console.dir / f"{created['slug']}.md").exists()
    assert console.call("POST", "/api/memories", {"name": "测品节奏", "description": "x"})[0] == 400  # 不误覆盖
    status, updated = console.call("PUT", f"/api/memories/{quote(created['slug'])}",
                                   {"name": "测品节奏", "description": "新品先小量测", "type": "experience", "body": "先上 3 款"})
    assert status == 200 and updated["body"] == "先上 3 款"
    assert console.call("DELETE", f"/api/memories/{quote(created['slug'])}")[0] == 200
    events = [e["event"] for e in console.call("GET", "/api/audit")[1]["events"]]
    assert events == ["deleted", "updated", "created"]


def test_validation_and_path_traversal(console):
    assert console.call("POST", "/api/memories", {"name": "x", "description": ""})[0] == 400
    assert console.call("PUT", "/api/memories/..%2F..%2Fetc", {"name": "a", "description": "b"})[0] == 400
    assert console.call("DELETE", "/api/memories/README")[0] == 400


def test_draft_edit_then_approve_and_reject(console):
    d1 = console.store.add_memory_draft({"name": "大促备货", "description": "旧描述", "body": "旧", "reason": "缺失"})
    d2 = console.store.add_memory_draft({"name": "无用", "description": "x", "body": "x", "reason": "x"})
    assert len(console.call("GET", "/api/drafts")[1]["drafts"]) == 2
    status, res = console.call("POST", f"/api/drafts/{d1}/approve",
                               {"name": "大促备货", "description": "提前 4 周备货", "type": "calendar", "body": "新"})
    assert status == 200 and "提前 4 周备货" in (console.dir / f"{res['memory']}.md").read_text()
    assert console.call("POST", f"/api/drafts/{d2}/reject", {"note": "一次性信息"})[0] == 200
    assert console.call("POST", f"/api/drafts/{d2}/approve", {})[0] == 400  # 已处理的不能再采纳
    assert console.call("GET", "/api/drafts")[1]["drafts"] == []
    assert len(console.call("GET", "/api/drafts?status=" + quote("已采纳"))[1]["drafts"]) == 1


def test_recalls_listed_newest_first(console):
    console.store.log_recall("T-0001", "ACOS", ["广告健康线"])
    console.store.log_recall("T-0002", "备货", [])
    recalls = console.call("GET", "/api/recalls")[1]["recalls"]
    assert [r["task_id"] for r in recalls] == ["T-0002", "T-0001"]
    assert len(console.call("GET", "/api/recalls?task=T-0001")[1]["recalls"]) == 1
