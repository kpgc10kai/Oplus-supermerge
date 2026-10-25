"""Reader/writer for Android dynamic-partition metadata (liblp / lpmake).

Layout of a super image produced by lpmake (see
system/core/fs_mgr/liblp/utility.cpp):

    0x0000  reserved (LP_PARTITION_RESERVED_BYTES)
    0x1000  LpMetadataGeometry (4096 bytes, struct is 52 bytes)
    0x2000  geometry backup
    0x3000  primary metadata slot 0 .. slot N-1  (metadata_max_size each)
            backup metadata slot 0 .. slot N-1
            <data, aligned to the block-device alignment>
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

from .util import Fail, human_size, split_slot

GEOMETRY_MAGIC = 0x616C4467
GEOMETRY_STRUCT_SIZE = 52
GEOMETRY_SIZE = 4096
RESERVED_BYTES = 4096
HEADER_MAGIC = 0x414C5030
MAJOR_VERSION = 10
SECTOR_SIZE = 512

ATTR_READONLY = 0x1
ATTR_SLOT_SUFFIXED = 0x2
ATTR_UPDATED = 0x4
ATTR_DISABLED = 0x8

GROUP_SLOT_SUFFIXED = 0x1

EXTENT_LINEAR = 0
EXTENT_ZERO = 1

PART_ENTRY_SIZE = 52
EXTENT_ENTRY_SIZE = 24
GROUP_ENTRY_SIZE = 48
BLOCK_DEVICE_ENTRY_SIZE = 64


def attr_text(attrs: int) -> str:
    names = []
    if attrs & ATTR_READONLY:
        names.append("readonly")
    if attrs & ATTR_SLOT_SUFFIXED:
        names.append("slot-suffixed")
    if attrs & ATTR_UPDATED:
        names.append("updated")
    if attrs & ATTR_DISABLED:
        names.append("disabled")
    return "|".join(names) if names else "none"


@dataclass
class Geometry:
    metadata_max_size: int = 65536
    metadata_slot_count: int = 2
    logical_block_size: int = 4096

    @property
    def metadata_start(self) -> int:
        """Offset of the first primary metadata copy."""
        return RESERVED_BYTES + GEOMETRY_SIZE * 2

    @property
    def backup_metadata_start(self) -> int:
        return self.metadata_start + self.metadata_max_size * self.metadata_slot_count

    @property
    def metadata_end(self) -> int:
        """First byte available for partition data (all copies included)."""
        return self.metadata_start + self.metadata_max_size * self.metadata_slot_count * 2

    def to_bytes(self) -> bytes:
        buf = bytearray(GEOMETRY_SIZE)
        struct.pack_into("<II", buf, 0, GEOMETRY_MAGIC, GEOMETRY_STRUCT_SIZE)
        struct.pack_into("<III", buf, 40, self.metadata_max_size,
                         self.metadata_slot_count, self.logical_block_size)
        checksum = hashlib.sha256(bytes(buf[:GEOMETRY_STRUCT_SIZE])).digest()
        buf[8:40] = checksum
        return bytes(buf)


@dataclass
class Partition:
    name: str
    attributes: int = ATTR_READONLY
    first_extent_index: int = 0
    num_extents: int = 0
    group_index: int = 0

    @property
    def base_name(self) -> str:
        return split_slot(self.name)[0]


@dataclass
class Extent:
    num_sectors: int
    target_type: int = EXTENT_LINEAR
    target_data: int = 0
    target_source: int = 0

    @property
    def size_bytes(self) -> int:
        return self.num_sectors * SECTOR_SIZE


@dataclass
class Group:
    name: str = "default"
    flags: int = 0
    maximum_size: int = 0


@dataclass
class BlockDevice:
    first_logical_sector: int = 2048
    alignment: int = 1 << 20
    alignment_offset: int = 0
    size: int = 0
    partition_name: str = "super"
    flags: int = 0


@dataclass
class Metadata:
    geometry: Geometry = field(default_factory=Geometry)
    partitions: list[Partition] = field(default_factory=list)
    extents: list[Extent] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    block_devices: list[BlockDevice] = field(default_factory=list)
    major_version: int = MAJOR_VERSION
    minor_version: int = 2
    header_size: int = 256
    flags: int = 0

    # ------------------------------------------------------------- accessors
    def group(self, name: str) -> Group | None:
        for g in self.groups:
            if g.name == name:
                return g
        return None

    def group_for(self, part: Partition) -> Group:
        if 0 <= part.group_index < len(self.groups):
            return self.groups[part.group_index]
        return Group()

    def partition(self, name: str) -> Partition | None:
        for p in self.partitions:
            if p.name == name:
                return p
        return None

    def extents_of(self, part: Partition) -> list[Extent]:
        start = part.first_extent_index
        return self.extents[start:start + part.num_extents]

    def total_sectors(self, part: Partition) -> int:
        return sum(e.num_sectors for e in self.extents_of(part))

    def partition_size(self, part: Partition) -> int:
        return self.total_sectors(part) * SECTOR_SIZE

    # ------------------------------------------------------------- serialize
    def to_bytes(self) -> bytes:
        part_blob = b"".join(
            struct.pack("<36sIIII", _name_bytes(p.name), p.attributes,
                        p.first_extent_index, p.num_extents, p.group_index)
            for p in self.partitions
        )
        ext_blob = b"".join(
            struct.pack("<QIQI", e.num_sectors, e.target_type,
                        e.target_data, e.target_source)
            for e in self.extents
        )
        grp_blob = b"".join(
            struct.pack("<36sIQ", _name_bytes(g.name), g.flags, g.maximum_size)
            for g in self.groups
        )
        bd_blob = b"".join(
            struct.pack("<QIIQ36sI", b.first_logical_sector, b.alignment,
                        b.alignment_offset, b.size, _name_bytes(b.partition_name),
                        b.flags)
            for b in self.block_devices
        )

        descs = []
        offset = 0
        for blob, count, esize in (
            (part_blob, len(self.partitions), PART_ENTRY_SIZE),
            (ext_blob, len(self.extents), EXTENT_ENTRY_SIZE),
            (grp_blob, len(self.groups), GROUP_ENTRY_SIZE),
            (bd_blob, len(self.block_devices), BLOCK_DEVICE_ENTRY_SIZE),
        ):
            descs.append((offset, count, esize))
            offset += len(blob)
        tables = part_blob + ext_blob + grp_blob + bd_blob

        header_size = max(128, self.header_size or 128)
        if header_size not in (128, 256):
            header_size = 256 if self.minor_version >= 2 else 128
        buf = bytearray(header_size)
        struct.pack_into("<IHHI", buf, 0, HEADER_MAGIC, self.major_version,
                         self.minor_version, header_size)
        struct.pack_into("<I", buf, 44, len(tables))
        buf[48:80] = hashlib.sha256(tables).digest()
        for i, (off, count, esize) in enumerate(descs):
            struct.pack_into("<III", buf, 80 + i * 12, off, count, esize)
        if header_size >= 132:
            struct.pack_into("<I", buf, 128, self.flags)
        buf[12:44] = hashlib.sha256(bytes(buf)).digest()
        return bytes(buf) + tables

    # --------------------------------------------------------------- parse
    @classmethod
    def from_bytes(cls, geom: Geometry, raw: bytes) -> "Metadata":
        if len(raw) < 128:
            raise Fail("metadata block is shorter than 128 bytes")
        magic, major, minor, header_size = struct.unpack_from("<IHHI", raw, 0)
        if magic != HEADER_MAGIC:
            raise Fail(f"bad metadata header magic 0x{magic:08x}")
        if header_size < 128 or header_size > len(raw):
            raise Fail(f"bad metadata header size {header_size}")
        expected = raw[12:44]
        hdr = bytearray(raw[:header_size])
        hdr[12:44] = b"\0" * 32
        if hashlib.sha256(bytes(hdr)).digest() != expected:
            raise Fail("metadata header checksum mismatch")
        (tables_size,) = struct.unpack_from("<I", raw, 44)
        tables_checksum = raw[48:80]
        if header_size + tables_size > len(raw):
            raise Fail("metadata tables extend past the metadata block")
        tables = raw[header_size:header_size + tables_size]
        if hashlib.sha256(tables).digest() != tables_checksum:
            raise Fail("metadata tables checksum mismatch")

        descs = [struct.unpack_from("<III", raw, 80 + i * 12) for i in range(4)]

        def entries(idx: int, min_size: int):
            offset, count, esize = descs[idx]
            if esize < min_size:
                raise Fail(f"unexpected entry size {esize} in metadata table {idx}")
            if offset + count * esize > len(tables):
                raise Fail(f"metadata table {idx} overruns the table section")
            for i in range(count):
                yield tables[offset + i * esize: offset + (i + 1) * esize]

        meta = cls(
            geometry=geom,
            major_version=major,
            minor_version=minor,
            header_size=header_size,
        )
        for raw_ent in entries(0, PART_ENTRY_SIZE):
            name, attrs, first, num, grp = struct.unpack_from("<36sIIII", raw_ent, 0)
            meta.partitions.append(Partition(
                name=_read_name(name), attributes=attrs,
                first_extent_index=first, num_extents=num, group_index=grp,
            ))
        for raw_ent in entries(1, EXTENT_ENTRY_SIZE):
            num_sectors, ttype, tdata, tsource = struct.unpack_from("<QIQI", raw_ent, 0)
            meta.extents.append(Extent(num_sectors, ttype, tdata, tsource))
        for raw_ent in entries(2, GROUP_ENTRY_SIZE):
            name, flags, max_size = struct.unpack_from("<36sIQ", raw_ent, 0)
            meta.groups.append(Group(_read_name(name), flags, max_size))
        for raw_ent in entries(3, BLOCK_DEVICE_ENTRY_SIZE):
            fls, align, aoff, size, name, flags = struct.unpack_from(
                "<QIIQ36sI", raw_ent, 0)
            meta.block_devices.append(BlockDevice(fls, align, aoff, size,
                                                  _read_name(name), flags))
        if header_size >= 132:
            (meta.flags,) = struct.unpack_from("<I", raw, 128)
        if not meta.block_devices:
            raise Fail("metadata declares no block devices")
        for part in meta.partitions:
            if part.first_extent_index + part.num_extents > len(meta.extents):
                raise Fail(f"partition {part.name} references extents that are not present")
            if part.group_index >= len(meta.groups):
                raise Fail(f"partition {part.name} references a missing group")
        return meta


def _name_bytes(name: str) -> bytes:
    raw = name.encode("utf-8")
    if len(raw) > 36:
        raise Fail(f"partition/group name too long (max 36 bytes): {name}")
    return raw.ljust(36, b"\0")


def _read_name(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


# ------------------------------------------------------------------ reading

def read_geometry(path: str, offset: int = RESERVED_BYTES) -> Geometry:
    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read(GEOMETRY_SIZE)
    if len(raw) < GEOMETRY_STRUCT_SIZE:
        raise Fail(f"{path}: too small to contain super metadata")
    magic, struct_size = struct.unpack_from("<II", raw, 0)
    if magic != GEOMETRY_MAGIC:
        raise Fail(f"{path}: no LP geometry at offset 0x{offset:x}")
    if struct_size < 48 or struct_size > GEOMETRY_SIZE:
        raise Fail(f"{path}: unsupported geometry struct size {struct_size}")
    got = raw[8:40]
    probe = bytearray(raw[:struct_size])
    probe[8:40] = b"\0" * 32
    if hashlib.sha256(bytes(probe)).digest() != got:
        raise Fail(f"{path}: geometry checksum mismatch")
    max_size, slot_count, block_size = struct.unpack_from("<III", raw, 40)
    if not (4096 <= max_size <= (1 << 22)):
        raise Fail(f"{path}: implausible metadata_max_size {max_size}")
    if not (1 <= slot_count <= 4):
        raise Fail(f"{path}: implausible metadata_slot_count {slot_count}")
    if not (512 <= block_size <= 65536) or (block_size & (block_size - 1)):
        raise Fail(f"{path}: implausible logical block size {block_size}")
    return Geometry(max_size, slot_count, block_size)


def find_geometry_offset(path: str) -> int:
    for offset in (RESERVED_BYTES, RESERVED_BYTES + GEOMETRY_SIZE, 0):
        try:
            read_geometry(path, offset)
            return offset
        except Fail:
            continue
    raise Fail(f"{path}: does not look like a super image (no LP geometry found)")


def read_metadata(path: str, offset: int | None = None,
                  expand_slots: bool = True) -> tuple[Metadata, int]:
    if offset is None:
        offset = find_geometry_offset(path)
    geom = read_geometry(path, offset)
    candidates: list[tuple[str, int]] = []
    for slot in range(geom.metadata_slot_count):
        candidates.append(("primary", geom.metadata_start + slot * geom.metadata_max_size))
    for slot in range(geom.metadata_slot_count):
        candidates.append(("backup", geom.backup_metadata_start
                           + slot * geom.metadata_max_size))
    errors = []
    with open(path, "rb") as f:
        for kind, pos in candidates:
            f.seek(pos)
            raw = f.read(geom.metadata_max_size)
            if not raw:
                continue
            try:
                meta = Metadata.from_bytes(geom, raw)
            except Fail as exc:
                errors.append(f"{kind} slot: {exc}")
                continue
            if expand_slots:
                meta = expand_slot_attributes(meta)
            return meta, offset
    detail = "; ".join(errors) if errors else "no metadata copies found"
    raise Fail(f"{path}: no readable metadata ({detail})")


def expand_slot_attributes(meta: Metadata) -> Metadata:
    """Resolve LP_PARTITION_ATTR_SLOT_SUFFIXED the way liblp does at runtime.

    A slot-suffixed partition is one entry that maps to ``name_a`` or
    ``name_b`` depending on which metadata slot is read.  Since a super.img
    carries the same table in every slot, expand such entries into both
    concrete partitions so the file describes what the device will map.
    """
    if not any(p.attributes & ATTR_SLOT_SUFFIXED for p in meta.partitions) and \
            not any(g.flags & GROUP_SLOT_SUFFIXED for g in meta.groups):
        return meta

    groups: list[Group] = []
    group_index: dict[int, tuple[int, int]] = {}
    for idx, grp in enumerate(meta.groups):
        if grp.flags & GROUP_SLOT_SUFFIXED:
            base = split_slot(grp.name)[0]
            groups.append(Group(base + "_a", grp.flags & ~GROUP_SLOT_SUFFIXED, grp.maximum_size))
            groups.append(Group(base + "_b", grp.flags & ~GROUP_SLOT_SUFFIXED, grp.maximum_size))
            group_index[idx] = (len(groups) - 2, len(groups) - 1)
        else:
            groups.append(Group(grp.name, grp.flags, grp.maximum_size))
            group_index[idx] = (len(groups) - 1, len(groups) - 1)

    partitions: list[Partition] = []
    extents: list[Extent] = []
    for part in meta.partitions:
        own = meta.extents_of(part)
        idx = group_index.get(part.group_index, (0, 0))
        if part.attributes & ATTR_SLOT_SUFFIXED:
            base, existing = split_slot(part.name)
            if existing:
                # Already carries a concrete slot suffix; just drop the flag.
                partitions.append(Partition(
                    part.name, part.attributes & ~ATTR_SLOT_SUFFIXED,
                    len(extents), len(own), idx[0]))
                extents.extend(Extent(e.num_sectors, e.target_type,
                                     e.target_data, e.target_source) for e in own)
                continue
            for suffix, gi in (("_a", idx[0]), ("_b", idx[1])):
                partitions.append(Partition(
                    base + suffix,
                    part.attributes & ~ATTR_SLOT_SUFFIXED,
                    len(extents), len(own), gi,
                ))
                extents.extend(Extent(e.num_sectors, e.target_type,
                                     e.target_data, e.target_source) for e in own)
        else:
            partitions.append(Partition(
                part.name, part.attributes, len(extents), len(own), idx[0]))
            extents.extend(Extent(e.num_sectors, e.target_type,
                                 e.target_data, e.target_source) for e in own)

    block_devices = []
    for bd in meta.block_devices:
        block_devices.append(BlockDevice(
            bd.first_logical_sector, bd.alignment, bd.alignment_offset,
            bd.size, bd.partition_name, bd.flags & ~1,
        ))
    return Metadata(
        geometry=meta.geometry, partitions=partitions, extents=extents,
        groups=groups, block_devices=block_devices,
        major_version=meta.major_version, minor_version=meta.minor_version,
        header_size=meta.header_size, flags=meta.flags,
    )


def is_super_image(path: str) -> bool:
    try:
        find_geometry_offset(path)
        return True
    except Fail:
        return False


def metadata_data_start(geom: Geometry) -> int:
    return geom.metadata_end


def strict_validate(meta: Metadata, file_size: int | None = None) -> list[str]:
    """Apply the checks liblp's ParseGeometry/ParseMetadata performs.

    Kept deliberately close to system/core/fs_mgr/liblp/reader.cpp so a built
    image is accepted by lpdump, fastbootd and the device's own liblp.
    """
    problems: list[str] = []
    geom = meta.geometry
    if meta.major_version != MAJOR_VERSION:
        problems.append(f"major version {meta.major_version} != {MAJOR_VERSION}")
    if meta.minor_version > 2:
        problems.append(f"minor version {meta.minor_version} is newer than 2")
    expected_header = 256 if meta.minor_version >= 2 else 128
    if meta.header_size != expected_header:
        problems.append(
            f"header_size {meta.header_size} does not match version "
            f"10.{meta.minor_version} (expected {expected_header})"
        )
    if geom.metadata_max_size % SECTOR_SIZE:
        problems.append("metadata_max_size is not sector aligned")
    if geom.metadata_slot_count == 0:
        problems.append("metadata_slot_count is zero")
    if not meta.block_devices:
        problems.append("no block devices")
    valid_attrs = ATTR_READONLY | ATTR_SLOT_SUFFIXED
    if meta.minor_version >= 1:
        valid_attrs |= ATTR_UPDATED | ATTR_DISABLED
    import re
    name_re = re.compile(r"^[A-Za-z0-9_]+$")
    for part in meta.partitions:
        if part.attributes & ~valid_attrs:
            problems.append(f"{part.name}: unknown attribute bits 0x{part.attributes:x}")
        if part.num_extents == 0 and part.attributes & ATTR_DISABLED:
            problems.append(f"{part.name}: disabled partition has extents")
        if part.first_extent_index + part.num_extents > len(meta.extents):
            problems.append(f"{part.name}: extent list out of range")
        if part.group_index >= len(meta.groups):
            problems.append(f"{part.name}: group index out of range")
        if not name_re.match(part.name):
            problems.append(f"{part.name}: name has characters liblp rejects")
    for i, ext in enumerate(meta.extents):
        if ext.target_type == EXTENT_LINEAR and ext.target_source >= len(meta.block_devices):
            problems.append(f"extent {i}: invalid target block device")
    if meta.block_devices:
        region = geom.metadata_end
        if meta.block_devices[0].first_logical_sector * SECTOR_SIZE < region:
            problems.append(
                "first_logical_sector points inside the metadata region"
            )
        if file_size is not None and file_size < region:
            problems.append("file is smaller than the metadata region")
        if meta.block_devices[0].size and file_size is not None \
                and file_size > meta.block_devices[0].size:
            problems.append("file is larger than the declared block device size")
    return problems


def describe(meta: Metadata, path: str | None = None) -> str:
    lines = []
    if path:
        lines.append(f"image: {path}")
    lines.append(f"metadata version: {meta.major_version}.{meta.minor_version} "
                 f"(header {meta.header_size} bytes)")
    lines.append(f"metadata max size: {meta.geometry.metadata_max_size} bytes, "
                 f"{meta.geometry.metadata_slot_count} slot(s)")
    lines.append(f"logical block size: {meta.geometry.logical_block_size}")
    for bd in meta.block_devices:
        lines.append(f"block device: {bd.partition_name}  size={bd.size} "
                     f"({human_size(bd.size)})  alignment={bd.alignment} "
                     f"alignment_offset={bd.alignment_offset} "
                     f"first_logical_sector={bd.first_logical_sector}")
    lines.append("")
    lines.append(f"{'partition':<24}{'group':<26}{'size':>12}  extents  attributes")
    for part in meta.partitions:
        grp = meta.group_for(part)
        exts = meta.extents_of(part)
        first = ""
        if exts:
            first = f"@{exts[0].target_data * SECTOR_SIZE // 1024}K"
        lines.append(
            f"{part.name:<24}{grp.name:<26}{human_size(meta.partition_size(part)):>12}"
            f"  {len(exts):<7}  {attr_text(part.attributes)} {first}"
        )
    if meta.groups:
        lines.append("")
        for g in meta.groups:
            used = sum(meta.partition_size(p) for p in meta.partitions
                       if meta.group_for(p) is g or meta.group_for(p).name == g.name)
            lines.append(f"group {g.name:<26} max={human_size(g.maximum_size):>10} "
                         f"used={human_size(used):>10}")
    return "\n".join(lines)
