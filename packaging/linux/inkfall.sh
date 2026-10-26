#!/bin/sh
# Inkfall launcher for the standalone Linux distribution.
#
# Resolves the directory the archive was extracted to, so the tree can
# live anywhere the user has write access (no /opt install required).
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
binary="$here/Inkfall"

if [ ! -x "$binary" ]; then
    echo "inkfall: executable not found or not executable: $binary" >&2
    exit 1
fi

exec "$binary" "$@"
