"""Account locks shared by Python and Rust live runners on this host."""

import os
from contextlib import ExitStack, contextmanager
from pathlib import Path


def lock_directory():
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "prediction-market-arbitrage" / "account-locks"


@contextmanager
def account_locks(accounts):
    directory = lock_directory()
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for venue, address in sorted(set(accounts)):
            address = address.lower()
            if venue not in ("polymarket", "predict_fun") or len(address) != 42 or not address.startswith("0x"):
                raise ValueError("Invalid live account identity")
            int(address[2:], 16)
            handle = stack.enter_context((directory / f"{venue}-{address}.lock").open("a+b"))
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
