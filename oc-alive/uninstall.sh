#!/usr/bin/env bash
# Remove oc-alive, its unit, its config and its state.
set -euo pipefail

PURGE_CONFIG=1
for arg in "$@"; do
  case "$arg" in
    --keep-config) PURGE_CONFIG=0 ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

[[ ${EUID} -eq 0 ]] || { echo "must run as root" >&2; exit 1; }
step() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

step "stopping and disabling oc-alive.service"
systemctl disable --now oc-alive.service 2>/dev/null || true

step "removing unit, CLI wrapper and code"
rm -f /etc/systemd/system/oc-alive.service /usr/local/bin/oc-alive
rm -rf /opt/oc-alive
systemctl daemon-reload

if [[ $PURGE_CONFIG -eq 1 ]]; then
  step "removing config and state"
  rm -rf /etc/oc-alive /var/lib/oc-alive
else
  step "keeping /etc/oc-alive/config.json"
fi

step "removing system user"
if id -u oc-alive >/dev/null 2>&1; then
  userdel oc-alive 2>/dev/null || true
fi

step "done"
