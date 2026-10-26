"""Redact known deployment credentials from diagnostic output."""
import sys
from deploy_config import paths, read_env, SECRETS
try:
    values=read_env(paths()[1])
    secrets=[values[k] for k in SECRETS if values.get(k)]
except Exception:
    raise SystemExit('配置无法读取，拒绝输出未经脱敏的诊断日志')
for line in sys.stdin:
    for secret in secrets:line=line.replace(secret,'<REDACTED>')
    sys.stdout.write(line)
