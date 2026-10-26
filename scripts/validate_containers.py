"""Isolated real Docker/MySQL/FPM/HTTP validation. Never use production credentials."""
import argparse
import hashlib
import http.cookiejar
import json
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from deploy_config import compose_model, preflight, read_env, service, write_env
from migrate_config import collect

ROOT = Path(__file__).resolve().parents[1]


def command(args, check=True, input=None):
    result = subprocess.run(args, capture_output=True, timeout=240, input=input)
    if check and result.returncode:
        raise RuntimeError('Container operation failed: ' + args[0] + ' ' + args[1])
    return result


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_for(function, timeout=120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = function()
            if value:
                return value
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise RuntimeError('Container readiness timeout')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--no-embedded-dns', action='store_true', help='仅用于未启用 iptables 的临时测试引擎')
    args = parser.parse_args()
    prefix = 'mclan-test-' + secrets.token_hex(4)
    containers = [prefix + '-db', prefix + '-relay', prefix + '-panel']
    network = prefix + '-network'
    volume = prefix + '-status'
    result = {'production_access': False}
    with tempfile.TemporaryDirectory(prefix='mclan custom home ') as temporary:
        home = Path(temporary)
        for folder in ('server', 'panel', 'scripts'):
            shutil.copytree(ROOT / folder, home / folder, ignore=shutil.ignore_patterns('__pycache__'))
        env = home / '.env'
        password = "fixture $ # ' double\" slash\\ end-" + secrets.token_hex(5)
        token = 'fixture-token-' + secrets.token_hex(20)
        login_password = 'FixtureLogin-' + secrets.token_hex(8)
        values = dict(MCLINK_HOME=str(home), MCLINK_DB_HOST=containers[0], MCLINK_DB_PORT='3308',
                      MCLINK_DB_NAME='mclink', MCLINK_DB_USER='mclink',
                      MCLINK_DB_PASSWORD=password, MCLINK_ADMIN_API_TOKEN=token,
                      MCLINK_DOCKER_NETWORK=network, MCLINK_RELAY_CONTAINER=containers[1],
                      MCLINK_PANEL_CONTAINER=containers[2], MCLINK_STATUS_VOLUME=volume,
                      MCLINK_RELAY_PUBLIC_PORT=str(available_port()),
                      MCLINK_PANEL_BIND='127.0.0.1', MCLINK_PANEL_PORT=str(available_port()),
                      MCLINK_ADMIN_API_PORT='29101',
                      MCLINK_RELAY_API_URL='http://' + containers[1] + ':29101',
                      MCLINK_PANEL_TITLE='Fixture title', MCLINK_PANEL_ICP='Fixture ICP',
                      MCLINK_BOOTSTRAP_ADMIN='fixtureadmin:' + login_password)
        # Private PHP files exist with deliberately wrong credentials, proving
        # environment overrides and FPM access rather than a lucky fallback.
        (home / 'panel/config.php').write_text("<?php return ['db'=>['host'=>'wrong','port'=>3306,'database'=>'wrong','username'=>'wrong','password'=>'wrong','charset'=>'utf8mb4'],'relay'=>['base_url'=>'http://wrong','token'=>'wrong','timeout'=>6],'panel'=>['title'=>'wrong','icp'=>'','debug'=>false,'allow_self_register'=>false]];", 'utf-8')
        (home / 'panel/config.local.php').write_text("<?php return ['panel'=>['copyright'=>'local fixture copyright']];", 'utf-8')
        probe = home / 'probe.php'
        probe.write_text('''<?php
require '/var/www/lib/common.php';
$c=config();
$dbok=(int)db()->query('SELECT 1')->fetchColumn()===1;
$request=curl_init(rtrim($c['relay']['base_url'],'/').'/stats');
curl_setopt_array($request,[CURLOPT_RETURNTRANSFER=>true,CURLOPT_HTTPHEADER=>['Authorization: Bearer '.$c['relay']['token']],CURLOPT_TIMEOUT=>5]);
$response=curl_exec($request); $status=curl_getinfo($request,CURLINFO_HTTP_CODE);
$bad=curl_init(rtrim($c['relay']['base_url'],'/').'/stats');
curl_setopt_array($bad,[CURLOPT_RETURNTRANSFER=>true,CURLOPT_TIMEOUT=>5]);
curl_exec($bad); $badstatus=curl_getinfo($bad,CURLINFO_HTTP_CODE);
header('Content-Type: application/json');
echo json_encode(['db_ok'=>$dbok,'port'=>$c['db']['port'],'db_hash'=>hash('sha256',$c['db']['password']),'token_hash'=>hash('sha256',$c['relay']['token']),'api_status'=>$status,'bad_status'=>$badstatus,'copyright'=>$c['panel']['copyright']??'','title'=>$c['panel']['title'],'hint'=>deployment_hint('bash /opt/mc-link/rebuild.sh relay')]);
''', 'utf-8')
        override = home / 'probe.json'
        override.write_text(json.dumps({'services': {'mc-panel': {'volumes': [str(probe) + ':/var/www/panel/__probe.php:ro']}}}), 'utf-8')
        try:
            write_env(env, values)
            preflight(home, env)
            # Compare Compose-decoded credentials against literal input, not a
            # second home-grown dotenv parser.
            actual = service(compose_model(home / 'panel', env), 'mc-panel')['environment']
            assert actual['MCLINK_DB_PASSWORD'] == password
            assert actual['MCLINK_ADMIN_API_TOKEN'] == token
            result['special_character_compose_roundtrip'] = 'pass'
            before = env.read_bytes()
            write_env(env, values)
            assert env.read_bytes() == before
            exported = collect(home, env, running=False)
            output = home / 'exported.env'
            write_env(output, exported)
            assert read_env(output)['MCLINK_DB_PASSWORD'] == password
            result['migration_preserves_values'] = 'pass'
            command(['docker', 'network', 'create', network])
            root_password = secrets.token_hex(20)
            command(['docker', 'run', '-d', '--name', containers[0], '--network', network,
                     '-e', 'MYSQL_ROOT_PASSWORD=' + root_password,
                     '-e', 'MYSQL_DATABASE=mclink', 'mysql:8.0', '--port=3308'])
            wait_for(lambda: command(['docker', 'exec', containers[0], 'mysqladmin',
                '-uroot', '-p' + root_password, '--port=3308', '--protocol=tcp', 'ping'], check=False).returncode == 0)
            # The upstream image entrypoint does not SQL-escape quotes in
            # MYSQL_PASSWORD. Create only this disposable fixture user via stdin.
            escaped = password.replace('\\', '\\\\').replace("'", "''")
            sql = ("CREATE USER 'mclink'@'%' IDENTIFIED BY '" + escaped + "';"
                   "GRANT ALL ON mclink.* TO 'mclink'@'%';")
            command(['docker', 'exec', '-i', containers[0], 'mysql', '-uroot',
                     '-p' + root_password, '--port=3308', '--protocol=tcp'], input=sql.encode())
            hosts = []
            if args.no_embedded_dns:
                ip = command(['docker', 'inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', containers[0]]).stdout.decode().strip()
                hosts.append(containers[0] + ':' + ip)
                result['test_engine_without_iptables_uses_extra_hosts'] = True
            for folder in ('server', 'panel'):
                override.write_text(json.dumps({'services': {
                    'mc-panel' if folder == 'panel' else 'mc-relay': {
                        **({'volumes': [str(probe) + ':/var/www/panel/__probe.php:ro']} if folder == 'panel' else {}),
                        **({'extra_hosts': hosts} if hosts else {})}}}), 'utf-8')
                cmd = ['docker', 'compose', '-p', prefix + '-' + folder, '--project-directory', str(home / folder),
                       '--env-file', str(env), '-f', str(home / folder / 'docker-compose.yml')]
                cmd += ['-f', str(override)]
                command(cmd + ['up', '-d', '--no-build'])
                if folder == 'server' and args.no_embedded_dns:
                    ip = command(['docker', 'inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', containers[1]]).stdout.decode().strip()
                    hosts.append(containers[1] + ':' + ip)
            url = 'http://127.0.0.1:' + values['MCLINK_PANEL_PORT']
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            def probe_ok():
                with opener.open(url + '/__probe.php', timeout=5) as response:
                    report = json.load(response)
                return report if report.get('db_ok') and report.get('api_status') == 200 else None
            report = wait_for(probe_ok)
            assert report['port'] == 3308 and report['bad_status'] == 401
            assert report['db_hash'] == hashlib.sha256(password.encode()).hexdigest()
            assert report['token_hash'] == hashlib.sha256(token.encode()).hexdigest()
            assert report['copyright'] == 'local fixture copyright' and report['title'] == 'Fixture title'
            assert str(home) in report['hint']
            result.update(fpm_http_database='pass', admin_api_auth='pass', custom_db_port='pass',
                          php_local_config_and_environment_priority='pass', custom_home_network_names_ports='pass')
            html = opener.open(url + '/index.php?action=login', timeout=5).read().decode('utf-8')
            csrf = re.search(r'name="csrf"\s+value="([^"]+)"', html)
            assert csrf, 'CSRF form missing'
            form = urllib.parse.urlencode({'csrf': csrf.group(1), 'action': 'login', 'username': 'fixtureadmin', 'password': login_password}).encode()
            response = opener.open(url + '/index.php?action=login', form, timeout=10)
            dashboard = response.read().decode('utf-8')
            assert 'fixtureadmin' in dashboard and 'action=logout' in dashboard
            result['http_account_login'] = 'pass'
            # Running container drift is a hard stop without changing any file.
            drift = dict(values, MCLINK_ADMIN_API_TOKEN=token + '-wrong')
            other = home / 'drift.env'
            write_env(other, drift)
            from deploy_config import ConfigError
            try:
                collect(home, other)
            except ConfigError:
                result['running_drift_blocks_migration'] = 'pass'
            else:
                raise AssertionError('live drift was not rejected')
            result['result'] = 'pass'
        finally:
            for name in reversed(containers):
                command(['docker', 'rm', '-f', '-v', name], check=False)
            command(['docker', 'volume', 'rm', volume], check=False)
            command(['docker', 'network', 'rm', network], check=False)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2) + '\n', 'utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
