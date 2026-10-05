#!/usr/bin/env bash
# Administrator-only handover from user units to system units. No database changes.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    printf 'Run: sudo /home/ndvi/crypto-bot/scripts/install-production-systemd.sh\n' >&2
    exit 1
fi
ROOT=/home/ndvi/crypto-bot
UNITS=(crypto-market-scanner.service crypto-web.service crypto-paper.service)
[[ -x "$ROOT/.venv/bin/market-scanner" && -f "$ROOT/.env" ]]
systemd-analyze verify "$ROOT/deploy/crypto-market-scanner.service" "$ROOT/deploy/crypto-web.service" "$ROOT/deploy/crypto-paper.service"
# Stop and disable the existing user units before starting system units: no double scanner.
runuser -u ndvi -- env XDG_RUNTIME_DIR="/run/user/$(id -u ndvi)" systemctl --user disable --now "${UNITS[@]}"
for unit in "${UNITS[@]}"; do install -m 644 "$ROOT/deploy/$unit" "/etc/systemd/system/$unit"; done
systemctl daemon-reload
systemctl enable --now "${UNITS[@]}"
systemctl is-enabled "${UNITS[@]}"
systemctl is-active "${UNITS[@]}"
