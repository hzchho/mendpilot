"""Frozen selection, hidden-field separation, generation and independent scoring."""
import json
from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from mendpilot.evaluation.dataset import load_plan, select_ids, read_instances, benchmark_dir, SPLIT_SHA256

pytestmark = pytest.mark.eval


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / 'data-root'
    (root / 'data').mkdir(parents=True)
    plan = load_plan()
    (root / 'REVISION.txt').write_text(plan['dataset_revision'])
    for split in ('dev', 'test'):
        rows = [dict(instance_id=r['instance_id'], repo=r['repo'], base_commit='abc123',
                     problem_statement='Repair the public API', patch='HIDDEN_FIX',
                     test_patch='HIDDEN_TEST', FAIL_TO_PASS='["hidden-test"]') for r in plan[split]]
        pq.write_table(pa.Table.from_pylist(rows), root / 'data' / f'{split}-00000-of-00001.parquet')
    return root


def test_frozen_selection_checksum_and_no_hidden_fields(dataset, tmp_path):
    ids = select_ids('test')
    assert len(ids) == 10
    rows = read_instances(dataset, 'test', ids)
    assert set(rows[0]) == {'instance_id', 'repo', 'base_commit', 'problem_statement'}
    assert 'HIDDEN' not in json.dumps(rows)
    assert read_instances(dataset, 'test', ids, hidden=True)[0]['patch'] == 'HIDDEN_FIX'
    with pytest.raises(ValueError):
        select_ids('test', ids[:5])
    with pytest.raises(ValueError):
        select_ids('dev', [ids[0]])
    corrupt = tmp_path / 'corrupt'
    corrupt.mkdir()
    (corrupt / 'split.json').write_bytes((benchmark_dir() / 'split.json').read_bytes() + b' ')
    (corrupt / 'split.sha256').write_text(SPLIT_SHA256)
    with pytest.raises(ValueError, match='checksum'):
        load_plan(corrupt)
    (dataset / 'REVISION.txt').write_text('wrong')
    with pytest.raises(ValueError, match='revision'):
        read_instances(dataset, 'test', ids)


@pytest.mark.parametrize('style', ['official', 'compact', 'failure'])
def test_scorer_handles_official_and_installed_harness_without_changing_tests(tmp_path, monkeypatch, style):
    from mendpilot.evaluation.scorer import grade
    import swebench.harness.run_evaluation as harness
    import swebench.harness.test_spec.test_spec as specs
    spec = SimpleNamespace(eval_script_list=['python -m pip install -e .', 'pytest test_original.py'])
    row = dict(instance_id='example__repo-1', test_patch='secret test')
    monkeypatch.setattr(specs, 'make_test_spec', lambda r, **kw: spec)
    def score(received, pred, **kwargs):
        assert received is spec and pred['model_patch'] == 'PATCH'
        assert received.eval_script_list[1] == 'pytest test_original.py'
        assert '--no-build-isolation' in received.eval_script_list[0]
        assert kwargs['rm_image'] is False
        return (row['instance_id'], {row['instance_id']: {'resolved': True}}) if style == 'official' else (
            {'completed': True, 'resolved': True} if style == 'compact' else None)
    monkeypatch.setattr(harness, 'run_instance', score)
    result = grade(row, 'PATCH', object(), 'test', tmp_path)
    assert result['completed'] is (style != 'failure')
    assert result['resolved'] is (style != 'failure')
    assert (tmp_path / 'example__repo-1-offline-setup.json').is_file()


def test_independent_evaluate_restores_cwd_and_preserves_prior_attempt(dataset, tmp_path, monkeypatch):
    from mendpilot.evaluation.scorer import evaluate
    import docker
    ids = select_ids('dev')[:1]
    out = tmp_path / 'generated'
    folder = out / 'mendpilot' / ids[0]
    folder.mkdir(parents=True)
    (folder / 'submitted.patch').write_text('')
    (out / 'manifest.json').write_text(json.dumps(dict(split='dev', instance_ids=ids,
        variant='mendpilot', benchmark_sha256=SPLIT_SHA256)))
    client = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(docker, 'from_env', lambda: client)
    original = Path.cwd()
    args = SimpleNamespace(output=out, dataset_dir=dataset, score_name='primary')
    assert evaluate(args) == 0
    summary = json.loads((out / 'evaluation/primary/evaluation-summary.json').read_text())
    assert summary == {'tasks': 1, 'resolved': 0, 'harness_errors': 0}
    with pytest.raises(FileExistsError):
        evaluate(args)
    assert Path.cwd() == original


