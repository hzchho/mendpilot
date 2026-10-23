"""Real local commands: failed final checks must be actionable, not accepted."""
import json
import subprocess
import sys
import shlex
import os
from pathlib import Path
import pytest
from minisweagent.exceptions import Submitted
from mendpilot.environments.local import LocalRepairEnvironment
from mendpilot.core.verification import VerificationEnvironment

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Local shell requires POSIX')


@pytest.mark.parametrize('marker', [False, True])
def test_submission_replays_latest_test_and_returns_failure(tmp_path, marker):
    (tmp_path / 'source').mkdir()
    (tmp_path / 'source/app.py').write_text('value = 1\n')
    env = LocalRepairEnvironment(tmp_path)
    setup = env.execute({'command': 'git init -q && git add . && git -c user.name=Test -c user.email=test@example.invalid commit -qm input && git rev-parse HEAD'})
    assert setup['returncode'] == 0
    gate = VerificationEnvironment(env, setup['output'].strip(), root=str(env.workspace), **env.tool_options)
    script = env.runtime / 'repro.py'
    script.write_text('from pathlib import Path\nassert Path("app.py").read_text().strip() == "value = 2"\n')
    (env.workspace / 'app.py').write_text('value = 2\n')
    assert gate.execute({'command': shlex.quote(sys.executable) + ' ' + shlex.quote(str(script))})['returncode'] == 0
    (env.workspace / 'app.py').write_text('value = 3\n')
    submit = 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && git diff --binary' if marker else gate.tool_command + ' submit'
    failed = gate.execute({'command': submit})
    assert failed['returncode'] != 0 and 'AUTOMATIC VERIFICATION FAILED' in failed['output']
    (env.workspace / 'app.py').write_text('value = 2\n')
    with pytest.raises(Submitted) as caught:
        gate.execute({'command': submit})
    assert '+value = 2' in caught.value.messages[0]['extra']['submission']
    assert [e['returncode'] for e in gate.events if e['event'] == 'automatic_post_test'] == [1, 0]


def test_tools_submit_nonempty_patch_and_leave_replay_to_runner(tmp_path):
    (tmp_path / 'source').mkdir()
    (tmp_path / 'source/app.py').write_text('value = 1\n')
    env = LocalRepairEnvironment(tmp_path)
    base = env.execute({'command': 'git init -q && git add . && git -c user.name=Test -c user.email=test@example.invalid commit -qm input && git rev-parse HEAD'})['output'].strip()
    gate = VerificationEnvironment(env, base, root=str(env.workspace), **env.tool_options)
    assert gate.execute({'command': gate.tool_command + ' submit'})['returncode'] != 0
    (env.workspace / 'new.py').write_text('value = 2\n')
    with pytest.raises(Submitted) as caught:
        gate.execute({'command': gate.tool_command + ' submit'})
    assert 'new.py' in caught.value.messages[0]['extra']['submission']
    assert not gate.collect()['checks']
