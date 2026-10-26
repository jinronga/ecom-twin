import io
import json
import os
from unittest.mock import patch

import pytest

from ecom_twin.feishu import FeishuError, make_feishu
from ecom_twin.feishu_native import NativeFeishu, normalize_message


def event(**overrides):
    message = {'message_id': 'om_new', 'chat_type': 'p2p', 'message_type': 'text',
               'content': '{"text":"列出店铺"}', 'parent_id': 'om_report'}
    message.update(overrides)
    return {'header': {'event_id': 'event_unique'},
            'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': 'ou_owner'}}, 'message': message}}


def test_official_message_normalization_and_thread_parent():
    assert normalize_message(event()) == {
        'event_id': 'event_unique', 'sender_id': 'ou_owner', 'message_id': 'om_new',
        'chat_type': 'p2p', 'content': '列出店铺', 'parent_id': 'om_report', 'chat_id': ''}
    image = normalize_message(event(message_type='image', content='{"image_key":"img_v2_fixture"}'))
    assert image['content'] == '[飞书图片消息]'
    assert image['message_type'] == 'image'
    assert image['image_keys'] == ['img_v2_fixture']
    assert normalize_message(event(message_type='image', content='{}')) is None
    post = normalize_message(event(
        message_type='post',
        content=json.dumps({'zh_cn': {'title': '', 'content': [
            [{'tag': 'text', 'text': '创建备货单'}],
            [{'tag': 'img', 'image_key': 'img_one'}],
            [{'tag': 'img', 'image_key': 'img_two'}],
        ]}})))
    assert post['content'] == '[飞书图文消息]'
    assert post['prompt_content'] == '创建备货单'
    assert post['image_keys'] == ['img_one', 'img_two']
    assert normalize_message(event(content='[]')) is None
    assert normalize_message(event(content='bad json')) is None
    bot_event = event()
    bot_event['event']['sender']['sender_type'] = 'app'
    assert normalize_message(bot_event) is None


def test_native_client_reuses_token_and_preserves_message_api_mapping(monkeypatch):
    client = NativeFeishu('cli_fixture', 'private-fixture')
    requests = []
    responses = [
        {'code': 0, 'tenant_access_token': 'memory-token', 'expire': 7200},
        {'code': 0, 'bot': {'app_name': '运营分身'}},
        {'code': 0, 'data': {'message_id': 'om_sent'}}]

    def transport(request, timeout):
        requests.append(request)
        return io.BytesIO(json.dumps(responses.pop(0)).encode())
    monkeypatch.setattr('urllib.request.urlopen', transport)
    assert client.check_connection()['bot_name'] == '运营分身'
    assert client.send_text('ou_owner', 'hello') == 'om_sent'
    assert len(requests) == 3
    assert requests[-1].headers['Authorization'] == 'Bearer memory-token'
    assert json.loads(requests[-1].data)['receive_id'] == 'ou_owner'
    assert 'receive_id_type=open_id' in requests[-1].full_url


def test_native_errors_redact_credentials(monkeypatch):
    client = NativeFeishu('cli_fixture', 'private-fixture')
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: io.BytesIO(
        b'{"code":10003,"msg":"bad private-fixture"}'))
    with pytest.raises(FeishuError) as error:
        client.tenant_token()
    assert 'private-fixture' not in str(error.value)
    assert '10003' in str(error.value)


def test_native_downloads_image_to_private_task_path(monkeypatch, tmp_path):
    client = NativeFeishu('cli_fixture', 'private-fixture')
    responses = [io.BytesIO(b'{"code":0,"tenant_access_token":"memory-token","expire":7200}'),
                 io.BytesIO(b'\x89PNG\r\n\x1a\nfixture-image')]
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: responses.pop(0))
    path = client.download_image('om_image', 'img_v2_fixture', tmp_path / 'attachments' / 'image')
    assert path.suffix == '.img'
    assert path.read_bytes().startswith(b'\x89PNG')
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_native_factory_and_console_override(monkeypatch):
    monkeypatch.setenv('FEISHU_APP_ID', 'cli_fixture')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'private-fixture')
    monkeypatch.delenv('ECOM_TWIN_FEISHU', raising=False)
    assert isinstance(make_feishu(), NativeFeishu)
    monkeypatch.setenv('ECOM_TWIN_FEISHU', 'console')
    assert type(make_feishu()).__name__ == 'ConsoleFeishu'


def test_native_event_parent_resumes_original_task_without_api_lookup(env, tmp_path):
    from test_mcp_and_dispatcher import dispatcher, dm, ok_script
    d, calls = dispatcher(env, tmp_path, {'claude_code': [ok_script('s1', 'first'), ok_script('s1', 'second')]})
    d.handle_message(dm('任务一'))
    report = env.feishu.sent[-1]['message_id']
    with patch.object(env.feishu, 'get_parent_id', side_effect=AssertionError('should use event parent')):
        assert '已并回' in d.handle_message({**dm('继续', mid='om_next'), 'parent_id': report})
    assert calls[-1][1] == 's1'


def test_official_sdk_callback_reaches_message_consumer_without_network(monkeypatch):
    lark = pytest.importorskip('lark_oapi')
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    from ecom_twin.feishu_native import consume_native_messages
    monkeypatch.setattr('ecom_twin.tooling.install_tool', lambda *a: {'ok': True})
    typed_event = lark.JSON.unmarshal(json.dumps(event()), P2ImMessageReceiveV1)
    def fake_start(client):
        processor = client._event_handler._processorMap['p2.im.message.receive_v1']
        processor.do(typed_event)
    monkeypatch.setattr(lark.ws.Client, 'start', fake_start)
    messages = consume_native_messages('cli_fixture', 'private-fixture')
    try:
        assert next(messages)['parent_id'] == 'om_report'
    finally:
        messages.close()
