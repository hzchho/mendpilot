"""Small stdlib-only ACI, copied into each repair environment (Python >= 3.6).

Inspired by SWE-agent's bounded views, unique replacements and submit review.
All checks use only issue-visible material; no benchmark answers are available.
"""
import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize

STATE = Path(os.environ.get("MENDPILOT_TOOL_STATE", "/tmp/mendpilot-state"))


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root)] + list(args), stderr=subprocess.PIPE)


def load():
    return json.loads((STATE / "state.json").read_text())


def save(state):
    STATE.mkdir(parents=True, exist_ok=True)
    temp = STATE / "state.tmp"
    temp.write_text(json.dumps(state, indent=2))
    temp.replace(STATE / "state.json")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fingerprint(root, base):
    return digest(git(root, "diff", "--binary", base))


def visible_path(name, root):
    path = (Path(root) / name).resolve()
    if path != Path(root).resolve() and Path(root).resolve() not in path.parents:
        raise ValueError("Read/edit path must stay within the repository")
    return path


def clip(text, limit=7000):
    if len(text) <= limit:
        return text
    return text[:limit // 2] + "\n[output omitted; full log saved]\n" + text[-limit // 2:]


def snapshot(root, base, patch, function):
    """Run with an isolated tracked tree, without switching the live checkout."""
    with tempfile.TemporaryDirectory(prefix="mendpilot-tree-") as name:
        tree = Path(name) / "repo"
        git(root, "worktree", "add", "--detach", "--quiet", str(tree), base)
        try:
            # Preserve generated version modules from the INITIAL environment.
            # These files are ignored by git, so a worktree otherwise loses them.
            # Never copy current agent-edited files into a baseline checkout.
            state = load()
            for runtime in state.get("runtime_files", []):
                target = tree / runtime["path"]
                frozen = STATE / "runtime" / runtime["sha256"]
                if digest(frozen.read_bytes()) != runtime["sha256"]:
                    raise ValueError("Frozen runtime file hash mismatch")
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(frozen.read_bytes())
            if patch:
                subprocess.run(["git", "-C", str(tree), "apply", "--binary", "-"],
                               input=patch, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return function(tree)
        finally:
            git(root, "worktree", "remove", "--force", str(tree))


def command_identity(argv, cwd):
    files = {}
    for arg in argv:
        # Hash script files, including /tmp reproductions. Pytest node ids use ::.
        name = arg.split("::", 1)[0]
        path = Path(name) if os.path.isabs(name) else Path(cwd) / name
        if path.is_file() and path.suffix == ".py":
            files[str(path.resolve())] = digest(path.read_bytes())
    return digest(json.dumps([argv, str(cwd), files], sort_keys=True).encode()), files


def execute_check(argv, cwd, root, tree=None, timeout=40):
    original_cwd = str(cwd)
    identity, scripts = command_identity(argv, original_cwd)
    script_store = STATE / "scripts"
    script_store.mkdir(parents=True, exist_ok=True)
    for name, sha in scripts.items():
        saved = script_store / (sha + ".py")
        if not saved.exists():
            saved.write_bytes(Path(name).read_bytes())
    run_argv = list(argv)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if tree is not None:
        cwd = str(tree / Path(cwd).resolve().relative_to(Path(root).resolve()))
        run_argv = [str(tree / Path(arg).relative_to(root))
                    if arg.startswith(str(root).rstrip("/") + "/") else arg for arg in argv]
        # Replace editable-package paths for this subprocess only.
        old = [x for x in env.get("PYTHONPATH", "").split(os.pathsep)
               if x and not x.startswith(str(root))]
        env["PYTHONPATH"] = os.pathsep.join([str(tree), str(tree / "src")] + old)
    else:
        env["PYTHONPATH"] = os.pathsep.join([str(root), str(Path(root) / "src"), env.get("PYTHONPATH", "")])
    started = time.monotonic()
    proc = subprocess.Popen(run_argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    timed_out = False
    try:
        data = proc.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGKILL)
        data = proc.communicate()[0]
    output = data.decode("utf-8", errors="replace")
    counts = [int(x) for x in re.findall(r"(\d+) passed", output)]
    counts += [int(x) for x in re.findall(r"Ran (\d+) tests?", output)]
    zero_tests = bool(re.search(r"(?:no tests ran|collected 0 items|Ran 0 tests)", output))
    skipped = [int(value) for match in re.findall(r"(\d+) (?:skipped|xfailed)|skipped=(\d+)", output)
               for value in match if value]
    if skipped and (not counts or ("Ran " in output and max(skipped) >= max(counts))):
        zero_tests = True
    infrastructure_error = bool(re.search(
        r"(?:ModuleNotFoundError: No module named|ERROR collecting|command not found|python[^\n]*: can't open file)", output))
    return {"argv": argv, "cwd": original_cwd, "identity": identity, "scripts": scripts,
            "returncode": proc.returncode, "timed_out": timed_out,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "test_count": max(counts) if counts else None, "zero_tests": zero_tests,
            "infrastructure_error": infrastructure_error,
            "output": output, "passed": proc.returncode == 0 and not timed_out and not zero_tests}


def check(args, state):
    argv = args.command
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise ValueError("Supply an executable command after --")
    if any(x in {";", "&&", "||", "|", ">", "<"} for x in argv):
        raise ValueError("Use --cwd and an argv command, without shell operators")
    cwd = visible_path(args.cwd or state["root"], state["root"])
    # Explicit checks always execute. Cached evidence is reused by submit only,
    # never to hide a later failed rerun requested by the agent.
    callback = lambda tree: execute_check(argv, cwd, state["root"], tree, args.timeout)
    result = snapshot(state["root"], state["base"], b"", callback) if args.baseline else callback(None)
    result.update(kind=args.kind, baseline=args.baseline,
                  code_hash="base" if args.baseline else fingerprint(state["root"], state["base"]))
    number = len(state["checks"])
    log = STATE / ("check-%03d.log" % number)
    log.write_text(result.pop("output"))
    result["log"] = str(log)
    state["checks"].append(result)
    save(state)
    print(json.dumps(result, indent=2))
    print(clip(log.read_text()))
    return 0 if result["passed"] else 1


def current_checks(state):
    code = fingerprint(state["root"], state["base"])
    checks = [c for c in state["checks"] if not c["baseline"] and c["passed"]
              and c["code_hash"] == code
              and command_identity(c["argv"], c["cwd"])[0] == c["identity"]]
    # Keep only the latest result for each kind/command. A later failure invalidates success.
    latest = {}
    for c in state["checks"]:
        if not c["baseline"]:
            latest[(c["kind"], c["identity"])] = c
    return [c for c in checks if latest[(c["kind"], c["identity"])] is c]


def verify(args, state):
    patch = Path(args.patch).read_bytes()
    if not patch.strip():
        raise ValueError("Empty patch; implement the source fix first")
    checks = current_checks(state)
    repros = [c for c in checks if c["kind"] == "repro"]
    regressions = [c for c in checks if c["kind"] == "regression"]
    baseline_latest = {c["identity"]: c for c in state["checks"] if c["baseline"] and c["kind"] == "repro"}
    paired = []
    for c in repros:
        b = baseline_latest.get(c["identity"])
        if b and b["returncode"] != 0 and not b["timed_out"] and not b["zero_tests"] and not b["infrastructure_error"]:
            paired.append(c)
    candidates = [(p, r) for p in reversed(paired) for r in reversed(regressions) if r["identity"] != p["identity"]]
    missing = []
    if not paired:
        missing.append("Run the same issue-specific check with --kind repro --baseline and then --kind repro; it should fail on base and pass on the fix.")
    if not candidates:
        missing.append("Run a distinct relevant existing regression test, or a separate edge-case assertion script, with --kind regression.")
    if missing:
        raise ValueError("\n".join(missing))
    chosen = candidates[0]
    cache_key = digest(json.dumps({"patch": digest(patch),
                                  "code": fingerprint(state["root"], state["base"]),
                                  "checks": chosen, "check_count": len(state["checks"]),
                                  "runtime": state.get("runtime_files", []),
                                  "timeout": args.timeout,
                                  "untracked": {name: digest((Path(state["root"]) / name).read_bytes())
                                      for name in git(state["root"], "ls-files", "--others", "--exclude-standard").decode().splitlines()
                                      if name != "patch.txt" and not name.endswith((".patch", ".pyc", ".pyo"))
                                      and (Path(state["root"]) / name).is_file()},
                                  "live_runtime": {item["path"]: digest((Path(state["root"]) / item["path"]).read_bytes())
                                      for item in state.get("runtime_files", [])}}, sort_keys=True).encode())
    if not getattr(args, "force", False):
        cached = next((s for s in reversed(state["submissions"])
                       if s.get("cache_key") == cache_key and s["passed"]), None)
        if cached:
            state.setdefault("verification_cache_hits", []).append(cache_key)
            save(state)
            print(json.dumps({"passed": True, "cached": True, "patch_sha256": digest(patch)}))
            return 0
    # Compare complete tracked trees to detect stale or partial patches, then check
    # the frozen patch in that clean worktree. Reproduction scripts stay in /tmp.
    def review(tree):
        git(tree, "add", "-N", ".")
        changed = git(state["root"], "diff", "--name-only", state["base"]).decode().splitlines()
        changed += [name for name in git(state["root"], "ls-files", "--others", "--exclude-standard").decode().splitlines()
                    if name != "patch.txt" and not name.endswith((".patch", ".pyc", ".pyo"))
                    and not any(part in {"__pycache__", ".pytest_cache"} for part in Path(name).parts)]
        patched = git(tree, "diff", "--name-only", state["base"]).decode().splitlines()
        for name in set(changed + patched):
            live, final = Path(state["root"]) / name, tree / name
            if live.exists() != final.exists() or (live.exists() and live.read_bytes() != final.read_bytes()):
                raise ValueError("Submitted patch differs from tested worktree at " + name + "; regenerate patch.txt")
        results = []
        for c in chosen:
            result = execute_check(c["argv"], c["cwd"], state["root"], tree, timeout=args.timeout)
            results.append(result)
        return results
    results = snapshot(state["root"], state["base"], patch, review)
    record = {"patch_sha256": digest(patch), "code_hash": fingerprint(state["root"], state["base"]),
              "cache_key": cache_key, "results": results, "passed": all(r["passed"] for r in results)}
    state["submissions"].append(record)
    save(state)
    print(clip(json.dumps(record, indent=2)))
    return 0 if record["passed"] else 1


def initial_runtime(root):
    """Freeze only small, ignored version modules before the agent can edit."""
    recorded = []
    names = git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").decode().split("\0")
    for name in names:
        path = Path(name)
        if not name or path.name not in {"_version.py", "_version_generated.py", "_scm_version.py"}:
            continue
        if any(part in {".venv", "venv", "site-packages", "build", "dist"} for part in path.parts):
            continue
        source = visible_path(name, root)
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 131072:
            continue
        content = source.read_bytes()
        sha = digest(content)
        target = STATE / "runtime" / sha
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        recorded.append({"path": name, "sha256": sha})
    return recorded


def submit(args, state):
    """Generate a nonempty patch; the runner owns final test replay."""
    result = subprocess.run(["git", "-C", state["root"], "diff", "--check"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        print(result.stdout.decode(errors="replace"))
        return 1
    git(state["root"], "add", "-N", ".")
    patch = git(state["root"], "diff", "--binary", state["base"])
    if not patch.strip():
        print("Empty patch; implement the source fix first")
        return 1
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "final.patch").write_bytes(patch)
    print(json.dumps({"submission_ready": True, "submission": patch.decode(errors="replace"),
                      "verification": "runner_managed"}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation")
    p = sub.add_parser("init")
    p.add_argument("--root", required=True)
    p.add_argument("--base", required=True)
    p = sub.add_parser("view")
    p.add_argument("path")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--lines", type=int, default=100)
    p = sub.add_parser("search")
    p.add_argument("text")
    p.add_argument("path", nargs="?", default=".")
    p = sub.add_parser("tests", help="Suggest visible tests using changed module/symbol names")
    p.add_argument("--path")
    sub.add_parser("edit", help="Read JSON {path, old, new} from stdin; unique replacement + syntax check")
    p = sub.add_parser("check")
    p.add_argument("--kind", choices=["repro", "regression"], required=True)
    p.add_argument("--baseline", action="store_true")
    p.add_argument("--cwd")
    p.add_argument("--timeout", type=int, default=40)
    p.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("status")
    p = sub.add_parser("verify")
    p.add_argument("--patch", required=True)
    p.add_argument("--timeout", type=int, default=40)
    p.add_argument("--force", action="store_true", help="Replay even if the same final patch was just verified")
    p = sub.add_parser("submit", help="Generate final patch for runner verification")
    p.add_argument("--timeout", type=int, default=40)
    p.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.operation == "init":
        root = str(Path(args.root).resolve())
        state = {"root": root, "base": args.base, "runtime_files": initial_runtime(root),
                 "checks": [], "submissions": [], "edits": [], "reads": []}
        save(state)
        print(json.dumps({"repository": root,
                          "top_level": sorted(p.name for p in Path(root).iterdir() if p.name != ".git")[:60],
                          "available_commands": [name for name in ["python", "git", "grep", "find", "rg", "pytest"] if shutil.which(name)],
                          "unavailable_commands": [name for name in ["rg", "pytest"] if not shutil.which(name)]}))
        return 0
    state = load()
    if args.operation == "view":
        path = visible_path(args.path, state["root"])
        lines = path.read_text().splitlines()
        start = max(1, args.start)
        if start == 1 and len(lines) > 250 and path.suffix == ".py":
            try:
                tree = ast.parse(path.read_bytes())
                outline = [(n.lineno, n.name) for n in ast.walk(tree)
                           if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))]
                print("Outline:", sorted(outline)[:60])
            except SyntaxError:
                pass
        end = min(len(lines), start + max(1, min(args.lines, 150)) - 1)
        print("%s lines %s-%s of %s" % (path, start, end, len(lines)))
        print(clip("\n".join("%d: %s" % (i + 1, lines[i]) for i in range(start - 1, end))))
        state["reads"].append({"path": str(path), "start": start, "end": end})
        save(state)
    elif args.operation == "search":
        path = visible_path(args.path, state["root"])
        result = subprocess.run(["git", "-C", state["root"], "grep", "-n", "-I", "-F", "-e", args.text,
                                 "--", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        lines = result.stdout.decode(errors="replace").splitlines()
        print(clip("\n".join(lines[:40])))
        print("%s matches; showing at most 40. Narrow the path/text if needed." % len(lines))
    elif args.operation == "tests":
        changed = [args.path] if args.path else git(state["root"], "diff", "--name-only", state["base"]).decode().splitlines()
        terms = set()
        for name in changed:
            path = visible_path(name, state["root"])
            if path.suffix != ".py" or not path.is_file():
                continue
            if path.stem not in {"__init__", "utils", "base", "core"}:
                terms.add(path.stem)
            diff = git(state["root"], "diff", "--unified=2", state["base"], "--", name).decode(errors="replace")
            terms.update(re.findall(r"\bdef ([A-Za-z_][A-Za-z_0-9]+)\(", diff))
        candidates = []
        for name in git(state["root"], "ls-files").decode().splitlines():
            path = Path(name)
            if path.suffix != ".py" or not (path.name.startswith(("test_", "unittest_")) or path.name.endswith("_test.py")):
                continue
            content = (Path(state["root"]) / name).read_text(errors="replace")
            matches = sorted(term for term in terms if re.search(r"\b" + re.escape(term) + r"\b", content))
            score = len(matches) + sum(3 for term in terms if term in name)
            if score:
                candidates.append({"path": name, "score": score, "matched_names": matches[:10]})
        print(json.dumps(sorted(candidates, key=lambda x: (-x["score"], x["path"]))[:10], indent=2))
        print("Heuristic candidates from visible repository tests only. Inspect assertions and select relevant node ids; this is not coverage proof.")
    elif args.operation == "edit":
        edit = json.load(sys.stdin)
        path = visible_path(edit["path"], state["root"])
        raw = path.read_bytes()
        encoding = tokenize.detect_encoding(iter(raw.splitlines(keepends=True)).__next__)[0] if path.suffix == ".py" else "utf-8"
        original = raw.decode(encoding)
        if not edit["old"] or original.count(edit["old"]) != 1:
            raise ValueError("Old text must match exactly once; read a narrower region first")
        revised = original.replace(edit["old"], edit["new"], 1)
        if path.suffix == ".py":
            try:
                ast.parse(original)
            except SyntaxError:
                print("Original file already has syntax errors; inspect changes manually")
            else:
                ast.parse(revised)  # Reject before writing if a new syntax error appears.
        path.write_bytes(revised.encode(encoding))
        state["edits"].append({"path": str(path), "before": digest(original.encode()), "after": digest(revised.encode())})
        save(state)
        print(clip(git(state["root"], "diff", "--", str(path)).decode(errors="replace")))
    elif args.operation == "check":
        return check(args, state)
    elif args.operation == "verify":
        return verify(args, state)
    elif args.operation == "submit":
        return submit(args, state)
    elif args.operation == "status":
        print(json.dumps({"code_hash": fingerprint(state["root"], state["base"]),
                          "current_passed_checks": current_checks(state),
                          "recent_checks": state["checks"][-6:]}, indent=2))
    else:
        parser.error("Choose view/search/edit/check/status")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, SyntaxError, OSError, subprocess.CalledProcessError) as exc:
        print("MENDPILOT TOOL: %s" % exc, file=sys.stderr)
        sys.exit(1)
