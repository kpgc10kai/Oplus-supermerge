import re
import struct
import sys

TAGS = {0: "property", 1: "hashtree", 2: "hash", 3: "kernel_cmdline", 4: "chain"}


def parse(path):
    d = open(path, "rb").read()
    if d[:4] != b"AVB0":
        return f"{path}: no AVB0 magic"
    major, minor, auth_size, aux_size, algo = struct.unpack_from(">IIQQI", d, 4)
    (hash_off, hash_sz, sig_off, sig_sz, key_off, key_sz,
     meta_off, meta_sz, desc_off, desc_sz, rollback, flags, rollback_loc) = \
        struct.unpack_from(">QQQQQQQQQQQII", d, 32)
    release = d[128:176].split(b"\0")[0].decode("utf-8", "replace")
    aux_base = 256 + auth_size
    pos = aux_base + desc_off
    end = pos + desc_sz
    out = [f"=== {path} ===",
           f"  {major}.{minor} algo={algo} flags=0x{flags:x} rollback={rollback} "
           f"release={release!r}"]
    while pos + 16 <= end:
        tag, nbytes = struct.unpack_from(">QQ", d, pos)
        body = d[pos + 16:pos + 16 + nbytes]
        kind = TAGS.get(tag, f"tag{tag}")
        text = [m.group().decode("ascii") for m in re.finditer(rb"[ -~]{4,}", body)]
        extra = ""
        if tag == 2 and len(body) >= 8:  # hash: image_size at +0
            size, = struct.unpack_from(">Q", body, 0)
            extra = f" size={size}"
        elif tag == 1 and len(body) >= 16:  # hashtree: image_size at +8
            size, = struct.unpack_from(">Q", body, 8)
            extra = f" size={size}"
        out.append(f"  {kind:<14}{extra:<20} {' | '.join(text[:6])}")
        pos += 16 + nbytes
    return "\n".join(out)


for p in sys.argv[1:]:
    print(parse(p))
