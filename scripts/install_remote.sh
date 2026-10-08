#!/usr/bin/env bash
set -euo pipefail
umask 077
target=$1
archive=$2
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 || "$(uname -m)" != x86_64 ]]; then
    echo "This deployment requires Ubuntu 24.04 x86_64." >&2
    exit 1
fi
if ! command -v flock >/dev/null; then
    echo "Missing flock. Ask an administrator to install util-linux first." >&2
    exit 1
fi
mkdir -p "$target/data/prediction/live"
exec 9>>"$target/data/prediction/live/orders.lock"
if ! flock -n 9; then
    echo "Live journal is in use. Stop the server process before uploading an update." >&2
    exit 1
fi
tar -xzf "$archive" -C "$target"
# The installer acquires this same lock again before modifying the environment.
flock -u 9
exec 9>&-
bash "$target/scripts/deploy_ubuntu.sh"
echo "Remote installation completed: $target"
