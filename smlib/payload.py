"""Android OTA ``payload.bin`` (update_engine) parser and partition extractor.

Supports full payloads (REPLACE / REPLACE_BZ / REPLACE_XZ / REPLACE_ZSTD /
ZERO / DISCARD) and delta payloads that only need the previous image
(SOURCE_COPY / MOVE / BSDIFF / SOURCE_BSDIFF).
"""

from __future__ import annotations

import bz2
import hashlib
import lzma
import os
import struct
import threading
from dataclasses import dataclass, field

from . import bspatch
from .protowire import Msg
from .util import Fail, Console, align_up, human_size, sha256_file

PAYLOAD_MAGIC = b"CrAU"

OP_REPLACE = 0
OP_REPLACE_BZ = 1
OP_MOVE = 2
OP_BSDIFF = 3
OP_SOURCE_COPY = 4
OP_SOURCE_BSDIFF = 5
OP_ZERO = 6
OP_DISCARD = 7
OP_REPLACE_XZ = 8
OP_PUFFDIFF = 9
OP_BROTLI_BSDIFF = 10
OP_ZUCCHINI = 11
OP_LZ4DIFF_BSDIFF = 12
OP_LZ4DIFF_PUFFDIFF = 13
OP_REPLACE_ZSTD = 14

OP_NAMES = {
    OP_REPLACE: "REPLACE",
    OP_REPLACE_BZ: "REPLACE_BZ",
    OP_MOVE: "MOVE",
    OP_BSDIFF: "BSDIFF",
    OP_SOURCE_COPY: "SOURCE_COPY",
    OP_SOURCE_BSDIFF: "SOURCE_BSDIFF",
    OP_ZERO: "ZERO",
    OP_DISCARD: "DISCARD",
    OP_REPLACE_XZ: "REPLACE_XZ",
    OP_PUFFDIFF: "PUFFDIFF",
    OP_BROTLI_BSDIFF: "BROTLI_BSDIFF",
    OP_ZUCCHINI: "ZUCCHINI",
    OP_LZ4DIFF_BSDIFF: "LZ4DIFF_BSDIFF",
    OP_LZ4DIFF_PUFFDIFF: "LZ4DIFF_PUFFDIFF",
    OP_REPLACE_ZSTD: "REPLACE_ZSTD",
}

DELTA_OPS = {OP_MOVE, OP_BSDIFF, OP_SOURCE_COPY, OP_SOURCE_BSDIFF,
             OP_PUFFDIFF, OP_BROTLI_BSDIFF, OP_ZUCCHINI,
             OP_LZ4DIFF_BSDIFF, OP_LZ4DIFF_PUFFDIFF}


@dataclass
class Extent:
    start_block: int
    num_blocks: int


@dataclass
class Op:
    kind: int
    data_offset: int = 0
    data_length: int = 0
    dst_extents: list[Extent] = field(default_factory=list)
    src_extents: list[Extent] = field(default_factory=list)
    dst_length: int | None = None
    src_length: int | None = None
    data_sha256: bytes | None = None
    src_sha256: bytes | None = None

    @property
    def name(self) -> str:
        return OP_NAMES.get(self.kind, f"OP_{self.kind}")


@dataclass
class Partition:
    name: str
    size: int
    ops: list[Op]
    old_size: int = 0
    filesystem_type: str | None = None
    hash_hex: str | None = None
    version: str | None = None

    @property
    def is_delta(self) -> bool:
        return any(op.kind in DELTA_OPS for op in self.ops)


