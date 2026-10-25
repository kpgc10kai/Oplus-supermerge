"""High-level super.img reader and builder."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

from . import lp, sparse
from .util import Console, Fail, align_up, human_size, scratch_path

IO_CHUNK = 8 << 20


def _read_upto(src, n: int) -> bytes:
    """Read exactly *n* bytes unless EOF is reached (tolerates short reads)."""
    parts = []
    got = 0
    while got < n:
        buf = src.read(n - got)
        if not buf:
            break
        parts.append(buf)
        got += len(buf)
    return b"".join(parts)


@dataclass
class Entry:
    """One partition to place into the new super."""

    name: str
    group: str
    attributes: int
    size: int
    path: str | None = None
    origin: str = "file"
    template_extents: list[lp.Extent] | None = None
    template_path: str | None = None
    order: int = 0

    @property
    def placed_size(self) -> int:
        return align_up(self.size, lp.SECTOR_SIZE)

@dataclass
class LayoutTemplate:
    """A super layout without a backing image of its own.

    Used for OPPO packages, where the layout comes from ``super_meta.<nv>.raw``
    / ``super_def.<nv>.json`` and the partition data lives in ``IMAGES/``.
    """

    meta: lp.Metadata
    sources: dict[str, str] = field(default_factory=dict)
    label: str = "layout"
    has_data: bool = False
    raw_path: str | None = None

    @property
    def block_device(self) -> lp.BlockDevice:
        return self.meta.block_devices[0]

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SuperImage:
    """Read-only view over an existing super.img (raw or sparse)."""

    def __init__(self, path: str, log: Console | None = None,
                 tmp_dir: str | None = None):
        self.path = path
        self.raw_path = path
        self.temp_path: str | None = None
        if sparse.is_sparse(path):
            tmp_dir = tmp_dir or os.path.dirname(os.path.abspath(path)) or "."
            tmp = scratch_path(tmp_dir, "super_", ".raw")
            if log:
                log.info(f"expanding sparse super image -> {tmp}")
            sparse.decode(path, tmp)
            self.raw_path = tmp
            self.temp_path = tmp
        self.meta, self.geometry_offset = lp.read_metadata(self.raw_path)
        self.geometry = self.meta.geometry
        self.block_device = self.meta.block_devices[0]
        self.size = os.path.getsize(self.raw_path)

    def close(self) -> None:
        if self.temp_path and os.path.exists(self.temp_path):
            try:
                os.remove(self.temp_path)
            except OSError:
                pass
        self.temp_path = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def data_start(self) -> int:
        return self.geometry.metadata_end

    @property
    def has_data(self) -> bool:
        return self.size > self.data_start

    def partition_names(self) -> list[str]:
        return [p.name for p in self.meta.partitions]

    # ------------------------------------------------------------------ read
    def iter_partition(self, name: str, chunk: int = IO_CHUNK):
        part = self.meta.partition(name)
        if part is None:
            raise Fail(f"super image has no partition {name}")
        with open(self.raw_path, "rb") as f:
            for ext in self.meta.extents_of(part):
                if ext.target_type == lp.EXTENT_ZERO:
                    remaining = ext.size_bytes
                    while remaining:
                        n = min(chunk, remaining)
                        yield b"\0" * n
                        remaining -= n
                    continue
                if ext.target_type != lp.EXTENT_LINEAR:
                    raise Fail(f"{name}: unsupported extent type {ext.target_type}")
                f.seek(ext.target_data * lp.SECTOR_SIZE)
                remaining = ext.size_bytes
                while remaining:
                    buf = f.read(min(chunk, remaining))
                    if not buf:
                        raise Fail(f"{name}: extent runs past end of {self.path}")
                    yield buf
                    remaining -= len(buf)

    def extract_partition(self, name: str, out_path: str) -> int:
        total = 0
        with open(out_path, "wb") as out:
            for buf in self.iter_partition(name):
                out.write(buf)
                total += len(buf)
        return total

    def extract_all(self, out_dir: str, names: list[str] | None = None,
                    make_sparse: bool = False,
                    log: Console | None = None) -> dict[str, str]:
        os.makedirs(out_dir, exist_ok=True)
        todo = [p for p in self.meta.partitions if not names or p.name in names]
        if names:
            missing = set(names) - {p.name for p in todo}
            if missing:
                raise Fail(f"super has no partition(s): {', '.join(sorted(missing))}")
        out: dict[str, str] = {}
        for part in todo:
            target = os.path.join(out_dir, part.name + ".img")
            size = self.extract_partition(part.name, target)
            if make_sparse:
                enc = target + ".sparse"
                sparse.encode(target, enc)
                os.replace(enc, target)
            out[part.name] = target
            if log:
                log.info(f"  {part.name}: {human_size(size)}")
        return out

    # ---------------------------------------------------------------- checks
    def strict_check(self) -> list[str]:
        """Checks modelled on liblp itself, plus metadata copy consistency."""
        problems = lp.strict_validate(self.meta, self.size)
        geom = self.geometry
        n = geom.metadata_max_size
        with open(self.raw_path, "rb") as f:
            head = f.read(geom.metadata_end)
        copies = []
        for offset in (geom.metadata_start, geom.backup_metadata_start):
            for slot in range(geom.metadata_slot_count):
                begin = offset + slot * n
                copies.append(head[begin:begin + n])
        if len({bytes(c) for c in copies}) != 1:
            problems.append("metadata copies are not identical across the image")
        with open(self.raw_path, "rb") as f:
            f.seek(lp.RESERVED_BYTES)
            if f.read(4) != struct.pack("<I", lp.GEOMETRY_MAGIC):
                problems.append("primary geometry missing at 0x1000")
            f.seek(lp.RESERVED_BYTES + lp.GEOMETRY_SIZE)
            if f.read(4) != struct.pack("<I", lp.GEOMETRY_MAGIC):
                problems.append("geometry backup missing at 0x2000")
        return problems

    def check(self) -> list[str]:
        """Structural sanity checks; returns a list of problems."""
        problems: list[str] = []
        bd_size = self.block_device.size
        if bd_size and self.size > bd_size:
            problems.append(
                f"file is {human_size(self.size)} but metadata declares "
                f"{human_size(bd_size)}"
            )
        occupied: list[tuple[int, int, str]] = []
        for part in self.meta.partitions:
            exts = self.meta.extents_of(part)
            if len(exts) != part.num_extents:
                problems.append(f"{part.name}: extent table truncated")
            for ext in exts:
                begin = ext.target_data * lp.SECTOR_SIZE
                end = begin + ext.size_bytes
                if begin < self.data_start:
                    problems.append(
                        f"{part.name}: extent at {begin} overlaps metadata region"
                    )
                if bd_size and end > bd_size:
                    problems.append(f"{part.name}: extent ends past block device size")
                occupied.append((begin, end, part.name))
        occupied.sort()
        for (b1, e1, n1), (b2, e2, n2) in zip(occupied, occupied[1:]):
            if b2 < e1:
                problems.append(f"{n1} overlaps {n2}")
        for grp in self.meta.groups:
            used = sum(self.meta.partition_size(p) for p in self.meta.partitions
                       if self.meta.group_for(p).name == grp.name)
            if grp.maximum_size and used > grp.maximum_size:
                problems.append(
                    f"group {grp.name}: {human_size(used)} used > "
                    f"{human_size(grp.maximum_size)} maximum"
                )
        return problems


class SuperBuilder:
    """Build a new super image, normally seeded from an existing layout."""

    def __init__(self, base: lp.Metadata | None = None, *,
                 geometry: lp.Geometry | None = None,
                 block_device: lp.BlockDevice | None = None):
        if base is not None:
            self.base = base
            self.geometry = geometry or base.geometry
            self.groups = [lp.Group(g.name, g.flags, g.maximum_size)
                           for g in base.groups]
            self.block_device = block_device or (
                lp.BlockDevice(**vars(base.block_devices[0])) if base.block_devices
                else lp.BlockDevice())
            self.minor_version = base.minor_version
            self.header_size = base.header_size
        else:
            self.base = None
            self.geometry = geometry or lp.Geometry()
            self.groups = []
            self.block_device = block_device or lp.BlockDevice()
            self.minor_version = 2
            self.header_size = 256
        self.entries: list[Entry] = []
        self.notes: list[str] = []

    # --------------------------------------------------------------- compose
    def ensure_group(self, name: str) -> lp.Group:
        for g in self.groups:
            if g.name == name:
                return g
        grp = lp.Group(name=name, flags=0, maximum_size=0)
        self.groups.append(grp)
        return grp

    def add(self, entry: Entry) -> None:
        if any(e.name == entry.name for e in self.entries):
            raise Fail(f"partition {entry.name} added twice")
        entry.order = len(self.entries)
        self.ensure_group(entry.group)
        self.entries.append(entry)

    # ------------------------------------------------------------------ plan
    def plan(self, total_size: int | None = None,
             auto_grow_groups: bool = True,
             alignment: int | None = None) -> dict:
        """Compute metadata and layout; returns a plan dict for inspection."""
        geom = self.geometry
        align = alignment or self.block_device.alignment or geom.logical_block_size
        aoff = self.block_device.alignment_offset or 0

        def snap(value: int) -> int:
            return align_up(value - aoff, align) + aoff

        fls = max(self.block_device.first_logical_sector,
                  align_up(geom.metadata_end, lp.SECTOR_SIZE) // lp.SECTOR_SIZE)
        data_start = snap(fls * lp.SECTOR_SIZE)

        cursor = data_start
        extents: list[lp.Extent] = []
        partitions: list[lp.Partition] = []
        group_used: dict[str, int] = {}
        placements = []
        entry_extents: list[tuple[int, int]] = []   # (first_extent_index, count)
        block = geom.logical_block_size or 4096
        for entry in self.entries:
            size = align_up(entry.size, block)
            if size == 0:
                # Declared but empty (OPPO ships slot B like this).
                entry_extents.append((len(extents), 0))
                placements.append((entry, 0, 0))
                continue
            cursor = snap(cursor)
            if cursor % lp.SECTOR_SIZE:
                raise Fail(f"{entry.name}: misaligned offset {cursor}")
            extents.append(lp.Extent(size // lp.SECTOR_SIZE, lp.EXTENT_LINEAR,
                                     cursor // lp.SECTOR_SIZE, 0))
            entry_extents.append((len(extents) - 1, 1))
            group_used[entry.group] = group_used.get(entry.group, 0) + size
            placements.append((entry, cursor, size))
            cursor += size

        end = cursor
        if total_size is None:
            total_size = max(self.block_device.size or 0, end)
        if total_size < end:
            raise Fail(
                f"data does not fit: needs {human_size(end)} but the image size is "
                f"{human_size(total_size)}; pass a larger --super-size"
            )
        if total_size % geom.logical_block_size:
            total_size = align_up(total_size, geom.logical_block_size)

        for grp in self.groups:
            used = group_used.get(grp.name, 0)
            if used > grp.maximum_size:
                if not auto_grow_groups:
                    raise Fail(
                        f"group {grp.name} needs {human_size(used)} but its maximum is "
                        f"{human_size(grp.maximum_size)}; allow growth or fix the layout"
                    )
                grown = align_up(used, geom.logical_block_size)
                self.notes.append(
                    f"group {grp.name} grown {human_size(grp.maximum_size)} -> "
                    f"{human_size(grown)}"
                )
                grp.maximum_size = grown
            elif grp.maximum_size == 0:
                grp.maximum_size = align_up(used, geom.logical_block_size)

        # Extent table must be ordered exactly like the partition table.
        for entry, (first, count) in zip(self.entries, entry_extents):
            partitions.append(lp.Partition(
                name=entry.name,
                attributes=entry.attributes,
                first_extent_index=first,
                num_extents=count,
                group_index=self._group_index(entry.group),
            ))

        bd = lp.BlockDevice(
            first_logical_sector=fls,
            alignment=align,
            alignment_offset=aoff,
            size=total_size,
            partition_name=self.block_device.partition_name,
            flags=self.block_device.flags,
        )
        meta = lp.Metadata(
            geometry=geom,
            partitions=partitions,
            extents=extents,
            groups=self.groups,
            block_devices=[bd],
            minor_version=self.minor_version,
            header_size=self.header_size,
        )
        return {
            "metadata": meta,
            "data_start": data_start,
            "total_size": total_size,
            "placements": placements,
            "end": end,
            "groups": {g.name: g.maximum_size for g in self.groups},
        }

    def _group_index(self, name: str) -> int:
        for i, g in enumerate(self.groups):
            if g.name == name:
                return i
        raise Fail(f"internal error: unknown group {name}")

    # ----------------------------------------------------------------- write
    def write(self, out_path: str, plan: dict, log: Console | None = None,
              progress=None) -> None:
        geom = self.geometry
        meta = plan["metadata"]
        meta_bytes = meta.to_bytes()
        if len(meta_bytes) > geom.metadata_max_size:
            raise Fail(
                f"metadata ({len(meta_bytes)} bytes) exceeds metadata_max_size "
                f"({geom.metadata_max_size}); too many partitions for this layout"
            )
        total = plan["total_size"]
        tmp = out_path + ".tmp"
        template_handles: dict[str, object] = {}
        try:
            with open(tmp, "wb") as f:
                f.write(b"\0" * lp.RESERVED_BYTES)
                geom_bytes = geom.to_bytes()
                f.write(geom_bytes)          # primary geometry
                f.write(geom_bytes)          # geometry backup
                pad = geom.metadata_max_size - len(meta_bytes)
                for _slot in range(geom.metadata_slot_count):   # primary copies
                    f.write(meta_bytes)
                    f.write(b"\0" * pad)
                for _slot in range(geom.metadata_slot_count):   # backup copies
                    f.write(meta_bytes)
                    f.write(b"\0" * pad)
                f.seek(plan["data_start"])
                total_entries = len(plan["placements"])
                for i, (entry, offset, size) in enumerate(plan["placements"]):
                    if size == 0:
                        continue
                    f.seek(offset)
                    self._emit(f, entry, size, template_handles, log)
                    if progress:
                        progress(i + 1, total_entries, entry)
                f.truncate(total)
            os.replace(tmp, out_path)
        finally:
            for handle in template_handles.values():
                try:
                    handle.close()
                except Exception:
                    pass
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _emit(self, out, entry: Entry, size: int,
              handles: dict[str, object], log: Console | None) -> None:
        written = 0
        for buf in self._iter_entry(entry, size, handles):
            out.write(buf)
            written += len(buf)
        if written < size:
            if log:
                log.debug(f"  {entry.name}: zero-padded {human_size(size - written)}")
            out.write(b"\0" * (size - written))

    def _iter_entry(self, entry: Entry, size: int, handles: dict[str, object]):
        """Yield the data of one partition in block-aligned chunks.

        Reads are buffered so that short reads (network drives, filters) still
        produce block-aligned output; the sparse writer depends on that.
        """
        block = 4096
        written = 0
        pending = bytearray()

        def take(n: int) -> bytes:
            chunk = bytes(pending[:n])
            del pending[:n]
            return chunk

        if entry.path:
            with open(entry.path, "rb") as src:
                while written < size:
                    buf = _read_upto(src, min(IO_CHUNK, size - written))
                    if not buf:
                        break
                    written += len(buf)
                    pending.extend(buf)
                    while len(pending) >= IO_CHUNK:
                        yield take(IO_CHUNK)
        elif entry.template_extents is not None and entry.template_path:
            handle = handles.get(entry.template_path)
            if handle is None:
                handle = open(entry.template_path, "rb")
                handles[entry.template_path] = handle
            for ext in entry.template_extents:
                handle.seek(ext.target_data * lp.SECTOR_SIZE)
                remaining = min(ext.size_bytes, size - written)
                while remaining > 0:
                    buf = _read_upto(handle, min(IO_CHUNK, remaining))
                    if not buf:
                        raise Fail(f"{entry.name}: template extent runs past its file")
                    written += len(buf)
                    remaining -= len(buf)
                    pending.extend(buf)
                    while len(pending) >= IO_CHUNK:
                        yield take(IO_CHUNK)
                if written >= size:
                    break
        while written < size:
            n = min(IO_CHUNK, size - written)
            written += n
            pending.extend(b"\0" * n)
            while len(pending) >= IO_CHUNK:
                yield take(IO_CHUNK)
        if pending:
            pad = (-len(pending)) % block
            yield bytes(pending) + b"\0" * pad
            pending.clear()

    # ------------------------------------------------------------ sparse out
    def write_sparse(self, out_path: str, plan: dict, log: Console | None = None,
                     progress=None) -> None:
        """Write the image straight to Android sparse format.

        A 19 GiB super mostly consists of holes, so streaming sparse chunks
        avoids materialising a giant raw file first.
        """
        from . import sparse as sp

        geom = self.geometry
        meta_bytes = plan["metadata"].to_bytes()
        if len(meta_bytes) > geom.metadata_max_size:
            raise Fail(
                f"metadata ({len(meta_bytes)} bytes) exceeds metadata_max_size "
                f"({geom.metadata_max_size}); too many partitions for this layout"
            )
        total = plan["total_size"]
        blk = 4096
        data_start = plan["data_start"]
        end = plan["end"]

        head = bytearray(data_start)
        geom_bytes = geom.to_bytes()
        head[lp.RESERVED_BYTES:lp.RESERVED_BYTES + len(geom_bytes)] = geom_bytes
        head[lp.RESERVED_BYTES + lp.GEOMETRY_SIZE:
             lp.RESERVED_BYTES + 2 * lp.GEOMETRY_SIZE] = geom_bytes
        pos = geom.metadata_start
        for _ in range(geom.metadata_slot_count * 2):
            head[pos:pos + len(meta_bytes)] = meta_bytes
            pos += geom.metadata_max_size

        piece = IO_CHUNK

        # Segments in file order: metadata head, alignment gaps, partition data.
        segments: list[tuple[str, object, int]] = [("head", None, data_start)]
        cursor = data_start
        for entry, offset, size in plan["placements"]:
            if size == 0:
                continue
            if offset > cursor:
                segments.append(("gap", None, offset - cursor))
            segments.append(("data", entry, size))
            cursor = offset + size
        if total > cursor:
            segments.append(("gap", None, total - cursor))

        handles: dict[str, object] = {}
        tmp = out_path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                # Placeholder header; the real counts are written once the
                # stream is complete, so short reads cannot desynchronise it.
                f.write(b"\0" * sp.HEADER_SIZE)
                chunks_written = 0
                blocks_written = 0

                def emit_raw(buf: bytes) -> None:
                    nonlocal chunks_written, blocks_written
                    if len(buf) % blk:
                        buf = buf + b"\0" * (blk - len(buf) % blk)
                    nblocks = len(buf) // blk
                    f.write(struct.pack(sp.CHUNK_FMT, sp.CHUNK_RAW, 0, nblocks,
                                        sp.CHUNK_SIZE + len(buf)))
                    f.write(buf)
                    chunks_written += 1
                    blocks_written += nblocks

                def emit_hole(length: int) -> None:
                    nonlocal chunks_written, blocks_written
                    remaining = length
                    while remaining > 0:
                        n = min(piece, remaining)
                        nblocks = n // blk
                        if nblocks:
                            f.write(struct.pack(sp.CHUNK_FMT, sp.CHUNK_DONT_CARE,
                                                0, nblocks, sp.CHUNK_SIZE))
                            chunks_written += 1
                            blocks_written += nblocks
                        remaining -= nblocks * blk
                        if nblocks == 0:
                            break

                total_entries = sum(1 for _e, _o, s in plan["placements"] if s)
                done = 0
                for kind, entry, length in segments:
                    if kind == "head":
                        for start in range(0, length, piece):
                            emit_raw(bytes(head[start:start + piece]))
                    elif kind == "gap":
                        emit_hole(length)
                    else:
                        for buf in self._iter_entry(entry, length, handles):
                            emit_raw(buf)
                        if progress:
                            done += 1
                            progress(done, total_entries, entry)

                if blocks_written != total // blk:
                    raise Fail(
                        f"sparse writer covered {blocks_written} blocks but the "
                        f"image needs {total // blk}"
                    )
                f.seek(0)
                f.write(struct.pack(
                    sp.HEADER_FMT, sp.SPARSE_MAGIC, 1, 0,
                    sp.HEADER_SIZE, sp.CHUNK_SIZE, blk,
                    total // blk, chunks_written, 0,
                ))
            os.replace(tmp, out_path)
        finally:
            for handle in handles.values():
                try:
                    handle.close()
                except Exception:
                    pass
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
