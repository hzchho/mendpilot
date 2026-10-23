"""Shared repair workflow used by the CLI and the local web worker."""
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlsplit

from mendpilot.source import snapshot_source
from mendpilot.environments.docker import docker_copy, execute_checked, export_fixed
from mendpilot.models.factory import build_model, validate_config


@dataclass
class RepairOptions:
    output: Path
    project: Path | None = None
    file: Path | None = None
    image: str | None = None
    test_command: str | None = None
    max_source_mb: int = 50
    dry_run: bool = False




def write_json(path, value, credential=''):
    from mendpilot.models.common import clean_text
    path.write_text(clean_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), credential), encoding='utf-8')






def visible_test(env, command, timeout):
    if not command:
        return {'status': 'not_requested', 'passed': None}
    started = time.monotonic()
    result = env.execute({'command': 'set -o pipefail; ' + command}, timeout=timeout)
    zero_tests = bool(re.search(r'\bRan 0 tests?\b|\bno tests ran\b|\bcollected 0 items\b', result['output']))
    if zero_tests and result['returncode'] == 0:
        result.update(original_returncode=0, returncode=1)
    return dict(result, passed=result['returncode'] == 0, command=command,
                zero_tests=zero_tests,
                elapsed_seconds=round(time.monotonic() - started, 3))






def local_repair_prompt(gate, runtime):
    """Use real, per-run paths; never map arbitrary model commands by string replacement."""
    from mendpilot.core.prompts import repair_prompt
    # Substitute placeholders first so a real workspace under /tmp is not
    # accidentally rewritten a second time when rendering the instructions.
    prompt = repair_prompt().replace('/tmp', '__MENDPILOT_RUNTIME__')
    prompt = prompt.replace('python __MENDPILOT_RUNTIME__/mendpilot_tools.py', gate.tool_command)
    for name in ('repro.py', 'regression.py'):
        prompt = prompt.replace('python __MENDPILOT_RUNTIME__/' + name,
            gate.python_command + ' ' + shlex.quote(str(runtime / name)))
    prompt = prompt.replace('__MENDPILOT_RUNTIME__', str(runtime))
    prompt = prompt.replace('The container has no network: use grep/find if rg is unavailable,',
        'Commands run in the current local Python environment; use grep/find if rg is unavailable,')
    return prompt


