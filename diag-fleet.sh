#!/bin/bash
# Read-only diagnostics using the same effective configuration as rebuild.
set -uo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/scripts/deploy-common.sh"
load_deployment || exit 1
bash "$MCLINK_HOME/diag-relay.sh"
bash "$MCLINK_HOME/diag-panel.sh"
bash "$MCLINK_HOME/check-db.sh"
