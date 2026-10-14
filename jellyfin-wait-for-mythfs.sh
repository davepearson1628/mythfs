#!/bin/bash
set -u

MOUNT=/Volume1/jellyfin/mythfs

ready() {
    local fs source

    fs=$(findmnt -M "$MOUNT" -n -o FSTYPE 2>/dev/null) || return 1
    source=$(findmnt -M "$MOUNT" -n -o SOURCE 2>/dev/null) || return 1

    [ "$fs" = fuse ] && [ "$source" = mythfs ] || return 1

    timeout 8 runuser -u jellyfin -- /usr/bin/python3 -c '
import os
import sys

for directory, _, files in os.walk(sys.argv[1]):
    for filename in files:
        if not filename.lower().endswith(".ts"):
            continue
        try:
            with open(os.path.join(directory, filename), "rb") as f:
                if len(f.read(188)) == 188:
                    sys.exit(0)
        except OSError:
            continue

sys.exit(1)
' "$MOUNT" >/dev/null 2>&1
}

echo "JELLYFIN-MYTHFS: Waiting for readable FUSE filesystem."

for attempt in $(seq 0 60); do
    if ready; then
        echo "JELLYFIN-MYTHFS: Ready. Jellyfin may start."
        exit 0
    fi

    if [ "$attempt" -lt 60 ]; then
        sleep 3
    fi
done

echo "JELLYFIN-MYTHFS: ERROR - MythFS not ready after 180 seconds."
echo "JELLYFIN-MYTHFS: Jellyfin startup blocked."
exit 1
