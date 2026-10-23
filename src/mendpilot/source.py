"""Read a user's files into a bounded, independent source snapshot (no Git required)."""
import fnmatch
import hashlib
from pathlib import Path
import shutil


EXCLUDED = {'.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '__pycache__',
            '.pytest_cache', '.mypy_cache', '.ruff_cache', '.ssh', '.aws',
            'artifacts', 'logs', 'run', 'dist', 'build', '*.egg-info',
            '.env', '.env.*', '*.pem', '*.key', '*.pyc', '*.pyo'}


def snapshot_source(destination, *, project=None, file=None, max_bytes=50 * 1024 * 1024):
    """Copy regular files only; return paths/hashes, never alter the source tree.

    --file alone means ONE file. With --project it is a focus hint within that tree.
    .mendpilotignore uses shell globs relative to the project, or matching a basename.
    Symlinks are skipped, including links to files outside the input directory.
    """
    destination = Path(destination).resolve()
    project = Path(project).expanduser().resolve() if project else None
    file = Path(file).expanduser() if file else None
    if project is None and file is None:
        raise ValueError('Supply --project or --file')
    if project is not None and not project.is_dir():
        raise ValueError('--project must be an existing directory')
    if file is not None:
        if project is not None and not file.is_absolute():
            file = project / file
        if file.is_symlink():
            raise ValueError('--file cannot be a symbolic link')
        file = file.resolve()
        if not file.is_file():
            raise ValueError('--file must be an existing regular file')
    root = project or file.parent
    if destination == root or root in destination.parents:
        raise ValueError('--output must be outside the source project directory')
    if file is not None and not file.is_relative_to(root):
        raise ValueError('--file must be within --project')
    patterns = set(EXCLUDED)
    if project and not (root / '.mendpilotignore').is_symlink() and (root / '.mendpilotignore').is_file():
        patterns.update(line.strip().rstrip('/') for line in
                        (root / '.mendpilotignore').read_text(encoding='utf-8').splitlines()
                        if line.strip() and not line.lstrip().startswith('#'))
    destination.mkdir(parents=True, exist_ok=False)
    files, skipped = [], []
    size = 0

    def excluded(path):
        relative = path.relative_to(root).as_posix()
        return path.is_symlink() or any(fnmatch.fnmatchcase(relative, p) or
                                       fnmatch.fnmatchcase(path.name, p) for p in patterns)

    def copy(path):
        nonlocal size
        relative = path.relative_to(root).as_posix()
        if excluded(path):
            skipped.append(relative)
            return
        if not path.is_file():
            skipped.append(relative)
            return
        size += path.stat().st_size
        if size > max_bytes:
            raise ValueError('Source exceeds size limit; exclude data/weights with .mendpilotignore')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(path.stat().st_mode & 0o777)
        files.append({'path': relative, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                      'bytes': target.stat().st_size})

    def walk(folder):
        for path in sorted(folder.iterdir()):
            if excluded(path):
                skipped.append(path.relative_to(root).as_posix())
            elif path.is_dir():
                walk(path)
            else:
                copy(path)

    if project:
        walk(root)
    else:
        copy(file)
    focus = file.relative_to(root).as_posix() if file else None
    if not files or (focus and focus not in {item['path'] for item in files}):
        raise ValueError('No usable source files, or the focus file was excluded')
    return {'source_root': str(root), 'mode': 'project' if project else 'file',
            'focus_file': focus, 'files': files, 'skipped': skipped, 'total_bytes': size}
