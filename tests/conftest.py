"""Optional suites stay optional in a base developer installation."""
import importlib.util
import os
import pytest


def pytest_ignore_collect(collection_path, config):
    if collection_path.name == 'test_web.py':
        return any(importlib.util.find_spec(name) is None for name in ('fastapi', 'multipart'))
    if collection_path.name == 'test_evaluation.py':
        return any(importlib.util.find_spec(name) is None for name in ('pyarrow', 'swebench', 'docker'))
    return False


def pytest_collection_modifyitems(items):
    if os.name == 'posix':
        return
    for item in items:
        if item.path.name in {'test_local_environment.py', 'test_web.py', 'test_evidence.py'} or 'end_to_end_repair' in item.name:
            item.add_marker(pytest.mark.skip(reason='Local shell execution requires Linux/macOS'))
