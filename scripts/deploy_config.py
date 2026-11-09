"""Read-only deployment validation. Never generate or rotate credentials."""
import argparse
import json
import os
from pathlib import Path
import subprocess

SECRETS = ('MCLINK_DB_PASSWORD', 'MCLINK_ADMIN_API_TOKEN')
DB_KEYS = ('MCLINK_DB_HOST', 'MCLINK_DB_PORT', 'MCLINK_DB_NAME', 'MCLINK_DB_USER')


class ConfigError(Exception):
    pass


def paths(home=None, env=None):
    root = Path(home or os.environ.get('MCLINK_HOME') or Path(__file__).resolve().parents[1]).resolve()
    source = Path(env or os.environ.get('MCLINK_ENV_FILE') or root / '.env').resolve()
    return root, source


def read_env(path):
    """Read the literal single-line values emitted by migration, including quotes.

    Compose remains authoritative for interpolation; secret values must survive
    rendering unchanged. Unsupported/multiline syntax is refused, never guessed.
    """
    result = {}
    if not Path(path).is_file():
        return result
    for number, line in enumerate(Path(path).read_text('utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        if '=' not in line:
            raise ConfigError(f'配置文件第 {number} 行格式无效')
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if value.startswith(("'", '"')):
            delimiter = value[0]
            index = 1
            while index < len(value):
                if value[index] == '\\' and index + 1 < len(value) and (delimiter == '"' or value[index + 1] == "'"):
                    index += 2
                    continue
                if value[index] == delimiter:
                    rest = value[index + 1:].strip()
                    if rest and not rest.startswith('#'):
                        raise ConfigError(f'{key}：引号后存在无效内容')
                    value = value[:index + 1]
                    break
                index += 1
        if value.startswith("'"):
            if not value.endswith("'") or len(value) < 2:
                raise ConfigError(f'{key}：不支持的多行或未闭合引号')
            value = value[1:-1].replace("\\'", "'")
        elif value.startswith('"'):
            if not value.endswith('"') or len(value) < 2:
                raise ConfigError(f'{key}：未闭合引号')
            # Docker double quoted strings support escapes and interpolation.
            try:
                value = json.loads(value).replace('$$', '$')
            except ValueError:
                raise ConfigError(f'{key}：不支持的转义，请使用迁移工具生成配置') from None
        else:
            value = value.split(' #', 1)[0].rstrip().replace('$$', '$')
        if key in result and result[key] != value:
            raise ConfigError(f'{key}：重复定义且冲突')
        result[key] = value
    return result


def quote(value):
    value = str(value)
    if '\n' in value or '\r' in value or '\x00' in value:
        raise ConfigError('不能迁移包含换行或 NUL 的配置值')
    # Compose dotenv interpolation uses $$ for a literal dollar. JSON quoting
    # handles trailing backslashes and quotes without changing the value.
    return json.dumps(value.replace('$', '$$'), ensure_ascii=False)


def write_env(path, values):
    path = Path(path)
    if path.exists():
        existing = read_env(path)
        for key, value in values.items():
            if key in existing and existing[key] and existing[key] != str(value):
                raise ConfigError(f'{key}：目标配置冲突，未覆盖')
        values = {**existing, **{k: str(v) for k, v in values.items()}}
        if values == existing:
            return
        backup = path.with_name(path.name + '.pre-migration.bak')
        if not backup.exists():
            fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.migration-tmp')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write('# Private deployment configuration. Do not commit or distribute.\n')
            for key, value in sorted(values.items()):
                stream.write(f'{key}={quote(value)}\n')
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def docker_json(args, optional=False, env=None):
    try:
        proc = subprocess.run(['docker', *args], capture_output=True, timeout=45, env=env)
    except (OSError, subprocess.TimeoutExpired):
        if optional:
            return None
        raise ConfigError('Docker Compose 不可用或超时；未执行服务变更') from None
    if proc.returncode:
        if optional:
            return None
        # Compose errors may contain expanded secrets: never forward stderr.
        raise ConfigError('Docker 配置解析失败；请检查配置路径、必填项及 Compose 版本')
    try:
        return json.loads(proc.stdout)
    except (ValueError, UnicodeError):
        raise ConfigError('Docker 未返回有效配置 JSON') from None


def compose_model(folder, env):
    args = ['compose', '--project-directory', str(folder)]
    if Path(env).is_file():
        args += ['--env-file', str(env)]
    model = docker_json([*args, '-f', str(folder / 'docker-compose.yml'), 'config', '--format', 'json'],
                        env={**os.environ, 'MCLINK_HOME': str(folder.parent)})
    # Compose config escapes dollar signs when serializing a reusable model.
    # Decode that serialization in environment values before comparisons.
    for item in model.get('services', {}).values():
        item['environment'] = {key: value.replace('$$', '$') if isinstance(value, str) else value
                               for key, value in item.get('environment', {}).items()}
    return model


def service(model, name):
    try:
        return model['services'][name]
    except KeyError:
        raise ConfigError(f'缺少服务 {name}') from None


def validate_models(panel, relay, literal):
    pe, re = panel.get('environment', {}), relay.get('environment', {})
    for key in (*DB_KEYS, *SECRETS):
        a, b = str(pe.get(key, '')), str(re.get(key, ''))
        # Old panel used the default database port implicitly.
        if key == 'MCLINK_DB_PORT':
            a, b = a or '3306', b or '3306'
        if not a or a != b:
            raise ConfigError(f'{key}：面板与中继配置缺失或冲突')
        if key in literal and literal[key] != a:
            raise ConfigError(f'{key}：共享配置与实际解析结果冲突，检查环境变量及引号')
        if key in SECRETS:
            if 'CHANGE_ME' in a.upper() or a.startswith('${'):
                raise ConfigError(f'{key}：仍为占位符')
            if key not in literal:
                raise ConfigError(f'{key}：配置文件与实际解析结果冲突，检查环境变量及引号')


def public_values(panel, relay, models):
    db = relay['environment']
    port = next(p for p in panel.get('ports', []) if int(p['target']) == 80)
    bind = port.get('host_ip', '127.0.0.1')
    bind = '127.0.0.1' if bind in ('0.0.0.0', '::') else bind
    if ':' in bind:
        bind = '[' + bind.strip('[]') + ']'
    return {
        'PANEL_CONTAINER': panel.get('container_name', 'mc-panel'),
        'RELAY_CONTAINER': relay.get('container_name', 'mc-relay'),
        'DB_CONTAINER': db.get('MCLINK_DB_HOST', 'web-mysql80'),
        'PANEL_URL': f"http://{bind}:{port['published']}/index.php?action=login",
        'RELAY_IMAGE': relay['image'], 'PANEL_IMAGE': panel['image'],
    }


def preflight(root, env):
    if not env.is_file():
        raise ConfigError('缺少共享 .env；先运行保值迁移，不要生成新密码')
    if any(root / folder in env.parents for folder in ('panel', 'server')):
        raise ConfigError('共享配置必须放在部署根目录或外部私有目录，不能放进 Docker 构建上下文')
    literal = read_env(env)
    for key in SECRETS:
        if not literal.get(key):
            raise ConfigError(f'{key}：缺失')
    pm, rm = compose_model(root / 'panel', env), compose_model(root / 'server', env)
    panel, relay = service(pm, 'mc-panel'), service(rm, 'mc-relay')
    validate_models(panel, relay, literal)
    return public_values(panel, relay, (pm, rm))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home')
    parser.add_argument('--env')
    parser.add_argument('--public', action='store_true', help='仅输出无敏感信息的部署参数')
    args = parser.parse_args()
    try:
        root, env = paths(args.home, args.env)
        public = preflight(root, env)
        if args.public:
            for key, value in public.items():
                if '\n' in str(value) or '\r' in str(value):
                    raise ConfigError('部署参数不允许换行')
                print(key + '=' + str(value))
        else:
            print('[OK] 共享配置已找到；两端凭据一致；未改变服务或数据库')
        return 0
    except (ConfigError, ValueError, KeyError, StopIteration):
        import sys
        error = sys.exc_info()[1]
        print('[失败] ' + (str(error) if isinstance(error, ConfigError) else '部署配置结构无效'))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
