import json
import threading
import time
import tomllib
import urllib.error
import urllib.request

import pytest

from ecom_twin.setup_config import SetupConfig, feishu_credentials
from ecom_twin.setup_web import SetupApp, SetupServer
from ecom_twin.store import Store
from ecom_twin.models import Task, TaskSource
from ecom_twin import tooling


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv('FEISHU_APP_ID', raising=False)
    monkeypatch.delenv('FEISHU_APP_SECRET', raising=False)
    path = tmp_path / 'settings.toml'
    path.write_text('[storage]\ndb_path="old.db"\n[limits]\nmin_margin_rate=0.25\n[runtime]\nmax_retries_before_switch=3\n')
    env = tmp_path / '.env'
    env.write_text('# Keep existing entries\nMIAOSHOU_APP_SECRET=existing-private-value\n')
    return SetupConfig(path, env)


def spec():
    return {'app_id': 'cli_fixture', 'app_secret': 'private-fixture',
            'feishu': {'owner_open_id': 'ou_owner'},
            'runtime': {'default': 'codex', 'fallback': 'claude_code', 'cli': {'codex': '/tmp/codex'}},
            'allowed_shops': ['tiktok:12'], 'approvers': {'tiktok:12': {'primary': 'ou_owner', 'delegate': ''}}}


def test_save_preserves_other_configuration_and_hides_secret(config):
    view = config.save(spec())
    raw = tomllib.loads(config.config_path.read_text())
    assert raw['storage']['db_path'] == 'old.db'
    assert raw['limits']['min_margin_rate'] == 0.25
    assert raw['runtime']['max_retries_before_switch'] == 3
    assert 'private-fixture' not in json.dumps(view)
    assert 'private-fixture' not in config.config_path.read_text()
    assert 'MIAOSHOU_APP_SECRET=existing-private-value' in config.env_path.read_text()
    assert feishu_credentials(config.env_path) == ('cli_fixture', 'private-fixture')
    assert config.env_path.stat().st_mode & 0o777 == 0o600
    assert config.config_path.stat().st_mode & 0o777 == 0o600
    data = spec()
    data['app_secret'] = ''
    config.save(data)
    assert feishu_credentials(config.env_path)[1] == 'private-fixture'


def test_browser_config_is_saved_and_rejects_nonlocal_debug_endpoint(config):
    data = spec() | {'browser': {'enabled': True, 'browser_url': 'http://127.0.0.1:9222',
                                 'user_data_dir': '~/.agent-chrome'}}
    view = config.save(data)
    assert view['browser']['enabled'] and view['browser']['user_data_dir'] == '~/.agent-chrome'
    before = config.config_path.read_bytes()
    with pytest.raises(ValueError, match='本机 HTTP'):
        config.save(data | {'browser': {'enabled': True, 'browser_url': 'http://0.0.0.0:9222'}})
    assert config.config_path.read_bytes() == before


@pytest.mark.parametrize('field,value', [('app_id', 'cli_valid\nINJECT=yes'), ('app_secret', 'secret\nINJECT=yes'),
                                        ('allowed_shops', ['invalid']), ('feishu', {'owner_open_id': 'wrong'}),
                                        ('runtime', {'default': 'arbitrary-command'})])
def test_invalid_save_does_not_change_files(config, field, value):
    before = (config.config_path.read_bytes(), config.env_path.read_bytes())
    data = spec()
    data[field] = value
    with pytest.raises(ValueError):
        config.save(data)
    assert before == (config.config_path.read_bytes(), config.env_path.read_bytes())


def test_changing_app_id_requires_new_secret(config):
    config.save(spec())
    with pytest.raises(ValueError, match='更换'):
        config.save(spec() | {'app_id': 'cli_other', 'app_secret': ''})


def test_explicit_environment_credentials_have_priority(config, monkeypatch):
    config.save(spec())
    monkeypatch.setenv('FEISHU_APP_SECRET', 'environment-private')
    assert feishu_credentials(config.env_path)[1] == 'environment-private'
    assert config.view()['credentials_from_environment']
    assert 'environment-private' not in json.dumps(config.view())