def test_run_records_portable_protocol_and_source_snapshot(dataset, tmp_path, monkeypatch):
    import yaml
    import zipfile
    import mendpilot.evaluation.runner as runner
    ids = select_ids('dev')[:1]
    config = tmp_path / 'model.yaml'
    config.write_text(yaml.safe_dump({'model': {'name': 'mock', 'base_url': 'http://localhost:1234/v1', 'api': 'responses'}}))
    monkeypatch.setenv('MENDPILOT_API_KEY', 'fake-run-only-credential')
    def generation(row, output, config, credential, tokenizer):
        assert set(row) == {'instance_id', 'repo', 'base_commit', 'problem_statement'}
        assert 'MENDPILOT_API_KEY' not in os.environ
        assert credential == 'fake-run-only-credential'
        return {'instance_id': row['instance_id'], 'exit_status': 'Submitted', 'artifact_errors': []}
    monkeypatch.setattr(runner, 'run_instance', generation)
    args = SimpleNamespace(split='dev', ids=ids, dataset_dir=dataset, config=config,
                           tokenizer_path=None, output=tmp_path / 'run-output')
    assert runner.run(args) == 0
    manifest = json.loads((args.output / 'manifest.json').read_text())
    assert manifest['variant'] == 'mendpilot' and 'variants' not in manifest
    assert manifest['benchmark_sha256'] == SPLIT_SHA256
    assert manifest['framework_version'] == '2.4.6'
    assert 'observation_template' in manifest['upstream_benchmark_config']['model']
    with zipfile.ZipFile(args.output / 'source.zip') as archive:
        assert 'src/mendpilot/core/agent.py' in archive.namelist()
        assert 'benchmarks/swebench-lite/protocol.yaml' in archive.namelist()
    assert 'fake-run-only-credential' not in (args.output / 'manifest.json').read_text()


@pytest.mark.docker
@pytest.mark.skipif(not os.environ.get('MENDPILOT_BENCH_DATASET'), reason='Set MENDPILOT_BENCH_DATASET for official-image smoke test')
def test_generation_with_real_official_image_and_mock_model(tmp_path):
    from test_cli import model_server
    from mendpilot.evaluation.runner import run_instance
    from mendpilot.runner import validate_config
    iid = select_ids('test')[0]
    row = read_instances(Path(os.environ['MENDPILOT_BENCH_DATASET']), 'test', [iid])[0]
    assert 'patch' not in row and 'test_patch' not in row
    # A synthetic visible task tests orchestration, not benchmark correctness.
    row['problem_statement'] = 'Create mendpilot_probe.py with value = 2'
    commands = ["printf 'value = 2\\n' > mendpilot_probe.py", 'python /tmp/mendpilot_tools.py submit']
    with model_server(commands) as (url, requests):
        config = validate_config(dict(model=dict(name='mock', base_url=url, api='responses'),
            limits=dict(input_tokens=24000, output_tokens=512, steps=8, seconds=120, tool_timeout=30)))
        result = run_instance(row, tmp_path, config, 'fake-benchmark-credential')
    assert result['exit_status'] == 'Submitted', result
    assert not result['artifact_errors'], result
    assert result['api_calls'] == result['http_attempts'] == 2
    folder = tmp_path / 'mendpilot' / iid
    assert 'mendpilot_probe.py' in (folder / 'submitted.patch').read_text()
    assert 'fake-benchmark-credential' not in (folder / 'trajectory.json').read_text()
    assert all('test_patch' not in json.dumps(r['body']) for r in requests)
