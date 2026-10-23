"""Prepare images, generate MendPilot patches, or score them independently."""
import argparse
from pathlib import Path


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='Check or pull official task images')
    run = commands.add_parser('run', help='Generate patches using MendPilot only')
    evaluate = commands.add_parser('evaluate', help='Independent SWE-bench scoring')
    for cmd in (prepare, run):
        cmd.add_argument('--split', choices=['dev', 'test'], default='test')
        cmd.add_argument('--ids', nargs='+', help='Dev subset only; test is the full fixed 10')
    for cmd in (prepare, run, evaluate):
        cmd.add_argument('--dataset-dir', type=Path, required=True)
    prepare.add_argument('--check-only', action='store_true')
    run.add_argument('--config', type=Path, required=True, help='Same model/limits YAML as repair CLI')
    run.add_argument('--tokenizer-path', type=Path, help='Required for model-specific Chat token counting; local files only')
    run.add_argument('--output', type=Path, required=True, help='New run directory')
    evaluate.add_argument('--output', type=Path, required=True, help='Existing generation directory')
    evaluate.add_argument('--score-name', default='primary', help='New scoring attempt label; never overwrites')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command == 'prepare':
            from mendpilot.evaluation.prepare import prepare
            return prepare(args)
        if args.command == 'run':
            from mendpilot.evaluation.runner import run
            return run(args)
        from mendpilot.evaluation.scorer import evaluate
        return evaluate(args)
    except ImportError as error:
        p.exit(2, f'Evaluation dependencies unavailable ({error.name}); install mendpilot[eval].\n')
    except (ValueError, OSError) as error:
        p.exit(2, f'Evaluation input/setup error: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
