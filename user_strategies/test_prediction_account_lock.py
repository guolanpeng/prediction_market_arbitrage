import os
import subprocess
import sys
from pathlib import Path

import pytest

from user_strategies.prediction_account_lock import account_locks


def test_accounts_are_locked_across_processes_and_released(tmp_path, monkeypatch):
    name = "LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME"
    monkeypatch.setenv(name, str(tmp_path))
    identity = [("polymarket", "0x" + "2" * 40)]
    code = "from user_strategies.prediction_account_lock import account_locks; " \
           "lock=account_locks([('polymarket','0x'+'2'*40)]); lock.__enter__()"
    with account_locks(identity):
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0


def test_invalid_identity_does_not_become_a_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME", str(tmp_path))
    with pytest.raises(ValueError):
        with account_locks([("polymarket", "../account")]):
            pass
