"""Public defaults with an optional private, external address profile."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

PUBLIC_SERVER_HOST = 'relay.example.invalid'
PUBLIC_UPDATE_URL = 'https://updates.example.invalid'


def _profile_path():
    if 'MCLINK_CLIENT_PROFILE' in os.environ:
        value = os.environ['MCLINK_CLIENT_PROFILE'].strip()
        return Path(value).expanduser() if value else None
    if getattr(sys, 'frozen', False):
        candidates = [Path(sys.executable).resolve().parent / 'client-profile.local.json']
    else:
        root = Path(__file__).resolve().parents[3]
        candidates = [root / 'client-profile.local.json', root.parent / 'mclan-private/client-profile.local.json']
    return next((path for path in candidates if path.is_file()), None)


def _valid_update_url(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def _read_profile():
    path = _profile_path()
    if path is None:
        return {}
    try:
        if path.stat().st_size > 16384:
            raise ValueError('oversized profile')
        data = json.loads(path.read_text('utf-8-sig'))
        if not isinstance(data, dict) or set(data) - {'server_host', 'server_port', 'update_url'}:
            raise ValueError('unexpected profile fields')
        if 'server_host' in data:
            host = data['server_host']
            if not isinstance(host, str) or not host or any(c.isspace() or c in '/@\\' for c in host):
                raise ValueError('invalid host')
        if 'server_port' in data and (type(data['server_port']) is not int or not 1 <= data['server_port'] <= 65535):
            raise ValueError('invalid port')
        if 'update_url' in data and (not isinstance(data['update_url'], str) or not _valid_update_url(data['update_url'])):
            raise ValueError('invalid update URL')
        return data
    except (OSError, ValueError, TypeError):
        raise ValueError('客户端私有地址配置无效或不可读取；请检查配置文件，不会静默替换为其他服务器。') from None


_PROFILE = _read_profile()
DEFAULT_SERVER_HOST = os.environ.get('MCLINK_DEFAULT_SERVER_HOST', _PROFILE.get('server_host', PUBLIC_SERVER_HOST))
DEFAULT_SERVER_PORT = int(os.environ.get('MCLINK_DEFAULT_SERVER_PORT', _PROFILE.get('server_port', 19100)))
if not 1 <= DEFAULT_SERVER_PORT <= 65535:
    raise ValueError('客户端默认服务器端口必须为 1–65535')
_configured_url = os.environ.get('MCLINK_UPDATE_URL', _PROFILE.get('update_url', PUBLIC_UPDATE_URL))
UPDATE_URL = _configured_url if _valid_update_url(_configured_url) else PUBLIC_UPDATE_URL


def update_url(value=None):
    value = str(value or UPDATE_URL).strip()
    return value if _valid_update_url(value) else UPDATE_URL


UPDATE_SITE_TEXT = urlsplit(UPDATE_URL).netloc
