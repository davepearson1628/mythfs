#!/bin/bash
set -Eeuo pipefail

CONFIG=/etc/mythfs.conf
PARENT=/fuse-parent

# Obtain the real mountpoint from MythFS configuration.
# Fail closed if the configuration is missing or invalid.
TARGET=$(/usr/bin/python3 - "$CONFIG" "$PARENT" <<'PY'
import configparser
import os
import sys

config_file, parent = sys.argv[1:]

cfg = configparser.ConfigParser(interpolation=None)

if not cfg.read(config_file):
    raise SystemExit("ENTRYPOINT: Cannot read configuration")

if not cfg.has_section("mythfs"):
    raise SystemExit("ENTRYPOINT: Missing mythfs section")

target = cfg["mythfs"].get("mountpoint", "").strip()

# Require exactly one direct child of the shared parent.
if (
    not target.startswith(parent + "/")
    or os.path.dirname(target) != parent
    or os.path.basename(target) in ("", ".", "..")
    or os.path.normpath(target) != target
):
    raise SystemExit(
        "ENTRYPOINT: Unsafe or unexpected FUSE mountpoint"
    )

print(target)
PY
)

echo "MYTHFS-ENTRYPOINT: Checking $TARGET"

# Check the mount table BEFORE testing directory access.
# A disconnected FUSE mount can fail ordinary directory tests.
FS=$(findmnt -M "$TARGET" -n -o FSTYPE 2>/dev/null || true)
SOURCE=$(findmnt -M "$TARGET" -n -o SOURCE 2>/dev/null || true)

if [ -n "$FS" ]; then
    echo "MYTHFS-ENTRYPOINT: Existing mount $FS / $SOURCE"

    if [ "$FS" != fuse ] || [ "$SOURCE" != mythfs ]; then
        echo "MYTHFS-ENTRYPOINT: Unexpected mount; refusing cleanup."
        exit 41
    fi

    echo "MYTHFS-ENTRYPOINT: Releasing previous MythFS mount."

    if ! timeout 20 umount "$TARGET"; then
        echo "MYTHFS-ENTRYPOINT: Normal unmount failed."
        exit 42
    fi

    if findmnt -M "$TARGET" >/dev/null 2>&1; then
        echo "MYTHFS-ENTRYPOINT: Previous mount still present."
        exit 43
    fi

    echo "MYTHFS-ENTRYPOINT: Previous FUSE mount released."
else
    echo "MYTHFS-ENTRYPOINT: No previous FUSE mount."
fi

if [ ! -d "$TARGET" ]; then
    echo "MYTHFS-ENTRYPOINT: Underlying directory unavailable."
    exit 44
fi

echo "MYTHFS-ENTRYPOINT: Starting MythFS at $TARGET"

exec /usr/bin/python3 /usr/local/bin/mythfs.py \
    --config "$CONFIG"
