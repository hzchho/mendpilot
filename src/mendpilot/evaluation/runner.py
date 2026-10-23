"""MendPilot-only generation. Hidden patches/tests are never read here."""
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import time
import zipfile

import yaml
from minisweagent.config import builtin_config_dir
from minisweagent.environments.docker import DockerEnvironment
from mendpilot.core.agent import RepairAgent
from mendpilot.core.budget import BudgetEnvironment
from mendpilot.core.context import ContextWindow
from mendpilot.core.prompts import repair_prompt
from mendpilot.core.verification import VerificationEnvironment
from mendpilot.evaluation.dataset import benchmark_dir, load_plan, select_ids, read_instances, image_name, SPLIT_SHA256
from mendpilot.models.common import RuntimeSecretFilter, clean_text
from mendpilot.models.factory import build_model
from mendpilot.runner import validate_config, write_json
from mendpilot.environments.docker import execute_checked


def fingerprint():
    package = Path(__file__).resolve().parents[1]
    return {p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(package.rglob('*')) if p.is_file() and p.suffix in {'.py', '.yaml', '.html', '.css', '.js'}}


def run_instance(row, output, config, credential, tokenizer=None):
    iid = row['instance_id']
    folder = Path(output) / 'mendpilot' / iid
    folder.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    result = dict(instance_id=iid, variant='mendpilot', exit_status='SetupError', submission='')
    env = agent = gate = budget = model = window = None
    errors = []
    try:
        inspected = subprocess.run(['docker', 'image', 'inspect', image_name(iid)], check=True, capture_output=True, text=True)
        result['image_id'] = json.loads(inspected.stdout)[0]['Id']
        # Use the upstream benchmark environment, without sibling source checkouts.
        base = yaml.safe_load((builtin_config_dir / 'benchmarks/swebench.yaml').read_text())
        environment = {k: v for k, v in base['environment'].items() if k != 'environment_class'}
        environment['timeout'] = config['limits']['tool_timeout']
        env = DockerEnvironment(image=image_name(iid), **environment)
        execute_checked(env, 'git reset --hard ' + shlex.quote(row['base_commit']) + ' && git clean -fd')
        deadline = time.monotonic() + config['limits']['seconds']
        budget = BudgetEnvironment(env, deadline)
        gate = VerificationEnvironment(budget, row['base_commit'])
        templates = {k: base['model'][k] for k in ('observation_template', 'format_error_template')}
        templates['observation_template'] = templates['observation_template'].replace(
            'output.exception_info', "output.get('exception_info', '')")
        model, window = build_model(config, credential, deadline, templates=templates)
        if config['model']['api'] == 'responses':
            model.total_output_limit = 65536
        else:
            if tokenizer is None:
                raise ValueError('Chat benchmark requires a local tokenizer')
            window = ContextWindow(tokenizer, config['limits']['input_tokens'])
            model.window = window
            model.config.model_kwargs.update(temperature=0, seed=42)
            model.retry_limit = 0  # Local Chat protocol: one HTTP attempt per query
        agent_config = dict(base['agent'])
        agent_config.pop('wall_time_limit_seconds', None)
        agent_config.update(step_limit=config['limits']['steps'], cost_limit=0)
        agent_config['instance_template'] = (agent_config['instance_template'].split('## Submission', 1)[0]
            + '\n</instructions>\n' + repair_prompt() + '\n## Observed initial environment\n' + gate.initial_observation)
        agent = RepairAgent(model, gate, output_path=folder / 'trajectory.json', **agent_config)
        gate.remaining_calls = lambda: max(0, agent.config.step_limit - agent.n_calls)
        result.update(agent.run(row['problem_statement']))
    except Exception as error:
        result.update(exit_status=type(error).__name__)
        if hasattr(error, 'category'):
            result.update(failure_category=error.category, http_status=error.status)
        else:
            result['error'] = clean_text(str(error), credential)
    finally:
        if agent is not None:
            try:
                trajectory = agent.save(folder / 'trajectory.json')
                usage = []
                for message in trajectory['messages']:
                    response = message if message.get('object') == 'response' else message.get('extra', {}).get('response', {})
                    if isinstance(response, dict) and response.get('usage'):
                        usage.append(response['usage'])
                result.update(prompt_tokens=sum(u.get('prompt_tokens', u.get('input_tokens', 0)) or 0 for u in usage),
                    completion_tokens=sum(u.get('completion_tokens', u.get('output_tokens', 0)) or 0 for u in usage),
                    reasoning_tokens=sum((u.get('output_tokens_details') or {}).get('reasoning_tokens', 0) or 0 for u in usage))
            except Exception as error:
                errors.append('trajectory:' + type(error).__name__)
        if env is not None:
            try:
                env.execute({'command': 'git add -N .'}, timeout=30)
                diff = env.execute({'command': 'git diff --binary'}, timeout=30)
                (folder / 'worktree.diff').write_text(diff['output'], encoding='utf-8')
                subprocess.run([env.config.executable, 'cp', env.container_id + ':/tmp/mendpilot-state',
                                str(folder / 'verification')], check=True, capture_output=True, timeout=30)
            except Exception as error:
                errors.append('workspace:' + type(error).__name__)
            finally:
                try:
                    subprocess.run([env.config.executable, 'rm', '-f', env.container_id], check=True, capture_output=True, timeout=60)
                    env.container_id = None
                except Exception as error:
                    errors.append('cleanup:' + type(error).__name__)
        patch = result.pop('submission', '')
        (folder / 'submitted.patch').write_text(patch, encoding='utf-8')
        result.update(api_calls=agent.n_calls if agent else 0, http_attempts=getattr(model, 'http_attempts', 0),
            elapsed_seconds=round(time.monotonic() - start, 2), artifact_errors=errors,
            context_events=window.events if window else [], validation_events=gate.events if gate else [],
            control_events=getattr(agent, 'control_events', []), adapter_events=getattr(model, 'adapter_events', []),
            transport_events=getattr(model, 'transport_events', []), tool_calls=budget.tool_calls if budget else 0,
            tool_seconds=round(budget.tool_seconds, 3) if budget else 0)
        write_json(folder / 'result.json', result, credential)
        trajectory_path = folder / 'trajectory.json'
        if trajectory_path.exists():
            trajectory_path.write_text(clean_text(trajectory_path.read_text(), credential), encoding='utf-8')
    return result


