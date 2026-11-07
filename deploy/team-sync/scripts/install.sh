#!/bin/sh
# Installs a private virtual environment. Does not start or alter system services.
set -eu
if [ "$#" -ne 1 ]; then
  echo 'Usage: install.sh /absolute/new/install/directory' >&2
  exit 2
fi
case "$1" in /*) ;; *) echo 'Use an absolute installation directory' >&2; exit 2;; esac
if [ -e "$1" ]; then
  echo 'Installation directory already exists; choose a new versioned directory' >&2
  exit 2
fi
source_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
python_bin=${PYTHON:-python3}
"$python_bin" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
umask 077
"$python_bin" -m venv "$1"
"$1/bin/python" -m pip install "$source_root/backend/core[team-server]"
"$1/bin/cst" team-server serve --help >/dev/null
printf 'Installed and CLI checked: %s/bin/cst\n' "$1"
printf 'Next: configure environment, run team-server migrate, enroll verified identities, then team-server serve.\n'
