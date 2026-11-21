#!/bin/sh
set -eu
umask 077
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
neb_prefix="$HOME/.local"
service=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --help)
      printf '%s\n' 'Apollo.NEB Linux (Python 3.10+, no sudo)' \
        './install.sh [--service] [--prefix PATH] [--config-dir PATH] [--state-dir PATH] -- [configure options]' \
        '--service: install and start the user service; workers do not start automatically.' \
        'Examples: ./install.sh; ./install.sh --service -- --wallet YOUR_NEB_MAINNET_ADDRESS --totp' \
        'Run ./install.sh -- --help for configuration options.'
      exit 0 ;;
    --service) service=1; shift ;;
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
case "$neb_prefix" in /*) ;; *) printf 'Prefix must be absolute.\n' >&2; exit 2;; esac
case "${neb_cfg:-/}:${neb_state:-/}" in /*:/*) ;; *) printf 'Configuration and state paths must be absolute.\n' >&2; exit 2;; esac
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"; assert sys.platform == "linux"'
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
install -d -m0700 "$base"
mkdir -p -- "$neb_prefix/bin"
release=$(mktemp -d "$base/release.XXXXXXXX")
install -d -m0700 "$release/web"
install -m0600 "$src/neb.py" "$src/neb_core.py" "$src/neb_app.py" "$src/neb_history.py" "$src/neb_runtime.py" "$release/"
install -m0600 "$src/web/index.html" "$src/web/ui.js" "$src/web/ui.css" "$release/web/"
if ! python3 -I "$release/neb.py" "$@"; then
  rm -- "$release/neb.py" "$release/neb_core.py" "$release/neb_app.py" "$release/neb_history.py" "$release/neb_runtime.py" \
    "$release/web/index.html" "$release/web/ui.js" "$release/web/ui.css"
  rmdir -- "$release/web"
  # Python bytecode, if created, is disposable and inside the failed stage.
  if [ -d "$release/__pycache__" ]; then
    find "$release/__pycache__" -maxdepth 1 -name '*.pyc' -type f -delete
    rmdir -- "$release/__pycache__"
  fi
  rmdir -- "$release"
  exit 1
fi
install -m0700 "$src/apollo-neb" "$neb_prefix/bin/apollo-neb"
ln -s "$(basename "$release")" "$release/.switch"
mv -Tf -- "$release/.switch" "$base/current"
if [ "$service" -eq 1 ]; then
  unitdir="$HOME/.config/systemd/user"
  install -d "$unitdir"
  install -m0644 "$src/apollo-neb.service" "$unitdir/apollo-neb.service"
  systemctl --user daemon-reload
  systemctl --user enable --now apollo-neb.service
  systemctl --user is-active --quiet apollo-neb.service
  printf '%s\n' 'User service started. No workers were started.'
else
  printf 'Installed: %s/bin/apollo-neb\n' "$neb_prefix"
  printf '%s\n' 'Launch with apollo-neb serve, using the same config/state paths if overridden.'
fi
