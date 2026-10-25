"""Walk a UEFI firmware volume: FFS files -> sections -> compressed FVs.

Usage: uefi_fv.py <image> [--dump DIR]
"""

from __future__ import annotations

import lzma
import os
import struct
import sys

FV_SIG = b"_FVH"
FFS_PAD = 0xF0

SECTION_NAMES = {
    0x01: "COMPRESSION", 0x02: "GUID_DEFINED", 0x03: "DISPOSABLE", 0x10: "PE32",
    0x11: "PIC", 0x12: "TE", 0x13: "DXE_DEPEX", 0x14: "VERSION", 0x15: "UI",
    0x16: "COMPAT16", 0x17: "FV_IMAGE", 0x18: "FREEFORM_SUBTYPE_GUID",
    0x19: "RAW", 0x1B: "PEI_DEPEX", 0x1C: "SMM_DEPEX",
}
FFS_NAMES = {
    0x01: "RAW", 0x02: "FREEFORM", 0x03: "SECURITY_CORE", 0x04: "PEI_CORE",
    0x05: "DXE_CORE", 0x06: "PEIM", 0x07: "DRIVER", 0x08: "COMBINED_PEIM_DRIVER",
    0x09: "APPLICATION", 0x0A: "SMM", 0x0B: "FV_IMAGE", 0x0C: "COMBINED_SMM_DXE",
    0x0D: "SMM_CORE", 0x0E: "MM_STANDALONE", 0x0F: "MM_CORE_STANDALONE",
}
GUID_NAMES = {
    "ee4e5898-3914-4259-9d6e-dc7bd79403cf": "LZMA",
    "a31280ad-481e-41b6-95e8-127f4c984779": "Tiano",
    "fc1bcdb0-7d31-49aa-936a-a4600d9dd083": "CRC32",
    "3d532050-5cda-4fd0-879e-0f7f630d5afb": "Brotli",
}


def lzma_decompress(blob: bytes):
    for fmt in (lzma.FORMAT_ALONE, lzma.FORMAT_AUTO):
        try:
            return lzma.decompress(blob, format=fmt)
        except Exception:
            continue
    return None


class State:
    def __init__(self, dump_dir: str | None):
        self.dump_dir = dump_dir
        self.count = 0
        self.findings: set[str] = set()

    def save(self, data: bytes, tag: str) -> None:
        self.count += 1
        if not self.dump_dir:
            return
        os.makedirs(self.dump_dir, exist_ok=True)
        with open(os.path.join(self.dump_dir, f"{tag}_{self.count:03d}.bin"), "wb") as f:
            f.write(data)

    def scan(self, data: bytes, label: str) -> None:
        for word in (b"android_esp", b"efisp", b"LoadImage", b"LinuxLoader",
                     b"fastboot", b"SystemPartition"):
            if word in data:
                self.findings.add(f"{word.decode()} in {label} ({len(data)} B)")


def find_fvs(buf: bytes) -> list[int]:
    out = []
    pos = 0
    while True:
        pos = buf.find(FV_SIG, pos)
        if pos < 0:
            return out
        base = pos - 40
        if base >= 0:
            length, = struct.unpack_from("<Q", buf, base + 32)
            if 0 < length <= len(buf) - base:
                out.append(base)
        pos += 4


def walk_fv(buf: bytes, base: int, depth: int, st: State, name: str = "") -> None:
    hdr_len, = struct.unpack_from("<H", buf, base + 48)
    length, = struct.unpack_from("<Q", buf, base + 32)
    print(f"{'  ' * depth}FV {name} @0x{base:x} len=0x{length:x} hdr=0x{hdr_len:x}")
    pos = base + hdr_len
    end = base + length
    while pos + 24 <= end:
        size = buf[pos + 20] | (buf[pos + 21] << 8) | (buf[pos + 22] << 16)
        ftype = buf[pos + 18]
        if size < 24:
            break
        if ftype == FFS_PAD:
            pos = (pos + size + 7) & ~7
            continue
        if pos + size > end:
            break
        print(f"{'  ' * depth}  FFS {FFS_NAMES.get(ftype, hex(ftype))} "
              f"size=0x{size:x} @0x{pos:x}")
        walk_sections(buf, pos + 24, pos + size, depth + 2, st)
        pos = (pos + size + 7) & ~7


def walk_sections(buf: bytes, pos: int, end: int, depth: int, st: State) -> None:
    while pos + 4 <= end:
        size = buf[pos] | (buf[pos + 1] << 8) | (buf[pos + 2] << 16)
        stype = buf[pos + 3]
        if size < 4 or pos + size > end:
            break
        body = buf[pos + 4:pos + size]
        name = SECTION_NAMES.get(stype, hex(stype))
        print(f"{'  ' * depth}SEC {name} size=0x{size:x} @0x{pos:x}")
        if stype == 0x01 and len(body) >= 5:          # COMPRESSION
            ctype = body[4]
            data = body[5:]
            if ctype == 2:
                out = lzma_decompress(data)
                if out:
                    print(f"{'  ' * (depth + 1)}-> LZMA {len(data)} -> {len(out)} B")
                    st.save(out, f"lzma_{st.count}")
                    st.scan(out, "lzma-section")
                    if out[40:44] == b"_FVH" or find_fvs(out):
                        for fv in find_fvs(out):
                            walk_fv(out, fv, depth + 2, st, "inflated")
        elif stype == 0x02 and len(body) >= 20:       # GUID_DEFINED
            g = body[0:16]
            guid_str = "-".join([g[3::-1].hex(), g[5:3:-1].hex(), g[7:5:-1].hex(),
                                 g[8:10].hex(), g[10:16].hex()])
            data_off, attrs = struct.unpack_from("<HH", body, 16)
            data = buf[pos + data_off:pos + size] if data_off < size else b""
            print(f"{'  ' * depth}    guid={guid_str} ({GUID_NAMES.get(guid_str, '?')}) "
                  f"attrs=0x{attrs:x} data=0x{len(data):x}")
            out = lzma_decompress(data) if data else None
            if out is None and find_fvs(data):
                out = data
            if out:
                print(f"{'  ' * (depth + 1)}-> inflated {len(data)} -> {len(out)} B")
                st.save(out, f"guid_{st.count}")
                st.scan(out, "guid-section")
                for fv in find_fvs(out):
                    walk_fv(out, fv, depth + 2, st, "inflated")
        elif stype in (0x10, 0x12):                   # PE32 / TE
            st.save(body, f"pe_{st.count}")
            st.scan(body, "pe-section")
        elif stype == 0x17:                           # nested FV image
            st.save(body, f"fv_{st.count}")
            st.scan(body, "fv-image")
            for fv in find_fvs(body):
                walk_fv(body, fv, depth + 2, st, "nested")
        elif stype in (0x19, 0x18):                   # RAW / FREEFORM
            st.save(body, f"raw_{st.count}")
            st.scan(body, "raw-section")
        pos = (pos + size + 3) & ~3


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: uefi_fv.py <image> [--dump DIR]", file=sys.stderr)
        return 2
    image = open(argv[0], "rb").read()
    dump = argv[argv.index("--dump") + 1] if "--dump" in argv else None
    st = State(dump)
    fvs = find_fvs(image)
    print(f"FVs found: {[hex(x) for x in fvs]}")
    for fv in fvs:
        walk_fv(image, fv, 0, st, "root")
    print(f"\nblobs saved: {st.count}")
    for line in sorted(st.findings):
        print("  hit:", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
