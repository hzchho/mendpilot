"""Real CLI + Docker + MendPilot loop, with a local deterministic HTTP model (no charges)."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import shutil
import shlex
import subprocess
import sys

import os
import pytest
import yaml

from mendpilot.cli import main, load_config, parser
from mendpilot.runner import visible_test


ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = 'fake-general-cli-test-credential'


@contextmanager
def model_server(commands):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            index = len(requests)
            requests.append({'path': self.path, 'body': body, 'auth': self.headers.get('Authorization')})
            command = commands[min(index, len(commands) - 1)]
            call_id = 'call_' + str(index)
            arguments = json.dumps({'command': command})
            if self.path.endswith('/responses'):
                response = {'id': 'resp_' + str(index), 'object': 'response', 'created_at': 1,
                    'status': 'completed', 'model': 'fake-repair-model', 'parallel_tool_calls': False,
                    'tools': [], 'tool_choice': 'auto', 'output': [
                        {'id': 'fc_' + str(index), 'type': 'function_call', 'name': 'bash',
                         'call_id': call_id, 'arguments': arguments, 'status': 'completed'}],
                    'usage': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20}}
            else:
                response = {'id': 'chat_' + str(index), 'object': 'chat.completion', 'created': 1,
                    'model': 'fake-repair-model', 'choices': [{'index': 0, 'finish_reason': 'tool_calls',
                        'message': {'role': 'assistant', 'content': 'Inspect and repair the reported issue.',
                            'tool_calls': [{'id': call_id, 'type': 'function',
                                'function': {'name': 'bash', 'arguments': arguments}}]}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
            data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dry_run_reads_task_and_focus_without_docker_or_model(tmp_path):
    output = tmp_path / 'preview'
    code = main(['--project', str(ROOT / 'examples/buggy_average'), '--file', 'average.py',
                 '--task-file', str(ROOT / 'examples/average-task.md'),
                 '--output', str(output), '--dry-run'])
    assert code == 0
    manifest = json.loads((output / 'manifest.json').read_text())
    assert manifest['source']['focus_file'] == 'average.py'
    assert len(manifest['source']['files']) == 2
    assert 'ZeroDivisionError' in (output / 'task.md').read_text()
    assert not (output / 'trajectory.json').exists()


def test_unknown_key_field_in_yaml_is_rejected(tmp_path):
    path = tmp_path / 'bad.yaml'
    path.write_text('model:\n  name: test\n  base_url: http://localhost/v1\n  api_key: dummy\n')
    args = parser().parse_args(['--file', str(path), '--task', 'test',
                               '--config', str(path), '--output', str(tmp_path / 'output')])
    with pytest.raises(ValueError, match='never put API keys'):
        load_config(args)


def test_zero_unittest_tests_are_not_reported_as_verification():
    class EmptyTests:
        def execute(self, *args, **kwargs):
            return {'returncode': 0, 'output': 'Ran 0 tests in 0.001s\nOK', 'exception_info': ''}
    result = visible_test(EmptyTests(), 'python -m unittest discover', 10)
    assert not result['passed'] and result['zero_tests']
    assert result['returncode'] == 1 and result['original_returncode'] == 0


@pytest.mark.parametrize('api,broken,with_test,file_only,backend', [
    ('chat_completions', False, True, False, 'docker'), ('responses', False, True, False, 'docker'),
    ('chat_completions', True, True, False, 'docker'), ('chat_completions', False, False, False, 'docker'),
    ('chat_completions', False, True, True, 'docker'),
    ('chat_completions', False, True, False, 'local'), ('responses', False, True, False, 'local'),
    ('chat_completions', True, True, False, 'local'), ('chat_completions', False, False, False, 'local'),
    ('chat_completions', False, True, True, 'local'),
    ('chat_completions', False, True, False, 'empty-image'),
])
def test_end_to_end_repair_keeps_source_and_exports_real_patch(tmp_path, monkeypatch, api, broken, with_test, file_only, backend):
    if backend == 'docker' and not os.environ.get('MENDPILOT_TEST_IMAGE'):
        pytest.skip('Set MENDPILOT_TEST_IMAGE for Docker integration')
    project = tmp_path / 'project'
    project.mkdir()
    for name in ['average.py', 'test_average.py']:
        (project / name).write_bytes((ROOT / 'examples/buggy_average' / name).read_bytes())
    original = (project / 'average.py').read_bytes()
    (project / '.env').write_text('EXCLUDED_TEST_VALUE=not-for-the-model')
    command = 'python -m unittest -v test_average'
    if file_only:
        command = 'python -c "from average import average; assert average([]) == 0; assert average([2, 4]) == 3"'
    replacement = 'return 1' if broken else 'return sum(values) / len(values) if values else 0'
    edit = "python -c \"from pathlib import Path; p=Path('average.py'); p.write_text(p.read_text().replace('return sum(values) / len(values)', '" + replacement + "'))\""
    output = tmp_path / 'output with spaces'
    submit = ('python /tmp/mendpilot_tools.py submit' if backend == 'docker' else
              shlex.quote(sys.executable) + ' ' + shlex.quote(str(output / 'runtime/mendpilot_tools.py')) + ' submit')
    commands = ['cat average.py', command, edit, command, submit]
    if backend != 'docker':
        original_run = subprocess.run
        def no_docker(*args, **kwargs):
            assert Path(args[0][0]).name != 'docker', 'Local execution must not invoke Docker'
            return original_run(*args, **kwargs)
        monkeypatch.setattr(subprocess, 'run', no_docker)
    global_paths = [Path('/tmp/mendpilot_tools.py'), Path('/tmp/mendpilot-state/state.json')]
    original_global = [p.read_bytes() if p.exists() else None for p in global_paths]
    with model_server(commands) as (url, requests):
        config = tmp_path / 'model.yaml'
        config.write_text(yaml.safe_dump({'model': {'name': 'fake-repair-model', 'api': api, 'base_url': url},
            'limits': {'input_tokens': 24000, 'output_tokens': 512, 'steps': 8, 'seconds': 120, 'tool_timeout': 20}}))
        monkeypatch.setenv('MENDPILOT_API_KEY', FAKE_KEY)
        args = ['--project', str(project), '--file', 'average.py', '--task',
            'average([]) should return 0; preserve nonempty behavior. Do not edit tests.',
            '--config', str(config), '--output', str(output)]
        if file_only:
            args = ['--file', str(project / 'average.py')] + args[4:]
        if with_test:
            args += ['--test-command', command]
        if backend == 'docker':
            args += ['--image', os.environ.get('MENDPILOT_TEST_IMAGE', 'python:3.11')]
        elif backend == 'empty-image':
            args += ['--image', '']
        code = main(args)
    result = json.loads((output / 'result.json').read_text())
    assert (project / 'average.py').read_bytes() == original
    assert not (project / '.git').exists()
    assert not (output / 'source/.env').exists()
    assert not (output / 'fixed/.git').exists()
    if file_only:
        assert not (output / 'source/test_average.py').exists()
    assert not result['artifact_errors'], result
    manifest = json.loads((output / 'manifest.json').read_text())
    assert result['execution_mode'] == manifest['execution_mode'] == ('docker' if backend == 'docker' else 'local')
    if backend != 'docker':
        assert manifest['image'] is None and manifest['python_executable'] == sys.executable
        assert (output / 'runtime/state/state.json').is_file()
        assert (output / 'workspace/average.py').is_file()
        assert original_global == [p.read_bytes() if p.exists() else None for p in global_paths]
        instructions = json.dumps(requests[0]['body'])
        assert 'The container has no network' not in instructions
        assert 'python /tmp/mendpilot_tools.py' not in instructions
    assert requests and all(r['auth'] == 'Bearer ' + FAKE_KEY for r in requests)
    assert result['http_attempts'] == len(requests)
    assert 'MENDPILOT_API_KEY' not in __import__('os').environ
    if broken:
        assert code == 1 and not result['success']
        assert result['final_test']['passed'] is False
        assert not (output / 'submitted.patch').read_text()
        assert (output / 'worktree.diff').read_text()
    else:
        assert code == 0 and result['success'], result
        assert '+    return sum(values) / len(values) if values else 0' in (output / 'submitted.patch').read_text()
        assert 'if values else 0' in (output / 'fixed/average.py').read_text()
        patched = tmp_path / 'patch-check'
        shutil.copytree(project, patched)
        subprocess.run(['git', 'apply', '--check', str(output / 'submitted.patch')], cwd=patched, check=True)
        subprocess.run(['git', 'apply', str(output / 'submitted.patch')], cwd=patched, check=True)
        assert (patched / 'average.py').read_bytes() == (output / 'fixed/average.py').read_bytes()
        if with_test:
            assert json.loads((output / 'test-before.json').read_text())['passed'] is False
            assert result['final_test']['passed'] is True
            assert result['verification'] == 'visible_tests_passed'
        else:
            assert result['verification'] == 'not_requested'
    # Nothing persisted receives the key (including config, trajectories and test logs).
    for path in output.rglob('*'):
        if path.is_file():
            assert FAKE_KEY.encode() not in path.read_bytes(), path
    assert not any('EXCLUDED_TEST_VALUE' in json.dumps(r['body']) for r in requests)
