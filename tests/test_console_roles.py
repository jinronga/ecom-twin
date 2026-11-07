"""控制台按角色区分权限（DESIGN.md 12）：运营只能看和审阅草稿，负责人才能改配置、记忆库。"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from ecom_twin.console import ROLE_OWNER, ROLE_REVIEWER, allowed, make_server
from ecom_twin.memory import MemoryStore, render_memory
from ecom_twin.store import Store


@pytest.fixture
def server(tmp_path):
    store = Store(":memory:")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "ads.md").write_text(render_memory("广告健康线", "判断 ACOS 时打开", "experience", "30% 以内健康"))
    srv, owner = make_server(store, MemoryStore(store, mem_dir), port=0, token="owner-tok", review_token="ops-tok")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, token, body=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-Console-Token": token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    yield call, store
    srv.shutdown()


def test_whoami_and_reviewer_can_read_everything(server):
    call, _ = server
    assert call("GET", "/api/whoami", "owner-tok") == (200, {"role": ROLE_OWNER})
    assert call("GET", "/api/whoami", "ops-tok") == (200, {"role": ROLE_REVIEWER})
    for path in ("/api/memories", "/api/drafts", "/api/recalls", "/api/intake", "/api/listings", "/api/memory-bundle"):
        assert call("GET", path, "ops-tok")[0] == 200, path


def test_reviewer_cannot_change_config_or_memories_but_can_review_drafts(server):
    call, store = server
    body = {"name": "广告健康线", "description": "判断 ACOS 时打开", "type": "experience", "body": "改了"}
    status, data = call("PUT", "/api/memories/ads", "ops-tok", body)
    assert status == 403 and "负责人" in data["error"]
    assert call("POST", "/api/config", "ops-tok", {"section": "limits", "key": "max_daily_writes_per_shop", "value": 999})[0] == 403
    assert call("POST", "/api/memory-bundle", "ops-tok", {"text": "x"})[0] == 403
    assert "forbidden" in store.audit_events("/api/memories/ads")
    # 审阅草稿放行：草稿不存在是业务错误（400），不是权限错误（403）
    assert call("POST", "/api/drafts/D-404/reject", "ops-tok", {})[0] != 403
    assert call("PUT", "/api/memories/ads", "owner-tok", body)[0] == 200     # 负责人照常能改


def test_allowed_rules():
    assert allowed(ROLE_OWNER, "DELETE", "/api/memories/x")
    assert allowed(ROLE_REVIEWER, "GET", "/api/config")
    assert allowed(ROLE_REVIEWER, "POST", "/api/drafts/D-1/approve")
    assert not allowed(ROLE_REVIEWER, "POST", "/api/drafts/D-1/edit")
    assert not allowed(ROLE_REVIEWER, "POST", "/api/memories/x/rollback")
