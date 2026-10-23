"""Exercise the actual tool subprocesses and git worktrees, without model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

HELPER = Path(__file__).resolve().parents[1] / "src/mendpilot/tools/repair_tools.py"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "arithmetic.py").write_text("def add(a, b):\n    return a - b\n")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)
    git("init", "-q")
    git("-c", "user.name=DemoTest", "-c", "user.email=test@example.invalid", "add", ".")
    git("-c", "user.name=DemoTest", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
    env = dict(os.environ, MENDPILOT_TOOL_STATE=str(tmp_path / "state"))
    def tool(*args, data=None):
        return subprocess.run([sys.executable, str(HELPER), *map(str, args)], cwd=root,
                              env=env, input=data, text=True, capture_output=True)
    assert tool("init", "--root", root, "--base", git("rev-parse", "HEAD").decode().strip()).returncode == 0
    repro = tmp_path / "repro.py"
    repro.write_text("from arithmetic import add\nassert add(2, 3) == 5\n")
    regression = tmp_path / "regression.py"
    regression.write_text("from arithmetic import add\nassert add(-1, 1) == 0\nassert add(0, 0) == 0\n")
    return root, tool, git, repro, regression, tmp_path


def fixed_evidence(repo):
    root, tool, git, repro, regression, temp = repo
    assert tool("check", "--kind", "repro", "--baseline", "--", sys.executable, repro).returncode == 1
    edit = {"path": "arithmetic.py", "old": "return a - b", "new": "return a + b"}
    assert tool("edit", data=json.dumps(edit)).returncode == 0
    for kind, script in [("repro", repro), ("regression", regression)]:
        result = tool("check", "--kind", kind, "--cwd", root, "--", sys.executable, script)
        assert result.returncode == 0, result.stdout + result.stderr
    patch = temp / "candidate.patch"
    patch.write_bytes(git("diff", "--binary"))
    return patch


def test_full_check_uses_original_then_final_patch(repo):
    patch = fixed_evidence(repo)
    root, tool, git, repro, regression, temp = repo
    result = tool("verify", "--patch", patch)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads((temp / "state/state.json").read_text())
    assert "AssertionError" in Path(evidence["checks"][0]["log"]).read_text()
    assert evidence["submissions"][-1]["passed"]
    assert len(evidence["submissions"][-1]["results"]) == 2
    for c in evidence["checks"]:
        for sha in c["scripts"].values():
            assert (temp / "state/scripts" / (sha + ".py")).is_file()
    assert len(git("worktree", "list", "--porcelain").decode().split("worktree ")) == 2


def test_edit_rejects_syntax_error_and_ambiguous_match(repo):
    root, tool, *_ = repo
    before = (root / "arithmetic.py").read_bytes()
    bad = {"path": "arithmetic.py", "old": "return a - b", "new": "return ("}
    assert tool("edit", data=json.dumps(bad)).returncode != 0
    assert (root / "arithmetic.py").read_bytes() == before
    bad.update(old="a", new="x")
    assert tool("edit", data=json.dumps(bad)).returncode != 0


def test_changed_script_invalidates_verification(repo):
    patch = fixed_evidence(repo)
    _, tool, _, repro, *_ = repo
    repro.write_text("assert True\n")
    result = tool("verify", "--patch", patch)
    assert result.returncode != 0 and "same issue-specific check" in result.stderr


def test_changed_source_requires_recheck(repo):
    patch = fixed_evidence(repo)
    root, tool, *_ = repo
    (root / "arithmetic.py").write_text("def add(a, b):\n    return a * b\n")
    assert tool("verify", "--patch", patch).returncode != 0


def test_stale_patch_rejected_even_after_current_checks(repo):
    patch = fixed_evidence(repo)
    root, tool, _, repro, regression, *_ = repo
    (root / "arithmetic.py").write_text("def add(a, b):\n    return sum([a, b])\n")
    for kind, script in [("repro", repro), ("regression", regression)]:
        assert tool("check", "--kind", kind, "--", sys.executable, script).returncode == 0
    result = tool("verify", "--patch", patch)
    assert result.returncode != 0 and "differs from tested worktree" in result.stderr


def test_failed_recheck_invalidates_earlier_success(repo):
    patch = fixed_evidence(repo)
    _, tool, _, repro, _, temp = repo
    # Same check/script/code, external state changes its result.
    marker = temp / "marker"
    marker.touch()
    repro.write_text("from pathlib import Path\nfrom arithmetic import add\nassert add(2, 3) == 5\nassert Path(%r).exists()\n" % str(marker))
    assert tool("check", "--kind", "repro", "--baseline", "--", sys.executable, repro).returncode == 1
    assert tool("check", "--kind", "repro", "--", sys.executable, repro).returncode == 0
    marker.unlink()
    assert tool("check", "--kind", "repro", "--", sys.executable, repro).returncode == 1
    assert tool("verify", "--patch", patch).returncode != 0


def test_zero_tests_and_timeout_are_not_success(repo):
    _, tool, _, _, _, temp = repo
    zero = temp / "zero.py"
    zero.write_text("print('Ran 0 tests')\n")
    assert tool("check", "--kind", "regression", "--", sys.executable, zero).returncode == 1
    zero.write_text("print('1 skipped in 0.01s')\n")
    assert tool("check", "--kind", "regression", "--", sys.executable, zero).returncode == 1
    slow = temp / "slow.py"
    slow.write_text("import time\ntime.sleep(10)\n")
    assert tool("check", "--kind", "regression", "--timeout", "1", "--", sys.executable, slow).returncode == 1


def test_missing_dependency_is_not_reproduction_evidence(repo):
    root, tool, git, repro, regression, temp = repo
    (root / "arithmetic.py").write_text("import missing_example_dependency\ndef add(a, b):\n    return a - b\n")
    git("add", ".")
    git("-c", "user.name=DemoTest", "-c", "user.email=test@example.invalid", "commit", "-qm", "broken dependency")
    assert tool("init", "--root", root, "--base", git("rev-parse", "HEAD").decode().strip()).returncode == 0
    assert tool("check", "--kind", "repro", "--baseline", "--", sys.executable, repro).returncode == 1
    (root / "arithmetic.py").write_text("def add(a, b):\n    return a + b\n")
    for kind, script in [("repro", repro), ("regression", regression)]:
        assert tool("check", "--kind", kind, "--", sys.executable, script).returncode == 0
    patch = temp / "candidate.patch"
    patch.write_bytes(git("diff"))
    assert tool("verify", "--patch", patch).returncode != 0


def test_view_search_are_bounded_and_paths_confined(repo):
    root, tool, *_ = repo
    result = tool("view", "arithmetic.py")
    assert "1: def add" in result.stdout
    assert "arithmetic.py:1:" in tool("search", "def add").stdout
    assert tool("view", "../repro.py").returncode != 0


def test_import_error_from_the_target_behavior_is_valid_failure_evidence(repo):
    root, tool, git, repro, regression, temp = repo
    (root / "arithmetic.py").write_text("def add(a, b):\n    raise ImportError('target lookup failed')\n")
    git("add", ".")
    git("-c", "user.name=DemoTest", "-c", "user.email=test@example.invalid", "commit", "-qm", "lookup bug")
    assert tool("init", "--root", root, "--base", git("rev-parse", "HEAD").decode().strip()).returncode == 0
    baseline = tool("check", "--kind", "repro", "--baseline", "--", sys.executable, repro)
    assert baseline.returncode == 1 and "ImportError: target lookup failed" in baseline.stdout
    (root / "arithmetic.py").write_text("def add(a, b):\n    return a + b\n")
    for kind, script in [("repro", repro), ("regression", regression)]:
        assert tool("check", "--kind", kind, "--", sys.executable, script).returncode == 0
    assert tool("submit").returncode == 0




def test_clean_checkout_preserves_initial_generated_runtime_without_copying_live_edits(repo):
    root, tool, git, repro, regression, temp = repo
    (root / ".gitignore").write_text("__pycache__/\n_version.py\n")
    (root / "arithmetic.py").write_text("from _version import version\ndef add(a, b):\n    return a - b\n")
    git("add", ".")
    git("-c", "user.name=DemoTest", "-c", "user.email=test@example.invalid", "commit", "-qm", "versioned package")
    (root / "_version.py").write_text("version = 'initial'\n")
    assert tool("init", "--root", root, "--base", git("rev-parse", "HEAD").decode().strip()).returncode == 0
    # The baseline must use the captured initial file, not this later live edit.
    (root / "_version.py").write_text("version = 'modified'\n")
    repro.write_text("from arithmetic import add, version\nassert version == 'initial'\nassert add(2, 3) == 5\n")
    result = tool("check", "--kind", "repro", "--baseline", "--", sys.executable, repro)
    assert result.returncode == 1
    state = json.loads((temp / "state/state.json").read_text())
    log = Path(state["checks"][-1]["log"]).read_text()
    assert not state["checks"][-1]["infrastructure_error"]
    assert "assert add(2, 3) == 5" in log and "ModuleNotFoundError" not in log
    assert state["runtime_files"][0]["path"] == "_version.py"




