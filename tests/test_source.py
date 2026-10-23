"""Source snapshots never mutate input or silently read outside the selected tree."""
import hashlib
from pathlib import Path

import pytest

from mendpilot.source import snapshot_source


def test_single_file_does_not_copy_siblings(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'app.py').write_bytes(b'value = 1\r\n')
    (project / 'private.txt').write_text('not requested')
    result = snapshot_source(tmp_path / 'copy', file=project / 'app.py')
    assert [p['path'] for p in result['files']] == ['app.py']
    assert (tmp_path / 'copy/app.py').read_bytes() == b'value = 1\r\n'
    assert result['files'][0]['sha256'] == hashlib.sha256(b'value = 1\r\n').hexdigest()
    assert not (tmp_path / 'copy/private.txt').exists()


def test_project_copies_dirty_and_untracked_files_without_touching_git(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    for name in ['app.py', 'new.py', '.env', 'cert.pem']:
        (project / name).write_text(name)
    (project / '.git').mkdir()
    (project / '.git/index').write_bytes(b'private git index')
    (project / 'weights').mkdir()
    (project / 'weights/model.bin').write_text('large data')
    (project / '.mendpilotignore').write_text('weights/\n')
    result = snapshot_source(tmp_path / 'copy', project=project, file=Path('app.py'))
    assert result['focus_file'] == 'app.py'
    assert {p['path'] for p in result['files']} == {'app.py', 'new.py', '.mendpilotignore'}
    assert {'.git', '.env', 'cert.pem', 'weights'} <= set(result['skipped'])
    assert (project / '.git/index').read_bytes() == b'private git index'
    assert (project / 'new.py').read_text() == 'new.py'


def test_no_symlink_is_followed(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'app.py').write_text('value = 1')
    outside = tmp_path / 'secret.txt'
    outside.write_text('private')
    try:
        (project / 'linked.txt').symlink_to(outside)
        (project / '.mendpilotignore').symlink_to(outside)
    except OSError:
        pytest.skip('Symlink creation not supported')
    result = snapshot_source(tmp_path / 'copy', project=project)
    assert 'linked.txt' in result['skipped']
    assert '.mendpilotignore' in result['skipped']
    assert not (tmp_path / 'copy/linked.txt').exists()


@pytest.mark.parametrize('case', ['nested-output', 'outside-focus', 'size-limit', 'excluded-focus'])
def test_reject_unsafe_or_incomplete_inputs(tmp_path, case):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'app.py').write_text('value = 1')
    (project / '.env').write_text('dummy')
    outside = tmp_path / 'outside.py'
    outside.write_text('outside')
    destination = project / 'output' if case == 'nested-output' else tmp_path / 'copy'
    options = {'project': project}
    if case == 'outside-focus':
        options['file'] = outside
    elif case == 'size-limit':
        options['max_bytes'] = 1
    elif case == 'excluded-focus':
        options['file'] = Path('.env')
    with pytest.raises(ValueError):
        snapshot_source(destination, **options)
