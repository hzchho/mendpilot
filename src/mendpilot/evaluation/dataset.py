"""Frozen task selection; generation reads visible fields only."""
import hashlib
import json
from pathlib import Path
import sysconfig

SPLIT_SHA256 = '5c4c8b13ca35420f301dc8d9ac95293226ae247c04742b2dfc3da71e196c388c'
VISIBLE_COLUMNS = ['instance_id', 'repo', 'base_commit', 'problem_statement']


def benchmark_dir():
    checkout = Path(__file__).resolve().parents[3] / 'benchmarks/swebench-lite'
    if checkout.is_dir():
        return checkout
    return Path(sysconfig.get_path('data')) / 'share/mendpilot/swebench-lite'


def load_plan(directory=None):
    directory = Path(directory) if directory else benchmark_dir()
    raw = (directory / 'split.json').read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    declared = (directory / 'split.sha256').read_text().split()[0]
    if digest != SPLIT_SHA256 or digest != declared:
        raise ValueError('Frozen split checksum mismatch')
    plan = json.loads(raw)
    dev = [r['instance_id'] for r in plan['dev']]
    test = [r['instance_id'] for r in plan['test']]
    if len(dev) != 8 or len(test) != 10 or len(set(dev + test)) != 18:
        raise ValueError('Expected disjoint 8 dev + 10 test tasks')
    return plan


def select_ids(split, ids=None):
    standard = [r['instance_id'] for r in load_plan()[split]]
    if ids is None:
        return standard
    if not ids or len(ids) != len(set(ids)) or not set(ids) <= set(standard):
        raise ValueError('IDs must be distinct members of the frozen split')
    if split == 'test' and ids != standard:
        raise ValueError('Formal test requires all 10 tasks in the fixed order')
    return ids


def read_instances(dataset_dir, split, ids, *, hidden=False):
    import pyarrow.parquet as pq
    root = Path(dataset_dir).expanduser().resolve()
    revision = (root / 'REVISION.txt').read_text().strip()
    if revision != load_plan()['dataset_revision']:
        raise ValueError('Dataset revision differs from frozen split')
    paths = sorted((root / 'data').glob(f'{split}-*.parquet'))
    if not paths:
        raise ValueError('No parquet shards found for ' + split)
    rows = []
    for path in paths:
        rows.extend(pq.read_table(path, columns=None if hidden else VISIBLE_COLUMNS).to_pylist())
    by_id = {r['instance_id']: r for r in rows}
    if not set(ids) <= by_id.keys():
        raise ValueError('Selected tasks missing from dataset')
    return [by_id[iid] for iid in ids]


def image_name(iid):
    return f"swebench/sweb.eval.x86_64.{iid.replace('__', '_1776_').lower()}:latest"
