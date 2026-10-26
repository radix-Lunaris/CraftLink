"""Export existing effective deployment values; never change database passwords."""
import argparse
from pathlib import Path
from deploy_config import (ConfigError, DB_KEYS, SECRETS, compose_model, docker_json,
                           paths, read_env, service, validate_models, write_env)


def collect(root, env, running=True, notices=None):
    pm, rm = compose_model(root / 'panel', env), compose_model(root / 'server', env)
    panel, relay = service(pm, 'mc-panel'), service(rm, 'mc-relay')
    pe, re = panel.get('environment', {}), relay.get('environment', {})
    credentials = {key: str(re.get(key, '')) for key in SECRETS}
    existing = read_env(env)
    # Legacy literals can be exported when no .env exists. An existing secret
    # declaration must agree; it must not silently override a live deployment.
    validate_models(panel, relay, {**credentials, **{k: v for k, v in existing.items() if k in SECRETS}})
    if running:
        for config in (panel, relay):
            name = config.get('container_name')
            inspected = docker_json(['inspect', name], optional=True) if name else None
            if inspected:
                live = dict(entry.split('=', 1) for entry in inspected[0]['Config'].get('Env', []) if '=' in entry)
                for key in (*DB_KEYS, *SECRETS):
                    desired = config.get('environment', {}).get(key)
                    if desired is not None and key in live and str(desired) != live[key]:
                        raise ConfigError(f'{key}：运行容器与磁盘配置冲突，未导出')
            elif notices is not None:
                notices.append('未取得旧运行容器，仅核对磁盘配置：' + str(name))
    values = {key: str(value) for key, value in re.items() if key.startswith('MCLINK_') and key not in ('MCLINK_HOME', 'MCLINK_PANEL_LOCAL_URL') and value is not None}
    values.update({key: str(value) for key, value in pe.items() if key.startswith('MCLINK_') and key not in ('MCLINK_HOME', 'MCLINK_PANEL_LOCAL_URL') and value is not None})
    values.setdefault('MCLINK_DB_PORT', '3306')
    values['MCLINK_PANEL_CONTAINER'] = panel.get('container_name', 'mc-panel')
    values['MCLINK_RELAY_CONTAINER'] = relay.get('container_name', 'mc-relay')
    pp = next(p for p in panel['ports'] if int(p['target']) == 80)
    rp = next(p for p in relay['ports'] if int(p['target']) == 19100)
    values.update(MCLINK_PANEL_BIND=pp.get('host_ip', '0.0.0.0'),
                  MCLINK_PANEL_PORT=str(pp['published']), MCLINK_RELAY_PUBLIC_PORT=str(rp['published']))
    values['MCLINK_DOCKER_NETWORK'] = rm['networks']['mclink']['name']
    values['MCLINK_STATUS_VOLUME'] = rm.get('volumes', {}).get('mc-link-status', {}).get('name', 'mc-link-status')
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-dir', required=True, type=Path)
    parser.add_argument('--env', type=Path, help='旧部署的共享配置（默认旧目录 .env）')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        root, source = paths(args.from_dir, args.env)
        notices = []
        values = collect(root, source, notices=notices)
        old = read_env(args.output)
        for key, value in values.items():
            if key in old and old[key] and old[key] != value:
                raise ConfigError(f'{key}：目标文件冲突，未覆盖')
        if not args.check:
            write_env(args.output, values)
        for notice in notices:
            print('[注意] ' + notice)
        print('[OK] 旧配置已找到且一致；' + ('检查完成，未写入' if args.check else '保值导出完成'))
        return 0
    except (ConfigError, KeyError, StopIteration, ValueError, OSError) as error:
        print('[失败] ' + (str(error) if isinstance(error, ConfigError) else '旧配置读取失败；未写入'))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
