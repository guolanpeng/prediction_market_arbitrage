"""Release packaging must preserve private data and reject unsafe archives."""

import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import rust_release as release


def source_tree(path):
    for name in release.FIXED:
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("public source\n", encoding="utf-8")
    (path / ".env.example").write_text("POLYMARKET_PRIVATE_KEY=\nPOLYMARKET_SIGNATURE_TYPE=3\n", encoding="utf-8")
    (path / "rust").mkdir()
    (path / "rust/main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (path / "user_strategies").mkdir()
    (path / "user_strategies/prediction_rust.py").write_text("# public launcher\n", encoding="utf-8")
    for name in (".env", "deploy.local.json", "data/prediction/live/orders.jsonl", "logs/live.log", "python/.venv/private.txt"):
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("PRIVATE", encoding="utf-8")
    return path


def package(tmp_path, monkeypatch):
    source = source_tree(tmp_path / "source")
    stage = tmp_path / "stage"
    release.snapshot(source, stage)
    binary = tmp_path / "prediction-market"
    binary.write_bytes(b"fake ELF fixture")
    monkeypatch.setattr(release, "inspect_binary", lambda _: {"required_glibc": "2.35", "needed_libraries": ["libc.so.6"]})
    monkeypatch.setattr(release, "smoke", lambda _: None)
    archive = tmp_path / "release.tar.gz"
    release.package(stage, binary, archive)
    return stage, archive


def test_release_has_hashes_and_excludes_credentials_runtime_and_venv(tmp_path, monkeypatch):
    stage, archive = package(tmp_path, monkeypatch)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert release.BINARY in names
    assert {"Cargo.lock", "python/uv.lock", "python/requirements.txt", "SHA256SUMS"} <= names
    assert not any(name in names for name in (".env", "deploy.local.json", "logs/live.log", "data/prediction/live/orders.jsonl"))
    assert not any(".venv" in name for name in names)
    assert release.verify(stage)


@pytest.mark.parametrize("name", ["../.env", "/tmp/.env", "target/release/../../.env", ".env", "data/orders.jsonl", "user_strategies/data/private.py", "rust/../.env", "rust//main.rs", ""])
def test_release_path_allowlist_rejects_private_and_traversal_paths(name):
    assert not release.allowed(name)


def test_secret_in_env_example_stops_packaging(tmp_path):
    source = source_tree(tmp_path / "source")
    (source / ".env.example").write_text("POLYMARKET_PRIVATE_KEY=DO_NOT_SHIP\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Credential"):
        release.snapshot(source, tmp_path / "stage")


def test_bad_checksum_and_extra_file_are_rejected(tmp_path, monkeypatch):
    stage, _ = package(tmp_path, monkeypatch)
    binary = stage / release.BINARY
    before = binary.read_bytes()
    binary.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        release.verify(stage)
    binary.write_bytes(before)
    (stage / ".env").write_text("private")
    with pytest.raises(ValueError, match="manifest"):
        release.verify(stage)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "traversal", "private", "duplicate"])
def test_unsafe_tar_members_are_rejected_before_extraction(tmp_path, kind):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("README.md")
        if kind in ("symlink", "hardlink"):
            member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            member.linkname = "../.env"
            tar.addfile(member)
        else:
            member.name = "../escape" if kind == "traversal" else ".env" if kind == "private" else "README.md"
            member.size = 1
            tar.addfile(member, io.BytesIO(b"x"))
            if kind == "duplicate":
                tar.addfile(member, io.BytesIO(b"x"))
    stage = tmp_path / "unpack"
    stage.mkdir()
    with pytest.raises(ValueError, match="archive"):
        release.unpack(archive, stage)
    assert not any(stage.iterdir())


def test_install_preserves_journal_env_and_logs_and_backs_up_binary(tmp_path, monkeypatch):
    stage, archive = package(tmp_path, monkeypatch)
    unpack = tmp_path / "unpack"
    unpack.mkdir()
    release.unpack(archive, unpack)
    target = tmp_path / "server"
    private = {".env": b"KEY=PRIVATE", "data/prediction/live/orders.jsonl": b'UNRESOLVED_INTENT\n', "logs/live.log": b"history", "python/.venv/private.txt": b"environment"}
    for name, data in private.items():
        file = target / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
    old = target / release.BINARY
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old binary")
    release.install(unpack, target)
    for name, data in private.items():
        assert (target / name).read_bytes() == data
    assert old.read_bytes() == (stage / release.BINARY).read_bytes()
    assert len(list((target / "dist/rust-backups").glob("*.bak"))) == 1


def test_symlink_destination_is_refused_before_writes(tmp_path, monkeypatch):
    stage, _ = package(tmp_path, monkeypatch)
    target = tmp_path / "server"
    target.mkdir()
    outside = tmp_path / "private"
    outside.write_text("private")
    try:
        (target / "README.md").symlink_to(outside)
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    with pytest.raises(ValueError, match="symlink"):
        release.install(stage, target)
    assert outside.read_text() == "private"
    assert not (target / "Cargo.toml").exists()


def test_elf_checks_glibc_baseline_and_external_libraries(tmp_path, monkeypatch):
    binary = tmp_path / "runner"
    header = bytearray(20)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = (62).to_bytes(2, "little")
    binary.write_bytes(header)
    monkeypatch.setattr(release.subprocess, "check_output", lambda command, **_: "GLIBC_2.35 GLIBC_2.17" if "--version-info" in command else "Shared library: [libc.so.6]")
    assert release.inspect_binary(binary)["required_glibc"] == "2.35"
    monkeypatch.setattr(release.subprocess, "check_output", lambda *_args, **_kwargs: "GLIBC_2.39")
    with pytest.raises(ValueError, match="newer"):
        release.inspect_binary(binary)
    binary.write_bytes(b"MZ Windows executable")
    with pytest.raises(ValueError, match="ELF64"):
        release.inspect_binary(binary)


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell wrapper test")
def test_powershell_build_only_never_calls_ssh_and_passes_wsl_arguments(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    original = Path(__file__).resolve().parents[1] / "scripts/upload_rust.ps1"
    shutil.copyfile(original, scripts / "upload_rust.ps1")
    driver = tmp_path / "driver.ps1"
    driver.write_text(r'''
$ErrorActionPreference='Stop'
$global:LASTEXITCODE=0
function wsl.exe {
    if ($args -contains 'wslpath') { Write-Output ('/fixture/' + $args[-1]); return }
    if ($args[1] -ne 'Ubuntu-22.04' -or $args[3] -ne 'bash' -or $args[-2] -ne '3' -or $args[-1] -ne '1') { throw 'Wrong WSL build arguments' }
    $archive=$args[7].Substring('/fixture/'.Length)
    [IO.File]::WriteAllText($archive,'fake archive for wrapper control-flow test')
}
function ssh.exe { throw 'BuildOnly must not contact SSH' }
function scp.exe { throw 'BuildOnly must not upload' }
& (Join-Path $PSScriptRoot 'scripts/upload_rust.ps1') -BuildOnly -Offline -Jobs 3 -ConfigFile (Join-Path $PSScriptRoot 'missing.json')
''', encoding="utf-8")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "BuildOnly completed" in result.stdout
    assert len(list((tmp_path / "dist").glob("*.tar.gz"))) == 1
    assert not list((tmp_path / "dist").glob("rust-upload-staging-*"))