def run_repair(args, config, source, task, credential):
    from mendpilot.core.agent import RepairAgent
    from mendpilot.core.budget import BudgetEnvironment
    from mendpilot.core.verification import VerificationEnvironment
    from mendpilot.core.prompts import repair_prompt

    output = args.output.resolve()
    docker_mode = bool(args.image)
    workspace = '/testbed' if docker_mode else str(output / 'workspace')
    limits = config['limits']
    env = agent = gate = model = window = None
    started = time.monotonic()
    result = {'exit_status': 'SetupError', 'success': False, 'submission': '',
              'test_scope': 'user-visible tests only; not SWE-bench evaluation', 'stage': 'create_environment',
              'execution_mode': 'docker' if docker_mode else 'local'}
    try:
        print('[1/4] Preparing a project copy (' + result['execution_mode'] + ')...', flush=True)
        if docker_mode:
            from minisweagent.environments.docker import DockerEnvironment
            env = DockerEnvironment(image=args.image, cwd=workspace, timeout=limits['tool_timeout'],
                interpreter=['bash', '-c'], container_timeout=f"{limits['seconds'] + 600}s",
                run_args=['--rm', '--network=none', '--cpus=4', '--memory=8g', '--pids-limit=256'],
                env={'PYTHONDONTWRITEBYTECODE': '1'})
            execute_checked(env, 'mkdir -p /testbed')
            execute_checked(env, "python -c \"from pathlib import Path; "
                            "assert not any(Path('/testbed').iterdir()), 'Image /testbed must be empty'\"")
        else:
            from mendpilot.environments.local import LocalRepairEnvironment
            env = LocalRepairEnvironment(output, timeout=limits['tool_timeout'])
        result['stage'] = 'copy_and_initialize_source'
        if docker_mode:
            docker_copy(env, str(output / 'source') + '/.', env.container_id + ':/testbed')
        base = execute_checked(env, "git init -q && git add -f . && "
            "git -c user.name=MendPilot -c user.email=repair@example.invalid commit -qm input && git rev-parse HEAD")
        result['base_commit'] = base
        baseline = visible_test(env, args.test_command, limits['tool_timeout'])
        write_json(output / 'test-before.json', baseline, credential)
        result['stage'] = 'initialize_agent'
        deadline = time.monotonic() + limits['seconds']
        budget = BudgetEnvironment(env, deadline)
        gate = VerificationEnvironment(budget, base, root=workspace, budget_finalize_calls=4,
                                           **({} if docker_mode else env.tool_options))
        model, window = build_model(config, credential, deadline)
        window.workspace_root = workspace
        workflow = repair_prompt() if docker_mode else local_repair_prompt(gate, env.runtime)
        prompt = ('Repair the user project in ' + workspace + '. Do not weaken existing tests to hide failures. '
                  'Preserve unrelated behavior.\n\n{{task}}\n\n' + workflow
                  + '\n## Observed initial environment\n' + gate.initial_observation)
        class ConsoleRepairAgent(RepairAgent):
            def query(self):
                print(f'  Repair turn {self.n_calls + 1}/{self.config.step_limit}', flush=True)
                return super().query()
        agent = ConsoleRepairAgent(model, gate, system_template='You repair Python projects using the bash tool.',
            instance_template=prompt, step_limit=limits['steps'], cost_limit=0,
            output_path=output / 'trajectory.json')
        gate.remaining_calls = lambda: max(0, agent.config.step_limit - agent.n_calls)
        if args.test_command:
            # Explicit user checks are also replayed at submission, even if the
            # model never ran them, or later replaces the inferred latest check.
            original_finalize = gate._automatic_finalize
            def finalize(patch, **kwargs):
                checked = original_finalize(patch, **kwargs)
                if checked['returncode']:
                    return checked
                check = visible_test(budget, args.test_command, limits['tool_timeout'])
                gate.events.append({'event': 'user_test_replay', **check})
                return dict(check, output='USER TEST: ' + check.get('output', ''))
            gate._automatic_finalize = finalize
        print('[2/4] Agent is reading, editing and testing the copied code...', flush=True)
        result['stage'] = 'agent_run'
        result.update(agent.run(task))
    except Exception as error:
        result.update(exit_status=type(error).__name__, error_type=type(error).__name__)
        # Never serialize SDK exception bodies or headers.
        if hasattr(error, 'category'):
            result['failure_category'] = error.category
        elif type(error) is RuntimeError:
            result['error'] = str(error)
        elif docker_mode and result['stage'] == 'create_environment':
            result['error'] = 'Unable to create Docker container; verify Docker access and --image availability.'
    finally:
        print('[3/4] Saving patch, code and visible test results...', flush=True)
        errors = []
        if agent is not None:
            try:
                agent.save(output / 'trajectory.json')
            except Exception as error:
                errors.append('trajectory:' + type(error).__name__)
        if env is not None:
            try:
                # Include newly-created files, without producing a user commit.
                env.execute({'command': 'git add -N .'}, timeout=30)
                diff = env.execute({'command': 'git diff --binary HEAD'}, timeout=30)
                if diff['returncode']:
                    raise RuntimeError('Could not collect working diff')
                (output / 'worktree.diff').write_text(diff['output'], encoding='utf-8')
                # Frozen pre-test code, so test-generated files cannot alter the artifact.
                if docker_mode:
                    export_fixed(env, output)
                else:
                    from mendpilot.environments.local import export_local_fixed
                    export_local_fixed(env.workspace, output / 'fixed')
                final_test = visible_test(env, args.test_command, limits['tool_timeout'])
                write_json(output / 'test-after.json', final_test, credential)
                result['final_test'] = final_test
            except Exception as error:
                errors.append('workspace:' + type(error).__name__)
            finally:
                try:
                    if docker_mode:
                        # Synchronous removal also handles timeouts and interrupted runs.
                        removed = subprocess.run([env.config.executable, 'rm', '-f', env.container_id],
                            capture_output=True, timeout=60)
                        if removed.returncode:
                            errors.append('cleanup:docker_rm_failed')
                        else:
                            env.container_id = None
                    else:
                        env.cleanup()
                except Exception as error:
                    errors.append('cleanup:' + type(error).__name__)
        patch = result.pop('submission', '')
        (output / 'submitted.patch').write_text(patch, encoding='utf-8')
        result.update(api_calls=agent.n_calls if agent else 0,
            http_attempts=getattr(model, 'http_attempts', 0),
            elapsed_seconds=round(time.monotonic() - started, 3),
            context_events=window.events if window else [],
            validation_events=gate.events if gate else [],
            control_events=getattr(agent, 'control_events', []),
            transport_events=getattr(model, 'transport_events', []), artifact_errors=errors)
        result['success'] = (result['exit_status'] == 'Submitted' and bool(patch.strip())
                             and not errors and result.get('final_test', {}).get('passed') is not False)
        result['verification'] = ('visible_tests_passed' if result.get('final_test', {}).get('passed')
                                  else 'visible_tests_failed' if args.test_command else 'not_requested')
        write_json(output / 'result.json', result, credential)
        # Trajectory persistence belongs to upstream; redact runtime credentials after save.
        from mendpilot.models.common import clean_text
        trajectory = output / 'trajectory.json'
        if trajectory.exists():
            trajectory.write_text(clean_text(trajectory.read_text(encoding='utf-8'), credential), encoding='utf-8')
    print('[4/4] ' + result['exit_status'] + '; verification=' + result['verification'], flush=True)
    print('Artifacts: ' + str(output), flush=True)
    return 0 if result['success'] else 1



