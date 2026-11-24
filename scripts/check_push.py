"""Require a one-time authorization for an exact branch update."""
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REMOTES = {'https://github.com/radix-Lunaris/CraftLink.git', 'https://github.com/radix-Lunaris/CraftLink', 'git@github.com:radix-Lunaris/CraftLink.git'}
DEV_REF = 'refs/heads/dev'
EXPERIMENT_REF = 'refs/heads/feat/experimental-peer-relay'
ZERO = '0' * 40


def validate_updates(remote, updates, approval):
    if remote not in REMOTES or approval.get('status') != 'approved':
        raise ValueError('missing approval or unexpected repository')
    if len(updates) != 1 or len(updates[0]) != 4:
        raise ValueError('only one branch update is allowed')
    local_ref, commit, remote_ref, previous = updates[0]
    if remote_ref not in {DEV_REF, EXPERIMENT_REF} or local_ref != remote_ref:
        raise ValueError('unexpected branch')
    if remote_ref != approval.get('ref', DEV_REF):
        raise ValueError('branch does not match approval')
    if not re.fullmatch(r'[0-9a-f]{40}', commit) or not re.fullmatch(r'[0-9a-f]{40}', previous):
        raise ValueError('invalid commit IDs')
    if commit == ZERO or (previous == ZERO and remote_ref != EXPERIMENT_REF):
        raise ValueError('branch deletion or creation is not authorized')
    if previous == ZERO:
        base = approval.get('base', '')
        if not re.fullmatch(r'[0-9a-f]{40}', base) or base == ZERO:
            raise ValueError('experiment creation requires an exact dev base')
    if commit != approval.get('commit') or previous != approval.get('previous'):
        raise ValueError('commit changed after approval')
    return commit, previous


def main():
    try:
        updates = [line.split() for line in sys.stdin if line.strip()]
        experimental = len(updates) == 1 and len(updates[0]) == 4 and updates[0][2] == EXPERIMENT_REF
        filename = 'experiment-push-approval.json' if experimental else 'push-approval.json'
        approval_path = ROOT / 'logs' / filename
        approval = json.loads(approval_path.read_text('utf-8'))
        commit, previous = validate_updates(sys.argv[2], updates, approval)
        if previous == ZERO:
            previous = approval['base']
            dev = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse',
                                           'refs/remotes/origin/dev'], text=True).strip()
            if dev != previous:
                raise ValueError('experiment base differs from origin/dev')
        subprocess.run(['git', '-C', str(ROOT), 'merge-base', '--is-ancestor', previous, commit], check=True, capture_output=True)
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/check_secrets.py'), '--history'], cwd=ROOT)
        if result.returncode:
            raise ValueError('history privacy scan failed')
        if experimental:
            approval['status'] = 'consumed'
            approval_path.write_text(json.dumps(approval) + '\n', encoding='utf-8')
        print('Push authorized: exact branch and commit; history scan passed.')
        return 0
    except (OSError, ValueError, KeyError, IndexError, subprocess.CalledProcessError):
        print('Push blocked: a fresh exact branch authorization and clean history scan are required.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
