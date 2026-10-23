"""Real upload -> local child -> Agent -> download, using a fake HTTP model."""
import json
import os
from pathlib import Path
import re
import time

import pytest

pytest.importorskip('fastapi')
pytest.importorskip('multipart')
from fastapi.testclient import TestClient

from mendpilot.web.server import create_app
from mendpilot.web.tasks import MAX_FILE_BYTES, validate_upload
from test_cli import model_server


KEY = 'fake-web-runtime-credential-12345'
SOURCE = b'def average(values):\n    return sum(values) / len(values)\n'


def client_for(tmp_path):
    return TestClient(create_app(tmp_path / 'web results'), base_url='http://127.0.0.1:8765')


def headers(client):
    page = client.get('/')
    assert page.status_code == 200
    token = re.search(r'name="repair-token" content="([^"]+)"', page.text).group(1)
    return {'X-Repair-Token': token}


def form(url='http://127.0.0.1:1/v1', api='chat_completions'):
    return dict(task='average([]) should return 0; preserve nonempty behavior.',
                model='fake-repair-model', base_url=url, api=api, api_key=KEY)


def wait_until(client, task_id, predicate, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        response = client.get('/api/tasks/' + task_id)
        assert response.status_code == 200
        data = response.json()
        if predicate(data):
            return data
        time.sleep(0.1)
    raise AssertionError('Task did not reach expected state: ' + json.dumps(data))


@pytest.mark.parametrize('filename,content', [('../app.py', SOURCE), ('a\\app.py', SOURCE),
    ('app.txt', SOURCE), ('app.py', b''), ('app.py', b'\xff'), ('app.py', b'\x00'),
    ('app.py', b'x' * (MAX_FILE_BYTES + 1))])
def test_invalid_uploads(filename, content):
    with pytest.raises(ValueError):
        validate_upload(filename, content)


def test_local_request_boundaries_and_bad_inputs(tmp_path):
    with client_for(tmp_path) as client:
        auth = headers(client)
        assert client.get('/static/app.js').status_code == 200
        assert client.get('/api/defaults').json()['api'] == 'chat_completions'
        assert client.get('/', headers={'Host': 'attacker.example'}).status_code == 403
        assert client.get('/', headers={'Origin': 'https://attacker.example'}).status_code == 403
        assert client.post('/api/tasks', data=form(), files={'file': ('a.py', SOURCE)}).status_code == 403
        assert client.post('/api/tasks', headers=auth, data=form(),
                           files={'file': ('../a.py', SOURCE)}).status_code == 400
        bad = form()
        bad['base_url'] = 'https://user:password@model.invalid/v1'
        assert client.post('/api/tasks', headers=auth, data=bad,
                           files={'file': ('a.py', SOURCE)}).status_code == 400
        bad = form('https://model.invalid/v1')
        bad['api_key'] = ''
        assert client.post('/api/tasks', headers=auth, data=bad,
                           files={'file': ('a.py', SOURCE)}).status_code == 400
        assert client.post('/api/tasks', headers=auth, data=form(), files=[
            ('file', ('a.py', SOURCE)), ('file', ('b.py', SOURCE))]).status_code == 400
        assert client.get('/api/tasks/missing').status_code == 404
        assert not (tmp_path / 'web results').exists()


@pytest.mark.skipif(os.name != 'posix', reason='Real local repair needs bash/git on Linux/macOS')
@pytest.mark.parametrize('api', ['chat_completions', 'responses'])
def test_upload_repairs_and_downloads_using_current_environment(tmp_path, monkeypatch, api):
    # Fail immediately if this web path ever tries to start Docker.
    import subprocess
    original = subprocess.Popen
    def no_docker(*args, **kwargs):
        assert Path(args[0][0]).name != 'docker'
        return original(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', no_docker)
    monkeypatch.setenv('MENDPILOT_API_KEY', 'inherited-key-must-not-reach-child')
    edit = ('python -c "from pathlib import Path; p=Path(\'average.py\'); '
            'p.write_text(p.read_text().replace(\'return sum(values) / len(values)\', '
            '\'return sum(values) / len(values) if values else 0\'))"')
    check = 'python -c "from average import average; assert average([]) == 0; assert average([2,4]) == 3"'
    submit = 'python "$TMPDIR/mendpilot_tools.py" submit'
    with model_server(['cat average.py', edit, check, submit]) as (url, requests):
        with client_for(tmp_path) as client:
            response = client.post('/api/tasks', headers=headers(client), data=form(url, api),
                files={'file': ('average.py', SOURCE, 'text/x-python')})
            assert response.status_code == 202, response.text
            task_id = response.json()['id']
            task = wait_until(client, task_id, lambda x: x['status'] in {'completed', 'failed'})
            assert task['status'] == 'completed', task
            assert task['result']['execution_mode'] == 'local'
            assert task['result']['verification'] == 'not_requested'
            assert task['result']['api_calls'] == len(requests)
            assert all(r['auth'] == 'Bearer ' + KEY for r in requests)
            assert 'if values else 0' in client.get(f'/api/tasks/{task_id}/download?kind=file').text
            patch = client.get(f'/api/tasks/{task_id}/preview').json()['patch']
            assert '+    return sum(values) / len(values) if values else 0' in patch
            assert client.get(f'/api/tasks/{task_id}/download?kind=patch').text == patch
            assert client.get(f'/api/tasks/{task_id}/download?kind=../../input').status_code == 400
            root = client.app.state.manager.get(task_id).root
            assert (root / 'input/average.py').read_bytes() == SOURCE
            manifest = json.loads((root / 'result/manifest.json').read_text())
            assert manifest['image'] is None and manifest['source']['mode'] == 'file'
            assert len(manifest['source']['files']) == 1
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path
            assert b'inherited-key-must-not-reach-child' not in path.read_bytes(), path


@pytest.mark.skipif(os.name != 'posix', reason='Real local worker cancellation')
def test_single_task_and_cancellation_stop_active_command(tmp_path):
    # The separate bash session must be stopped as well as the Python worker.
    command = ('python -c "import os,time; from pathlib import Path; '
        'Path(os.environ[\'TMPDIR\'], \'active.pid\').write_text(str(os.getpid())); time.sleep(90)"')
    with model_server([command]) as (url, _):
        with client_for(tmp_path) as client:
            auth = headers(client)
            response = client.post('/api/tasks', headers=auth, data=form(url), files={'file': ('average.py', SOURCE)})
            assert response.status_code == 202
            task_id = response.json()['id']
            root = client.app.state.manager.get(task_id).root
            wait_until(client, task_id, lambda _: (root / 'result/runtime/active.pid').exists())
            pid = int((root / 'result/runtime/active.pid').read_text())
            assert client.get(f'/api/tasks/{task_id}/download').status_code == 409
            assert client.post('/api/tasks', headers=auth, data=form(url),
                               files={'file': ('second.py', SOURCE)}).status_code == 409
            assert client.post(f'/api/tasks/{task_id}/cancel', headers=auth).status_code == 200
            data = wait_until(client, task_id, lambda t: t['status'] == 'cancelled')
            assert KEY not in json.dumps(data)
            stat = Path(f'/proc/{pid}/stat')
            assert not stat.exists() or stat.read_text().split()[2] == 'Z'


@pytest.mark.skipif(os.name != 'posix', reason='Real local worker failure')
def test_model_failure_is_reported_without_exposing_credentials(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post('/api/tasks', headers=headers(client), data=form(), files={'file': ('average.py', SOURCE)})
        assert response.status_code == 202
        data = wait_until(client, response.json()['id'], lambda x: x['status'] == 'failed')
        assert data['result']['failure_category'] == 'connection_error'
        assert KEY not in json.dumps(data)
        assert data['result']['success'] is False
