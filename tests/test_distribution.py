"""Base installation must work without optional web or evaluation packages."""
import subprocess
import sys



def test_repair_imports_and_bench_help_do_not_load_optional_dependencies():
    script = '''
import importlib.abc
import sys
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pyarrow','transformers','swebench','docker','fastapi','uvicorn'}:
            raise ImportError('optional import blocked: ' + fullname)
sys.meta_path.insert(0, BlockOptional())
import mendpilot.cli, mendpilot.runner
from mendpilot.models.factory import build_model
from mendpilot.evaluation.cli import main
try:
    main(['--help'])
except SystemExit as e:
    assert e.code == 0
'''
    subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, text=True)
