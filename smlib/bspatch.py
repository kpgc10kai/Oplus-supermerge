"""Binary patch (BSDIFF40) application, used for delta OTA operations."""

from __future__ import annotations

import bz2

from .util import Fail

MAGIC = b"BSDIFF40"


def _offtin(buf: bytes) -> int:
    """bsdiff's sign-magnitude little-endian int64."""
    y = int.from_bytes(buf[7:8] + buf[6:7] + buf[5:6] + buf[4:5]
                       + buf[3:4] + buf[2:3] + buf[1:2] + buf[0:1], "big")
    if y >= 1 << 63:
        return -(y & ~(1 << 63))
    return y


def _decompress_stream(data: bytes) -> bytes:
    out = bytearray()
    while data:
        dec = bz2.BZ2Decompressor()
        out += dec.decompress(data)
        if not dec.eof:
            raise Fail("corrupt bzip2 stream in bsdiff patch")
        data = dec.unused_data
        if data and not data.startswith(b"BZh"):
            break
    return bytes(out)


def apply(patch: bytes, old: bytes) -> bytes:
    if not patch.startswith(MAGIC):
        raise Fail("not a BSDIFF40 patch")
    if len(patch) < 32:
        raise Fail("truncated bsdiff patch")
    ctrl_len = _offtin(patch[8:16])
    diff_len = _offtin(patch[16:24])
    new_size = _offtin(patch[24:32])
    if ctrl_len < 0 or diff_len < 0 or new_size < 0:
        raise Fail("corrupt bsdiff header")
    ctrl_start = 32
    diff_start = ctrl_start + ctrl_len
    extra_start = diff_start + diff_len
    if extra_start > len(patch):
        raise Fail("corrupt bsdiff section lengths")

    ctrl = _decompress_stream(patch[ctrl_start:diff_start])
    diff = _decompress_stream(patch[diff_start:extra_start])
    extra = _decompress_stream(patch[extra_start:])

    new = bytearray(new_size)
    old_size = len(old)
    old_pos = 0
    new_pos = 0
    cpos = dpos = epos = 0
    while new_pos < new_size:
        if cpos + 24 > len(ctrl):
            raise Fail("truncated bsdiff control block")
        add_len = _offtin(ctrl[cpos:cpos + 8])
        copy_len = _offtin(ctrl[cpos + 8:cpos + 16])
        seek = _offtin(ctrl[cpos + 16:cpos + 24])
        cpos += 24
        if add_len < 0 or copy_len < 0 or new_pos + add_len > new_size:
            raise Fail("corrupt bsdiff control values")
        if dpos + add_len > len(diff):
            raise Fail("truncated bsdiff diff block")
        for i in range(add_len):
            src = old_pos + i
            base = old[src] if 0 <= src < old_size else 0
            new[new_pos + i] = (diff[dpos + i] + base) & 0xFF
        new_pos += add_len
        dpos += add_len
        old_pos += add_len
        if new_pos + copy_len > new_size:
            raise Fail("bsdiff copy overruns new file")
        if epos + copy_len > len(extra):
            raise Fail("truncated bsdiff extra block")
        new[new_pos:new_pos + copy_len] = extra[epos:epos + copy_len]
        new_pos += copy_len
        epos += copy_len
        old_pos += seek
    return bytes(new)
