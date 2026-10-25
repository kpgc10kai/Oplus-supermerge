"""Android sparse-image decoder/encoder (libsparse format)."""

from __future__ import annotations

import os
import struct

from .util import Fail

SPARSE_MAGIC = 0xED26FF3A
CHUNK_RAW = 0xCAC1
CHUNK_FILL = 0xCAC2
CHUNK_DONT_CARE = 0xCAC3
CHUNK_CRC32 = 0xCAC4

HEADER_FMT = "<IHHHHIIII"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 28
CHUNK_FMT = "<HHII"
CHUNK_SIZE = struct.calcsize(CHUNK_FMT)    # 12

IO_CHUNK = 8 << 20


def is_sparse(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return len(head) == 4 and struct.unpack("<I", head)[0] == SPARSE_MAGIC


def read_header(f) -> dict:
    f.seek(0)
    raw = f.read(HEADER_SIZE)
    if len(raw) != HEADER_SIZE:
        raise Fail("file too small to be a sparse image")
    (magic, major, _minor, file_hdr_sz, chunk_hdr_sz, blk_sz,
     total_blks, total_chunks, checksum) = struct.unpack(HEADER_FMT, raw)
    if magic != SPARSE_MAGIC:
        raise Fail("not an Android sparse image")
    if major != 1:
        raise Fail(f"unsupported sparse image major version {major}")
    if file_hdr_sz < HEADER_SIZE or chunk_hdr_sz < CHUNK_SIZE:
        raise Fail("corrupt sparse image header")
    return {
        "file_hdr_sz": file_hdr_sz,
        "chunk_hdr_sz": chunk_hdr_sz,
        "blk_sz": blk_sz,
        "total_blks": total_blks,
        "total_chunks": total_chunks,
        "image_checksum": checksum,
        "expanded_size": total_blks * blk_sz,
    }


def decode(src: str, dst: str, keep_holes: bool = True) -> int:
    """Expand a sparse image to a raw one. Returns the expanded size."""
    with open(src, "rb") as f:
        hdr = read_header(f)
        blk = hdr["blk_sz"]
        f.seek(hdr["file_hdr_sz"])
        written_blocks = 0
        with open(dst, "wb") as out:
            out.truncate(hdr["expanded_size"])
            pos = 0
            for _ in range(hdr["total_chunks"]):
                raw = f.read(CHUNK_SIZE)
                if len(raw) != CHUNK_SIZE:
                    raise Fail("truncated sparse chunk header")
                ctype, _res, chunk_blocks, total_sz = struct.unpack(CHUNK_FMT, raw)
                if hdr["chunk_hdr_sz"] > CHUNK_SIZE:
                    f.seek(hdr["chunk_hdr_sz"] - CHUNK_SIZE, os.SEEK_CUR)
                payload_len = total_sz - hdr["chunk_hdr_sz"]
                nbytes = chunk_blocks * blk
                if ctype == CHUNK_RAW:
                    if payload_len < nbytes:
                        raise Fail("truncated raw chunk in sparse image")
                    remaining = nbytes
                    while remaining:
                        buf = f.read(min(IO_CHUNK, remaining))
                        if not buf:
                            raise Fail("truncated raw chunk payload")
                        out.seek(pos)
                        out.write(buf)
                        pos += len(buf)
                        remaining -= len(buf)
                elif ctype == CHUNK_FILL:
                    if payload_len < 4:
                        raise Fail("truncated fill chunk in sparse image")
                    pattern = f.read(4)
                    f.seek(payload_len - 4, os.SEEK_CUR)
                    if keep_holes and pattern == b"\x00\x00\x00\x00":
                        pos += nbytes
                    else:
                        out.seek(pos)
                        block = pattern * (blk // 4)
                        remaining = nbytes
                        while remaining:
                            n = min(IO_CHUNK, remaining)
                            reps = (n + blk - 1) // blk
                            out.write((block * reps)[:n])
                            remaining -= n
                        pos += nbytes
                elif ctype == CHUNK_DONT_CARE:
                    f.seek(payload_len, os.SEEK_CUR)
                    pos += nbytes
                elif ctype == CHUNK_CRC32:
                    f.seek(payload_len, os.SEEK_CUR)
                else:
                    raise Fail(f"unknown sparse chunk type 0x{ctype:04x}")
                written_blocks += chunk_blocks
            if written_blocks != hdr["total_blks"]:
                raise Fail(
                    f"sparse image block count mismatch "
                    f"(header {hdr['total_blks']}, chunks {written_blocks})"
                )
            out.truncate(hdr["expanded_size"])
    return hdr["expanded_size"]


def encode(src: str, dst: str, blk_sz: int = 4096, detect_holes: bool = True) -> int:
    """Wrap a raw image into the sparse format. Returns the output size."""
    total = os.path.getsize(src)
    if total % blk_sz:
        raise Fail(f"raw image size {total} is not a multiple of block size {blk_sz}")
    total_blks = total // blk_sz

    chunks: list[tuple[str, int, object]] = []
    with open(src, "rb") as f:
        block_index = 0
        while block_index < total_blks:
            f.seek(block_index * blk_sz)
            buf = f.read(IO_CHUNK // blk_sz * blk_sz)
            if not buf:
                break
            nblocks = len(buf) // blk_sz
            # Split the run into maximal same-kind stretches inside this window.
            i = 0
            while i < nblocks:
                start = i * blk_sz
                blk = buf[start:start + blk_sz]
                if detect_holes and blk.count(0) == blk_sz:
                    j = i + 1
                    while j < nblocks and buf[j * blk_sz:(j + 1) * blk_sz].count(0) == blk_sz:
                        j += 1
                    chunks.append(("dontcare", j - i, None))
                    i = j
                elif blk.count(blk[0]) == blk_sz:
                    j = i + 1
                    while j < nblocks and buf[j * blk_sz:(j + 1) * blk_sz] == blk:
                        j += 1
                    chunks.append(("fill", j - i, blk[:4]))
                    i = j
                else:
                    j = i + 1
                    while j < nblocks:
                        nxt = buf[j * blk_sz:(j + 1) * blk_sz]
                        if detect_holes and nxt.count(0) == blk_sz:
                            break
                        if nxt.count(nxt[0]) == blk_sz and nxt == blk:
                            break
                        j += 1
                    chunks.append(("raw", j - i, buf[i * blk_sz:j * blk_sz]))
                    i = j
            block_index += nblocks

    with open(dst, "wb") as out:
        out.write(struct.pack(
            HEADER_FMT, SPARSE_MAGIC, 1, 0, HEADER_SIZE, CHUNK_SIZE,
            blk_sz, total_blks, len(chunks), 0,
        ))
        for kind, nblocks, payload in chunks:
            if kind == "raw":
                ctype = CHUNK_RAW
                data = payload
            elif kind == "fill":
                ctype = CHUNK_FILL
                data = payload
            else:
                ctype = CHUNK_DONT_CARE
                data = b""
            total_sz = CHUNK_SIZE + len(data)
            out.write(struct.pack(CHUNK_FMT, ctype, 0, nblocks, total_sz))
            if data:
                out.write(data)
    return os.path.getsize(dst)


def write_raw_like(raw_path: str, out_path: str, sparse: bool) -> str:
    """If sparse is requested, encode raw_path into out_path; else copy."""
    if sparse:
        encode(raw_path, out_path)
        return out_path
    import shutil
    shutil.copyfile(raw_path, out_path)
    return out_path
