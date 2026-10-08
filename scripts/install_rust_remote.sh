#!/usr/bin/env bash
# Install an already-built binary. No Cargo, uv sync, account authentication or orders.
set -euo pipefail
umask 077

target=${1:?Target directory required}
archive=${2:?Archive required}
helper=${3:?Release verification helper required}
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 || "$(uname -m)" != x86_64 ]]; then
    echo "Rust installation requires Ubuntu 24.04 x86_64." >&2
    exit 1
fi
[[ "$target" =~ ^/[A-Za-z0-9_./-]+$ && "$target" != / && "$target" != *'/../'* && "$target" != */.. ]] || {
    echo "Invalid deployment directory." >&2; exit 1;
}
for tool in python3 tar flock ldd mktemp find; do
    command -v "$tool" >/dev/null || { echo "Missing server tool: $tool" >&2; exit 1; }
done
workspace=$(mktemp -d /tmp/prediction-rust-install.XXXXXXXX)
cleanup() {
    case "$workspace" in /tmp/prediction-rust-install.*) rm -rf -- "$workspace" ;; esac
}
trap cleanup EXIT
python3 "$helper" unpack "$archive" "$workspace"
binary="$workspace/target/release/prediction-market"
dependencies=$(ldd "$binary")
if [[ "$dependencies" == *'not found'* ]]; then
    echo "A required runtime library is missing; target files were not modified." >&2
    exit 1
fi
"$binary" --help >/dev/null
python3 - "$binary" <<'PY'
import json, subprocess, sys
from decimal import Decimal
sample = {"quantity":"10","asks_a":[["0.2","20"]],"asks_b":[["0.3","20"]],
          "configs":[{"model":"quadratic_theta","rate":"0","places":"0.00001"},
                     {"model":"min_price","rate":"0","places":"0.000001"}]}
result = subprocess.run([sys.argv[1],"offline","--operation","calculate"], input=json.dumps(sample),
                        capture_output=True,text=True,check=True,timeout=15)
assert Decimal(json.loads(result.stdout)["net"]) == 5, "Offline smoke calculation failed"
PY

# Validate the destination hierarchy before creating directories or opening locks.
python3 "$helper" verify "$workspace"
python3 - "$target" <<'PY'
import os, pathlib, sys
path=pathlib.Path(os.path.abspath(sys.argv[1]))
if any(part.is_symlink() for part in (path,*path.parents)):
    raise SystemExit("Deployment directory must not contain symlinks")
PY
mkdir -p "$target/data/prediction/live"
exec 9>>"$target/data/prediction/live/orders.lock"
flock -n 9 || { echo "Live journal is in use. Stop the process before installing." >&2; exit 1; }
# Also honor account locks used by a process with a different journal path.
account_locks=${XDG_CACHE_HOME:-$HOME/.cache}/prediction-market-arbitrage/account-locks
if [[ -d "$account_locks" ]]; then
    while IFS= read -r -d '' lock; do
        exec {account_fd}>>"$lock"
        flock -n "$account_fd" || { echo "A live account on this host is in use. Installation refused." >&2; exit 1; }
    done < <(find "$account_locks" -maxdepth 1 -type f -name '*.lock' -print0)
fi
python3 "$helper" install "$workspace" "$target"
echo "Installed Rust release at: $target/target/release/prediction-market"
echo "Existing .env, journals, logs and Python virtual environment were preserved."
echo "No server compilation, account authentication, approvals or live trading were started."
