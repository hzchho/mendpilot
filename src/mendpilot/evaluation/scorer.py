"""Independent grading; this is the only consumer of hidden task fields."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import time

from mendpilot.evaluation.dataset import read_instances, select_ids, SPLIT_SHA256
from mendpilot.runner import write_json


def grade(row, patch, client, run_id, folder):
    from swebench.harness.run_evaluation import run_instance
    from swebench.harness.test_spec.test_spec import make_test_spec
    iid = row['instance_id']
    score = dict(instance_id=iid, variant='mendpilot', resolved=False,
                 patch_sha256=hashlib.sha256(patch.encode()).hexdigest())
    if not patch.strip():
        return score | dict(completed=True, reason='empty_submission')
    spec = make_test_spec(row, namespace='swebench')
    changes = []
    for index, line in enumerate(spec.eval_script_list):
        if 'python -m pip install ' in line:
            adapted = line.replace('python -m pip install ', 'python -m pip install --no-index --no-deps --no-build-isolation ')
            changes.append(dict(original=line, offline=adapted))
            spec.eval_script_list[index] = adapted
    write_json(folder / f'{iid}-offline-setup.json', changes)
    prediction = dict(instance_id=iid, model_name_or_path='mendpilot', model_patch=patch)
    started = time.monotonic()
    graded = run_instance(spec, prediction, rm_image=False, force_rebuild=False, client=client,
                          run_id=run_id, timeout=300)
    # Official releases return (instance_id, report); some installed harnesses
    # return a compact completed/resolved dictionary. Support both explicitly.
    if isinstance(graded, tuple) and len(graded) == 2:
        report = graded[1].get(iid, {})
        score.update(completed=bool(report), resolved=bool(report.get('resolved', False)))
    elif isinstance(graded, dict):
        score.update(graded)
    else:
        score.update(completed=False, reason='harness_error')
    score['elapsed_seconds'] = round(time.monotonic() - started, 2)
    return score


def evaluate(args):
    import fcntl
    import docker
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.score_name):
        raise ValueError('Invalid score-name')
    output = args.output.expanduser().resolve()
    manifest = json.loads((output / 'manifest.json').read_text())
    ids = select_ids(manifest['split'], manifest['instance_ids'])
    if manifest.get('variant') != 'mendpilot' or manifest['benchmark_sha256'] != SPLIT_SHA256:
        raise ValueError('Not a frozen MendPilot generation manifest')
    rows = read_instances(args.dataset_dir, manifest['split'], ids, hidden=True)
    root = output / 'evaluation'
    root.mkdir(exist_ok=True)
    old_cwd = Path.cwd()
    client = docker.from_env()
    try:
        with (root / '.evaluate.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            attempt = root / args.score_name
            attempt.mkdir(exist_ok=False)
            os.chdir(attempt)
            scores = []
            for row in rows:
                patch = (output / 'mendpilot' / row['instance_id'] / 'submitted.patch').read_text()
                score = grade(row, patch, client, 'mendpilot-' + args.score_name + '-' + hashlib.sha256(str(output).encode()).hexdigest()[:12], attempt)
                score['harness_version'] = importlib.metadata.version('swebench')
                score['evaluator_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                scores.append(score)
                write_json(attempt / 'scores.json', scores)
            summary = dict(tasks=len(scores), resolved=sum(bool(s.get('resolved')) for s in scores),
                           harness_errors=sum(not s.get('completed', False) for s in scores))
            write_json(attempt / 'evaluation-summary.json', summary)
            print(json.dumps(summary), flush=True)
            return int(summary['harness_errors'] > 0)
    finally:
        os.chdir(old_cwd)
        client.close()
