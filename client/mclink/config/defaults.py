"""Public client defaults. Never place service credentials in client builds."""
import os
from urllib.parse import urlsplit

DEFAULT_SERVER_HOST = os.environ.get('MCLINK_DEFAULT_SERVER_HOST', 'www.example.invalid')
DEFAULT_SERVER_PORT = int(os.environ.get('MCLINK_DEFAULT_SERVER_PORT', '19100'))
UPDATE_URL = os.environ.get('MCLINK_UPDATE_URL', 'https://mc.example.invalid')

def update_url(value=None):
    value = str(value or UPDATE_URL).strip()
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        return 'https://mc.example.invalid'
    return value

UPDATE_URL = update_url(UPDATE_URL)
UPDATE_SITE_TEXT = urlsplit(UPDATE_URL).netloc