@pytest.fixture
def server(config, monkeypatch):
    monkeypatch.setattr('ecom_twin.setup_web.tool_status', lambda key, path: {'key': key, 'ok': True})
    app = SetupApp(config.config_path, config.env_path)
    server = SetupServer(('127.0.0.1', 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    app.close()
    thread.join(timeout=3)


def request(server, path, data=None, token=True, **headers):
    if token:
        headers['X-Setup-Token'] = server.token
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}{path}', data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read()


def test_api_requires_token_and_rejects_cross_origin(server):
    assert request(server, '/api/state', token=False)[0] == 401
    assert request(server, '/api/state', Origin='https://untrusted.example')[0] == 403
    assert request(server, '/api/state', Host='evil.example')[0] == 403
    assert request(server, '/api/save', spec(), token=False)[0] == 401
    assert request(server, '/api/state')[0] == 200


def test_save_api_never_returns_secret(server, monkeypatch):
    monkeypatch.setattr('ecom_twin.setup_web.install_tool', lambda key, path: {'key': key, 'ok': True})
    status, body = request(server, '/api/save', spec())
    assert status == 200
    assert 'private-fixture' not in body.decode()
    state = request(server, '/api/state')[1]
    assert 'private-fixture' not in state.decode()
    assert json.loads(state)['config']['secret_configured']


def test_background_install_failure_is_visible_without_raw_error(server, monkeypatch):
    def fail(*args):
        raise OSError('private-fixture from upstream')
    monkeypatch.setattr('ecom_twin.setup_web.install_tool', fail)
    response = json.loads(request(server, '/api/install', {'keys': ['feishu_sdk']})[1])
    for _ in range(100):
        job = json.loads(request(server, '/api/jobs/' + response['job_id'])[1])
        if job['status'] == 'failed':
            break
        time.sleep(.01)
    assert job['status'] == 'failed'
    assert 'private-fixture' not in json.dumps(job)
    assert '重试' in job['message']


def test_install_api_rejects_arbitrary_commands(server):
    assert request(server, '/api/install', {'keys': ['sh -c arbitrary']})[0] == 400


def test_tool_install_is_idempotent_and_preserves_configured_path(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(tooling, 'tool_status', lambda key, path, root: {'ok': True, 'key': key})
    monkeypatch.setattr(tooling, '_run', lambda args: called.append(args))
    assert tooling.install_tool('codex', root=tmp_path)['ok']
    assert called == []
    monkeypatch.setattr(tooling, 'tool_status', lambda key, path, root: {'ok': False})
    with pytest.raises(ValueError, match='路径'):
        tooling.install_tool('codex', '/bad/configured/path', root=tmp_path)
    assert called == []


def test_missing_runtime_installs_known_package_and_verifies(monkeypatch, tmp_path):
    called, statuses = [], iter([{'ok': False}, {'ok': True, 'path': '/managed/codex'}])
    monkeypatch.setattr(tooling, 'tool_status', lambda *args: next(statuses))
    monkeypatch.setattr(tooling, 'ensure_npm', lambda root: '/trusted/npm')
    monkeypatch.setattr(tooling, '_run', lambda args: called.append(args))
    result = tooling.install_tool('codex', root=tmp_path)
    assert result['ok']
    assert called == [['/trusted/npm', 'install', '--global', '--prefix', str(tmp_path / 'node'),
                       '@openai/codex', '--no-fund', '--no-audit']]


def test_node_download_checksum_mismatch_stops_install(monkeypatch, tmp_path):
    import io
    monkeypatch.setattr(tooling.shutil, 'which', lambda name: None)
    monkeypatch.setattr(tooling.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(tooling.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: io.BytesIO(
        b'000000 node-v22.1.0-darwin-arm64.tar.gz\n'))
    monkeypatch.setattr(tooling, '_download', lambda url, path: path.write_bytes(b'not a valid archive'))
    with pytest.raises(RuntimeError, match='校验失败'):
        tooling.ensure_npm(tmp_path)
    assert not (tmp_path / 'node-runtime').exists()


def test_service_start_uses_saved_config_and_stop_terminates_process_group(server, monkeypatch):
    from ecom_twin.config import PROJECT_ROOT
    server.app.config.config_path.write_text('[feishu]\nowner_open_id="ou_owner"\n')
    server.app.config.env_path = PROJECT_ROOT / '.env'
    monkeypatch.setattr('ecom_twin.setup_web.feishu_credentials', lambda *a: ('cli_fixture', 'private-fixture'))
    calls, signals = [], []
    class FakeProcess:
        pid = 123456
        stopped = False
        def poll(self):
            return 0 if self.stopped else None
        def wait(self, timeout):
            self.stopped = True
            return 0
    process = FakeProcess()
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return process
    monkeypatch.setattr('ecom_twin.setup_web.subprocess.Popen', popen)
    monkeypatch.setattr('ecom_twin.setup_web.os.killpg', lambda pid, sig: signals.append((pid, sig)))
    assert json.loads(request(server, '/api/service/start', {})[1])['running']
    assert json.loads(request(server, '/api/service/start', {})[1])['running']
    assert len(calls) == 1
    assert '--config' in calls[0][0] and calls[0][0][-1] == 'serve'
    assert calls[0][1]['start_new_session']
    assert not json.loads(request(server, '/api/service/stop', {})[1])['running']
    assert signals[0][0] == process.pid


def test_monitor_api_exposes_read_only_dispatch_snapshot_without_sender_ids(tmp_path):
    config_path = tmp_path / 'settings.toml'
    db_path = tmp_path / 'monitor.db'
    config_path.write_text(f'[storage]\ndb_path="{db_path}"\n')
    store = Store(db_path)
    task = Task(store.next_id('T'), '监控测试', TaskSource.SELF_DM, '测试')
    store.save_task(task)
    store.log_message({'message_id': 'om-private', 'sender_id': 'ou-private',
                       'chat_type': 'p2p', 'content': '测试对话'}, 'inbound', task.id)
    app = SetupApp(config_path, tmp_path / '.env')
    snapshot = app.monitoring()
    assert snapshot['stats']['tasks'] == 1
    assert snapshot['stats']['messages'] == 1
    assert snapshot['messages'][0]['content'] == '测试对话'
    assert 'sender_id' not in snapshot['messages'][0]
    app.close()


def test_monitor_api_exposes_queue_and_pause_controls(tmp_path):
    config_path = tmp_path / 'settings.toml'
    db_path = tmp_path / 'queue.db'
    config_path.write_text(f'[storage]\ndb_path="{db_path}"\n')
    store = Store(db_path)
    task = Task(store.next_id('T'), '队列测试', TaskSource.SELF_DM, '测试')
    store.save_task(task)
    prompt = tmp_path / 'prompt.json'
    prompt.write_text('{}')
    store.enqueue_task(task.id, str(prompt))
    app = SetupApp(config_path, tmp_path / '.env')
    snapshot = app.monitoring()
    assert snapshot['queue']['items'][0]['task_id'] == task.id
    assert not snapshot['queue']['paused']
    store.set_queue_paused(True)
    assert app.monitoring()['queue']['paused']
    app.close()
