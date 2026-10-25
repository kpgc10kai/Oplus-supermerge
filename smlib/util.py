"""Small shared helpers: sizing, hashing, slot-name handling, logging."""

from __future__ import annotations

import hashlib
import os
import re
import sys

SECTOR_SIZE = 512
BLOCK_SIZE = 4096


class Fail(Exception):
    """Fatal, user-facing error (printed without a traceback)."""


def align_up(value: int, alignment: int) -> int:
    if alignment <= 0:
        return value
    return (value + alignment - 1) // alignment * alignment


def align_down(value: int, alignment: int) -> int:
    if alignment <= 0:
        return value
    return value // alignment * alignment


def human_size(n: int) -> str:
    if n is None:
        return "?"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            if u == "B":
                return f"{int(f)} B"
            return f"{f:.2f} {u}"
        f /= 1024.0
    return f"{n} B"


def parse_size(text: str) -> int:
    """Parse '4096', '512K', '8M', '6G' (also KiB/MiB/GiB, decimal accepted)."""
    text = str(text).strip()
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([kKmMgGtT]?)(i?[bB])?", text)
    if not m:
        raise Fail(f"cannot parse size: {text!r}")
    value = float(m.group(1))
    unit = m.group(2).lower()
    mult = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[unit]
    return int(value * mult)


SLOT_RE = re.compile(r"^(?P<base>.+)_(?P<slot>[ab])$")


def split_slot(name: str) -> tuple[str, str | None]:
    """'system_a' -> ('system', 'a'); 'system' -> ('system', None)."""
    m = SLOT_RE.match(name)
    if m:
        return m.group("base"), m.group("slot")
    return name, None


def sha256_file(path: str, chunk: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Console:
    """Tiny logger that keeps stdout clean for machine-readable output."""

    def __init__(self, quiet: bool = False, verbose: bool = False):
        self.quiet = quiet
        self.verbose = verbose

    def info(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=sys.stderr, flush=True)

    def debug(self, msg: str) -> None:
        if self.verbose and not self.quiet:
            print(f"  [debug] {msg}", file=sys.stderr, flush=True)

    def warn(self, msg: str) -> None:
        print(f"warning: {msg}", file=sys.stderr, flush=True)


def setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def scratch_dir(base: str, prefix: str) -> str:
    """Create a unique directory with default permissions.

    tempfile.mkdtemp() creates mode-0700 directories, which the Windows
    sandboxed token cannot traverse afterwards; create them plainly instead.
    """
    import uuid
    os.makedirs(base, exist_ok=True)
    for _ in range(100):
        cand = os.path.join(base, prefix + uuid.uuid4().hex[:10])
        if not os.path.exists(cand):
            os.makedirs(cand)
            return cand
    raise Fail(f"cannot create a scratch directory under {base}")


def scratch_path(directory: str, prefix: str, suffix: str = "") -> str:
    """Return a unique, not-yet-existing path inside *directory*."""
    import uuid
    os.makedirs(directory, exist_ok=True)
    for _ in range(100):
        cand = os.path.join(directory, prefix + uuid.uuid4().hex[:10] + suffix)
        if not os.path.exists(cand):
            return cand
    raise Fail(f"cannot find a free scratch name under {directory}")


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1
