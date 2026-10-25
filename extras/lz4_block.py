"""LZ4 block decompression (raw block format, no frame header)."""

from __future__ import annotations


class Lz4Error(Exception):
    pass


def decompress_block(src: bytes, expected: int) -> bytes:
    """Decode an LZ4 block, stopping once *expected* bytes are produced."""
    dst = bytearray()
    pos = 0
    n = len(src)
    while pos < n and len(dst) < expected:
        token = src[pos]
        pos += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                if pos >= n:
                    raise Lz4Error("truncated literal length")
                b = src[pos]
                pos += 1
                lit_len += b
                if b != 255:
                    break
        if lit_len:
            if pos + lit_len > n:
                raise Lz4Error("truncated literals")
            dst += src[pos:pos + lit_len]
            pos += lit_len
        if pos + 2 > n:
            break
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0 or offset > len(dst):
            raise Lz4Error(f"bad match offset {offset} at {pos}")
        match_len = token & 0x0F
        if match_len == 15:
            while True:
                if pos >= n:
                    raise Lz4Error("truncated match length")
                b = src[pos]
                pos += 1
                match_len += b
                if b != 255:
                    break
        match_len += 4
        start = len(dst) - offset
        if match_len <= offset:
            dst += dst[start:start + match_len]
        else:
            for i in range(match_len):
                dst.append(dst[start + i])
        if len(dst) > expected + 64:
            # keep decoding bounded even on corrupt input
            break
    if len(dst) < expected:
        raise Lz4Error(f"short output ({len(dst)} < {expected})")
    return bytes(dst[:expected])
