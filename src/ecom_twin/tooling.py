"""缺失工具安装：固定官方来源、本地目录、安装后验证；不接受任意安装命令。"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
from pathlib import Path

from .config import PROJECT_ROOT
from .runtime.cli_check import CLI_SPECS, check

TOOLS_ROOT = PROJECT_ROOT / 'data/tools'
PACKAGES = {'claude_code': '@anthropic-ai/claude-code', 'codex': '@openai/codex', 'opencode': 'opencode-ai'}
BINARIES = {'claude_code': 'claude', 'codex': 'codex', 'opencode': 'opencode'}
CHROME_DEVTOOLS_PACKAGE = 'chrome-devtools-mcp'
CHROME_DEVTOOLS_BINARY = 'chrome-devtools-mcp'
_INSTALL_LOCK = threading.Lock()


def activate_tools(root: Path = TOOLS_ROOT):
    bins = [root / 'node/bin', root / 'node-runtime/bin']
    for directory in reversed(bins):
        if directory.exists() and str(directory) not in os.environ.get('PATH', '').split(os.pathsep):
            os.environ['PATH'] = str(directory) + os.pathsep + os.environ.get('PATH', '')


def _run(command, timeout=600):
    # 安装器不继承业务密钥，也不把原始输出转交给网页/日志。
    env = {k: v for k, v in os.environ.items() if not k.startswith(('FEISHU_', 'MIAOSHOU_'))}
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError('安装超时，请检查网络后重试') from None
    except OSError:
        raise RuntimeError('无法启动安装器，请检查本机工具目录权限') from None
    if result.returncode:
        raise RuntimeError(f'安装器退出（{result.returncode}），请检查网络、磁盘空间和目录权限')


def _download(url, path):
    with urllib.request.urlopen(url, timeout=60) as response, path.open('wb') as stream:
        shutil.copyfileobj(response, stream)


def ensure_npm(root=TOOLS_ROOT):
    activate_tools(root)
    if shutil.which('npm') and shutil.which('node'):
        return shutil.which('npm')
    os_name = {'Darwin': 'darwin', 'Linux': 'linux'}.get(platform.system())
    arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64', 'AMD64': 'x64'}.get(platform.machine())
    if not os_name or not arch:
        raise RuntimeError('自动安装 Node.js 支持 macOS / Linux 的 arm64 和 x64')
    base = 'https://nodejs.org/dist/latest-v22.x/'
    with urllib.request.urlopen(base + 'SHASUMS256.txt', timeout=60) as response:
        sums = response.read().decode()
    entry = next((line.split() for line in sums.splitlines() if line.endswith(f'-{os_name}-{arch}.tar.gz')), None)
    if not entry:
        raise RuntimeError('未找到当前系统的 Node.js 官方发行包')
    digest, filename = entry
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        temp = Path(temporary)
        archive = temp / filename
        _download(base + filename, archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise RuntimeError('Node.js 下载校验失败')
        with tarfile.open(archive) as package:
            package.extractall(temp, filter='data')
        extracted = temp / filename.removesuffix('.tar.gz')
        destination = root / 'node-runtime'
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(extracted), destination)
    activate_tools(root)
    return str(root / 'node-runtime/bin/npm')


def tool_status(key, configured='', root=TOOLS_ROOT):
    activate_tools(root)
    if key == 'feishu_sdk':
        installed = importlib.util.find_spec('lark_oapi') is not None
        return {'key': key, 'label': '飞书连接组件', 'ok': installed,
                'version': 'lark-oapi' if installed else '', 'path': '', 'error': '' if installed else '尚未安装'}
    if key == 'chrome_devtools':
        binary = shutil.which(CHROME_DEVTOOLS_BINARY)
        if not binary:
            return {'key': key, 'label': 'Chrome DevTools MCP', 'ok': False,
                    'version': '', 'path': '', 'error': '尚未安装'}
        try:
            result = subprocess.run([binary, '--version'], stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        ok = result is not None and result.returncode == 0
        return {'key': key, 'label': 'Chrome DevTools MCP', 'ok': ok,
                'version': result.stdout.strip()[:80] if ok else '', 'path': binary,
                'error': '' if ok else '版本验证失败'}
    status = check(key, configured, timeout=8)
    return vars(status) | {'usable': status.usable}


def install_tool(key, configured='', root=TOOLS_ROOT):
    if key not in {*CLI_SPECS, 'feishu_sdk', 'chrome_devtools'}:
        raise ValueError('不支持的工具')
    with _INSTALL_LOCK:
        activate_tools(root)
        current = tool_status(key, configured, root)
        if current['ok']:
            return current
        if configured:
            raise ValueError('已配置的路径不可用，请先清空路径或修正路径，再自动安装')
        if key == 'feishu_sdk':
            _run([sys.executable, '-m', 'pip', 'install', 'lark-oapi>=1.4,<2'])
            import importlib
            importlib.invalidate_caches()
        elif key == 'chrome_devtools':
            npm = ensure_npm(root)
            _run([npm, 'install', '--global', '--prefix', str(root / 'node'),
                  CHROME_DEVTOOLS_PACKAGE, '--no-fund', '--no-audit'])
            activate_tools(root)
        elif key == 'cursor':
            if platform.system() not in ('Darwin', 'Linux'):
                raise RuntimeError('Cursor Agent 自动安装支持 macOS / Linux')
            # 官方安装脚本先下载到本地，然后由 bash 执行。
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=root) as temporary:
                script = Path(temporary) / 'cursor-install.sh'
                _download('https://cursor.com/install', script)
                _run(['bash', str(script)])
        else:
            npm = ensure_npm(root)
            _run([npm, 'install', '--global', '--prefix', str(root / 'node'), PACKAGES[key], '--no-fund', '--no-audit'])
            activate_tools(root)
        result = tool_status(key, '', root)
        if not result['ok']:
            raise RuntimeError('安装已结束，但工具验证未通过，请检查工具路径后重试')
        return result


def ensure_browser_tools(root=TOOLS_ROOT):
    """准备 Chrome DevTools MCP；使用 npm 管理目录，不执行任意安装命令。"""
    result = install_tool('chrome_devtools', root=root)
    return result['path']


def ensure_pillow():
    """桌面截图标注需要的受信依赖；已有安装时不触发网络。"""
    if importlib.util.find_spec('PIL') is None:
        _run([sys.executable, '-m', 'pip', 'install', 'Pillow>=10,<13'])
        importlib.invalidate_caches()
    if importlib.util.find_spec('PIL') is None:
        raise RuntimeError('Pillow 安装后仍不可用，请检查当前 Python 环境')


def ensure_task_tools(settings):
    """serve 启动时自动补齐飞书 SDK 与所选默认/备用底座。"""
    from .setup_config import feishu_credentials
    activate_tools()
    if all(feishu_credentials()):
        install_tool('feishu_sdk')
    for key in dict.fromkeys((settings.runtime.default, settings.runtime.fallback)):
        path = settings.runtime.cli.get(key, '')
        if not tool_status(key, path)['ok']:
            print(f'正在安装缺失工具：{CLI_SPECS[key].label}', flush=True)
            install_tool(key, path)
    if getattr(settings, 'browser', None) and settings.browser.enabled:
        if not tool_status('chrome_devtools').get('ok'):
            print('正在安装缺失工具：Chrome DevTools MCP', flush=True)
            ensure_browser_tools()
