"""Command-line interface for single-file/project repair, locally or in Docker."""
import argparse
import getpass
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from mendpilot.runner import RepairOptions, execute_repair, validate_config

CONFIG_DIR = Path(__file__).with_name('profiles')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', type=Path, help='Copy an entire project (no Git repository required)')
    p.add_argument('--file', type=Path, help='One source file, or a focus file relative to --project')
    tasks = p.add_mutually_exclusive_group(required=True)
    tasks.add_argument('--task', help='Bug description, reproduction and expected behavior')
    tasks.add_argument('--task-file', type=Path, help='UTF-8 text/Markdown problem description')
    p.add_argument('--test-command', help='Visible test command, run from the copied project root')
    p.add_argument('--config', type=Path, default=CONFIG_DIR / 'qwen.yaml')
    p.add_argument('--model', help='Override model.name in the configuration')
    p.add_argument('--base-url', help='Override model.base_url')
    p.add_argument('--api', choices=['chat_completions', 'responses'])
    p.add_argument('--image', default=None, help='Optional Docker image; omitted/empty uses the current local environment')
    p.add_argument('--output', type=Path, required=True, help='New output directory, outside the source project')
    p.add_argument('--max-source-mb', type=int, default=50)
    p.add_argument('--dry-run', action='store_true', help='Validate and snapshot files; no Docker/model/API key needed')
    return p



def load_config(args):
    import yaml
    try:
        config = yaml.safe_load(args.config.read_text(encoding='utf-8'))
    except yaml.YAMLError:
        raise ValueError('Invalid YAML configuration') from None
    return validate_config(config, {'name': args.model, 'base_url': args.base_url, 'api': args.api})


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        config = load_config(args)
        task = args.task if args.task is not None else args.task_file.read_text(encoding='utf-8')
        credential = ''
        if not args.dry_run:
            credential = os.environ.pop(config['model']['api_key_env'], '')
            local_api = urlsplit(config['model']['base_url']).hostname in {'localhost', '127.0.0.1', '::1'}
            if not credential and not local_api:
                credential = (getpass.getpass('API key (runtime only): ') if sys.stdin.isatty()
                              else sys.stdin.readline()).strip()
        options = RepairOptions(**{name: getattr(args, name) for name in RepairOptions.__dataclass_fields__})
        return execute_repair(options, config, task, credential)
    except (ValueError, OSError, TypeError) as error:
        p.exit(2, f'Input/setup error: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
