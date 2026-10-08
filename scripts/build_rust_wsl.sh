#!/usr/bin/env bash
# Build locally under WSL Ubuntu 22.04. Never contact the deployment server.
set -euo pipefail
umask 077

source_root=${1:?Repository path required}
stage=${2:?Windows release staging path required}
archive=${3:?Windows archive path required}
jobs=${4:-4}
offline=${5:-0}
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 22.04 || "$(uname -m)" != x86_64 ]]; then
    echo "Local build requires WSL Ubuntu 22.04 x86_64." >&2
    exit 1
fi
[[ "$jobs" =~ ^[0-9]+$ && "$jobs" -ge 1 && "$jobs" -le 256 ]] || { echo "Invalid build jobs." >&2; exit 1; }
[[ "$offline" == 0 || "$offline" == 1 ]] || { echo "Invalid offline mode." >&2; exit 1; }
export PATH="$HOME/.cargo/bin:$PATH"
for tool in cargo rustup gcc cmake python3 readelf getconf flock mktemp; do
    command -v "$tool" >/dev/null || { echo "Missing WSL build tool: $tool" >&2; exit 1; }
done
version=$(sed -n 's/^channel = "\([0-9.]*\)"$/\1/p' "$source_root/rust-toolchain.toml")
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Expected a pinned Rust toolchain." >&2; exit 1; }

cache_root=${XDG_CACHE_HOME:-$HOME/.cache}/prediction-market-arbitrage/wsl-release
mkdir -p "$cache_root"
cache_root=$(realpath -- "$cache_root")
exec 8>>"$cache_root/build.lock"
flock -n 8 || { echo "Another WSL Rust release build is running." >&2; exit 1; }
workspace=$(mktemp -d "$cache_root/work.XXXXXXXX")
cleanup() {
    case "$workspace" in "$cache_root"/work.*) rm -rf -- "$workspace" ;; esac
}
trap cleanup EXIT
python3 "$source_root/scripts/rust_release.py" snapshot "$source_root" "$workspace"
cd "$workspace"

# Explicit portable baseline overrides any inherited per-machine Rust flags.
unset RUSTFLAGS CARGO_BUILD_RUSTFLAGS CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS
export CARGO_ENCODED_RUSTFLAGS=$'-C\x1ftarget-cpu=x86-64'
export CARGO_TARGET_DIR="$cache_root/target"
export CARGO_BUILD_JOBS="$jobs"
toolchain=""
for installed in "$version" stable; do
    compiler=$(rustup run "$installed" rustc --version 2>/dev/null || true)
    manager=$(rustup run "$installed" cargo --version 2>/dev/null || true)
    if [[ "$compiler" == "rustc $version "* && "$manager" == "cargo $version "* ]]; then
        toolchain="$installed"
        break
    fi
done
if [[ -z "$toolchain" ]]; then
    if [[ "$offline" == 1 ]]; then
        echo "No complete installed Rust $version toolchain; install the pinned version before an offline build." >&2
        exit 1
    fi
    rustup toolchain install "$version" --profile minimal --component rustfmt --component clippy
    toolchain="$version"
fi
echo "Using installed toolchain: $toolchain (required Rust $version)"
rustup run "$toolchain" rustc --version
args=(--release --locked --target x86_64-unknown-linux-gnu --jobs "$jobs")
[[ "$offline" == 0 ]] || args+=(--offline)
echo "Building Linux release on Ubuntu 22.04 with portable x86-64 CPU instructions."
rustup run "$toolchain" cargo build "${args[@]}"
rustup run "$toolchain" cargo test "${args[@]}" --lib
binary="$CARGO_TARGET_DIR/x86_64-unknown-linux-gnu/release/prediction-market"
# Read ELF requirements and run only --help / offline calculation. No .env load.
python3 scripts/rust_release.py package "$workspace" "$binary" "$archive"
python3 scripts/rust_release.py snapshot "$workspace" "$stage"
cp -- "$workspace/RUST_RELEASE.json" "$stage/RUST_RELEASE.json"
cp -- "$workspace/SHA256SUMS" "$stage/SHA256SUMS"
mkdir -p "$stage/target/release"
cp -- "$binary" "$stage/target/release/prediction-market"
chmod 755 "$stage/target/release/prediction-market"
python3 scripts/rust_release.py verify "$stage"
echo "WSL Linux release archive is ready. No upload or live trading was started."
