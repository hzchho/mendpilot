"""One local repair at a time, each in the current interpreter's child process."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4


MAX_FILE_BYTES = 1024 * 1024
TERMINAL = {'completed', 'failed', 'cancelled'}


class BusyError(Exception):
    pass


def redact(text, credential=''):
    if credential:
        text = text.replace(credential, '[REDACTED]')
    return re.sub(r'\bsk-[A-Za-z0-9_-]{16,}\b', '[REDACTED]', text)


def validate_upload(filename, content):
    if (not filename or '/' in filename or '\\' in filename or '\x00' in filename
            or filename in {'.', '..'} or len(filename) > 180
            or not filename.lower().endswith('.py') or any(ord(c) < 32 for c in filename)):
        raise ValueError('请上传单个 .py 文件，文件名不能包含目录。')
    if not content or len(content) > MAX_FILE_BYTES:
        raise ValueError('文件不能为空，大小不能超过 1 MiB。')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('请使用 UTF-8 编码的 Python 文件。') from None
    if '\x00' in text or not text.strip():
        raise ValueError('文件必须包含有效的文本内容。')


@dataclass
class Task:
    id: str
    filename: str
    root: Path
    status: str = 'pending'
    log: str = ''
    result: dict | None = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    process: subprocess.Popen | None = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    cancelled: bool = False


class TaskManager:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.tasks = {}
        self.lock = threading.RLock()
        self.closed = False

    def submit(self, filename, content, task_text, config, credential):
        validate_upload(filename, content)
        with self.lock:
            if self.closed:
                raise BusyError('服务正在关闭。')
            if any(task.status not in TERMINAL for task in self.tasks.values()):
                raise BusyError('已有任务运行中，请等待完成或先停止该任务。')
            task_id = uuid4().hex
            root = self.root / task_id
            incoming = root / 'input'
            incoming.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            (incoming / filename).write_bytes(content)
            task = Task(task_id, filename, root)
            self.tasks[task_id] = task
            task.thread = threading.Thread(target=self._run,
                args=(task, task_text, config, credential), daemon=True)
            task.thread.start()
            return task_id

    def _append(self, task, text, credential):
        text = redact(text, credential)
        with self.lock:
            task.log = (task.log + text)[-65536:]
            path = task.root / 'run.log'
            if not path.exists() or path.stat().st_size < 1024 * 1024:
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(text[:65536])

    def _run(self, task, task_text, config, credential):
        process = reader = None
        try:
            payload = dict(file=str(task.root / 'input' / task.filename),
                output=str(task.root / 'result'), task=task_text, config=config, credential=credential)
            env = dict(os.environ)
            # Neither this request's key nor inherited common model keys go to code execution.
            for name in list(env):
                if name.endswith(('_API_KEY', '_AUTH_TOKEN')) or name == 'MENDPILOT_RUNTIME_KEY':
                    env.pop(name)
            env.update(PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8')
            with self.lock:
                if task.cancelled:
                    task.status = 'cancelled'
                    return
                process = subprocess.Popen([sys.executable, '-u', '-m', 'mendpilot.web.worker'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace', env=env,
                    start_new_session=(os.name == 'posix'))
                task.process = process
                task.status = 'running'
            def read_logs():
                for line in process.stdout:
                    self._append(task, line, credential)
            reader = threading.Thread(target=read_logs, daemon=True)
            reader.start()
            try:
                process.stdin.write(json.dumps(payload, ensure_ascii=False))
                process.stdin.close()
            finally:
                payload.clear()
            try:
                code = process.wait(timeout=config['limits']['seconds'] + 600)
            except subprocess.TimeoutExpired:
                self._stop_process(process)
                code = 124
                self._append(task, '任务超过总时限，已停止。\n', credential)
            reader.join(timeout=10)
            path = task.root / 'result/result.json'
            result = json.loads(redact(path.read_text(encoding='utf-8'), credential)) if path.is_file() else None
            with self.lock:
                task.result = result
                task.status = ('cancelled' if task.cancelled else
                               'completed' if code == 0 and result and result.get('success') else 'failed')
                if task.status == 'failed':
                    task.error = '本次未完成有效修复，请查看日志与退出状态。'
        except Exception as error:
            if process and process.poll() is None:
                self._stop_process(process)
            with self.lock:
                task.status = 'cancelled' if task.cancelled else 'failed'
                task.error = '任务启动或收集失败：' + type(error).__name__
            self._append(task, task.error + '\n', credential)
        finally:
            with self.lock:
                task.process = None
            # A small non-secret status record is useful after the web service stops.
            (task.root / 'status.json').write_text(json.dumps(self.snapshot(task.id),
                ensure_ascii=False, indent=2), encoding='utf-8')

    @staticmethod
    def _stop_process(process):
        if process.poll() is not None:
            return
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)
        except ProcessLookupError:
            pass

    def get(self, task_id):
        with self.lock:
            if task_id not in self.tasks:
                raise KeyError(task_id)
            return self.tasks[task_id]

    def snapshot(self, task_id):
        with self.lock:
            task = self.get(task_id)
            result = task.result or {}
            return dict(id=task.id, filename=task.filename, status=task.status,
                log=task.log, error=task.error, created=task.created,
                result={key: result[key] for key in (
                    'success', 'exit_status', 'verification', 'api_calls', 'http_attempts',
                    'elapsed_seconds', 'execution_mode', 'failure_category') if key in result})

    def cancel(self, task_id):
        with self.lock:
            task = self.get(task_id)
            if task.status in TERMINAL:
                return
            task.cancelled = True
            process = task.process
        if process:
            self._stop_process(process)

    def close(self):
        with self.lock:
            self.closed = True
            tasks = list(self.tasks.values())
        for task in tasks:
            self.cancel(task.id)
        for task in tasks:
            if task.thread:
                task.thread.join(timeout=20)