def run(args):
    import getpass
    import sys
    from urllib.parse import urlsplit
    ids = select_ids(args.split, args.ids)
    rows = read_instances(args.dataset_dir, args.split, ids)
    config = validate_config(yaml.safe_load(args.config.read_text(encoding='utf-8')))
    tokenizer = None
    tokenizer_info = None
    if config['model']['api'] == 'chat_completions':
        if not args.tokenizer_path:
            raise ValueError('--tokenizer-path is required for model-specific Chat token counting')
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_path, local_files_only=True)
        tokenizer_info = {'path': str(args.tokenizer_path.resolve()), 'files_sha256': {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in args.tokenizer_path.iterdir()
            if p.is_file() and p.suffix in {'.json', '.jinja', '.model'}}}
    credential = os.environ.pop(config['model']['api_key_env'], '')
    local = urlsplit(config['model']['base_url']).hostname in {'127.0.0.1', 'localhost', '::1'}
    if not credential:
        credential = 'local' if local else (getpass.getpass('API key (runtime only): ') if sys.stdin.isatty() else sys.stdin.readline()).strip()
    if not credential:
        raise ValueError('Missing runtime credential')
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    handler = logging.FileHandler(output / 'run.log')
    handler.addFilter(RuntimeSecretFilter(credential))
    logging.getLogger().addHandler(handler)
    for name in ('httpx', 'httpcore', 'openai', 'litellm'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    if local:
        for variable in ('NO_PROXY', 'no_proxy'):
            os.environ[variable] = os.environ.get(variable, '') + ',localhost,127.0.0.1,::1'
    protocol = (benchmark_dir() / 'protocol.yaml').read_bytes()
    upstream_config = yaml.safe_load((builtin_config_dir / 'benchmarks/swebench.yaml').read_text())
    manifest = dict(dataset=load_plan()['dataset'], dataset_revision=load_plan()['dataset_revision'],
        split=args.split, instance_ids=ids, variant='mendpilot', config=config, tokenizer=tokenizer_info,
        benchmark_sha256=SPLIT_SHA256, protocol=yaml.safe_load(protocol), protocol_sha256=hashlib.sha256(protocol).hexdigest(),
        source_sha256=fingerprint(), framework_version=importlib.metadata.version('mini-swe-agent'),
        upstream_benchmark_config=upstream_config,
        created_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
    write_json(output / 'manifest.json', manifest, credential)
    package = Path(__file__).resolve().parents[1]
    with zipfile.ZipFile(output / 'source.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in manifest['source_sha256']:
            archive.write(package / name, 'src/mendpilot/' + name)
        for name in ('split.json', 'split.sha256', 'protocol.yaml'):
            archive.write(benchmark_dir() / name, 'benchmarks/swebench-lite/' + name)
    results = []
    try:
        for row in rows:
            results.append(run_instance(row, output, config, credential, tokenizer))
            write_json(output / 'results.json', results, credential)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    return int(any(r['exit_status'] != 'Submitted' or r['artifact_errors'] for r in results))
