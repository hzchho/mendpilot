import os
import subprocess
import time
import pytest
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.exceptions import Submitted
from mendpilot.core.budget import BudgetEnvironment
from mendpilot.core.verification import VerificationEnvironment
from mendpilot.core.prompts import repair_prompt
TOOL = 'python /tmp/mendpilot_tools.py'
pytestmark = [pytest.mark.docker, pytest.mark.skipif(not os.environ.get('MENDPILOT_TEST_IMAGE'), reason='Set MENDPILOT_TEST_IMAGE')]

def test_replays_latest_test_without_registration_and_keeps_budget_guard():
    env = DockerEnvironment(image=os.environ["MENDPILOT_TEST_IMAGE"], cwd="/", timeout=30,
                            interpreter=["bash", "-c"], run_args=["--rm", "--network=none", "--memory=2g", "--cpus=2"])
    try:
        result = env.execute({"command": "mkdir /testbed && cd /testbed && git init -q && git config user.email smoke@example.invalid && git config user.name Smoke && printf 'value = 1\\n' > app.py && git add . && git commit -qm initial"})
        assert result['returncode'] == 0
        env.config.cwd = '/testbed'
        base = env.execute({'command': 'git rev-parse HEAD'})['output'].strip()
        gate = VerificationEnvironment(BudgetEnvironment(env, time.monotonic()+100), base,
                                            budget_finalize_calls=4)
        remaining = [10]
        gate.remaining_calls = lambda: remaining[0]
        assert 'Registration is optional' in repair_prompt()
        assert gate.execute({'command': "printf 'from pathlib import Path\\nassert Path(\"/testbed/app.py\").read_text().strip() == \"value = 2\"\\n' > /tmp/repro.py"})['returncode'] == 0
        gate.execute({'command': "cd /testbed && printf 'value = 2\\n' > app.py"})
        test_result = gate.execute({'command': 'cd /testbed && PYTHONPATH=/testbed python /tmp/repro.py'})
        assert test_result['returncode'] == 0, test_result
        remaining[0] = 3
        blocked = gate.execute({'command': 'grep -R value -n .'})
        assert blocked['returncode'] == 1 and 'BUDGET FINALIZATION' in blocked['output']
        # Reproduce the former call-26 failure using the REAL upstream template.
        from jinja2 import Template, StrictUndefined
        from minisweagent.config import builtin_config_dir
        import yaml
        template = yaml.safe_load((builtin_config_dir / 'benchmarks/swebench.yaml').read_text())['model']['observation_template']
        assert 'BUDGET FINALIZATION' in Template(template, undefined=StrictUndefined).render(output=blocked)
        with pytest.raises(Submitted) as completed:
            gate.execute({'command': TOOL + ' submit'})
        assert '+value = 2' in completed.value.messages[0]['extra']['submission']
        assert any(event['event'] == 'automatic_post_test' for event in gate.events)
    finally:
        subprocess.run([env.config.executable, "rm", "-f", env.container_id], check=True, capture_output=True)
        env.container_id = None