"""Standard-library packaging and safe installation for the Linux Rust runner."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from decimal import Decimal
from pathlib import Path, PurePosixPath


BINARY = "target/release/prediction-market"
MANIFEST = "SHA256SUMS"
METADATA = "RUST_RELEASE.json"
FIXED = (
    "Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "README.md", "RUST_BTC.md",
    "UBUNTU.md", "AGENTS.md", ".env.example", ".python-version", ".gitignore", ".gitattributes",
    "deploy.example.json", "python/pyproject.toml", "python/uv.lock", "python/requirements.txt",
    "scripts/rust_release.py", "scripts/build_rust_wsl.sh", "scripts/install_rust_remote.sh",
    "scripts/upload_rust.ps1", "scripts/build_rust.ps1",
)
MAX_FILES = 512
MAX_BYTES = 256 * 1024 * 1024


def allowed(name):
    path = PurePosixPath(name)
    if not path.parts or path.as_posix() != name or path.is_absolute() or ".." in path.parts or "\\" in name or any(ord(c) < 32 for c in name):
        return False
    if name in (*FIXED, BINARY, MANIFEST, METADATA):
        return True
    if len(path.parts) == 2 and path.parts[0] == "rust" and path.suffix == ".rs":
        return True
    return len(path.parts) == 2 and path.parts[0] == "user_strategies" and path.suffix in (".py", ".md")


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def no_symlinks(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Release paths must not contain symlinks")
    return path


def snapshot(source, stage):
    source, stage = no_symlinks(source), no_symlinks(stage)
    if stage.exists() and any(stage.iterdir()):
        raise ValueError("Release staging directory must be empty")
    files = list(FIXED)
    files += [p.relative_to(source).as_posix() for p in sorted((source / "rust").glob("*.rs"))]
    files += [p.relative_to(source).as_posix() for p in sorted((source / "user_strategies").rglob("*"))
              if p.is_file() and p.suffix in (".py", ".md") and "__pycache__" not in p.parts]
    for name in files:
        if not allowed(name) or not (source / name).is_file():
            raise ValueError("Missing or invalid release source: " + name)
        no_symlinks(source / name)
    for line in (source / ".env.example").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().endswith(("PRIVATE_KEY", "API_KEY", "SECRET", "PASSPHRASE", "FUNDER", "ACCOUNT_ADDRESS")):
            if value.strip().strip("\"'"):
                raise ValueError("Credential values must be empty in .env.example")
    stage.mkdir(parents=True, exist_ok=True)
    for name in files:
        destination = stage / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
    return files


def inspect_binary(binary):
    with binary.open("rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4:6] != b"\x02\x01" or int.from_bytes(header[18:20], "little") != 62:
        raise ValueError("Expected an ELF64 little-endian x86_64 Linux binary")
    versions = subprocess.check_output(["readelf", "--version-info", str(binary)], text=True)
    matches = set(re.findall(r"GLIBC_([0-9]+(?:\.[0-9]+)+)", versions))
    maximum = max(matches, key=lambda value: tuple(map(int, value.split(".")))) if matches else "0.0"
    if tuple(map(int, maximum.split("."))) > (2, 35):
        raise ValueError("Binary requires glibc newer than Ubuntu 22.04: " + maximum)
    dynamic = subprocess.check_output(["readelf", "-d", str(binary)], text=True)
    libraries = re.findall(r"Shared library: \[([^\]]+)\]", dynamic)
    supported = {"libgcc_s.so.1", "libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2", "librt.so.1"}
    if not set(libraries) <= supported:
        raise ValueError("Unexpected external runtime libraries: " + ", ".join(sorted(set(libraries) - supported)))
    return {"required_glibc": maximum, "needed_libraries": sorted(libraries)}


def smoke(binary):
    subprocess.run([str(binary), "--help"], check=True, stdout=subprocess.DEVNULL, timeout=15)
    sample = {"quantity": "10", "asks_a": [["0.2", "20"]], "asks_b": [["0.3", "20"]],
              "configs": [{"model": "quadratic_theta", "rate": "0", "places": "0.00001"},
                          {"model": "min_price", "rate": "0", "places": "0.000001"}]}
    result = subprocess.run([str(binary), "offline", "--operation", "calculate"],
                            input=json.dumps(sample), text=True, check=True,
                            capture_output=True, timeout=15)
    data = json.loads(result.stdout)
    if data.get("state") != "POSITIVE_QUOTE_EDGE" or Decimal(data["net"]) != 5:
        raise ValueError("Offline calculation smoke test failed")


def package(stage, binary, archive):
    stage, binary = no_symlinks(stage), no_symlinks(binary)
    metadata = {"schema_version": 1, "build_os": "ubuntu", "build_version": "22.04",
                "server_os": "ubuntu", "server_version": "24.04", "architecture": "x86_64",
                "rust_target": "x86_64-unknown-linux-gnu", "cpu_target": "x86-64", "profile": "release"}
    metadata.update(inspect_binary(binary))
    smoke(binary)
    destination = stage / BINARY
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(binary, destination)
    destination.chmod(0o755)
    (stage / METADATA).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    files = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file())
    if len(files) > MAX_FILES or sum((stage / name).stat().st_size for name in files) > MAX_BYTES:
        raise ValueError("Release exceeds package size limits")
    if not all(allowed(name) and name != MANIFEST for name in files):
        raise ValueError("Unexpected release file")
    for name in files:
        no_symlinks(stage / name)
    (stage / MANIFEST).write_text("".join(f"{hash_file(stage / name)}  {name}\n" for name in files), encoding="utf-8")
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_name(archive.name + ".tmp")
    with tarfile.open(temporary, "w:gz") as output:
        for name in [*files, MANIFEST]:
            info = output.gettarinfo(str(stage / name), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if name == BINARY else 0o644
            with (stage / name).open("rb") as stream:
                output.addfile(info, stream)
    os.replace(temporary, archive)
    print(json.dumps({"archive": str(archive), "sha256": hash_file(archive), **metadata}))


def verify(stage):
    manifest = stage / MANIFEST
    if not manifest.is_file():
        raise ValueError("Missing SHA256SUMS")
    expected = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match or not allowed(match[2]) or match[2] == MANIFEST or match[2] in expected:
            raise ValueError("Invalid checksum manifest")
        expected[match[2]] = match[1]
    actual = {p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()} - {MANIFEST}
    if actual != set(expected) or not set((*FIXED, BINARY, METADATA)) <= actual:
        raise ValueError("Release files disagree with manifest")
    for name, expected_hash in expected.items():
        path = no_symlinks(stage / name)
        if hash_file(path) != expected_hash:
            raise ValueError("Release checksum mismatch: " + name)
    metadata = json.loads((stage / METADATA).read_text(encoding="utf-8"))
    if any(metadata.get(key) != value for key, value in {
        "schema_version": 1, "build_os": "ubuntu", "build_version": "22.04",
        "server_os": "ubuntu", "server_version": "24.04", "architecture": "x86_64",
        "rust_target": "x86_64-unknown-linux-gnu", "cpu_target": "x86-64", "profile": "release",
    }.items()):
        raise ValueError("Release platform metadata is invalid")
    return expected


def unpack(archive, stage):
    stage = no_symlinks(stage)
    if not stage.is_dir() or any(stage.iterdir()):
        raise ValueError("Unpack staging directory must exist and be empty")
    with tarfile.open(archive, "r:gz") as source:
        members = []
        names = set()
        total = 0
        for member in source:
            if not member.isfile() or not allowed(member.name) or member.name in names:
                raise ValueError("Unsafe or unexpected archive member")
            names.add(member.name)
            total += member.size
            if len(names) > MAX_FILES or total > MAX_BYTES or member.size > 64 * 1024 * 1024:
                raise ValueError("Archive exceeds size limits")
            members.append(member)
        for member in members:
            destination = stage / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as reader, destination.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
            destination.chmod(0o755 if member.name == BINARY else 0o644)
    verify(stage)


def install(stage, target):
    stage, target = no_symlinks(stage), no_symlinks(target)
    files = verify(stage)
    # Check every destination before writing any file. Never follow a symlink
    # into credentials/journals or another deployment directory.
    for name in (*files, MANIFEST):
        destination = no_symlinks(target / name)
        if destination.exists() and not destination.is_file():
            raise ValueError("Destination is not a regular file: " + name)
    if (target / BINARY).is_file():
        backup = target / "dist" / "rust-backups" / ("prediction-market-" + hash_file(target / BINARY) + ".bak")
        no_symlinks(backup)
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copyfile(target / BINARY, backup)
            backup.chmod(0o700)
    for name in [*(name for name in files if name != BINARY), MANIFEST, BINARY]:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".rust-install-", dir=destination.parent)
        try:
            with os.fdopen(handle, "wb") as writer, (stage / name).open("rb") as reader:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            os.chmod(temporary, 0o755 if name == BINARY else 0o644)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    for command, names in (("snapshot", ("source", "stage")), ("package", ("stage", "binary", "archive")),
                           ("unpack", ("archive", "stage")), ("verify", ("stage",)), ("install", ("stage", "target"))):
        command_parser = sub.add_parser(command)
        for name in names:
            command_parser.add_argument(name, type=Path)
    args = parser.parse_args()
    if args.operation == "snapshot":
        snapshot(args.source, args.stage)
    elif args.operation == "package":
        package(args.stage, args.binary, args.archive)
    elif args.operation == "unpack":
        unpack(args.archive, args.stage)
    elif args.operation == "verify":
        verify(args.stage)
    else:
        install(args.stage, args.target)


if __name__ == "__main__":
    main()
