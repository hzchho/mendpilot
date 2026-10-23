"""Run tools in a project copy using the currently active Python environment."""
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
from types import SimpleNamespace

from minisweagent.exceptions import Submitted


class LocalRepairEnvironment:
    """This is a local executor, not a sandbox. Each run has its own tool/state paths."""

    def __init__(self, output, *, timeout=60):
        if os.name != 'posix':
            raise RuntimeError('Local execution currently requires Linux/macOS with bash and git.')
        self.bash = shutil.which('bash')
        if not self.bash or not shutil.which('git'):
            raise RuntimeError('Local execution needs bash and git on PATH; no Docker is required.')
        output = Path(output).resolve()
        self.workspace = output / 'workspace'
        self.runtime = output / 'runtime'
        shutil.copytree(output / 'source', self.workspace)
        self.runtime.mkdir()
        self.python_executable = sys.executable
        self.config = SimpleNamespace(cwd=str(self.workspace), timeout=timeout)
        self.tool_options = {'tool_path': str(self.runtime / 'mendpilot_tools.py'),
                             'state_path': str(self.runtime / 'state'),
                             'python_executable': self.python_executable}
        self.exec_env = dict(os.environ)
        self.exec_env.update(PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(self.runtime),
                             MENDPILOT_TOOL_STATE=self.tool_options['state_path'])
        # Use this process's interpreter and prefer the copied project over any
        # editable installation pointing at the original source checkout.
        self.exec_env['PATH'] = str(Path(self.python_executable).parent) + os.pathsep + self.exec_env.get('PATH', '')
        self.exec_env['PYTHONPATH'] = os.pathsep.join([
            str(self.workspace), str(self.workspace / 'src'), self.exec_env.get('PYTHONPATH', '')])

    def execute(self, action, cwd='', *, timeout=None):
        process = subprocess.Popen([self.bash, '-c', action.get('command', '')],
            cwd=cwd or self.config.cwd, env=self.exec_env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            output = process.communicate(timeout=timeout or self.config.timeout)[0]
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output = process.communicate()[0]
        except BaseException:
            # Web worker cancellation must also stop the separately-sessioned
            # bash command, rather than leaving it running after the worker exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        result = {'returncode': -1 if timed_out else process.returncode,
                  'output': output.decode('utf-8', errors='replace'),
                  'exception_info': 'Local command timed out' if timed_out else ''}
        lines = result['output'].lstrip().splitlines(keepends=True)
        if not timed_out and result['returncode'] == 0 and lines and lines[0].strip() == 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT':
            raise Submitted({'role': 'exit', 'content': ''.join(lines[1:]), 'extra': {
                'exit_status': 'Submitted', 'submission': ''.join(lines[1:])}})
        return result

    def get_template_vars(self, **kwargs):
        # Do not expose host environment variables (or other service keys) in prompts.
        return platform.uname()._asdict() | {'cwd': self.config.cwd} | kwargs

    def serialize(self):
        return {'info': {'config': {'environment': {'environment_class': 'local',
            'cwd': self.config.cwd, 'timeout': self.config.timeout,
            'python_executable': self.python_executable}}}}

    def cleanup(self):
        # The independent workspace/runtime are retained as run artifacts.
        pass


def export_local_fixed(workspace, destination):
    """Export regular files only, matching the Docker export policy."""
    workspace, destination = Path(workspace), Path(destination)
    destination.mkdir()
    for folder, dirs, files in os.walk(workspace, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__', '.pytest_cache'}
                   and not Path(folder, d).is_symlink()]
        for name in files:
            path = Path(folder, name)
            if path.is_symlink() or not path.is_file():
                continue
            target = destination / path.relative_to(workspace)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
