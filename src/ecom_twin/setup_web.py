"""本地飞书接入配置页：回环地址、每次启动令牌、脱敏状态、固定工具安装任务。"""
from __future__ import annotations

import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import PROJECT_ROOT, load_settings
from .feishu import FeishuError
from .feishu_native import NativeFeishu
from .setup_config import SetupConfig, feishu_credentials
from .tooling import activate_tools, install_tool, tool_status

PAGE = Path(__file__).parent / 'web/setup.html'


class SetupApp:
    def __init__(self, config_path=None, env_path=None):
        self.config = SetupConfig(config_path, env_path)
        self.lock = threading.RLock()
        self.jobs = {}
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.process = None

    def service_status(self):
        running = self.process is not None and self.process.poll() is None
        return {'running': running, 'exit_code': self.process.poll() if self.process else None}

    def monitoring(self):
        """只读返回本地调度快照；不返回凭据、open_id 或内部会话 token。"""
        config = load_settings(self.config.config_path)
        db_path = Path(config.db_path)
        empty = {'stats': {'tasks': 0, 'running': 0, 'waiting': 0, 'failed': 0,
                           'proposals': 0, 'messages': 0},
                 'tasks': [], 'steps': [], 'proposals': [], 'messages': [], 'audit': [],
                 'queue': {'paused': False, 'items': []}}
        if str(db_path) == ':memory:' or not db_path.exists():
            return empty
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            def rows(sql, args=()):
                return [dict(row) for row in conn.execute(sql, args).fetchall()]

            tasks = rows("""SELECT id,title,source,scene,status,shop_key,current_step,runtime,
                conclusion,blocker,created_at,updated_at,last_heartbeat,tokens_in,tokens_out
                FROM tasks ORDER BY updated_at DESC LIMIT 100""")
            steps = rows("""SELECT id,task_id,name,status,summary,error,started_at,ended_at
                FROM steps ORDER BY id DESC LIMIT 100""")
            proposals = rows("""SELECT id,task_id,shop_key,action,risk,method,status,expires_at,
                approver,decided_at,reject_reason,reject_note,executed_at,created_at
                FROM proposals ORDER BY created_at DESC LIMIT 100""")
            has_messages = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_log'").fetchone())
            messages = rows("""SELECT id,message_id,direction,chat_type,content,task_id,created_at
                FROM message_log ORDER BY id DESC LIMIT 150""") if has_messages else []
            audit = rows("""SELECT entity,entity_id,event,created_at FROM audit_log
                ORDER BY id DESC LIMIT 150""")
            queue_rows = rows("""SELECT q.task_id,q.state,q.enqueued_at,q.started_at,q.finished_at,q.error,
                t.title,t.status AS task_status FROM task_queue q JOIN tasks t ON t.id=q.task_id
                WHERE q.state IN ('queued','running') ORDER BY q.enqueued_at,q.rowid""")
            paused = bool(conn.execute("SELECT value FROM kv WHERE key='queue:paused'").fetchone() and
                          conn.execute("SELECT value FROM kv WHERE key='queue:paused'").fetchone()[0] == '1')
            counts = conn.execute("""SELECT
                (SELECT COUNT(*) FROM tasks),
                (SELECT COUNT(*) FROM tasks WHERE status IN ('进行中','执行中')),
                (SELECT COUNT(*) FROM tasks WHERE status IN ('排队中','等待回复','待审批')),
                (SELECT COUNT(*) FROM tasks WHERE status='失败'),
                (SELECT COUNT(*) FROM proposals),
                (SELECT COUNT(*) FROM message_log)""").fetchone() if has_messages else conn.execute("""SELECT
                (SELECT COUNT(*) FROM tasks),
                (SELECT COUNT(*) FROM tasks WHERE status IN ('进行中','执行中')),
                (SELECT COUNT(*) FROM tasks WHERE status IN ('排队中','等待回复','待审批')),
                (SELECT COUNT(*) FROM tasks WHERE status='失败'),
                (SELECT COUNT(*) FROM proposals), 0""").fetchone()
            for row in messages:
                row.pop('message_id', None)
            return {'stats': dict(zip(('tasks','running','waiting','failed','proposals','messages'), counts)),
                    'tasks': tasks, 'steps': steps, 'proposals': proposals,
                    'messages': messages, 'audit': audit,
                    'queue': {'paused': paused, 'items': queue_rows}}
        finally:
            conn.close()

    def state(self):
        activate_tools()
        with self.lock:
            config = self.config.view()
        paths = config['runtime'].get('cli', {})
        with ThreadPoolExecutor(max_workers=5) as pool:
            statuses = list(pool.map(lambda key: tool_status(key, paths.get(key, '')),
                                     ['feishu_sdk', 'claude_code', 'codex', 'cursor', 'opencode', 'chrome_devtools']))
        with self.lock:
            jobs = {key: dict(value) for key, value in self.jobs.items()
                    if value['status'] in ('pending', 'running')}
        return {'config': config, 'tools': statuses, 'service': self.service_status(), 'active_jobs': jobs}

    def prepare(self, keys=None):
        config = self.config.view()
        rt = config['runtime']
        requested_keys = keys is not None
        keys = keys or ['feishu_sdk', rt.get('default', 'claude_code'), rt.get('fallback', 'codex')]
        if not isinstance(keys, list) or not keys or any(k not in {'feishu_sdk', 'claude_code', 'codex', 'cursor', 'opencode', 'chrome_devtools'} for k in keys):
            raise ValueError('工具列表无效')
        if not requested_keys and config.get('browser', {}).get('enabled'):
            keys.append('chrome_devtools')
        keys = list(dict.fromkeys(keys))
        job_id = secrets.token_hex(8)
        paths = rt.get('cli', {})
        with self.lock:
            self.jobs[job_id] = {'status': 'pending', 'message': '等待检查并安装缺失工具', 'results': []}

        def run():
            results = []
            try:
                for key in keys:
                    with self.lock:
                        self.jobs[job_id].update(status='running', message=f'正在准备 {key}')
                    results.append(install_tool(key, paths.get(key, '')))
                with self.lock:
                    self.jobs[job_id].update(status='done', message='工具已准备好；首次使用 agent 还需完成对应账号登录', results=results)
            except Exception as error:
                # 原始异常可能包含路径、URL 或上游响应，仅返回受控错误。
                message = str(error) if isinstance(error, (ValueError, RuntimeError)) else '工具下载失败，请检查网络后重试'
                with self.lock:
                    self.jobs[job_id].update(status='failed', message=message, results=results)
        self.pool.submit(run)
        return {'job_id': job_id}

    def dispatch(self, path, data):
        if path == '/api/save':
            with self.lock:
                self.config.save(data)
            return {'ok': True, **self.prepare()}
        if path == '/api/install':
            return self.prepare(data.get('keys'))
        if path == '/api/test':
            app_id, secret = feishu_credentials(self.config.env_path)
            if not app_id or not secret:
                raise ValueError('请先保存 App ID 和 App Secret')
            return NativeFeishu(app_id, secret).check_connection()
        if path == '/api/service/start':
            with self.lock:
                if self.process is None or self.process.poll() is not None:
                    config = load_settings(self.config.config_path)
                    if not config.feishu.owner_open_id:
                        raise ValueError('请先配置负责人 open_id')
                    if not all(feishu_credentials(self.config.env_path)):
                        raise ValueError('请先配置飞书应用凭证')
                    if self.config.env_path != PROJECT_ROOT / '.env':
                        raise ValueError('服务启动仅支持项目默认凭证文件')
                    self.process = subprocess.Popen(
                        [sys.executable, '-m', 'ecom_twin.cli', '--config', str(self.config.config_path.resolve()), 'serve'],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        cwd=PROJECT_ROOT, start_new_session=True)
            return self.service_status()
        if path == '/api/service/stop':
            with self.lock:
                if self.process and self.process.poll() is None:
                    if os.name == 'posix':
                        os.killpg(self.process.pid, signal.SIGTERM)
                    else:
                        self.process.terminate()
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        if os.name == 'posix':
                            os.killpg(self.process.pid, signal.SIGKILL)
                        else:
                            self.process.kill()
                        self.process.wait(timeout=5)
            return self.service_status()
        if path in ('/api/queue/pause', '/api/queue/resume'):
            config = load_settings(self.config.config_path)
            conn = sqlite3.connect(str(config.db_path), timeout=5)
            try:
                conn.execute("INSERT INTO kv(key,value) VALUES('queue:paused',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             ('1' if path.endswith('pause') else '0',))
                conn.commit()
            finally:
                conn.close()
            return {'paused': path.endswith('pause')}
        if path in ('/api/queue/cancel', '/api/queue/trigger'):
            task_id = str(data.get('task_id') or '').strip()
            if not task_id:
                raise ValueError('缺少 task_id')
            config = load_settings(self.config.config_path)
            conn = sqlite3.connect(str(config.db_path), timeout=5)
            try:
                if path.endswith('cancel'):
                    cur = conn.execute("UPDATE task_queue SET state='cancelled',finished_at=? WHERE task_id=? AND state='queued'",
                                       (datetime.now(timezone.utc).isoformat(), task_id))
                    if cur.rowcount:
                        conn.execute("UPDATE tasks SET status='已关闭',blocker='已从队列取消' WHERE id=?", (task_id,))
                else:
                    cur = conn.execute("UPDATE task_queue SET state='queued',enqueued_at=?,started_at=NULL,finished_at=NULL,error=NULL WHERE task_id=? AND state IN ('done','cancelled','failed')",
                                       (datetime.now(timezone.utc).isoformat(), task_id))
                    if cur.rowcount:
                        conn.execute("UPDATE tasks SET status='排队中',blocker='' WHERE id=?", (task_id,))
                conn.commit()
            finally:
                conn.close()
            return {'ok': bool(cur.rowcount), 'task_id': task_id}
        raise ValueError('接口不存在')

    def close(self):
        self.dispatch('/api/service/stop', {})
        self.pool.shutdown(wait=False, cancel_futures=True)


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        if address[0] != '127.0.0.1':
            raise ValueError('配置页面仅支持本机访问')
        self.app, self.token = app, secrets.token_urlsafe(32)
        super().__init__(address, SetupHandler)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_port}/#token={self.token}'


class SetupHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 禁止把访问 URL、请求体、凭证写入日志。

    def reply(self, status, payload, html=False):
        body = payload if html else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def permitted(self):
        expected = f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') != expected:
            self.reply(403, {'error': '访问地址无效'})
            return False
        if self.headers.get('Origin') not in (None, f'http://{expected}'):
            self.reply(403, {'error': '请求来源无效'})
            return False
        if not secrets.compare_digest(self.headers.get('X-Setup-Token', ''), self.server.token):
            self.reply(401, {'error': '请使用启动命令输出的完整本地链接打开配置页'})
            return False
        return True

    def do_GET(self):
        if self.path == '/':
            self.reply(200, PAGE.read_bytes(), html=True)
            return
        if not self.permitted():
            return
        try:
            if self.path == '/api/state':
                self.reply(200, self.server.app.state())
            elif self.path == '/api/monitor':
                self.reply(200, self.server.app.monitoring())
            elif self.path == '/api/service':
                self.reply(200, self.server.app.service_status())
            elif self.path.startswith('/api/jobs/'):
                with self.server.app.lock:
                    job = self.server.app.jobs.get(self.path.removeprefix('/api/jobs/'))
                    self.reply(200 if job else 404, job or {'error': '安装任务不存在'})
            else:
                self.reply(404, {'error': '接口不存在'})
        except Exception:
            self.reply(500, {'error': '读取配置失败，请检查配置文件格式和本地权限'})

    def do_POST(self):
        if not self.permitted():
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError('请求大小无效')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('请求必须是对象')
            result = self.server.app.dispatch(self.path, data)
            self.reply(200, result)
        except (ValueError, FeishuError) as error:
            self.reply(400, {'error': str(error)})
        except Exception:
            self.reply(500, {'error': '操作失败，请检查网络、配置格式和本地目录权限'})


def run_setup(config_path=None, port=8765, open_browser=True):
    app = SetupApp(config_path)
    server = SetupServer(('127.0.0.1', port), app)
    print(f'本地配置页面：{server.url}', flush=True)
    print('关闭此命令会停止从页面启动的任务服务。', flush=True)
    if open_browser:
        webbrowser.open(server.url)
    # 用户已要求缺失工具自动安装；只准备当前选中的底座和飞书组件。
    app.prepare()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
