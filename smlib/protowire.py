"""Minimal protobuf wire-format codec.

Only what payload.bin needs: decode a message into {field_number: [values]},
where LEN fields stay as raw ``bytes`` (callers decode nested messages), and
provide small encoders so the test-suite can synthesise payloads without a
protoc toolchain.
"""

from __future__ import annotations


class ProtoError(ValueError):
    pass


WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN = 2
WIRE_START_GROUP = 3
WIRE_END_GROUP = 4
WIRE_32BIT = 5


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtoError("varint longer than 64 bits")


def parse(buf: bytes) -> dict[int, list]:
    """Decode a protobuf message body into an ordered field map."""
    fields: dict[int, list] = {}
    pos = 0
    size = len(buf)
    while pos < size:
        key, pos = read_varint(buf, pos)
        field_no = key >> 3
        wire = key & 0x7
        if field_no == 0:
            raise ProtoError("field number 0 is invalid")
        if wire == WIRE_VARINT:
            value, pos = read_varint(buf, pos)
        elif wire == WIRE_64BIT:
            if pos + 8 > size:
                raise ProtoError("truncated fixed64")
            value = buf[pos:pos + 8]
            pos += 8
        elif wire == WIRE_LEN:
            length, pos = read_varint(buf, pos)
            if pos + length > size:
                raise ProtoError("truncated length-delimited field")
            value = buf[pos:pos + length]
            pos += length
        elif wire == WIRE_32BIT:
            if pos + 4 > size:
                raise ProtoError("truncated fixed32")
            value = buf[pos:pos + 4]
            pos += 4
        elif wire in (WIRE_START_GROUP, WIRE_END_GROUP):
            raise ProtoError("group wire types are not supported")
        else:
            raise ProtoError(f"invalid wire type {wire}")
        fields.setdefault(field_no, []).append(value)
    return fields


class Msg:
    """Convenience view over a parsed message."""

    __slots__ = ("fields",)

    def __init__(self, fields: dict[int, list] | None = None):
        self.fields = fields or {}

    @classmethod
    def from_bytes(cls, buf: bytes) -> "Msg":
        return cls(parse(buf))

    def has(self, no: int) -> bool:
        return no in self.fields

    def varint(self, no: int, default=None):
        vals = self.fields.get(no)
        if not vals:
            return default
        v = vals[-1]
        if isinstance(v, int):
            return v
        return default  # wrong wire type -> treat as absent

    def boolean(self, no: int, default=False) -> bool:
        return bool(self.varint(no, 1 if default else 0))

    def raw(self, no: int, default=None):
        vals = self.fields.get(no)
        if not vals:
            return default
        return vals[-1]

    def bytes(self, no: int, default=None) -> bytes | None:
        v = self.raw(no, default)
        return v if isinstance(v, (bytes, bytearray)) else default

    def string(self, no: int, default=None):
        v = self.bytes(no)
        if v is None:
            return default
        return bytes(v).decode("utf-8", "replace")

    def msgs(self, no: int) -> list["Msg"]:
        out = []
        for v in self.fields.get(no, []):
            if isinstance(v, (bytes, bytearray)):
                out.append(Msg.from_bytes(bytes(v)))
        return out

    def msg(self, no: int):
        for v in self.fields.get(no, []):
            if isinstance(v, (bytes, bytearray)):
                return Msg.from_bytes(bytes(v))
        return None


# ---------------------------------------------------------------- encoding

def enc_varint(value: int) -> bytes:
    out = bytearray()
    value &= (1 << 64) - 1
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def enc_tag(field_no: int, wire: int) -> bytes:
    return enc_varint((field_no << 3) | wire)


def enc_varint_field(field_no: int, value: int) -> bytes:
    return enc_tag(field_no, WIRE_VARINT) + enc_varint(value)


def enc_bytes_field(field_no: int, data: bytes) -> bytes:
    return enc_tag(field_no, WIRE_LEN) + enc_varint(len(data)) + bytes(data)


def enc_msg_field(field_no: int, body: bytes) -> bytes:
    return enc_bytes_field(field_no, body)
