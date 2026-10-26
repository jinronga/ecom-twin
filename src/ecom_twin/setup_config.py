"""配置页的持久化：保留未编辑配置，密钥只进权限为 0600 的 .env。"""
from __future__ import annotations

import json
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def dotenv_values(path: Path) -> dict[str, str]:
    result = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and not key.startswith('#'):
                result[key.strip()] = value.strip().strip('\"').strip("'")
    return result


def feishu_credentials(env_path: Path | None = None) -> tuple[str, str]:
    values = dotenv_values(env_path or PROJECT_ROOT / '.env')
    return tuple(os.environ.get(key) or values.get(key, '')
                 for key in ('FEISHU_APP_ID', 'FEISHU_APP_SECRET'))


def _toml(raw: dict[str, Any]) -> str:
    """序列化本项目配置的标量、数组与嵌套表（保留未知配置字段）。"""
    lines = []

    def scalar(value):
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if isinstance(value, (str, int, float, list)):
            return json.dumps(value, ensure_ascii=False, allow_nan=False)
        raise ValueError('配置含不支持的字段类型')

    def table(obj, prefix=()):
        if prefix:
            lines.append('[' + '.'.join(json.dumps(k, ensure_ascii=False) for k in prefix) + ']')
        for key, value in obj.items():
            if not isinstance(value, dict):
                lines.append(f'{json.dumps(key, ensure_ascii=False)} = {scalar(value)}')
        lines.append('')
        for key, value in obj.items():
            if isinstance(value, dict):
                table(value, (*prefix, key))
    table(raw)
    return '\n'.join(lines)


class SetupConfig:
    def __init__(self, config_path: Path | None = None, env_path: Path | None = None):
        self.config_path = Path(config_path or PROJECT_ROOT / 'config/settings.toml')
        self.env_path = Path(env_path or PROJECT_ROOT / '.env')

    def raw(self):
        return tomllib.loads(self.config_path.read_text(encoding='utf-8')) if self.config_path.exists() else {}

    def view(self):
        raw = self.raw()
        app_id, secret = feishu_credentials(self.env_path)
        return {'feishu': raw.get('feishu', {}), 'runtime': raw.get('runtime', {}),
                'browser': raw.get('browser', {}),
                'allowed_shops': raw.get('limits', {}).get('allowed_shops', []),
                'approvers': raw.get('approvers', {}), 'app_id': app_id,
                'secret_configured': bool(secret),
                'credentials_from_environment': bool(os.environ.get('FEISHU_APP_ID') or os.environ.get('FEISHU_APP_SECRET'))}

    def save(self, data):
        from .runtime.cli_check import CLI_SPECS
        if not isinstance(data, dict):
            raise ValueError('配置必须是对象')
        raw = self.raw()
        fs = data.get('feishu', {})
        allowed_fs = {'owner_open_id', 'base_app_token', 'tasks_table_id', 'steps_table_id', 'proposals_table_id'}
        if not isinstance(fs, dict) or set(fs) - allowed_fs:
            raise ValueError('飞书配置字段无效')
        for value in fs.values():
            if not isinstance(value, str) or '\n' in value or '\r' in value:
                raise ValueError('飞书配置应为单行文本')
        owner = fs.get('owner_open_id', raw.get('feishu', {}).get('owner_open_id', ''))
        if owner and not owner.startswith('ou_'):
            raise ValueError('负责人请填写 ou_ 开头的 open_id')
        runtime = data.get('runtime', {})
        if not isinstance(runtime, dict) or set(runtime) - {'default', 'fallback', 'cli'}:
            raise ValueError('运行时配置字段无效')
        for name in ('default', 'fallback'):
            if name in runtime and runtime[name] not in CLI_SPECS:
                raise ValueError('不支持的运行时')
        paths = runtime.get('cli', {})
        if not isinstance(paths, dict) or set(paths) - CLI_SPECS.keys() or any(not isinstance(v, str) for v in paths.values()):
            raise ValueError('工具路径无效')
        browser = data.get('browser', {})
        if not isinstance(browser, dict) or set(browser) - {'enabled', 'browser_url', 'chrome_path', 'user_data_dir'}:
            raise ValueError('浏览器配置字段无效')
        from .config import BrowserSettings
        try:
            BrowserSettings(**browser)
        except (TypeError, ValueError) as error:
            raise ValueError(str(error)) from None
        if any(not isinstance(browser.get(key, ''), str) for key in ('browser_url', 'chrome_path', 'user_data_dir')):
            raise ValueError('浏览器地址和路径必须是文本')
        if 'enabled' in browser and not isinstance(browser['enabled'], bool):
            raise ValueError('浏览器开关必须是布尔值')
        shops = data.get('allowed_shops', raw.get('limits', {}).get('allowed_shops', []))
        if not isinstance(shops, list) or any(not isinstance(s, str) or not s.startswith('tiktok:') or not s[7:].isdigit() for s in shops):
            raise ValueError('店铺格式应为 tiktok:数字ID')
        approvers = data.get('approvers', raw.get('approvers', {}))
        if not isinstance(approvers, dict):
            raise ValueError('审批人配置无效')
        for key, value in approvers.items():
            if key not in shops or not isinstance(value, dict) or set(value) - {'primary', 'delegate'}:
                raise ValueError('审批人必须关联白名单店铺')
            if not value.get('primary') or any(not isinstance(v, str) or (v and not v.startswith('ou_')) for v in value.values()):
                raise ValueError('审批人请填写 ou_ 开头的 open_id')
        app_id = data.get('app_id', self.view()['app_id'])
        secret = data.get('app_secret', '')
        if not isinstance(app_id, str) or not isinstance(secret, str) or any(c in app_id + secret for c in '\r\n\"\''):
            raise ValueError('应用凭证应为单行文本')
        if app_id and not app_id.startswith('cli_'):
            raise ValueError('App ID 请填写 cli_ 开头的应用ID')
        old_id, old_secret = feishu_credentials(self.env_path)
        if app_id != old_id and old_secret and not secret:
            raise ValueError('更换 App ID 时需同时填写新的 App Secret')
        raw.setdefault('feishu', {}).update(fs)
        raw.setdefault('runtime', {}).update({k: v for k, v in runtime.items() if k != 'cli'})
        raw['runtime'].setdefault('cli', {}).update(paths)
        if browser:
            raw.setdefault('browser', {}).update(browser)
        raw.setdefault('limits', {})['allowed_shops'] = shops
        raw['approvers'] = approvers
        serialized = _toml(raw)
        tomllib.loads(serialized)
        # 验证完成之后才写入；空密钥表示沿用已有值。
        updates = {'FEISHU_APP_ID': app_id}
        if secret:
            updates['FEISHU_APP_SECRET'] = secret
        if updates:
            lines = self.env_path.read_text(encoding='utf-8').splitlines() if self.env_path.exists() else []
            lines = [line for line in lines if line.partition('=')[0].strip() not in updates]
            lines.extend(f'{key}={value}' for key, value in updates.items())
            atomic_write(self.env_path, '\n'.join(lines) + '\n')
        atomic_write(self.config_path, serialized)
        return self.view()