def execute_repair(args: RepairOptions, config, task: str, credential=''):
    """Snapshot input and run the existing Agent; never modify the source file.

    Credentials belong only to this invocation, not to serializable configuration.
    Web callers run this function in an independent child process.
    """
    config = validate_config(config)
    args.image = (args.image or '').strip() or None
    if args.max_source_mb <= 0:
        raise ValueError('--max-source-mb must be positive')
    if not task.strip():
        raise ValueError('Task description cannot be empty')
    args.output = args.output.expanduser().resolve()
    # Validate containment before creating output anywhere inside the source.
    source_root = args.project.expanduser().resolve() if args.project else (
        args.file.expanduser().resolve().parent if args.file else None)
    if source_root is None:
        raise ValueError('Supply --project or --file')
    if args.output == source_root or source_root in args.output.parents:
        raise ValueError('--output must be outside the source project directory')
    args.output.mkdir(parents=True, exist_ok=False)
    source = snapshot_source(args.output / 'source', project=args.project, file=args.file,
                             max_bytes=args.max_source_mb * 1024 * 1024)
    if source['focus_file']:
        task += '\n\nFocus file: ' + source['focus_file']
    if args.test_command:
        task += '\n\nUser verification command (from the copied project root): ' + args.test_command
    (args.output / 'task.md').write_text(task, encoding='utf-8')
    manifest = {'config': config, 'image': args.image, 'source': source,
                'test_command': args.test_command, 'dry_run': args.dry_run,
                'execution_mode': 'docker' if args.image else 'local',
                'execution': ('Docker copy; no host source mount; network disabled' if args.image
                              else 'Local code copy; commands use current user permissions and Python environment'),
                'python_executable': None if args.image else sys.executable,
                'context_count': 'o200k_base estimate +25% +8192 overhead; not model-exact'}
    (args.output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.dry_run:
        print(f"Validated {len(source['files'])} files ({source['total_bytes']} bytes).")
        print('Snapshot: ' + str(args.output / 'source'))
        return 0
    local_api = urlsplit(config['model']['base_url']).hostname in {'localhost', '127.0.0.1', '::1'}
    if not credential:
        if local_api:
            credential = 'local'
        else:
            raise ValueError('Missing runtime API key')
    # Never allow a configured key variable to reach the code executor.
    os.environ.pop(config['model']['api_key_env'], None)
    if local_api:
        for variable in ('NO_PROXY', 'no_proxy'):
            entries = [item for item in os.environ.get(variable, '').split(',') if item]
            os.environ[variable] = ','.join(dict.fromkeys(entries + ['127.0.0.1', 'localhost', '::1']))
    os.environ.setdefault('MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT', '1')
    os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    from mendpilot.models.common import RuntimeSecretFilter
    logging.basicConfig(level=logging.WARNING)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RuntimeSecretFilter(credential))
    for name in ('httpx', 'httpcore', 'openai', 'litellm', 'LiteLLM'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    return run_repair(args, config, source, task, credential)
