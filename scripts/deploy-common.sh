#!/bin/bash
# One root and one environment file for all deployment/diagnostic entry points.
MCLINK_HOME="${MCLINK_HOME:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
MCLINK_ENV_FILE="${MCLINK_ENV_FILE:-$MCLINK_HOME/.env}"
export MCLINK_HOME MCLINK_ENV_FILE
export PYTHONUTF8=1
PANEL_DIR="$MCLINK_HOME/panel"
RELAY_DIR="$MCLINK_HOME/server"
ENV_FILE="$MCLINK_ENV_FILE"

compose_cmd () {
    docker compose --env-file "$MCLINK_ENV_FILE" "$@"
}

load_deployment () {
    local public_config
    public_config=$(python3 "$MCLINK_HOME/scripts/deploy_config.py" --home "$MCLINK_HOME" --env "$MCLINK_ENV_FILE" --public) || {
        printf '%s\n' "$public_config"
        return 1
    }
    while IFS='=' read -r key value; do
        case "$key" in
            PANEL_CONTAINER) PANEL_CONTAINER="$value" ;;
            RELAY_CONTAINER) RELAY_CONTAINER="$value" ;;
            DB_CONTAINER) DB_CONTAINER="$value" ;;
            PANEL_URL) PANEL_URL="$value" ;;
            RELAY_IMAGE) RELAY_IMAGE="$value" ;;
            PANEL_IMAGE) PANEL_IMAGE="$value" ;;
        esac
    done <<< "$public_config"
    export PANEL_CONTAINER RELAY_CONTAINER DB_CONTAINER PANEL_URL RELAY_IMAGE PANEL_IMAGE
}