class Payload:
    def __init__(self, path: str, properties: dict[str, str] | None = None):
        self.path = path
        self.size = os.path.getsize(path)
        self.properties = properties or {}
        self.version = 0
        self.manifest_size = 0
        self.metadata_sig_size = 0
        self.minor_version = 0
        self.partial_update = False
        self.block_size = 4096
        self.partitions: list[Partition] = []
        self.dynamic_groups: list[tuple[str, int, list[str]]] = []
        self._parse()

    # ------------------------------------------------------------------ parse
    def _parse(self) -> None:
        with open(self.path, "rb") as f:
            magic = f.read(4)
            if magic != PAYLOAD_MAGIC:
                raise Fail(
                    f"{self.path}: not a payload.bin (magic {magic!r}); "
                    "if this is an OPPO .ozip, decrypt/unpack it first"
                )
            (self.version,) = struct.unpack(">Q", self._read(f, 8))
            (self.manifest_size,) = struct.unpack(">Q", self._read(f, 8))
            if self.version >= 2:
                (self.metadata_sig_size,) = struct.unpack(">I", self._read(f, 4))
            if self.manifest_size == 0 or self.manifest_size > 256 << 20:
                raise Fail(f"implausible manifest size {self.manifest_size}")
            manifest = self._read(f, self.manifest_size)

        self.data_offset_base = (
            4 + 8 + 8 + (4 if self.version >= 2 else 0)
            + self.manifest_size + self.metadata_sig_size
        )
        if self.data_offset_base > self.size:
            raise Fail("manifest/signature region extends past end of file")

        root = Msg.from_bytes(manifest)
        self.minor_version = root.varint(12, 0)
        # DeltaArchiveManifest: 15 = dynamic_partition_metadata, 16 = partial_update
        self.partial_update = root.boolean(16, False)
        block_size = root.varint(3, 0)
        if block_size and 4096 <= block_size <= (1 << 20) and (block_size & (block_size - 1)) == 0:
            self.block_size = block_size

        for pu in root.msgs(13):
            self.partitions.append(self._parse_partition(pu))

        dyn = root.msg(15)
        if dyn is not None:
            for grp in dyn.msgs(1):
                names = []
                for raw in grp.fields.get(3, []):
                    if isinstance(raw, (bytes, bytearray)):
                        names.append(bytes(raw).decode("utf-8", "replace"))
                self.dynamic_groups.append((grp.string(1, ""), grp.varint(2, 0), names))

        if not self.partitions:
            raise Fail("payload manifest contains no partitions")

    @staticmethod
    def _read(f, n: int) -> bytes:
        data = f.read(n)
        if len(data) != n:
            raise Fail("unexpected end of file while reading payload")
        return data

    @staticmethod
    def _parse_partition(pu: Msg) -> Partition:
        name = pu.string(1, "")
        if not name:
            raise Fail("payload partition without a name")
        new_info = pu.msg(7)
        old_info = pu.msg(6)
        size = new_info.varint(1, 0) if new_info else 0
        part_hash = new_info.bytes(2) if new_info else None
        old_size = old_info.varint(1, 0) if old_info else 0
        ops = []
        for om in pu.msgs(8):
            op = Op(
                kind=om.varint(1, -1),
                data_offset=om.varint(2, 0),
                data_length=om.varint(3, 0),
                dst_length=om.varint(7, None),
                src_length=om.varint(5, None),
                data_sha256=om.bytes(8),
                src_sha256=om.bytes(9),
            )
            for em in om.msgs(6):
                op.dst_extents.append(Extent(em.varint(1, 0), em.varint(2, 0)))
            for em in om.msgs(4):
                op.src_extents.append(Extent(em.varint(1, 0), em.varint(2, 0)))
            ops.append(op)
        if size == 0:
            end = 0
            for op in ops:
                for ext in op.dst_extents:
                    end = max(end, (ext.start_block + ext.num_blocks))
            size = end * 4096
        return Partition(
            name=name,
            size=size,
            ops=ops,
            old_size=old_size,
            filesystem_type=pu.string(4, None),
            hash_hex=part_hash.hex() if part_hash else None,
            version=pu.string(17, None),
        )

    # --------------------------------------------------------------- helpers
    def find(self, name: str) -> Partition | None:
        for p in self.partitions:
            if p.name == name:
                return p
        return None

    def op_bounds_ok(self, base: int) -> bool:
        limit = self.size
        for part in self.partitions:
            for op in part.ops:
                if op.data_length <= 0:
                    continue
                start = base + op.data_offset
                if start < 0 or start + op.data_length > limit:
                    return False
        return True

    def resolve_data_base(self, mode: str = "auto") -> int:
        if mode == "relative":
            if not self.op_bounds_ok(self.data_offset_base):
                raise Fail("payload data offsets are out of bounds (relative mode)")
            return self.data_offset_base
        if mode == "absolute":
            if not self.op_bounds_ok(0):
                raise Fail("payload data offsets are out of bounds (absolute mode)")
            return 0
        candidates = [self.data_offset_base,
                      self.data_offset_base - self.metadata_sig_size,
                      0]
        unique = []
        for cand in candidates:
            if cand >= 0 and cand not in unique:
                unique.append(cand)
        valid = [c for c in unique if self.op_bounds_ok(c)]
        if not valid:
            raise Fail("cannot locate payload data section: operation offsets are out of bounds")
        if len(valid) == 1:
            return valid[0]
        # More than one base fits; settle it with an operation hash.
        for cand in valid:
            if self._probe_hash(cand):
                return cand
        return valid[0]

    def _probe_hash(self, base: int) -> bool:
        for part in self.partitions:
            for op in part.ops:
                if not op.data_length or not op.data_sha256:
                    continue
                with open(self.path, "rb") as f:
                    f.seek(base + op.data_offset)
                    blob = f.read(op.data_length)
                return hashlib.sha256(blob).digest() == op.data_sha256
        return False

    # -------------------------------------------------------------- extract
    def extract(
        self,
        part: Partition,
        out_path: str,
        old_image: str | None = None,
        data_base: int | None = None,
        verify_ops: bool = True,
        verify_hash: bool = False,
        log: Console | None = None,
    ) -> dict:
        if data_base is None:
            data_base = self.resolve_data_base()
        if part.is_delta and not old_image:
            raise Fail(
                f"partition {part.name} is a delta update "
                f"({', '.join(sorted({op.name for op in part.ops if op.kind in DELTA_OPS}))}); "
                "pass --old-image-dir with the matching previous images"
            )

        out_size = align_up(part.size, self.block_size)
        tmp = out_path + ".part"
        written = 0
        old_f = open(old_image, "rb") if old_image and part.is_delta else None
        try:
            with open(self.path, "rb") as payload_f, open(tmp, "wb") as out_f:
                out_f.truncate(out_size)
                for op in part.ops:
                    blob = b""
                    if op.data_length:
                        payload_f.seek(data_base + op.data_offset)
                        blob = payload_f.read(op.data_length)
                        if len(blob) != op.data_length:
                            raise Fail(f"{part.name}: truncated operation data")
                        if verify_ops and op.data_sha256:
                            got = hashlib.sha256(blob).digest()
                            if got != op.data_sha256:
                                raise Fail(
                                    f"{part.name}: operation data hash mismatch "
                                    f"({op.name} at offset {op.data_offset}); "
                                    "wrong payload or wrong data offset mode"
                                )
                    data = self._op_data(part, op, blob, old_f)
                    self._write_extents(out_f, op, data, part.name)
                    written += len(data)
            if log:
                log.debug(f"  {part.name}: {len(part.ops)} ops, {human_size(out_size)}")
            if verify_hash and part.hash_hex:
                got = sha256_file(tmp)
                if got != part.hash_hex:
                    raise Fail(
                        f"{part.name}: partition hash mismatch\n"
                        f"    expected {part.hash_hex}\n    got      {got}"
                    )
            os.replace(tmp, out_path)
        finally:
            if old_f:
                old_f.close()
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return {"name": part.name, "path": out_path, "size": out_size, "written": written}

    def _op_data(self, part: Partition, op: Op, blob: bytes, old_f) -> bytes:
        k = op.kind
        if k == OP_REPLACE:
            return blob
        if k == OP_REPLACE_BZ:
            return bz2.decompress(blob)
        if k == OP_REPLACE_XZ:
            return lzma.decompress(blob)
        if k == OP_REPLACE_ZSTD:
            try:
                import zstandard
            except ImportError:
                raise Fail(
                    f"{part.name}: payload uses ZSTD compression; "
                    "install the optional dependency: python -m pip install zstandard"
                )
            return zstandard.ZstdDecompressor().decompress(blob, max_output_size=1 << 31)
        if k in (OP_ZERO, OP_DISCARD):
            total = sum(e.num_blocks for e in op.dst_extents) * self.block_size
            return b"\x00" * total
        if k in (OP_SOURCE_COPY, OP_MOVE):
            if old_f is None:
                raise Fail(f"{part.name}: {op.name} needs the previous image")
            chunks = []
            for ext in op.src_extents:
                old_f.seek(ext.start_block * self.block_size)
                chunks.append(old_f.read(ext.num_blocks * self.block_size))
            return b"".join(chunks)
        if k in (OP_BSDIFF, OP_SOURCE_BSDIFF):
            if old_f is None:
                raise Fail(f"{part.name}: {op.name} needs the previous image")
            old_chunks = []
            for ext in op.src_extents:
                old_f.seek(ext.start_block * self.block_size)
                old_chunks.append(old_f.read(ext.num_blocks * self.block_size))
            old = b"".join(old_chunks)
            return bspatch.apply(blob, old)
        raise Fail(
            f"{part.name}: unsupported delta operation {op.name}; "
            "extract from a full OTA package instead"
        )

    def _write_extents(self, out_f, op: Op, data: bytes, part_name: str) -> None:
        if not op.dst_extents:
            return
        pos = 0
        for ext in op.dst_extents:
            nbytes = ext.num_blocks * self.block_size
            chunk = data[pos:pos + nbytes]
            if not chunk:
                break
            out_f.seek(ext.start_block * self.block_size)
            out_f.write(chunk)
            pos += len(chunk)
        if op.dst_length is not None and pos > op.dst_length:
            # Trailing padding inside the last extent; harmless, file is sized
            # independently, so nothing to do beyond not over-reporting.
            pass

    def extract_all(
        self,
        out_dir: str,
        names: list[str] | None = None,
        old_dir: str | None = None,
        jobs: int = 1,
        verify_ops: bool = True,
        verify_hash: bool = False,
        log: Console | None = None,
    ) -> dict[str, str]:
        os.makedirs(out_dir, exist_ok=True)
        todo = [p for p in self.partitions if not names or p.name in names]
        if names:
            missing = set(names) - {p.name for p in todo}
            if missing:
                raise Fail(f"payload has no partition(s): {', '.join(sorted(missing))}")
        data_base = self.resolve_data_base()
        results: dict[str, str] = {}
        lock = threading.Lock()

        def work(part: Partition) -> None:
            old = None
            if part.is_delta and old_dir:
                cand = os.path.join(old_dir, part.name + ".img")
                if os.path.exists(cand):
                    old = cand
            out = os.path.join(out_dir, part.name + ".img")
            self.extract(part, out, old_image=old, data_base=data_base,
                         verify_ops=verify_ops, verify_hash=verify_hash, log=log)
            with lock:
                results[part.name] = out

        if jobs and jobs > 1 and len(todo) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                for fut in [pool.submit(work, p) for p in todo]:
                    fut.result()
        else:
            for part in todo:
                work(part)
        return results


