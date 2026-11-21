#!/bin/sh
set -eu
umask 077
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
neb_prefix="$HOME/.local"
neb_cfg=
neb_state=
service=0
run_now=0
fee_notice='DevFee: 1.1% of active mining time. You can see its status in the dashboard.'
while [ "$#" -gt 0 ]; do
  case "$1" in
    --help)
      printf '%s\n' 'Apollo.NEB Linux (Python 3.10+, no sudo)' \
        './install.sh [--run | --service] [--prefix PATH] [--config-dir PATH] [--state-dir PATH] -- [configure options]' \
        '--run: open the local dashboard now; configure wallets and GPUs in the browser.' \
        '--service: install and start the user service; workers do not start automatically.' \
        "$fee_notice" \
        'Examples: ./install.sh; ./install.sh --service -- --wallet YOUR_NEB_MAINNET_ADDRESS --totp' \
        'Run ./install.sh -- --help for configuration options.'
      exit 0 ;;
    --service) service=1; shift ;;
    --run) run_now=1; shift ;;
    --prefix) neb_prefix="${2:?Missing prefix}"; shift 2 ;;
    --) shift; break ;;
    --config-dir|--state-dir)
      flag=$1; value="${2:?Missing directory}"; shift 2
      case "$flag" in
        --config-dir) neb_cfg=$value ;;
        --state-dir) neb_state=$value ;;
      esac ;;
    *) printf 'Unexpected installer option: %s. Configuration options go after --.\n' "$1" >&2; exit 2 ;;
  esac
done
if [ "${1:-}" = "--help" ]; then
  exec python3 -I "$src/neb.py" configure --help
fi
test "$(id -u)" -ne 0 || { printf '%s\n' 'Run as a dedicated regular Linux user, without sudo.' >&2; exit 1; }
test "$((service+run_now))" -le 1 || { printf '%s\n' 'Choose either --run or --service.' >&2; exit 2; }
printf '\n%s\n' 'APOLLO.NEB  /  LOCAL MINING' '01 / Checking Linux and installation paths'
printf '%s\n' "$fee_notice" 'Mining only begins when you start a worker.'
case "$neb_prefix" in /*) ;; *) printf 'Prefix must be absolute.\n' >&2; exit 2;; esac
case "${neb_cfg:-/}:${neb_state:-/}" in /*:/*) ;; *) printf 'Configuration and state paths must be absolute.\n' >&2; exit 2;; esac
python3 -I -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) and sys.platform == "linux" else "Linux with Python 3.10+ is required.")'
python3 -I "$src/neb_install_check.py" "$neb_prefix" "$HOME" "$service"
if [ "$service" -eq 1 ]; then
  test "$neb_prefix" = "$HOME/.local" && test -z "${neb_cfg:-}${neb_state:-}" &&
    test -z "${XDG_CONFIG_HOME:-}${XDG_STATE_HOME:-}" || {
      printf '%s\n' '--service requires default installation paths. For a custom prefix, run serve explicitly.' >&2; exit 2;
    }
  systemctl --user show-environment >/dev/null || { printf '%s\n' 'User systemd unavailable; install without --service.' >&2; exit 1; }
  if systemctl --user is-active --quiet apollo-neb.service; then
    printf '%s\n' 'Stop your apollo-neb.service before upgrading it.' >&2; exit 1
  fi
fi
if [ -n "${neb_state:-}" ]; then set -- --state-dir "$neb_state" configure "$@"; else set -- configure "$@"; fi
if [ -n "${neb_cfg:-}" ]; then set -- --config-dir "$neb_cfg" "$@"; fi
base="$neb_prefix/lib/apollo-neb"
mkdir -p -- "$base" "$neb_prefix/bin"
release=$(mktemp -d "$base/release.XXXXXXXX")
install -d -m0700 "$release/web"
modules='neb.py neb_core.py neb_app.py neb_history.py neb_runtime.py neb_setup.py neb_wallet.py neb_release.py neb_fleet.py neb_devfee.py neb_devfee_owner.py neb_network.py developer-support.json'
assets='index.html ui.js ui.css brand.svg qr.js devfee.html devfee.js view.html view.js live-metrics.js recovery-codes.js history-chart.js select-menu.js'
for f in $modules; do install -m0600 "$src/$f" "$release/$f"; done
for f in $assets; do install -m0600 "$src/web/$f" "$release/web/$f"; done
printf '%s\n' '02 / Preparing the administrator account'
if ! python3 -I "$release/neb.py" "$@" --publish-current "$neb_prefix"; then
  for f in $modules; do rm -- "$release/$f"; done
  for f in $assets; do rm -- "$release/web/$f"; done
  rmdir -- "$release/web"
  # Python bytecode, if created, is disposable and inside the failed stage.
  if [ -d "$release/__pycache__" ]; then
    find "$release/__pycache__" -maxdepth 1 -name '*.pyc' -type f -delete
    rmdir -- "$release/__pycache__"
  fi
  rmdir -- "$release"
  exit 1
fi
if [ ! -e "$neb_prefix/bin/apollo-neb" ]; then
  install -m0700 "$src/apollo-neb" "$neb_prefix/bin/apollo-neb"
fi
printf '%s\n' '03 / Installing the dashboard'
if [ "$service" -eq 1 ]; then
  unitdir="$HOME/.config/systemd/user"
  mkdir -p -- "$unitdir"
  if [ ! -e "$unitdir/apollo-neb.service" ]; then
    install -m0644 "$src/apollo-neb.service" "$unitdir/apollo-neb.service"
  fi
  systemctl --user daemon-reload
  systemctl --user enable --now apollo-neb.service
  systemctl --user is-active --quiet apollo-neb.service
  printf '%s\n' 'User service started. No workers were started.'
elif [ "$run_now" -eq 1 ]; then
  set -- serve
  if [ -n "${neb_state:-}" ]; then set -- --state-dir "$neb_state" "$@"; fi
  if [ -n "${neb_cfg:-}" ]; then set -- --config-dir "$neb_cfg" "$@"; fi
  exec "$neb_prefix/bin/apollo-neb" "$@"
else
  printf 'Installed: %s/bin/apollo-neb\n' "$neb_prefix"
  printf '%s\n' 'Launch with apollo-neb serve, using the same config/state paths if overridden.'
fi
