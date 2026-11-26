#!/bin/sh
set -eu
umask 077
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
neb_prefix="$HOME/.local"
neb_cfg=
neb_state=
service=0
run_now=0
join_origin=
join_stdin=0
share_diagnostics=0
allow_upgrades=0
fee_notice='DevFee: 1.1% of active mining time. You can see its status in the dashboard.'
while [ "$#" -gt 0 ]; do
  case "$1" in
    --help)
      printf '%s\n' 'Apollo.NEB Linux (Python 3.10+, working pidfd, no sudo)' \
        './install.sh [--run | --service] [--join HTTPS_ORIGIN] [--share-diagnostics] [--allow-upgrades] -- [configure options]' \
        '--run: open the local dashboard now; configure wallets and GPUs in the browser.' \
        '--service: install and start the user service; workers do not start automatically.' \
        '--join: pair with a management panel using an enrollment key before starting the service.' \
        '--share-diagnostics: allow the paired panel to read system and worker events and request a worker restart.' \
        '--allow-upgrades: allow updates from the central panel with --service and --join.' \
        'To override installation paths use --prefix, --config-dir or --state-dir.' \
        "$fee_notice" \
        'Examples: ./install.sh; ./install.sh --service -- --wallet YOUR_NEB_MAINNET_ADDRESS --totp' \
        'Run ./install.sh -- --help for configuration options.'
      exit 0 ;;
    --service) service=1; shift ;;
    --run) run_now=1; shift ;;
    --join) join_origin="${2:?Missing dashboard HTTPS origin}"; shift 2 ;;
    --join-stdin) join_stdin=1; shift ;;
    --share-diagnostics) share_diagnostics=1; shift ;;
    --allow-upgrades) allow_upgrades=1; shift ;;
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
if [ "$allow_upgrades" -eq 1 ] && { [ "$service" -ne 1 ] || [ -z "$join_origin" ]; }; then
  printf '%s\n' '--allow-upgrades requires --service and --join.' >&2
  exit 2
fi
test "$share_diagnostics" -eq 0 || test -n "$join_origin" || {
  printf '%s\n' '--share-diagnostics requires --join.' >&2; exit 2;
}
case "${join_origin:-https://}" in https://*) ;; *) printf '%s\n' '--join requires HTTPS.' >&2; exit 2;; esac
if [ "$join_stdin" -eq 1 ]; then
  if [ "$service" -ne 1 ] || [ -z "$join_origin" ] ||
      ! IFS= read -r setup_password || ! IFS= read -r enrollment_key ||
      [ -z "$setup_password" ] || [ -z "$enrollment_key" ]; then
    printf '%s\n' '--join-stdin requires --service, --join and two input lines (password and enrollment key).' >&2
    exit 2
  fi
fi
printf '\n%s\n' 'APOLLO.NEB  /  LOCAL MINING' '01 / Checking Linux and installation paths'
printf '%s\n' "$fee_notice" 'Mining only begins when you start a worker.'
case "$neb_prefix" in /*) ;; *) printf 'Prefix must be absolute.\n' >&2; exit 2;; esac
case "${neb_cfg:-/}:${neb_state:-/}" in /*:/*) ;; *) printf 'Configuration and state paths must be absolute.\n' >&2; exit 2;; esac
python3 -I -c '
import os, sys
if sys.version_info < (3, 10) or sys.platform != "linux":
    sys.exit("Linux with Python 3.10+ is required.")
sys.path.insert(0, sys.argv[1])
from neb_runtime import Refusal, _parent_pidfd
try:
    os.close(_parent_pidfd())
except Refusal as error:
    sys.exit(str(error))
' "$src"
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
modules='neb.py neb_core.py neb_app.py neb_history.py neb_runtime.py neb_setup.py neb_release.py neb_fleet.py neb_devfee.py neb_network.py neb_overview.py neb_platform.py neb_node_update.py neb_update_apply.py neb_install_check.py neb_remote_install.py neb_remote_bootstrap.py neb_atlas.py developer-support.json'
assets='index.html ui.js ui.css brand.svg qr.js view.js live-metrics.js recovery-codes.js history-chart.js select-menu.js'
for f in $modules; do install -m0600 "$src/$f" "$release/$f"; done
for f in $assets; do install -m0600 "$src/web/$f" "$release/web/$f"; done
printf '%s\n' '02 / Preparing the administrator account'
configure_release() {
  if [ "$join_stdin" -eq 1 ]; then
    printf '%s\n' "$setup_password" | python3 -I "$release/neb.py" "$@" --publish-current "$neb_prefix" --password-stdin
  else
    python3 -I "$release/neb.py" "$@" --publish-current "$neb_prefix"
  fi
}
if ! configure_release "$@"; then
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
unset setup_password
if [ ! -e "$neb_prefix/bin/apollo-neb" ]; then
  install -m0700 "$src/apollo-neb" "$neb_prefix/bin/apollo-neb"
fi
if [ -n "$join_origin" ]; then
  set -- node-pair --origin "$join_origin"
  if [ "$share_diagnostics" -eq 1 ]; then set -- "$@" --share-diagnostics; fi
  if [ "$allow_upgrades" -eq 1 ]; then set -- "$@" --allow-upgrades; fi
  if [ -n "${neb_state:-}" ]; then set -- --state-dir "$neb_state" "$@"; fi
  if [ -n "${neb_cfg:-}" ]; then set -- --config-dir "$neb_cfg" "$@"; fi
  if [ "$join_stdin" -eq 1 ]; then
    printf '%s\n' "$enrollment_key" | "$neb_prefix/bin/apollo-neb" "$@" --key-stdin
    unset enrollment_key
  else
    "$neb_prefix/bin/apollo-neb" "$@"
  fi
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
