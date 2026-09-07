#!/usr/bin/env bash
set -Eeuo pipefail

readonly SNAPSHOT_FILE="/var/lib/aimilivpn-export/pool-snapshot.json"

if [ "$#" -ne 0 ] || [ -n "${SSH_ORIGINAL_COMMAND:-}" ]; then
    printf 'restricted snapshot reader: commands are not allowed\n' >&2
    exit 126
fi

if [ ! -f "$SNAPSHOT_FILE" ] || [ ! -r "$SNAPSHOT_FILE" ]; then
    printf 'restricted snapshot reader: snapshot unavailable\n' >&2
    exit 1
fi

exec /bin/cat "$SNAPSHOT_FILE"
