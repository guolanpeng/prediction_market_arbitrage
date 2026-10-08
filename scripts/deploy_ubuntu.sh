#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_root"

source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 || "$(uname -m)" != x86_64 ]]; then
    echo "This script requires Ubuntu 24.04 x86_64." >&2
    exit 1
fi

if ! command -v curl >/dev/null || ! command -v flock >/dev/null; then
    if [[ $EUID -eq 0 ]]; then
        apt-get update
        apt-get install -y curl ca-certificates util-linux
    else
        if ! command -v sudo >/dev/null || ! sudo -n true 2>/dev/null; then
            echo "Missing curl or flock. An administrator must install curl ca-certificates util-linux first." >&2
            echo "No account password is required by this deployment script." >&2
            exit 1
        fi
        sudo -n apt-get update
        sudo -n apt-get install -y curl ca-certificates util-linux
    fi
fi

mkdir -p data/prediction/live logs/prediction
exec 9>>data/prediction/live/orders.lock
if ! flock -n 9; then
    echo "Live journal is in use. Stop the process before deploying." >&2
    exit 1
fi

if [[ -d python/.venv && ! -x python/.venv/bin/python ]]; then
    echo "python/.venv is not a Linux environment. Upload code without the Windows .venv." >&2
    exit 1
fi
if [[ -f SHA256SUMS ]]; then
    sha256sum -c SHA256SUMS
fi

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
    installer=$(mktemp)
    trap 'rm -f -- "$installer"' EXIT
    curl -LsSf https://astral.sh/uv/0.12.19/install.sh -o "$installer"
    UV_NO_MODIFY_PATH=1 sh "$installer"
fi

uv sync --project python --locked --group test --no-build --python 3.13
uv run --project python --no-sync python -m pytest user_strategies -q

if [[ ! -f .env ]]; then
    cp -- .env.example .env
    echo "Created an empty .env template. Fill it locally or securely copy your existing .env."
fi
chmod 600 .env
chmod -R go-rwx data logs
uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --trade-credentials

echo "Deployment checks completed. No live trading was started."
if ! command -v tmux >/dev/null; then
    echo "Optional tmux is not installed. Deployment succeeded; use a foreground SSH terminal or ask an administrator to install tmux."
fi
echo "Stop the old machine's live process and migrate its complete data/prediction journal before trading here."
echo "Then run the read-only account check from: $project_root"
echo "uv run --project python --no-sync --env-file .env python -m user_strategies.check_prediction_pair --auto-btc --live --quantity 10 --max-trades 1 --preflight-only"
