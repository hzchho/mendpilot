"""Prepare official task images without running an Agent."""
import subprocess
from mendpilot.evaluation.dataset import select_ids, image_name, read_instances


def prepare(args):
    ids = select_ids(args.split, args.ids)
    read_instances(args.dataset_dir, args.split, ids)
    for iid in ids:
        image = image_name(iid)
        found = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True)
        if found.returncode:
            if args.check_only:
                raise ValueError('Missing image: ' + image)
            subprocess.run(['docker', 'pull', image], check=True)
        print('Ready: ' + image, flush=True)
    return 0
