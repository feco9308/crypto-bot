#!/usr/bin/env bash
# Own PID files only; never touches the already running legacy collector.
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
RUNTIME_DIR="$PROJECT_ROOT/data/services"
mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"
ACTION="${1:-status}"
SERVICE="${2:-all}"

run_service() {
    local service="$1" pidfile="$RUNTIME_DIR/$1.pid" pid=""
    local -a command
    case "$service" in
        scanner) command=("$PROJECT_ROOT/.venv/bin/market-scanner" scan) ;;
        paper) command=("$PROJECT_ROOT/.venv/bin/market-scanner" paper run) ;;
        web) command=("$PROJECT_ROOT/.venv/bin/gunicorn" --config "$PROJECT_ROOT/gunicorn.conf.py" 'crypto_bot.web.app:create_app()') ;;
        *) printf 'Unknown service: %s\n' "$service" >&2; exit 2 ;;
    esac
    if [[ -f "$pidfile" ]]; then read -r pid < "$pidfile"; fi
    # Match a saved PID to this checkout's executable, protecting PID reuse.
    local alive=false
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && [[ -r "/proc/$pid/cmdline" ]]; then
        if [[ "$(readlink "/proc/$pid/cwd")" == "$PROJECT_ROOT" ]]; then
            alive=true
            for argument in "${command[@]}"; do
                if ! tr '\0' '\n' < "/proc/$pid/cmdline" | grep -Fxq -- "$argument"; then alive=false; break; fi
            done
        fi
    fi
    case "$ACTION" in
        start)
            if $alive; then printf '%s already running (PID %s)\n' "$service" "$pid"; return; fi
            setsid nohup "${command[@]}" </dev/null >>"$RUNTIME_DIR/$service.log" 2>&1 &
            pid="$!"; printf '%s\n' "$pid" > "$pidfile"
            sleep 1
            if ! kill -0 "$pid" 2>/dev/null; then printf '%s startup failed; see %s\n' "$service" "$RUNTIME_DIR/$service.log" >&2; rm -f "$pidfile"; return 1; fi
            printf '%s started (PID %s), log: %s\n' "$service" "$pid" "$RUNTIME_DIR/$service.log"
            ;;
        stop)
            if $alive; then kill -TERM "$pid"; printf '%s stopping (PID %s)\n' "$service" "$pid"; else printf '%s not running under this helper\n' "$service"; fi
            # Keep PID file until process exits; a quick start cannot overlap it.
            ;;
        status)
            if $alive; then printf '%s RUNNING PID %s\n' "$service" "$pid"; else printf '%s STOPPED (helper-managed processes only)\n' "$service"; fi
            ;;
        *) printf 'Usage: %s start|stop|status scanner|paper|web|all\n' "$0" >&2; exit 2 ;;
    esac
}
if [[ "$SERVICE" == all ]]; then
    for service in scanner paper web; do run_service "$service"; done
else run_service "$SERVICE"; fi
