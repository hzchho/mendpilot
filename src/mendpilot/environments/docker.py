import subprocess
import shlex
import tarfile

def docker_copy(env, source, target):
    subprocess.run([env.config.executable, 'cp', source, target], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

def execute_checked(env, command, timeout=60):
    result = env.execute({'command': command}, timeout=timeout)
    if result['returncode']:
        raise RuntimeError('Execution setup failed; check bash/python/git and the selected workspace. '
                           + result['output'][-1500:])
    return result['output'].strip()

def export_fixed(env, output):
    """Only regular files/directories are exported; never extract container symlinks."""
    script = '''import os, tarfile
from pathlib import Path
with tarfile.open('/tmp/repair-fixed.tar', 'w') as archive:
    for folder, dirs, files in os.walk('/testbed', followlinks=False):
        dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__', '.pytest_cache'}
                   and not Path(folder, d).is_symlink()]
        for name in files:
            p = Path(folder, name)
            if not p.is_symlink() and p.is_file():
                archive.add(str(p), arcname=str(p.relative_to('/testbed')), recursive=False)
'''
    execute_checked(env, 'python -c ' + shlex.quote(script))
    archive_path = output / 'fixed.tar'
    docker_copy(env, env.container_id + ':/tmp/repair-fixed.tar', str(archive_path))
    fixed = output / 'fixed'
    fixed.mkdir()
    with tarfile.open(archive_path) as archive:
        for member in archive:
            target = (fixed / member.name).resolve()
            if not target.is_relative_to(fixed.resolve()) or not member.isfile():
                raise ValueError('Invalid file in exported workspace')
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as src, target.open('wb') as dst:
                import shutil
                shutil.copyfileobj(src, dst)
            target.chmod(member.mode & 0o777)
    archive_path.unlink()
