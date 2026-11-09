#!/bin/bash
# Read-only diagnostics using the same effective configuration as rebuild.
set -uo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/scripts/deploy-common.sh"
load_deployment || exit 1
docker exec "$RELAY_CONTAINER" python relay_server.py --dbcheck 2>&1 | python3 "$MCLINK_HOME/scripts/redact.py"
