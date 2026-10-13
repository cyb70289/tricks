#!/usr/bin/env bash
# oc-alive installer: copies the code to /opt/oc-alive, creates a dedicated
# system user, writes /etc/oc-alive/config.json and installs + starts the unit.
#
#   sudo ./install.sh              install and start
#   sudo ./install.sh --no-start   install without starting
#   sudo ./install.sh --force-config   overwrite an existing config.json
set -euo pipefail

PREFIX=/opt/oc-alive
CONF_DIR=/etc/oc-alive
CONF_FILE=$CONF_DIR/config.json
UNIT_FILE=/etc/systemd/system/oc-alive.service
CLI=/usr/local/bin/oc-alive
SERVICE_USER=oc-alive
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NO_START=0
FORCE_CONFIG=0
for arg in "$@"; do
  case "$arg" in
    --no-start) NO_START=1 ;;
    --force-config) FORCE_CONFIG=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || die "must run as root (sudo $0)"
command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "python3 >= 3.10 is required"
[[ -d "$SOURCE_DIR/src/oc_alive" ]] || die "src/oc_alive not found next to install.sh"

step "installing code into $PREFIX"
install -d -m 0755 "$PREFIX"
rm -rf "$PREFIX/oc_alive"
cp -a "$SOURCE_DIR/src/oc_alive" "$PREFIX/oc_alive"
install -m 0644 "$SOURCE_DIR/README.md" "$PREFIX/README.md"
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

step "creating system user $SERVICE_USER"
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo "    user already exists"
else
  useradd --system --no-create-home --home-dir /nonexistent \
          --shell /usr/sbin/nologin --comment "oc-alive idle guard" "$SERVICE_USER"
fi

step "writing configuration"
install -d -m 0755 "$CONF_DIR"
if [[ -f "$CONF_FILE" && $FORCE_CONFIG -eq 0 ]]; then
  echo "    keeping existing $CONF_FILE (use --force-config to reset)"
else
  PYTHONPATH="$PREFIX" python3 - <<'PY'
from oc_alive import config
config.save(config.load())
print("    wrote", config.config_path())
PY
fi

step "installing CLI wrapper $CLI"
install -m 0755 "$SOURCE_DIR/bin/oc-alive" "$CLI"

step "installing systemd unit"
install -m 0644 "$SOURCE_DIR/packaging/oc-alive.service" "$UNIT_FILE"
systemctl daemon-reload

if command -v systemd-analyze >/dev/null; then
  if ! systemd-analyze verify "$UNIT_FILE" 2>&1 | sed 's/^/    /'; then
    warn "systemd-analyze verify reported issues (see above)"
  fi
fi

if [[ $NO_START -eq 1 ]]; then
  step "installed (not started). Start it with: systemctl enable --now oc-alive"
  exit 0
fi

step "enabling and restarting oc-alive.service"
systemctl enable oc-alive.service
# restart (not just start): an already-running unit must pick up the new code
systemctl restart oc-alive.service

step "waiting for the first status report"
sleep 8
systemctl is-active --quiet oc-alive.service \
  || { journalctl -u oc-alive.service -n 40 --no-pager; die "service failed to start"; }

echo
"$CLI" status || true
echo
step "done. Handy commands:"
cat <<'EOF'
    oc-alive status                 # live view of what it is doing
    oc-alive keys                   # every tunable + current value
    sudo oc-alive set net.target_percent 10
    sudo oc-alive set net.enabled false
    oc-alive pause / oc-alive resume            # soft pause (needs write access)
    sudo oc-alive pause / sudo oc-alive resume
    sudo oc-alive selftest          # prove the targets are really reached
    journalctl -u oc-alive -f       # logs
    sudo ./uninstall.sh             # remove everything
EOF
