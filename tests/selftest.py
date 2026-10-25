#!/usr/bin/env python3
"""Self-tests: synthesise payload.bin / super.img fixtures, then exercise the
real code paths end to end.  Run with:

    python tools/supermerge/tests/selftest.py
"""

from __future__ import annotations

import bz2
import hashlib
import io
import lzma
import os
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from smlib import lp, protowire, sparse  # noqa: E402
from smlib import payload as payload_mod  # noqa: E402
from smlib.payload import Payload  # noqa: E402
from smlib.superimg import Entry, SuperBuilder, SuperImage  # noqa: E402
from smlib.util import Fail, align_up, scratch_dir, scratch_path  # noqa: E402

PASSED = 0
FAILED = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}" + (f"  ({detail})" if detail else ""))


# --------------------------------------------------------------- fixtures

BLK = 4096


class PayloadFixture:
    """Builds a real payload.bin from in-memory partition images."""

    def __init__(self, version: int = 2):
        self.version = version
        self.parts: list[tuple[str, int, list[bytes]]] = []
        self.blobs = bytearray()

    def add(self, name: str, image: bytes, compress: str | None = None,
            zero_blocks: int = 0, old_size: int = 0, source_copy: bool = False,
            source: bytes | None = None) -> None:
        assert len(image) % BLK == 0
        ops: list[bytes] = []
        if source_copy:
            # one SOURCE_COPY op covering the whole image, reading `source`
            nblocks = len(source) // BLK
            ext = protowire.enc_msg_field(6, protowire.enc_varint_field(1, 0)
                                          + protowire.enc_varint_field(2, nblocks))
            src_ext = protowire.enc_msg_field(4, protowire.enc_varint_field(1, 0)
                                              + protowire.enc_varint_field(2, nblocks))
            ops.append(protowire.enc_varint_field(1, payload_mod.OP_SOURCE_COPY)
                       + ext + src_ext)
        else:
            block = 0
            body = image
            segs = []
            usable = len(body) - zero_blocks * BLK
            cursor = 0
            while cursor < usable:
                segs.append(body[cursor:cursor + BLK])
                cursor += BLK
            for seg in segs:
                raw = seg
                if compress == "xz":
                    data = lzma.compress(raw)
                    op_type = payload_mod.OP_REPLACE_XZ
                elif compress == "bz":
                    data = bz2.compress(raw)
                    op_type = payload_mod.OP_REPLACE_BZ
                else:
                    data = raw
                    op_type = payload_mod.OP_REPLACE
                offset = len(self.blobs)
                self.blobs += data
                digest = hashlib.sha256(data).digest()
                ext = protowire.enc_msg_field(
                    6, protowire.enc_varint_field(1, block)
                    + protowire.enc_varint_field(2, 1))
                ops.append(
                    protowire.enc_varint_field(1, op_type)
                    + protowire.enc_varint_field(2, offset)
                    + protowire.enc_varint_field(3, len(data))
                    + protowire.enc_bytes_field(8, digest)
                    + ext
                )
                block += 1
            if zero_blocks:
                ext = protowire.enc_msg_field(
                    6, protowire.enc_varint_field(1, block)
                    + protowire.enc_varint_field(2, zero_blocks))
                ops.append(protowire.enc_varint_field(1, payload_mod.OP_ZERO) + ext)
        self.parts.append((name, len(image), ops, old_size))

    def write(self, path: str) -> None:
        manifest = bytearray()
        for name, size, ops, old_size in self.parts:
            pu = protowire.enc_bytes_field(1, name.encode())
            if old_size:
                pu += protowire.enc_msg_field(
                    6, protowire.enc_varint_field(1, old_size))
            pu += protowire.enc_msg_field(7, protowire.enc_varint_field(1, size))
            for op in ops:
                pu += protowire.enc_msg_field(8, op)
            manifest += protowire.enc_msg_field(13, pu)
        header = b"CrAU" + struct.pack(">Q", self.version) + struct.pack(">Q", len(manifest))
        if self.version >= 2:
            header += struct.pack(">I", 0)
        with open(path, "wb") as f:
            f.write(header)
            f.write(manifest)
            f.write(self.blobs)


def make_image(seed: int, blocks: int) -> bytes:
    out = bytearray()
    for i in range(blocks):
        out += bytes(((seed * 37 + i * 11 + j) & 0xFF) for j in range(BLK))
    return bytes(out)


