"""Actual local processes: active interpreter, dependencies, independent state and timeout."""
import json
from pathlib import Path
import shlex
import sys
import time

from mendpilot.runner import local_repair_prompt
from mendpilot.environments.local import LocalRepairEnvironment


def environment(root):
    (root / 'source').mkdir(parents=True)
    (root / 'source/app.py').write_text('value = 1\n')
    return LocalRepairEnvironment(root)


def test_local_tools_use_current_interpreter_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('MENDPILOT_TEST_DEPENDENCY', 'current-environment')
    env = environment(tmp_path / 'run with spaces')
    command = ('python -c ' + shlex.quote('import os,sys,json; print(json.dumps([sys.executable, os.getenv("MENDPILOT_TEST_DEPENDENCY")]))'))
    result = env.execute({'command': command})
    assert result['returncode'] == 0
    assert json.loads(result['output']) == [sys.executable, 'current-environment']
    assert 'current-environment' not in json.dumps(env.serialize())
    assert 'MENDPILOT_TEST_DEPENDENCY' not in env.get_template_vars()


def test_local_timeout_terminates_the_process_group(tmp_path):
    env = environment(tmp_path / 'run')
    marker = env.runtime / 'should-not-exist'
    script = 'import time; from pathlib import Path; time.sleep(1); Path(' + repr(str(marker)) + ').write_text("late")'
    started = time.monotonic()
    result = env.execute({'command': 'python -c ' + shlex.quote(script)}, timeout=0.1)
    assert result['returncode'] == -1 and 'timed out' in result['exception_info']
    assert time.monotonic() - started < 1
    time.sleep(1.1)
    assert not marker.exists()


def test_each_run_has_its_own_state_and_prompt_paths(tmp_path):
    first = environment(tmp_path / 'one')
    second = environment(tmp_path / 'two')
    assert first.exec_env['MENDPILOT_TOOL_STATE'] != second.exec_env['MENDPILOT_TOOL_STATE']
    from types import SimpleNamespace
    gate = SimpleNamespace(tool_command=shlex.quote(sys.executable) + ' ' + shlex.quote(first.tool_options['tool_path']),
                           python_command=shlex.quote(sys.executable))
    prompt = local_repair_prompt(gate, first.runtime)
    assert gate.tool_command in prompt
    assert str(first.runtime / 'repro.py') in prompt
    assert '__MENDPILOT_RUNTIME__' not in prompt
    assert 'The container has no network' not in prompt
