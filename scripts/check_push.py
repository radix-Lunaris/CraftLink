"""Require a one-time, exact-commit authorization for a dev-only push."""
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REMOTES = {'https://github.com/radix-Lunaris/CraftLink.git', 'https://github.com/radix-Lunaris/CraftLink', 'git@github.com:radix-Lunaris/CraftLink.git'}


def validate_updates(remote, updates, approval):
    if remote not in REMOTES or approval.get('status') != 'approved':
        raise ValueError('missing approval or unexpected repository')
    if len(updates) != 1 or len(updates[0]) != 4:
        raise ValueError('only one dev update is allowed')
    local_ref, commit, remote_ref, previous = updates[0]
    if local_ref != 'refs/heads/dev' or remote_ref != 'refs/heads/dev':
        raise ValueError('only dev is authorized')
    if not re.fullmatch(r'[0-9a-f]{40}', commit) or not re.fullmatch(r'[0-9a-f]{40}', previous):
        raise ValueError('invalid commit IDs')
    if commit == '0' * 40 or previous == '0' * 40:
        raise ValueError('branch deletion or creation is not authorized')
    if commit != approval.get('commit') or previous != approval.get('previous'):
        raise ValueError('commit changed after approval')
    return commit, previous


def main():
    try:
        approval = json.loads((ROOT / 'logs/push-approval.json').read_text('utf-8'))
        updates = [line.split() for line in sys.stdin if line.strip()]
        commit, previous = validate_updates(sys.argv[2], updates, approval)
        subprocess.run(['git', '-C', str(ROOT), 'merge-base', '--is-ancestor', previous, commit], check=True, capture_output=True)
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/check_secrets.py'), '--history'], cwd=ROOT)
        if result.returncode:
            raise ValueError('history privacy scan failed')
        print('Push authorized: exact dev commit only; history scan passed.')
        return 0
    except (OSError, ValueError, KeyError, IndexError, subprocess.CalledProcessError):
        print('Push blocked: a fresh exact-commit dev authorization and clean history scan are required.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
