"""飞书自建应用：直接获取 tenant token，官方 SDK 长连接收消息。"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from pathlib import Path

from .feishu import FeishuError, LarkCliFeishu

BASE_URL = 'https://open.feishu.cn'


class NativeFeishu(LarkCliFeishu):
    """复用消息/看板请求映射，替换 CLI 传输层；token 仅保留在内存。"""
    def __init__(self, app_id: str, app_secret: str, timeout: float = 30):
        self.app_id, self.app_secret, self.timeout = app_id, app_secret, timeout
        self._token, self._expires = '', 0
        self._lock = threading.Lock()
        # 按负责人本人身份搜索聊天记录只能走 lark-cli 的用户授权（应用凭证没有这个权限），这里沿用父类的 CLI 设置
        self.binary, self.identity = 'lark-cli', 'bot'

    def _request(self, method, path, data=None, token=''):
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(BASE_URL + path, method=method, headers=headers,
                                         data=json.dumps(data).encode() if data is not None else None)
        return self._json_response(request)

    def _http_error(self, error: urllib.error.HTTPError) -> FeishuError:
        try:
            with error:
                payload = json.load(error)
        except (ValueError, OSError):
            return FeishuError(f'飞书 HTTP {error.code}：请求失败，返回内容不是有效 JSON')
        try:
            self._validate_payload(payload)
        except FeishuError as detail:
            return FeishuError(f'HTTP {error.code}：{detail}')
        return FeishuError(f'飞书 HTTP {error.code}：请求被拒绝')

    def _json_response(self, request):
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            raise self._http_error(error) from None
        except (urllib.error.URLError, TimeoutError):
            raise FeishuError('飞书网络请求失败，请检查网络后重试') from None
        except (ValueError, UnicodeError):
            raise FeishuError('飞书返回格式不正确：不是有效 JSON') from None
        self._validate_payload(payload)
        return payload

    def _validate_payload(self, payload):
        if not isinstance(payload, dict):
            raise FeishuError('飞书返回格式不正确')
        if payload.get('code', 0) != 0:
            # 不输出完整响应、token、请求头或凭证。
            code = payload.get('code')
            message = str(payload.get('msg') or '请求被拒绝')
            for secret in (self.app_secret, self._token):
                if secret:
                    message = message.replace(secret, '[已隐藏]')
            raise FeishuError(f'飞书接口错误 {code}：{message[:300]}')

    def tenant_token(self):
        with self._lock:
            if not self._token or time.monotonic() >= self._expires:
                response = self._request('POST', '/open-apis/auth/v3/tenant_access_token/internal',
                                         {'app_id': self.app_id, 'app_secret': self.app_secret})
                if not response.get('tenant_access_token'):
                    raise FeishuError('飞书未返回应用访问凭证')
                self._token = response['tenant_access_token']
                self._expires = time.monotonic() + max(0, int(response.get('expire', 7200)) - 60)
            return self._token

    def _api(self, method: str, path: str, params: dict | None = None, data: dict | None = None) -> dict:
        if params:
            path += '?' + urllib.parse.urlencode(params)
        response = self._request(method, path, data, self.tenant_token())
        return response.get('data', response)

    def check_connection(self):
        self.tenant_token()
        info = self._api('GET', '/open-apis/bot/v3/info')
        bot = info.get('bot', {})
        return {'ok': True, 'bot_name': bot.get('app_name') or bot.get('name') or '飞书机器人',
                'message': '应用认证和机器人信息读取成功。消息订阅需在飞书后台开启长连接并实测。'}

    def reply_image(self, message_id: str, image_path: str) -> str:
        """上传图片拿 image_key，再回复到消息的 thread（证据截图）。"""
        path = Path(image_path)
        boundary = 'ecomtwin' + os.urandom(8).hex()
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{path.name}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode() + path.read_bytes() + \
            f'\r\n--{boundary}--\r\n'.encode()
        request = urllib.request.Request(BASE_URL + '/open-apis/im/v1/images', method='POST', data=body,
                                         headers={'Authorization': 'Bearer ' + self.tenant_token(),
                                                  'Content-Type': f'multipart/form-data; boundary={boundary}'})
        try:
            payload = self._json_response(request)
        except FeishuError as error:
            raise FeishuError(f'上传图片失败：{error}') from None
        data = payload.get('data')
        image_key = data.get('image_key') if isinstance(data, dict) else None
        if not image_key:
            raise FeishuError('上传图片失败：没有返回 image_key')
        data = self._api('POST', f'/open-apis/im/v1/messages/{message_id}/reply',
                         data={'msg_type': 'image', 'content': json.dumps({'image_key': image_key})})
        return data['message_id']

    def download_image(self, message_id: str, image_key: str, destination: str | Path) -> Path:
        """下载入站图片到任务工作目录，限制大小并以临时文件原子落盘。

        图片可能包含账号或密码，因此不返回正文、不打印 image_key，目标文件权限为 0600。
        """
        if not message_id or not image_key:
            raise FeishuError('飞书图片参数缺失')
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        url = (f'/open-apis/im/v1/messages/{urllib.parse.quote(message_id, safe="")}'
               f'/resources/{urllib.parse.quote(image_key, safe="")}?type=image')
        request = urllib.request.Request(BASE_URL + url, method='GET',
                                         headers={'Authorization': 'Bearer ' + self.tenant_token()})
        temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response, temporary.open('wb') as out:
                content_type = (response.headers.get('Content-Type', '') if getattr(response, 'headers', None)
                                else '').split(';', 1)[0].strip().lower()
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 20 * 1024 * 1024:
                        raise FeishuError('飞书图片超过 20 MB，未保存')
                    out.write(chunk)
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                      "image/gif": ".gif"}.get(content_type, ".img")
            path = path.with_suffix(suffix)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            return path
        except (FeishuError, urllib.error.URLError, TimeoutError, OSError) as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(error, urllib.error.HTTPError):
                raise FeishuError(f'飞书图片下载失败：{self._http_error(error)}') from None
            if isinstance(error, FeishuError):
                raise FeishuError(f'飞书图片下载失败：{error}') from None
            raise FeishuError('飞书图片下载失败：网络或本地文件读写失败') from None


def normalize_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    """官方 im.message.receive_v1 → 项目统一消息。

    文本和图片都进入任务链路。图片正文不写入日志，统一使用安全占位符；
    ``image_key`` 只在内存消息中保留，供上层按需下载附件。
    """
    event = payload.get('event') or {}
    sender, message = event.get('sender') or {}, event.get('message') or {}
    if sender.get('sender_type') != 'user':
        return None
    message_type = message.get('message_type')
    if message_type not in {'text', 'image', 'post'}:
        return None
    try:
        raw_content = message.get('content') or '{}'
        content = raw_content if isinstance(raw_content, dict) else json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(content, dict):
        return None
    image_keys: list[str] = []
    if message_type == 'text':
        if not isinstance(content.get('text'), str):
            return None
        text = content['text']
    elif message_type == 'image':
        # 图片可能包含账号、密码或后台页面，禁止把原始内容/键值写入可见日志。
        image_key = content.get('image_key')
        if not isinstance(image_key, str) or not image_key:
            return None
        image_keys.append(image_key)
        text = '[飞书图片消息]'
    else:
        # 飞书客户端通常把“文字 + 多张图片”编码为 post 富文本。
        # 支持 zh_cn/en_us 等 locale，并递归提取文字和图片资源键。
        locale = content
        if isinstance(content.get('content'), list):
            pass
        elif isinstance(content, dict):
            locale = next((v for v in content.values() if isinstance(v, dict)
                           and isinstance(v.get('content'), list)), {})
        texts: list[str] = []
        def walk(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                tag = value.get('tag')
                if tag == 'text' and isinstance(value.get('text'), str):
                    texts.append(value['text'])
                elif tag == 'a' and isinstance(value.get('text'), str):
                    texts.append(value['text'])
                elif tag == 'img' and isinstance(value.get('image_key'), str):
                    image_keys.append(value['image_key'])
                else:
                    walk(value.get('content', []))
        walk(locale.get('content', []))
        text = '\n'.join(t for t in texts if t).strip()
        if not text and not image_keys:
            return None
        if image_keys:
            text = text or '[飞书图文消息]'
    sender_id = (sender.get('sender_id') or {}).get('open_id')
    if not sender_id or not message.get('message_id'):
        return None
    result = {'event_id': (payload.get('header') or {}).get('event_id') or message['message_id'],
            'message_id': message['message_id'], 'sender_id': sender_id,
            'chat_type': message.get('chat_type'), 'content': text,
            'parent_id': message.get('parent_id', ''), 'root_id': message.get('root_id', ''),
            'chat_id': message.get('chat_id', '')}
    if image_keys:
        result['prompt_content'] = text
        result['content'] = '[飞书图片消息]' if message_type == 'image' else '[飞书图文消息]'
        result['message_type'] = 'image' if message_type == 'image' else 'post'
        result['image_keys'] = image_keys
        result['log_content'] = '[飞书图文消息，已隐藏正文和图片]'
    elif message_type == 'post':
        result['message_type'] = 'post'
    return result


def consume_native_messages(app_id: str, app_secret: str):
    """SDK 在工作线程维护 WebSocket；主线程消费标准化消息并处理任务。"""
    import asyncio
    import queue
    from .tooling import install_tool
    install_tool('feishu_sdk')
    messages = queue.Queue(maxsize=1000)
    stop = threading.Event()
    loop_ready = threading.Event()
    context = {}

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        import lark_oapi as lark
        import lark_oapi.ws.client as ws_module
        # 官方 SDK 的 start() 使用模块级事件循环，线程内显式指定。
        ws_module.loop = loop
        context['loop'] = loop
        loop_ready.set()

        def receive(data):
            normalized = normalize_message(json.loads(lark.JSON.marshal(data)))
            if normalized:
                messages.put(normalized, timeout=10)

        handler = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(receive).build()
        client = lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.ERROR)
        try:
            client.start()
        except (Exception, asyncio.CancelledError):
            if not stop.is_set():
                messages.put(FeishuError('飞书长连接断开，请检查应用凭证、事件订阅和网络后重启服务'))
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(client._disconnect())
            loop.close()

    thread = threading.Thread(target=worker, daemon=True, name='feishu-websocket')
    thread.start()
    try:
        while True:
            try:
                item = messages.get(timeout=1)
            except queue.Empty:
                if not thread.is_alive():
                    raise FeishuError('飞书事件接收线程已结束')
                continue
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop.set()
        if loop_ready.is_set():
            loop = context['loop']
            if not loop.is_closed():
                def cancel_tasks():
                    for task in asyncio.all_tasks(loop):
                        task.cancel()
                try:
                    loop.call_soon_threadsafe(cancel_tasks)
                except RuntimeError:
                    pass  # 事件线程刚好已经自行结束。
        thread.join(timeout=5)
