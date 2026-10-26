"""Export a self-contained candidate without Git/private/runtime files."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from make_build import package_files

ROOT = Path(__file__).resolve().parents[1]


def main():
    # Read version without importing the GUI or starting an engine.
    import re
    source = (ROOT / 'client/mclink/config/settings.py').read_text('utf-8')
    version = re.search(r'^APP_VERSION = "([^"]+)"', source, re.M).group(1)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / ('v' + version + 'Full'))
    args = parser.parse_args()
    target = args.output.resolve()
    if not target.is_relative_to(ROOT / 'dist') or target == ROOT / 'dist':
        raise SystemExit('候选输出必须在本仓库 dist 的独立子目录内')
    if target.exists():
        raise SystemExit('候选目录已存在；拒绝覆盖，请审阅后指定新目录')
    subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/check_secrets.py')], check=True)
    artifacts = json.loads((ROOT / 'docs/client-artifacts.json').read_text('utf-8'))
    if artifacts['version'] != version:
        raise SystemExit('客户端构建版本与源码不一致')
    for item in artifacts['artifacts']:
        path = ROOT / item['file']
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise SystemExit('客户端构建哈希不一致')
    target.mkdir(parents=True)
    for name in package_files(str(ROOT)):
        if name in ('BUILD.txt', 'README.txt'):
            continue
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dest)
    for item in artifacts['artifacts']:
        path = ROOT / item['file']
        shutil.copy2(path, target / path.name)
        item['file'] = path.name
    (target / 'docs/client-artifacts.json').write_text(json.dumps(artifacts, indent=2) + '\n', 'utf-8')
    subprocess.run([sys.executable, '-B', str(target / 'scripts/make_build.py')], check=True)
    subprocess.run([sys.executable, '-B', str(target / 'scripts/check_deploy.py'), '--no-tests', '--strict-secrets'], check=True)
    archive = ROOT / 'dist' / ('MCLAN-v' + version + '-candidate.tar.gz')
    if archive.exists():
        raise SystemExit('候选压缩包已存在，未覆盖')
    with tarfile.open(archive, 'w:gz') as stream:
        for name in package_files(str(target)):
            stream.add(target / name, arcname=name, recursive=False)
    print('Candidate: ' + str(target))
    print('Archive: ' + str(archive))
    print('SHA256: ' + hashlib.sha256(archive.read_bytes()).hexdigest())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
