"""Isolated repair invocation; credentials arrive via stdin, never argv or files."""
import json
from pathlib import Path
import signal
import sys

from mendpilot.runner import RepairOptions, execute_repair


def main():
    payload = json.load(sys.stdin)
    credential = payload.pop('credential', '')
    # Give the runner a chance to collect artifacts and stop its active command.
    def interrupt(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    options = RepairOptions(output=Path(payload['output']), file=Path(payload['file']))
    try:
        return execute_repair(options, payload['config'], payload['task'], credential)
    except KeyboardInterrupt:
        print('Repair cancelled.', flush=True)
        return 130
    except Exception as error:
        # SDK error bodies and request payloads must not become server logs.
        print('Repair worker failed: ' + type(error).__name__, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