def read_properties(path: str) -> dict[str, str]:
    props: dict[str, str] = {}
    if not os.path.exists(path):
        return props
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            props[key.strip()] = value.strip()
    return props


def describe_version(props: dict[str, str]) -> str:
    """One-line summary of a payload_properties.txt."""
    if not props:
        return "unknown"
    bits = []
    if props.get("oplus_rom_version"):
        bits.append(props["oplus_rom_version"])
    if props.get("ota_target_version"):
        bits.append(props["ota_target_version"])
    elif props.get("post-build"):
        bits.append(props["post-build"])
    if props.get("android_version"):
        bits.append(f"android {props['android_version']}")
    if props.get("security_patch"):
        bits.append(f"patch {props['security_patch']}")
    return "  ".join(bits) if bits else "unknown"


def open_payload(path: str, work_dir: str) -> Payload:
    """Accept payload.bin directly, or an OTA .zip containing it."""
    if not os.path.exists(path):
        raise Fail(f"no such file: {path}")
    lower = path.lower()
    if lower.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n == "payload.bin"]
            if not names:
                raise Fail(f"{path}: no payload.bin inside the zip")
            props = {}
            if "payload_properties.txt" in zf.namelist():
                raw = zf.read("payload_properties.txt").decode("utf-8", "replace")
                for line in raw.splitlines():
                    if "=" in line and not line.startswith("#"):
                        key, value = line.split("=", 1)
                        props[key.strip()] = value.strip()
            os.makedirs(work_dir, exist_ok=True)
            target = os.path.join(work_dir, "payload.bin")
            if not os.path.exists(target) or os.path.getsize(target) != zf.getinfo(names[0]).file_size:
                with zf.open(names[0]) as src, open(target, "wb") as dst:
                    while True:
                        buf = src.read(8 << 20)
                        if not buf:
                            break
                        dst.write(buf)
        return Payload(target, props)
    if lower.endswith(".ozip"):
        raise Fail(
            f"{path}: OPPO .ozip is an encrypted container; "
            "unpack it with an ozip decryptor first, then pass the inner payload.bin"
        )
    props = read_properties(os.path.join(os.path.dirname(os.path.abspath(path)),
                                         "payload_properties.txt"))
    return Payload(path, props)
