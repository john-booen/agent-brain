#!/bin/sh
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec "${AGENT_BRAIN_PYTHON:-python3}" -B "$root/install.py" "$@"
