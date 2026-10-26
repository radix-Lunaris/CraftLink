#!/bin/bash
# Read-only diagnostics using the same effective configuration as rebuild.
set -uo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/scripts/deploy-common.sh"
load_deployment || exit 1
docker inspect "$RELAY_CONTAINER" --format '{{.State.Status}}'
docker logs --tail 60 "$RELAY_CONTAINER" 2>&1 | python3 "$MCLINK_HOME/scripts/redact.py"
