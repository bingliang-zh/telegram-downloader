#!/bin/sh
set -eu

# A bind mount can point at an ordinary directory when the host's RAID/NFS
# mount is not ready yet. Require an explicit marker when a deployment needs
# to fail closed instead of writing into the host root filesystem.
mount_marker="${DOWNLOAD_MOUNT_MARKER:-}"
if [ -n "$mount_marker" ] && [ ! -f "$mount_marker" ]; then
    echo "Required download mount is not ready; marker is missing: $mount_marker" >&2
    exit 78
fi

exec telegram-downloader "$@"
