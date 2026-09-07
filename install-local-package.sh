#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
[ -f "$SOURCE_DIR/install-zane.sh" ] || {
    printf 'ERROR: install-zane.sh 不在发布包中。\n' >&2
    exit 1
}

export AIMILI_LOCAL_SOURCE_DIR="$SOURCE_DIR"
exec bash "$SOURCE_DIR/install-zane.sh"
