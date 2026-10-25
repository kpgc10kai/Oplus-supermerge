#!/usr/bin/env python3
"""Minimal read-only EROFS extractor (no external tools, no compression).

Supports the flat (plain/inline) layouts used for small partition images such
as OPPO's ``my_manifest``.  Compressed inodes are reported rather than guessed.
"""

from __future__ import annotations

import struct
import sys

EROFS_MAGIC = 0xE0F5E1E2

LAYOUT_FLAT_PLAIN = 0
LAYOUT_COMPRESSED_LEGACY = 1
LAYOUT_FLAT_INLINE = 2
LAYOUT_FLAT_COMPRESSION = 3
LAYOUT_CHUNK_BASED = 4

LAYOUT_NAMES = {
    LAYOUT_FLAT_PLAIN: "flat-plain",
    LAYOUT_COMPRESSED_LEGACY: "compressed-legacy",
    LAYOUT_FLAT_INLINE: "flat-inline",
    LAYOUT_FLAT_COMPRESSION: "compressed",
    LAYOUT_CHUNK_BASED: "chunk-based",
}

Z_LCLUSTER_PLAIN = 0
Z_LCLUSTER_HEAD1 = 1
Z_LCLUSTER_NONHEAD = 2
Z_LCLUSTER_HEAD2 = 3

ADVISE_COMPACTED_2B = 0x0001
ADVISE_BIG_PCLUSTER_1 = 0x0002
ADVISE_BIG_PCLUSTER_2 = 0x0004
ADVISE_INLINE_PCLUSTER = 0x0008
ADVISE_FRAGMENT_PCLUSTER = 0x0020

LI_D0_CBLKCNT = 1 << 11
ALG_LZ4 = 0

FT_REG = 1
FT_DIR = 2
FT_CHR = 3
FT_BLK = 4
FT_FIFO = 5
FT_SOCK = 6
FT_SYMLINK = 7


class ErofsError(Exception):
    pass


