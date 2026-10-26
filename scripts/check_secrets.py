"""Scan staged blobs/history, without displaying matched credential values."""
import argparse
import ast
import re
import subprocess
from pathlib import Path
from deploy_config import read_env, SECRETS

ROOT = Path(__file__).resolve().parents[1]
ASSIGN = re.compile(r'^\s*(MCLINK_DB_PASSWORD|MCLINK_ADMIN_API_TOKEN)\s*[:=]\s*(.*?)\s*$', re.M)
TOKEN = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')


def git(*args):
    return subprocess.run(['git', '-C', str(ROOT), *args], check=True, capture_output=True).stdout


def findings(name, content, known=()):
    text = content.decode('utf-8', errors='replace')
    found = []
    for secret in known:
        if secret and secret.encode() in content:
            found.append('known-deployment-credential')
    if TOKEN.search(text):
        found.append('private-key-or-provider-token')
    candidates = []
    if name.endswith('.py'):
        try:
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.keyword) and node.arg in SECRETS and isinstance(node.value, ast.Constant):
                    candidates.append((node.arg, node.value.value))
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in SECRETS:
                            candidates.append((target.id, node.value.value))
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value in SECRETS and isinstance(value, ast.Constant):
                            candidates.append((key.value, value.value))
        except SyntaxError:
            found.append('invalid-python-source')
    else:
        candidates = [(match.group(1), match.group(2)) for match in ASSIGN.finditer(text)]
    for key, candidate in candidates:
        value = str(candidate or '').strip().strip('"\'')
        if value and not value.startswith('${') and not any(tag in value.upper() for tag in ('CHANGE_ME', 'YOUR_', '<REDACTED>')):
            found.append('literal-' + key)
    return sorted(set(found))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged', action='store_true')
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--private-env', type=Path, default=ROOT.parent / 'mclan-private/.env')
    args = parser.parse_args()
    known = []
    if args.private_env.is_file():
        private = read_env(args.private_env)
        known = [private[k] for k in SECRETS if private.get(k)]
    blobs = []
    if args.history:
        for row in git('rev-list', '--objects', '--all').decode('utf-8').splitlines():
            oid, _, name = row.partition(' ')
            if git('cat-file', '-t', oid).strip() == b'blob':
                blobs.append((name or oid, git('cat-file', 'blob', oid)))
    elif args.staged:
        names = git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z').decode('utf-8').split('\0')
        for name in filter(None, names):
            blobs.append((name, git('show', ':' + name)))
    else:
        names = git('ls-files', '-c', '-o', '--exclude-standard', '-z').decode('utf-8').split('\0')
        for name in set(filter(None, names)):
            path = ROOT / name
            if path.is_file():
                blobs.append((name, path.read_bytes()))
    issues = []
    for name, content in blobs:
        rules = findings(name, content, known)
        basename = Path(name).name
        private_path = (basename.startswith('.env') and not name.endswith('.example')
                        or basename.startswith('credentials.dat') or basename == 'config.local.php'
                        or basename in ('.device_id', 'settings.json')
                        or basename.startswith('mclink_config') and basename.endswith('.json')
                        or basename == 'config.php'
                        or Path(name).suffix in ('.exe', '.pem', '.pfx', '.key'))
        if (args.staged or args.history) and private_path:
            rules.append('private-or-build-file-in-index')
        for rule in rules:
            issues.append((name, rule))
    for name, rule in sorted(set(issues)):
        print('[BLOCK] ' + name + ': ' + rule + ' (value hidden)')
    print(f'[SCAN] {len(blobs)} blobs; {len(issues)} findings; no credential values printed')
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