def build_super(path: str, entries: list[tuple[str, str, bytes]], total: int,
                groups: list[str] | None = None) -> None:
    geom = lp.Geometry(metadata_max_size=65536, metadata_slot_count=2,
                       logical_block_size=4096)
    bd = lp.BlockDevice(first_logical_sector=2048, alignment=1 << 20,
                        alignment_offset=0, size=total, partition_name="super")
    group_names = groups or ["default"]
    base = lp.Metadata(
        geometry=geom,
        groups=[lp.Group(name=g, flags=0, maximum_size=total // 2) for g in group_names],
        block_devices=[bd],
        minor_version=2,
        header_size=256,
    )
    builder = SuperBuilder(base)
    tmpfiles = []
    for name, group, data in entries:
        assert len(data) % 4096 == 0
        tmp = scratch_path(os.path.dirname(path), "part_", ".bin")
        with open(tmp, "wb") as f:
            f.write(data)
        tmpfiles.append(tmp)
        builder.add(Entry(name, group, lp.ATTR_READONLY,
                          len(data), path=tmp, origin="fixture"))
    plan = builder.plan(total_size=total)
    builder.write(path, plan)
    for tmp in tmpfiles:
        os.remove(tmp)


# ------------------------------------------------------------------ tests

def test_sparse(tmp: str) -> None:
    print("sparse image round-trip")
    raw = os.path.join(tmp, "raw.img")
    sp = os.path.join(tmp, "sparse.img")
    back = os.path.join(tmp, "back.img")
    data = bytearray()
    data += b"\x00" * (4 * BLK)
    data += b"\xAA" * (2 * BLK)
    data += os.urandom(3 * BLK)
    data += b"\x00" * (2 * BLK)
    with open(raw, "wb") as f:
        f.write(data)
    sparse.encode(raw, sp)
    check(os.path.getsize(sp) < len(data) // 2, "sparse is smaller than raw")
    sparse.decode(sp, back)
    with open(back, "rb") as f:
        check(f.read() == bytes(data), "sparse decode reproduces the input")
    check(sparse.is_sparse(sp), "magic detected")
    check(not sparse.is_sparse(raw), "raw is not detected as sparse")


def test_lp_roundtrip(tmp: str) -> None:
    print("liblp metadata round-trip")
    path = os.path.join(tmp, "super_fixture.img")
    build_super(path, [
        ("system_a", "default", make_image(1, 4)),
        ("vendor_a", "qti_dynamic_partitions", make_image(2, 3)),
    ], total=32 << 20, groups=["default", "qti_dynamic_partitions"])
    meta, off = lp.read_metadata(path)
    check(off == 4096, "geometry at 0x1000")
    check(meta.geometry.metadata_slot_count == 2, "slot count preserved")
    check(meta.geometry.metadata_start == 0x3000, "primary metadata offset 0x3000")
    check(meta.geometry.backup_metadata_start == 0x3000 + 2 * 65536,
          "backup metadata follows the primary copies")
    with open(path, "rb") as f:
        f.seek(0x1000)
        check(struct.unpack("<I", f.read(4))[0] == lp.GEOMETRY_MAGIC,
              "primary geometry magic at 0x1000")
        f.seek(0x2000)
        check(struct.unpack("<I", f.read(4))[0] == lp.GEOMETRY_MAGIC,
              "geometry backup magic at 0x2000")
        f.seek(0x3000)
        check(struct.unpack("<I", f.read(4))[0] == lp.HEADER_MAGIC,
              "primary metadata magic at 0x3000")
        f.seek(meta.geometry.backup_metadata_start)
        check(struct.unpack("<I", f.read(4))[0] == lp.HEADER_MAGIC,
              "backup metadata magic")
    data_start = align_up(meta.geometry.metadata_end, 1 << 20)
    check(meta.extents[0].target_data * 512 == data_start,
          "first extent starts at the aligned metadata end")
    check([p.name for p in meta.partitions] == ["system_a", "vendor_a"],
          "partition order preserved")
    check(meta.partition_size(meta.partition("system_a")) == 4 * BLK,
          "partition size preserved")
    check(meta.group_for(meta.partition("system_a")).name == "default", "group mapping")
    with SuperImage(path) as img:
        check(img.check() == [], "freshly built image passes its own checks")
        check(img.strict_check() == [], "liblp conformance checks pass")
        blobs = b"".join(img.iter_partition("vendor_a"))
        check(blobs == make_image(2, 3), "vendor_a content matches")

    # checksum tamper detection: every metadata copy must be damaged
    with open(path, "r+b") as f:
        for base in (0x3000, 0x3000 + 2 * 65536):
            for slot in range(2):
                f.seek(base + slot * 65536 + 200)
                f.write(b"\x00\x01\x02")
    try:
        lp.read_metadata(path)
        check(False, "corrupt metadata is rejected")
    except Fail:
        check(True, "corrupt metadata is rejected")


def test_slot_expansion(tmp: str) -> None:
    print("slot-suffixed metadata expansion")
    path = os.path.join(tmp, "retrofit.img")
    geom = lp.Geometry(65536, 2, 4096)
    bd = lp.BlockDevice(2048, 1 << 20, 0, 16 << 20, "super", 1)
    meta = lp.Metadata(geometry=geom,
                       groups=[lp.Group("group", lp.GROUP_SLOT_SUFFIXED, 8 << 20)],
                       block_devices=[bd], minor_version=2, header_size=256)
    builder = SuperBuilder(meta)
    data = make_image(70, 2)
    src = os.path.join(tmp, "retro_sys.img")
    with open(src, "wb") as f:
        f.write(data)
    builder.add(Entry("system", "group",
                      lp.ATTR_READONLY | lp.ATTR_SLOT_SUFFIXED, len(data), path=src))
    builder.write(path, builder.plan(total_size=16 << 20))

    raw, _ = lp.read_metadata(path, expand_slots=False)
    check([p.name for p in raw.partitions] == ["system"],
          "raw metadata keeps the unsuffixed name")
    check(raw.partitions[0].attributes & lp.ATTR_SLOT_SUFFIXED, "raw attribute intact")
    expanded, _ = lp.read_metadata(path)
    check([p.name for p in expanded.partitions] == ["system_a", "system_b"],
          "slot attribute expanded to both slots")
    check([g.name for g in expanded.groups] == ["group_a", "group_b"],
          "slot-suffixed group expanded")
    check(all(not (p.attributes & lp.ATTR_SLOT_SUFFIXED) for p in expanded.partitions),
          "expanded partitions drop the slot attribute")
    with SuperImage(path) as img:
        check(b"".join(img.iter_partition("system_b")) == data,
              "both slots map the same data")


def test_payload(tmp: str) -> None:
    print("payload.bin extraction")
    fx = PayloadFixture()
    raw_img = make_image(10, 6)
    xz_img = make_image(11, 5)
    bz_head = make_image(12, 2)
    bz_img = bz_head + b"\x00" * (2 * BLK)
    fx.add("system", raw_img)
    fx.add("product", xz_img, compress="xz")
    fx.add("vendor", bz_img, compress="bz", zero_blocks=2)
    p = os.path.join(tmp, "payload.bin")
    fx.write(p)

    pl = Payload(p)
    check(len(pl.partitions) == 3, "three partitions parsed")
    check(pl.block_size == 4096, "block size defaulted to 4096")
    out = os.path.join(tmp, "parts")
    pl.extract_all(out, verify_hash=False)
    with open(os.path.join(out, "system.img"), "rb") as f:
        check(f.read(len(raw_img)) == raw_img, "REPLACE extraction")
    with open(os.path.join(out, "product.img"), "rb") as f:
        check(f.read(len(xz_img)) == xz_img, "REPLACE_XZ extraction")
    with open(os.path.join(out, "vendor.img"), "rb") as f:
        got = f.read()
    check(got == bz_img, "REPLACE_BZ + ZERO extraction")

    # corrupt one data byte -> hash check must fire
    with open(p, "r+b") as f:
        f.seek(pl.data_offset_base + 64)
        b = f.read(1)
        f.seek(pl.data_offset_base + 64)
        f.write(bytes([b[0] ^ 0xFF]))
    bad = Payload(p)
    try:
        bad.extract(bad.partitions[0], os.path.join(tmp, "bad.img"))
        check(False, "corrupt payload data is detected")
    except Fail as exc:
        check("hash mismatch" in str(exc), "corrupt payload data is detected")


def test_payload_source_copy(tmp: str) -> None:
    print("delta payload (SOURCE_COPY)")
    old = make_image(20, 4)
    fx = PayloadFixture()
    fx.add("system", old, source_copy=True, old_size=len(old), source=old)
    p = os.path.join(tmp, "payload_delta.bin")
    fx.write(p)
    pl = Payload(p)
    part = pl.partitions[0]
    check(part.is_delta, "delta operation detected")
    old_path = os.path.join(tmp, "old_system.img")
    with open(old_path, "wb") as f:
        f.write(old)
    out = os.path.join(tmp, "delta_out.img")
    pl.extract(part, out, old_image=old_path)
    with open(out, "rb") as f:
        check(f.read() == old, "SOURCE_COPY reproduces the new image")


def test_bspatch(tmp: str) -> None:
    print("bsdiff patch application")
    from smlib import bspatch
    old = bytes([10, 20, 30, 40])
    entry1 = (4).to_bytes(8, "little") + (0).to_bytes(8, "little") + (0).to_bytes(8, "little")
    entry2 = (0).to_bytes(8, "little") + (3).to_bytes(8, "little") + (0).to_bytes(8, "little")
    ctrl_bz = bz2.compress(entry1 + entry2)
    diff = bytes([1, 2, 3, 4])
    diff_bz = bz2.compress(diff)
    extra = b"abc"
    extra_bz = bz2.compress(extra)
    patch = (b"BSDIFF40" + struct.pack("<8s8s8s", len(ctrl_bz).to_bytes(8, "little"),
                                       len(diff_bz).to_bytes(8, "little"),
                                       (7).to_bytes(8, "little"))
             + ctrl_bz + diff_bz + extra_bz)
    new = bspatch.apply(patch, old)
    check(new == bytes([11, 22, 33, 44]) + b"abc", "diff+extra applied")


def test_region_table(tmp: str) -> None:
    print("region table lookups")
    from smlib import regions
    table = regions.load_table()
    check(regions.find(table, "IN").code == "IN", "resolves by code")
    check(regions.find(table, "in").code == "IN", "resolves case-insensitively")
    check(regions.find(table, "00011011").code == "IN", "resolves by NV value")
    check(regions.find(table, "india").code == "IN", "resolves by alias/name")
    check(regions.find(table, "ZZ") is None, "unknown code returns None")
    check(regions.is_generic("generic"), "generic sentinel recognised")
    check(table["IN"].matches("in_00011011"), "tag with code+numeric matches")
    check(not table["IN"].matches("RU"), "tag of another region does not match")


def _region_fixture(tmp: str):
    work = os.path.join(tmp, "regions_e2e")
    os.makedirs(work, exist_ok=True)
    tmpl = os.path.join(work, "super_template.img")
    build_super(tmpl, [("system_a", "default", make_image(80, 3)),
                       ("my_preload", "default", make_image(81, 2))],
                total=32 << 20)
    edl = os.path.join(work, "edl")
    os.makedirs(edl, exist_ok=True)
    variants = {
        "generic": make_image(90, 2),
        "IN": make_image(91, 2),
        "RU": make_image(92, 2),
    }
    for tag, data in variants.items():
        name = "my_preload.img" if tag == "generic" else f"my_preload_{tag}.img"
        with open(os.path.join(edl, name), "wb") as f:
            f.write(data)
    return work, tmpl, edl, variants


def test_region_build(tmp: str) -> None:
    print("region variant selection in build")
    import supermerge
    work, tmpl, edl, variants = _region_fixture(tmp)
    scratch = os.path.join(work, "scratch")

    def run(out_name, extra):
        out = os.path.join(work, out_name)
        return supermerge.main(["build", "--template", tmpl, "--edl", edl,
                                "--out", out, "--force", "-q",
                                "--workdir", scratch] + extra), out

    rc, _ = run("none.img", [])
    check(rc != 0, "ambiguous variants without --region are refused")

    rc, out = run("in.img", ["--region", "IN"])
    check(rc == 0, "--region IN builds")
    with SuperImage(out) as img:
        check(b"".join(img.iter_partition("my_preload")) == variants["IN"],
              "IN variant selected")
        check(b"".join(img.iter_partition("system_a")) == make_image(80, 3),
              "non-regional partition still comes from the template")

    rc, out = run("nv.img", ["--region", "00011011"])
    check(rc == 0, "--region by NV value builds")
    with SuperImage(out) as img:
        check(b"".join(img.iter_partition("my_preload")) == variants["IN"],
              "NV value 00011011 maps to the IN variant")

    rc, out = run("gen.img", ["--region", "generic"])
    check(rc == 0, "--region generic builds")
    with SuperImage(out) as img:
        check(b"".join(img.iter_partition("my_preload")) == variants["generic"],
              "generic variant selected")

    rc, _ = run("us.img", ["--region", "US"])
    check(rc != 0, "region with no matching file is refused")

    ru_path = os.path.join(edl, "my_preload_RU.img")
    rc, out = run("manual.img", ["--region", "IN", "--variant", f"my_preload={ru_path}"])
    check(rc == 0, "--variant override builds")
    with SuperImage(out) as img:
        check(b"".join(img.iter_partition("my_preload")) == variants["RU"],
              "--variant overrides the region choice")


def test_region_super_selection(tmp: str) -> None:
    print("region-based super template selection")
    import supermerge
    work = os.path.join(tmp, "regions_super")
    os.makedirs(work, exist_ok=True)
    in_img = make_image(100, 2)
    ru_img = make_image(101, 3)
    build_super(os.path.join(work, "super_IN.img"),
                [("my_preload", "default", in_img)], total=16 << 20)
    build_super(os.path.join(work, "super_RU.img"),
                [("my_preload", "default", ru_img)], total=16 << 20)
    out = os.path.join(work, "merged.img")
    rc = supermerge.main(["build", "--edl", work, "--out", out, "--force", "-q",
                          "--workdir", os.path.join(work, "scratch")])
    check(rc != 0, "several super images without a region are refused")
    rc = supermerge.main(["build", "--edl", work, "--region", "RU", "--out", out,
                          "--force", "-q", "--workdir", os.path.join(work, "scratch")])
    check(rc == 0, "super template picked by region")
    with SuperImage(out) as img:
        check(b"".join(img.iter_partition("my_preload")) == ru_img,
              "RU super template used")


def test_region_folders(tmp: str) -> None:
    print("region variants in per-region folders")
    import supermerge
    from smlib import edl as edlmod
    from smlib import regions
    work = os.path.join(tmp, "regions_folders")
    os.makedirs(work, exist_ok=True)
    tmpl = os.path.join(work, "super_template.img")
    build_super(tmpl, [("my_preload", "default", make_image(110, 2)),
                       ("my_stock", "default", make_image(111, 2))],
                total=32 << 20)
    edl = os.path.join(work, "edl")
    in_img = make_image(120, 2)
    ru_img = make_image(121, 2)
    for code, data in (("IN", in_img), ("RU", ru_img)):
        os.makedirs(os.path.join(edl, code), exist_ok=True)
        with open(os.path.join(edl, code, "my_preload.img"), "wb") as f:
            f.write(data)
    with open(os.path.join(edl, "my_stock.img"), "wb") as f:
        f.write(make_image(111, 2))

    table = regions.load_table()
    sources = edlmod.scan([edl], table=table)
    check(edlmod.region_tags(sources, table) == {"IN", "RU"},
          "folder names are detected as regions")

    out = os.path.join(work, "merged.img")
    rc = supermerge.main(["build", "--template", tmpl, "--edl", edl,
                          "--region", "IN", "--out", out, "--force", "-q",
                          "--workdir", os.path.join(work, "scratch")])
    check(rc == 0, "build with per-region folders succeeds")
    with SuperImage(out) as img:
        check(b"".join(img.iter_partition("my_preload")) == in_img,
              "variant chosen from the IN folder")
        check(b"".join(img.iter_partition("my_stock")) == make_image(111, 2),
              "non-regional partition taken from the package root")


def test_ofp_package(tmp: str) -> None:
    print("extracted OPPO package build (super_def + super_meta)")
    import json
    import supermerge
    from smlib import ofp as ofpmod
    from smlib import sparse as sparsemod

    root = os.path.join(tmp, "ofp_pkg")
    os.makedirs(os.path.join(root, "META"), exist_ok=True)
    os.makedirs(os.path.join(root, "IMAGES"), exist_ok=True)
    os.makedirs(os.path.join(root, "MY_MANIFEST"), exist_ok=True)

    sys_in = make_image(140, 4)
    sys_my = make_image(141, 4)
    stock = make_image(142, 2)
    for name, data in (("system_in.img", sys_in), ("system_my.img", sys_my),
                       ("my_stock.img", stock)):
        with open(os.path.join(root, "IMAGES", name), "wb") as f:
            f.write(data)

    variants = [
        {"nv_id": "00011011", "product_name": "CPH2841IN",
         "product_model": "CPH2841", "version_name": "V_IN"},
        # exported model: the region is only in the build prop
        {"nv_id": "00111000", "product_name": "CPH2841",
         "product_model": "CPH2841", "version_name": "V_MY"},
    ]
    with open(os.path.join(root, "version_info.txt"), "w", encoding="utf-8") as f:
        json.dump(variants, f)
    for nv, code in (("00011011", "IN"), ("00111000", "MY")):
        with open(os.path.join(root, "MY_MANIFEST", f"build.{nv}.prop"), "w",
                  encoding="utf-8") as f:
            f.write(f"ro.oplus.image.my_region.type={code}_all\n")
            f.write(f"ro.oplus.image.my_preload.type=export_25211_OPPO_{code}_NA\n")

    total = 32 << 20

    def super_def(image_name: str, image_size: int) -> dict:
        return {
            "super_device": {"total_size": str(total), "used_size": str(image_size),
                             "name": "super"},
            "block_devices": [{"block_size": "4096", "name": "super",
                               "alignment": "1048576", "size": str(total)}],
            "groups": [{"name": "default"},
                       {"name": "qti_dynamic_partitions_a",
                        "maximum_size": str(16 << 20)},
                       {"name": "qti_dynamic_partitions_b",
                        "maximum_size": str(16 << 20)}],
            "partitions": [
                {"name": "system_a", "group_name": "qti_dynamic_partitions_a",
                 "path": f"IMAGES/{image_name}", "size": str(image_size)},
                {"name": "my_stock_a", "group_name": "qti_dynamic_partitions_a",
                 "path": "IMAGES/my_stock.img", "size": str(len(stock))},
                {"name": "system_b", "group_name": "qti_dynamic_partitions_b"},
                {"name": "my_stock_b", "group_name": "qti_dynamic_partitions_b"},
            ],
        }

    for nv, img in (("00011011", "system_in.img"), ("00111000", "system_my.img")):
        with open(os.path.join(root, "META", f"super_def.{nv}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(super_def(img, len(sys_in)), f)

    # metadata blob as OPPO ships it: geometry immediately followed by metadata
    geom = lp.Geometry(65536, 3, 4096)
    bd = lp.BlockDevice(2048, 1 << 20, 0, total, "super", 0)
    groups = [lp.Group("default"),
              lp.Group("qti_dynamic_partitions_a", 0, 16 << 20),
              lp.Group("qti_dynamic_partitions_b", 0, 16 << 20)]
    extents = [lp.Extent(len(sys_in) // 512, lp.EXTENT_LINEAR, 2048, 0),
               lp.Extent(len(stock) // 512, lp.EXTENT_LINEAR,
                         2048 + len(sys_in) // 512, 0)]
    parts = [lp.Partition("system_a", 1, 0, 1, 1),
             lp.Partition("my_stock_a", 1, 1, 1, 1),
             lp.Partition("system_b", 1, 0, 0, 2),
             lp.Partition("my_stock_b", 1, 0, 0, 2)]
    blob = geom.to_bytes() + lp.Metadata(
        geometry=geom, partitions=parts, extents=extents, groups=groups,
        block_devices=[bd], minor_version=2, header_size=256).to_bytes()
    for nv in ("00011011", "00111000"):
        with open(os.path.join(root, "IMAGES", f"super_meta.{nv}.raw"), "wb") as f:
            f.write(blob)

    pkg = ofpmod.OfpPackage(root)
    check(pkg.resolve(region="IN").region == "IN", "region from product_name")
    check(pkg.resolve(region="MY").nv_id == "00111000",
          "region from ro.oplus.image.my_region.type")
    check(ofpmod.find_package(root) is not None,
          "find_package accepts a bare path string")

    scratch = os.path.join(tmp, "ofp_work")
    out = os.path.join(tmp, "ofp_merged.img")
    rc = supermerge.main(["build", "--edl", root, "--region", "IN", "--out", out,
                          "--verify", "--sparse", "--force", "-q",
                          "--workdir", scratch])
    check(rc == 0, "package build with --region IN succeeds")
    check(sparsemod.is_sparse(out), "sparse output written directly")
    with SuperImage(out) as img:
        names = [p.name for p in img.meta.partitions]
        check(names == ["system_a", "my_stock_a", "system_b", "my_stock_b"],
              f"partition table follows super_meta ({names})")
        check(b"".join(img.iter_partition("system_a")) == sys_in,
              "IN system image used")
        check(img.meta.partition_size(img.meta.partition("system_b")) == 0,
              "slot B stays empty, as in the OPPO package")
        check(img.meta.group_for(img.meta.partition("system_a")).name
              == "qti_dynamic_partitions_a", "group kept from super_meta")
        check(img.block_device.size == total, "super size kept")

    rc = supermerge.main(["verify", "--super", out, "--edl", root, "--region", "IN",
                          "-q", "--workdir", scratch])
    check(rc == 0, "verify against the package succeeds")

    out_my = os.path.join(tmp, "ofp_my.img")
    rc = supermerge.main(["build", "--edl", root, "--region", "MY", "--out", out_my,
                          "--force", "-q", "--workdir", scratch])
    check(rc == 0, "--region MY builds the prop-derived variant")
    with SuperImage(out_my) as img:
        check(b"".join(img.iter_partition("system_a")) == sys_my,
              "MY system image used")

    rc = supermerge.main(["build", "--edl", root, "--nv", "00111000",
                          "--out", os.path.join(tmp, "ofp_nv.img"), "--force",
                          "-q", "--workdir", scratch])
    check(rc == 0, "--nv selects a variant")

    rc = supermerge.main(["build", "--edl", root, "--out", os.path.join(tmp, "x.img"),
                          "--force", "-q", "--workdir", scratch])
    check(rc != 0, "ambiguous package without --region/--nv is refused")

    rc = supermerge.main(["build", "--edl", root, "--region", "JP",
                          "--out", os.path.join(tmp, "y.img"), "--force",
                          "-q", "--workdir", scratch])
    check(rc != 0, "region absent from the package is refused")


def test_sparse_short_reads(tmp: str) -> None:
    print("sparse output with short reads")
    import supermerge
    import smlib.superimg as simg
    from smlib import sparse as sparsemod

    work = os.path.join(tmp, "shortread")
    os.makedirs(work, exist_ok=True)
    tmpl = os.path.join(work, "t.img")
    data1 = make_image(150, 6)
    data2 = make_image(151, 4)
    build_super(tmpl, [("system", "default", data1), ("vendor", "default", data2)],
                total=32 << 20)
    out = os.path.join(work, "out.img")

    original = simg._read_upto
    calls = {"n": 0}

    def short(src, n):
        calls["n"] += 1
        return original(src, min(n, 100_000))   # deliberately not block aligned

    simg._read_upto = short
    try:
        rc = supermerge.main(["build", "--template", tmpl, "--out", out,
                              "--sparse", "--force", "-q",
                              "--workdir", os.path.join(work, "scratch")])
    finally:
        simg._read_upto = original
    check(rc == 0, "short reads do not break the sparse writer")
    check(calls["n"] > 0, "short read path exercised")
    check(sparsemod.is_sparse(out), "output is sparse")
    with SuperImage(out) as img:
        check(b"".join(img.iter_partition("system")) == data1, "content intact")
        check(img.strict_check() == [], "decoded image is consistent")


def test_merge_region_policy(tmp: str) -> None:
    print("merge policy for region partitions")
    import json
    import supermerge

    root = os.path.join(tmp, "ofp_region")
    os.makedirs(os.path.join(root, "META"), exist_ok=True)
    os.makedirs(os.path.join(root, "IMAGES"), exist_ok=True)

    sys_pkg = make_image(160, 3)
    region_pkg = make_image(161, 1)
    sys_ota = make_image(162, 3)
    region_ota = make_image(163, 1)
    for name, data in (("system.img", sys_pkg), ("my_region.img", region_pkg)):
        with open(os.path.join(root, "IMAGES", name), "wb") as f:
            f.write(data)
    with open(os.path.join(root, "version_info.txt"), "w", encoding="utf-8") as f:
        json.dump([{"nv_id": "00011011", "product_name": "CPH2841IN",
                    "product_model": "CPH2841"}], f)

    total = 32 << 20
    defn = {
        "super_device": {"total_size": str(total), "used_size": "1", "name": "super"},
        "block_devices": [{"block_size": "4096", "name": "super",
                           "alignment": "1048576", "size": str(total)}],
        "groups": [{"name": "default"},
                   {"name": "qti_dynamic_partitions_a", "maximum_size": str(16 << 20)},
                   {"name": "qti_dynamic_partitions_b", "maximum_size": str(16 << 20)}],
        "partitions": [
            {"name": "system_a", "group_name": "qti_dynamic_partitions_a",
             "path": "IMAGES/system.img", "size": str(len(sys_pkg))},
            {"name": "my_region_a", "group_name": "qti_dynamic_partitions_a",
             "path": "IMAGES/my_region.img", "size": str(len(region_pkg))},
            {"name": "system_b", "group_name": "qti_dynamic_partitions_b"},
            {"name": "my_region_b", "group_name": "qti_dynamic_partitions_b"},
        ],
    }
    with open(os.path.join(root, "META", "super_def.00011011.json"), "w",
              encoding="utf-8") as f:
        json.dump(defn, f)

    geom = lp.Geometry(65536, 3, 4096)
    bd = lp.BlockDevice(2048, 1 << 20, 0, total, "super", 0)
    groups = [lp.Group("default"),
              lp.Group("qti_dynamic_partitions_a", 0, 16 << 20),
              lp.Group("qti_dynamic_partitions_b", 0, 16 << 20)]
    extents = [lp.Extent(len(sys_pkg) // 512, lp.EXTENT_LINEAR, 2048, 0),
               lp.Extent(len(region_pkg) // 512, lp.EXTENT_LINEAR,
                         2048 + len(sys_pkg) // 512, 0)]
    parts = [lp.Partition("system_a", 1, 0, 1, 1),
             lp.Partition("my_region_a", 1, 1, 1, 1),
             lp.Partition("system_b", 1, 0, 0, 2),
             lp.Partition("my_region_b", 1, 0, 0, 2)]
    blob = geom.to_bytes() + lp.Metadata(
        geometry=geom, partitions=parts, extents=extents, groups=groups,
        block_devices=[bd], minor_version=2, header_size=256).to_bytes()
    with open(os.path.join(root, "IMAGES", "super_meta.00011011.raw"), "wb") as f:
        f.write(blob)

    fx = PayloadFixture()
    fx.add("system", sys_ota)
    fx.add("my_region", region_ota)
    payload = os.path.join(tmp, "region_payload.bin")
    fx.write(payload)

    scratch = os.path.join(tmp, "region_merge_work")

    def build(name, extra=()):
        out = os.path.join(tmp, name)
        rc = supermerge.main(["build", "--edl", root, "--region", "IN",
                              "--payload", payload, "--out", out, "--force",
                              "-q", "--workdir", scratch] + list(extra))
        return rc, out

    rc, out = build("merge_default.img")
    check(rc == 0, "merge with payload builds")
    with SuperImage(out) as img:
        check(b"".join(img.iter_partition("system_a")) == sys_ota,
              "system comes from the payload")
        check(b"".join(img.iter_partition("my_region_a")) == region_ota,
              "payload region partition wins by default")

    rc, out = build("merge_keep.img", ["--keep-region-partitions"])
    check(rc == 0, "merge with --keep-region-partitions builds")
    with SuperImage(out) as img:
        check(b"".join(img.iter_partition("my_region_a")) == region_pkg,
              "package region partition kept")
        check(b"".join(img.iter_partition("system_a")) == sys_ota,
              "system still comes from the payload")

    rc, out = build("merge_prefer_edl.img", ["--prefer", "edl"])
    check(rc == 0, "merge with --prefer edl builds")
    with SuperImage(out) as img:
        check(b"".join(img.iter_partition("system_a")) == sys_pkg,
              "--prefer edl takes the package image")

    rc = supermerge.main(["verify", "--super", out, "--edl", root, "--region", "IN",
                          "--payload", payload, "-q", "--workdir", scratch])
    check(rc == 0, "verify accepts either source for each partition")

    # model detection and mismatch warning
    from smlib.models import model_from_payload_properties, models_compatible
    check(model_from_payload_properties(
        {"compatible_ota_prefix_list": "PYB110,PYB110PRE"}) == "PYB110",
        "model from compatible_ota_prefix_list")
    check(model_from_payload_properties(
        {"ota_target_version": "CPH2841_11.C.10_1100_202610181148"}) == "CPH2841",
        "model from ota_target_version")
    check(models_compatible("CPH2841", "CPH2841IN"), "prefix models are compatible")
    check(not models_compatible("CPH2841", "PYB110"), "different models are incompatible")

    import contextlib
    import io
    with open(os.path.join(root, "build.prop"), "w", encoding="utf-8") as f:
        f.write("ro.product.model=CPH2841\n")
    with open(os.path.join(tmp, "payload_properties.txt"), "w", encoding="utf-8") as f:
        f.write("compatible_ota_prefix_list=PYB110\n")
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        rc, _ = build("merge_mismatch.img")
    check(rc == 0, "build still completes for a mismatched model pair")
    check("model mismatch" in stderr.getvalue(), "model mismatch is warned about")

    import zipfile
    zpath = os.path.join(tmp, "region_pkg.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirs, files in os.walk(root):
            for item in files:
                full = os.path.join(dirpath, item)
                zf.write(full, os.path.relpath(full, root).replace(os.sep, "/"))
    zout = os.path.join(tmp, "merge_zip.img")
    rc = supermerge.main(["build", "--edl", zpath, "--region", "IN",
                          "--payload", payload, "--out", zout, "--force",
                          "-q", "--workdir", scratch])
    check(rc == 0, "EDL package as a .zip builds")
    with SuperImage(zout) as img:
        check(b"".join(img.iter_partition("system_a")) == sys_ota,
              "zip EDL package yields the same merge")

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    old_stdin = sys.stdin
    sys.stdin = Tty(f"{root}\n{payload}\nIN\n\n\n")
    try:
        rc = supermerge.main(["wizard", "--dry-run", "-q", "--workdir", scratch])
    finally:
        sys.stdin = old_stdin
    check(rc == 0, "wizard collects the paths and a region (dry run)")


def test_wizard_requires_terminal(tmp: str) -> None:
    print("wizard refuses a non-interactive stdin")
    import supermerge

    class NotATty(io.StringIO):
        def isatty(self) -> bool:
            return False

    old = sys.stdin
    sys.stdin = NotATty("")
    try:
        rc = supermerge.main(["wizard", "-q", "--workdir", os.path.join(tmp, "wiz")])
    finally:
        sys.stdin = old
    check(rc == 1, "wizard fails cleanly without a terminal")


def test_merge(tmp: str) -> None:
    print("end-to-end merge (payload + EDL + template)")
    work = os.path.join(tmp, "merge")
    os.makedirs(work, exist_ok=True)

    sys_img = make_image(30, 5)
    vendor_img = make_image(31, 4)
    stock_img = make_image(32, 2)
    product_img = make_image(33, 3)

    tmpl = os.path.join(work, "super_template.img")
    build_super(tmpl, [
        ("system_a", "default", make_image(90, 5)),
        ("system_b", "default", make_image(91, 5)),
        ("vendor_a", "qti_dynamic_partitions", make_image(92, 4)),
        ("vendor_b", "qti_dynamic_partitions", make_image(93, 4)),
        ("my_stock", "default", make_image(94, 2)),
    ], total=64 << 20, groups=["default", "qti_dynamic_partitions"])

    fx = PayloadFixture()
    fx.add("system", sys_img, compress="xz")
    fx.add("vendor", vendor_img)
    fx.add("product", product_img)
    p = os.path.join(work, "payload.bin")
    fx.write(p)

    edl_dir = os.path.join(work, "edl")
    os.makedirs(edl_dir, exist_ok=True)
    with open(os.path.join(edl_dir, "my_stock.img"), "wb") as f:
        f.write(stock_img)
    with open(os.path.join(edl_dir, "rawprogram0.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0"?><data><program label="my_stock" '
                'filename="my_stock.img" sparse="false" '
                'start_sector="0" num_partition_sectors="16"/></data>')

    out = os.path.join(work, "super_merged.img")
    sys.argv = ["supermerge"]
    import supermerge
    rc = supermerge.main([
        "build", "--payload", p, "--edl", edl_dir, "--template", tmpl,
        "--out", out, "--add-extra", "--force", "--verify",
        "--workdir", os.path.join(work, "scratch"), "-q",
    ])
    check(rc == 0, "build exits cleanly")
    check(os.path.exists(out), "output written")

    with SuperImage(out) as img:
        names = [p.name for p in img.meta.partitions]
        check(names == ["system_a", "system_b", "vendor_a", "vendor_b",
                        "my_stock", "product"], f"partition set {names}")
        check(img.check() == [], "output layout is consistent")
        check(b"".join(img.iter_partition("system_a")) == sys_img,
              "system_a came from the payload")
        check(b"".join(img.iter_partition("system_b")) == sys_img,
              "system_b was mirrored from the active slot")
        check(b"".join(img.iter_partition("vendor_a")) == vendor_img,
              "vendor_a came from the payload")
        check(b"".join(img.iter_partition("my_stock")) == stock_img,
              "my_stock came from the EDL folder")
        check(b"".join(img.iter_partition("product")) == product_img,
              "product was appended as a new partition")
        grp = img.meta.group_for(img.meta.partition("product"))
        check(grp.name == "default", "new partition landed in the default group")
        for p_ in img.meta.partitions:
            used = img.meta.partition_size(p_)
            check(used % 4096 == 0, f"{p_.name}: block aligned")

    # the template itself must remain untouched
    with SuperImage(tmpl) as img:
        check(b"".join(img.iter_partition("system_a")) == make_image(90, 5),
              "template file was not modified")


def test_template_only(tmp: str) -> None:
    print("build from template data only (no payload)")
    work = os.path.join(tmp, "tplonly")
    os.makedirs(work, exist_ok=True)
    tmpl = os.path.join(work, "t.img")
    build_super(tmpl, [("system", "default", make_image(40, 3))],
                total=16 << 20)
    out = os.path.join(work, "out.img")
    import supermerge
    rc = supermerge.main(["build", "--template", tmpl, "--out", out,
                          "--force", "--workdir", os.path.join(work, "scratch"), "-q"])
    check(rc == 0, "rebuild from template data succeeds")
    with SuperImage(out) as img:
        check(b"".join(img.iter_partition("system")) == make_image(40, 3),
              "content copied from the template")


def test_slot_labelling(tmp: str) -> None:
    print("slot selection")
    work = os.path.join(tmp, "slots")
    os.makedirs(work, exist_ok=True)
    tmpl = os.path.join(work, "t.img")
    build_super(tmpl, [
        ("system_a", "default", make_image(50, 2)),
        ("system_b", "default", make_image(51, 2)),
    ], total=16 << 20)
    fx = PayloadFixture()
    fx.add("system", make_image(60, 2))
    p = os.path.join(work, "p.bin")
    fx.write(p)
    out = os.path.join(work, "out.img")
    import supermerge
    rc = supermerge.main(["build", "--payload", p, "--template", tmpl,
                          "--slot", "b", "--out", out, "--force",
                          "--workdir", os.path.join(work, "scratch"), "-q"])
    check(rc == 0, "slot b build succeeds")
    with SuperImage(out) as img:
        check(b"".join(img.iter_partition("system_b")) == make_image(60, 2),
              "payload written to slot b")
        check(b"".join(img.iter_partition("system_a")) == make_image(60, 2),
              "slot a filled from the same source")


def main() -> int:
    tmp = scratch_dir(ROOT, "supermerge_test_")
    try:
        for fn in (test_sparse, test_lp_roundtrip, test_slot_expansion, test_payload,
                   test_payload_source_copy, test_bspatch, test_region_table,
                   test_region_build, test_region_super_selection,
                   test_region_folders, test_ofp_package, test_sparse_short_reads,
                   test_merge_region_policy, test_wizard_requires_terminal, test_merge,
                   test_template_only, test_slot_labelling):
            fn(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