class Erofs:
    def __init__(self, path: str):
        with open(path, "rb") as f:
            self.blob = f.read()
        if len(self.blob) < 1024 + 128:
            raise ErofsError("file too small")
        sb = self.blob[1024:1024 + 128]
        magic, checksum, compat = struct.unpack_from("<III", sb, 0)
        if magic != EROFS_MAGIC:
            raise ErofsError(f"not EROFS (magic 0x{magic:08x})")
        self.blkszbits = sb[12]
        self.block_size = 1 << self.blkszbits
        self.root_nid, = struct.unpack_from("<H", sb, 14)
        self.inos, = struct.unpack_from("<Q", sb, 16)
        self.blocks, = struct.unpack_from("<I", sb, 36)
        self.meta_blkaddr, = struct.unpack_from("<I", sb, 40)
        self.incompat, = struct.unpack_from("<I", sb, 80)
        self.compat = compat
        self.available_compr_algs, = struct.unpack_from("<I", sb, 84)
        self.dirblkbits = sb[92]
        self.volume_name = sb[64:80].split(b"\0")[0].decode("utf-8", "replace")
        self.uuid = sb[48:64].hex()

    # ------------------------------------------------------------------ inode
    def inode_offset(self, nid: int) -> int:
        return self.meta_blkaddr * self.block_size + nid * 32

    def read_inode(self, nid: int) -> dict:
        pos = self.inode_offset(nid)
        raw = self.blob[pos:pos + 64]
        if len(raw) < 32:
            raise ErofsError(f"inode {nid} out of range")
        i_format, xattr_icount, mode, nlink = struct.unpack_from("<HHHH", raw, 0)
        version = i_format & 1
        layout = (i_format >> 1) & 7
        inode = {
            "nid": nid,
            "version": version,
            "layout": layout,
            "mode": mode,
            "nlink": nlink,
            "xattr_icount": xattr_icount,
            "offset": pos,
            "size": 32 if version == 0 else 64,
        }
        if version == 0:
            size, reserved, u, ino, uid, gid, reserved2 = struct.unpack_from(
                "<IIIIHHI", raw, 8)
            inode["i_size"] = size
            inode["u"] = u
        else:
            size, u, ino, uid, gid, mtime, mtime_nsec, nlink2 = struct.unpack_from(
                "<QIIIIQII", raw, 8)
            inode["i_size"] = size
            inode["u"] = u
            inode["mtime"] = mtime
            inode["nlink"] = nlink2
        inode["xattr_size"] = self._xattr_size(xattr_icount)
        return inode

    @staticmethod
    def _xattr_size(icount: int) -> int:
        if icount == 0:
            return 0
        # erofs_xattr_ibody_size(): 12-byte header + 4 bytes per extra entry
        return 12 + 4 * (icount - 1)

    # ------------------------------------------------------------------- data
    def read_data(self, inode: dict) -> bytes:
        layout = inode["layout"]
        size = inode["i_size"]
        if layout in (LAYOUT_COMPRESSED_LEGACY, LAYOUT_FLAT_COMPRESSION):
            return b""  # handled by the caller
        if layout == LAYOUT_FLAT_PLAIN:
            start = inode["u"] * self.block_size
            return self.blob[start:start + size]
        if layout == LAYOUT_FLAT_INLINE:
            start = inode["offset"] + inode["size"] + inode["xattr_size"]
            capacity = self.block_size - (start % self.block_size)
            head = self.blob[start:start + min(size, capacity)]
            if len(head) >= size:
                return head[:size]
            tail_start = inode["u"] * self.block_size
            tail = self.blob[tail_start:tail_start + (size - len(head))]
            return head + tail
        raise ErofsError(f"unsupported layout {layout}")

    # --------------------------------------------------------------- compressed
    def map_header(self, inode: dict) -> dict:
        end = inode["offset"] + inode["size"] + inode["xattr_size"]
        pos = (end + 7) & ~7
        raw = self.blob[pos:pos + 8]
        if len(raw) < 8:
            raise ErofsError("map header out of range")
        first, advise, algo, clusterbits = struct.unpack("<IHBB", raw)
        return {
            "pos": pos,
            "first": first,
            "idata_size": (first >> 16) & 0xFFFF,
            "fragmentoff": first,
            "advise": advise,
            "algo": algo,
            "clusterbits": clusterbits & 15,
        }

    def _lcluster_entries(self, inode: dict, header: dict) -> list[dict]:
        lclusterbits = self.blkszbits + header["clusterbits"]
        lsize = 1 << lclusterbits
        totalidx = (inode["i_size"] + lsize - 1) // lsize
        ebase = ((inode["offset"] + inode["size"] + inode["xattr_size"] + 7) & ~7) + 8
        compacted_4b_initial = ((32 - ebase % 32) // 4) & 7
        compacted_2b = 0
        if (header["advise"] & ADVISE_COMPACTED_2B) and compacted_4b_initial < totalidx:
            compacted_2b = ((totalidx - compacted_4b_initial) // 16) * 16
        lobits = max(lclusterbits, 12)
        entries = []
        for lcn in range(totalidx):
            pos = ebase
            amort = 2
            l = lcn
            if l >= compacted_4b_initial:
                pos += compacted_4b_initial * 4
                l -= compacted_4b_initial
                if l < compacted_2b:
                    amort = 1
                else:
                    pos += compacted_2b * 2
                    l -= compacted_2b
            pos += l * (1 << amort)
            vcnt = 2 if amort == 2 else 16
            packsize = vcnt << amort
            encodebits = (packsize - 4) * 8 // vcnt
            pack_start = pos - (pos % packsize)
            index = (pos - pack_start) >> amort
            bitpos = encodebits * index
            base = pack_start + bitpos // 8
            value = int.from_bytes(self.blob[base:base + 4], "little") >> (bitpos % 8)
            lo = value & ((1 << lobits) - 1)
            ltype = (value >> lobits) & 3
            blkaddr = struct.unpack_from("<I", self.blob, pack_start + packsize - 4)[0]
            entries.append({
                "lcn": lcn, "type": ltype, "lo": lo,
                "blkaddr": blkaddr, "pack_start": pack_start,
            })
        return entries

    def read_compressed(self, inode: dict) -> bytes:
        from lz4_block import decompress_block, Lz4Error

        header = self.map_header(inode)
        advise = header["advise"]
        if advise & ADVISE_FRAGMENT_PCLUSTER:
            raise ErofsError("file is stored in the packed inode (fragments)")
        lclusterbits = self.blkszbits + header["clusterbits"]
        lsize = 1 << lclusterbits
        entries = self._lcluster_entries(inode, header)
        if not entries:
            return b""

        heads = [i for i, e in enumerate(entries) if e["type"] != Z_LCLUSTER_NONHEAD]
        if not heads or heads[0] != 0:
            raise ErofsError("compressed file does not start with a HEAD lcluster")

        out = bytearray(inode["i_size"])
        pblk = None
        prev_cblk = 1
        prev_pack = None
        for hi, idx in enumerate(heads):
            entry = entries[idx]
            head_la = (entry["lcn"] << lclusterbits) + entry["lo"]
            if hi + 1 < len(heads):
                nxt = entries[heads[hi + 1]]
                next_la = (nxt["lcn"] << lclusterbits) + nxt["lo"]
            else:
                next_la = inode["i_size"]
            dec_len = next_la - head_la

            if pblk is None or entry["pack_start"] != prev_pack:
                pblk = entry["blkaddr"]
            else:
                pblk += prev_cblk
            prev_pack = entry["pack_start"]

            cblk = 1
            j = idx + 1
            if j < len(entries) and entries[j]["type"] == Z_LCLUSTER_NONHEAD:
                d0 = entries[j]["lo"]
                if d0 & LI_D0_CBLKCNT:
                    cblk = d0 & ~LI_D0_CBLKCNT

            window = self.blob[pblk * self.block_size:(pblk + cblk) * self.block_size]
            if not window:
                raise ErofsError(f"compressed blocks at {pblk} are outside the image")

            if entry["type"] in (Z_LCLUSTER_HEAD1, Z_LCLUSTER_HEAD2):
                algo = (header["algo"] >> (4 if entry["type"] == Z_LCLUSTER_HEAD2 else 0)) & 15
                if algo != ALG_LZ4:
                    raise ErofsError(f"compression algorithm {algo} is not supported")
                data = None
                last_error = None
                # The pcluster may start after metadata that shares its block.
                skip = len(window) - len(window.lstrip(b"\0"))
                for candidate in (skip, 0):
                    if candidate > len(window):
                        continue
                    try:
                        data = decompress_block(window[candidate:], dec_len)
                        break
                    except Lz4Error as exc:
                        last_error = exc
                if data is None:
                    raise ErofsError(
                        f"LZ4 decode failed at lcluster {entry['lcn']}: {last_error}")
            else:
                data = window[:dec_len]

            start = head_la
            end = min(start + len(data), len(out))
            out[start:end] = data[:end - start]
            prev_cblk = cblk
        return bytes(out)

    # -------------------------------------------------------------- directory
    def listdir(self, inode: dict) -> list[tuple[str, int, int]]:
        data = self.read_data(inode)
        if len(data) < 12:
            return []
        _nid0, nameoff0, _ft0, _res0 = struct.unpack_from("<QHBB", data, 0)
        # The dirent array ends where the first name starts.
        limit = nameoff0 if 12 <= nameoff0 <= len(data) else len(data)
        entries = []
        pos = 0
        while pos + 12 <= limit:
            nid, nameoff, ftype, _res = struct.unpack_from("<QHBB", data, pos)
            if nid == 0:
                break
            entries.append((nameoff, nid, ftype))
            pos += 12
        out = []
        for idx, (nameoff, nid, ftype) in enumerate(entries):
            end = entries[idx + 1][0] if idx + 1 < len(entries) else len(data)
            name = data[nameoff:end].split(b"\0")[0].decode("utf-8", "replace")
            if name in (".", ".."):
                continue
            out.append((name, nid, ftype))
        return out

    def walk(self, nid: int | None = None, prefix: str = ""):
        """Yield (path, inode, data_or_None, error)."""
        if nid is None:
            nid = self.root_nid
        inode = self.read_inode(nid)
        mode_type = (inode["mode"] >> 12) & 0xF
        if mode_type == 4:  # directory
            for name, child_nid, _ftype in self.listdir(inode):
                path = f"{prefix}/{name}" if prefix else name
                child = self.read_inode(child_nid)
                if ((child["mode"] >> 12) & 0xF) == 4:
                    yield from self.walk(child_nid, path)
                else:
                    yield from self._file(path, child)
        else:
            yield from self._file(prefix, inode)

    def _file(self, path: str, inode: dict):
        if inode["layout"] in (LAYOUT_COMPRESSED_LEGACY, LAYOUT_FLAT_COMPRESSION):
            try:
                data = self.read_compressed(inode)
            except ErofsError as exc:
                yield path, inode, None, str(exc)
                return
            yield path, inode, data, None
            return
        try:
            data = self.read_data(inode)
        except ErofsError as exc:
            yield path, inode, None, str(exc)
            return
        yield path, inode, data, None


def dump_text(img: Erofs, out_path: str) -> tuple[int, int]:
    """Write every file to a single text report; returns (files, failures)."""
    files = 0
    failures = 0
    with open(out_path, "w", encoding="utf-8", newline="\n") as out:
        out.write(f"# EROFS image dump\n")
        out.write(f"# block size {img.block_size}, inodes {img.inos}, "
                  f"volume {img.volume_name!r}, uuid {img.uuid}\n")
        out.write(f"# compat 0x{img.compat:x}, incompat 0x{img.incompat:x}\n")
        for path, inode, data, err in img.walk():
            mode = inode["mode"]
            if ((mode >> 12) & 0xF) == 4:
                continue
            files += 1
            out.write("\n" + "=" * 78 + "\n")
            out.write(f"== /{path}\n")
            out.write(f"== size={inode['i_size']} mode=0o{mode:o} "
                      f"layout={LAYOUT_NAMES.get(inode['layout'], '?')}\n")
            out.write("=" * 78 + "\n")
            if err:
                failures += 1
                out.write(f"[unreadable: {err}]\n")
                continue
            if data is None:
                out.write("[no data]\n")
                continue
            body = data.rstrip(b"\0")
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                out.write(f"[binary, {len(data)} bytes] first 64 bytes:\n")
                out.write(data[:64].hex(" ") + "\n")
                continue
            out.write(text)
            if not text.endswith("\n"):
                out.write("\n")
    return files, failures


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: erofs_extract.py <image> [--txt OUT] [--list]", file=sys.stderr)
        return 2
    image = argv[0]
    txt_path = None
    if "--txt" in argv:
        idx = argv.index("--txt")
        try:
            txt_path = argv[idx + 1]
        except IndexError:
            print("--txt needs a path", file=sys.stderr)
            return 2

    img = Erofs(image)
    print(f"block={img.block_size} inodes={img.inos} root_nid={img.root_nid} "
          f"compat=0x{img.compat:x} incompat=0x{img.incompat:x}")
    if txt_path:
        files, failures = dump_text(img, txt_path)
        print(f"wrote {txt_path}: {files} file(s), {failures} unreadable")
        return 0 if failures == 0 else 1
    for path, inode, data, err in img.walk():
        mode = inode["mode"]
        kind = "dir" if ((mode >> 12) & 0xF) == 4 else "file"
        print(f"{path:<40} {kind:<5} {LAYOUT_NAMES.get(inode['layout'], '?'):<16} "
              f"{inode['i_size']:>8} {err or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
