"""The shared-memory event ring between the capture DLL and the recorder.

Layout (little-endian; mirrored in native/capture/include/calder_ring.h):

Header, 64 bytes::

    u32 magic 'CRNG' | u32 version | u64 capacity | u64 write_pos | u64 read_pos
    u64 dropped | u64 qpc_frequency | u32 writer_pid | u32 flags | 8 reserved

Data region of ``capacity`` bytes follows. ``write_pos`` and ``read_pos`` are
monotonic byte counters; offsets are taken modulo ``capacity`` and records may
wrap. The single writer (all threads of the vendor process, serialized by a
lock in the DLL) publishes a record by advancing ``write_pos`` after copying
it; the recorder consumes up to ``write_pos`` and then advances ``read_pos``.
A writer that finds too little free space drops the record and increments
``dropped`` rather than blocking the vendor application.

Record, 48-byte header then ``meta_len`` bytes of UTF-8 JSON then ``data_len``
bytes of payload, padded to a multiple of 8::

    u32 size | u16 kind | u16 transport | u64 qpc | u32 pid | u32 tid
    u64 handle | u32 direction | i32 status | u32 meta_len | u32 data_len
"""

from __future__ import annotations

import ctypes
import json
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

MAGIC = 0x474E5243  # "CRNG"
VERSION = 1
HEADER_SIZE = 64
RECORD_HEADER = struct.Struct("<IHHQIIQIiII")
assert RECORD_HEADER.size == 48
HEADER = struct.Struct("<IIQQQQQII8x")
assert HEADER.size == HEADER_SIZE

OFF_WRITE = 16
OFF_READ = 24
OFF_DROPPED = 32

KINDS = {1: "open", 2: "close", 3: "data", 4: "config", 5: "signal", 6: "error", 7: "info"}
KIND_CODES = {v: k for k, v in KINDS.items()}
TRANSPORTS = {1: "serial", 2: "visa", 3: "ftdi", 4: "winusb", 5: "hid", 6: "tcp"}
TRANSPORT_CODES = {v: k for k, v in TRANSPORTS.items()}
DIRECTIONS = {0: "none", 1: "tx", 2: "rx"}
DIRECTION_CODES = {v: k for k, v in DIRECTIONS.items()}


class RingError(RuntimeError):
    """The ring is malformed or was written by an incompatible DLL."""


@dataclass(frozen=True)
class Record:
    kind: str
    transport: str
    qpc: int
    pid: int
    tid: int
    handle: int
    direction: str
    status: int
    meta: dict[str, Any] = field(default_factory=dict)
    data: bytes = b""


def pad8(n: int) -> int:
    return (n + 7) & ~7


def encode_record(record: Record) -> bytes:
    meta = json.dumps(record.meta, separators=(",", ":")).encode("utf-8") if record.meta else b""
    size = pad8(RECORD_HEADER.size + len(meta) + len(record.data))
    header = RECORD_HEADER.pack(
        size,
        KIND_CODES[record.kind],
        TRANSPORT_CODES.get(record.transport, 0),
        record.qpc,
        record.pid,
        record.tid,
        record.handle,
        DIRECTION_CODES[record.direction],
        record.status,
        len(meta),
        len(record.data),
    )
    return (header + meta + record.data).ljust(size, b"\0")


def decode_records(blob: bytes) -> Iterator[Record]:
    pos = 0
    while pos + RECORD_HEADER.size <= len(blob):
        size, kind, transport, qpc, pid, tid, handle, direction, status, meta_len, data_len = RECORD_HEADER.unpack_from(
            blob, pos
        )
        if size < RECORD_HEADER.size or size % 8 or pos + size > len(blob):
            raise RingError(f"corrupt record at offset {pos} (size {size})")
        if RECORD_HEADER.size + meta_len + data_len > size:
            raise RingError(f"record lengths exceed its size at offset {pos}")
        start = pos + RECORD_HEADER.size
        meta_raw = blob[start : start + meta_len]
        data = blob[start + meta_len : start + meta_len + data_len]
        try:
            meta = json.loads(meta_raw.decode("utf-8")) if meta_len else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            meta = {"meta_error": meta_raw.hex()}
        yield Record(
            KINDS.get(kind, f"kind{kind}"),
            TRANSPORTS.get(transport, "unknown"),
            qpc,
            pid,
            tid,
            handle,
            DIRECTIONS.get(direction, "none"),
            status,
            meta if isinstance(meta, dict) else {"meta": meta},
            bytes(data),
        )
        pos += size


class Ring:
    """A ring over any writable buffer (``mmap`` in production, ``bytearray`` in tests)."""

    def __init__(self, buffer: Any, *, initialize: bool, capacity: int | None = None, qpc_frequency: int = 0) -> None:
        self.buf = buffer
        if initialize:
            cap = capacity if capacity is not None else len(buffer) - HEADER_SIZE
            if cap <= 0 or cap % 8:
                raise RingError("capacity must be a positive multiple of 8")
            HEADER.pack_into(buffer, 0, MAGIC, VERSION, cap, 0, 0, 0, qpc_frequency, 0, 0)
        magic, version, capacity_, *_ = HEADER.unpack_from(buffer, 0)
        if magic != MAGIC or version != VERSION:
            raise RingError(f"not a Calder ring (magic {magic:#x}, version {version})")
        self.capacity = int(capacity_)
        # Aligned 8-byte loads and stores are atomic on x86 and x64.
        self._write = ctypes.c_uint64.from_buffer(buffer, OFF_WRITE)
        self._read = ctypes.c_uint64.from_buffer(buffer, OFF_READ)
        self._dropped = ctypes.c_uint64.from_buffer(buffer, OFF_DROPPED)

    def release(self) -> None:
        """Drop the views into the buffer so an mmap can be closed."""
        del self._write, self._read, self._dropped

    @property
    def dropped(self) -> int:
        return int(self._dropped.value)

    @property
    def qpc_frequency(self) -> int:
        return int(struct.unpack_from("<Q", self.buf, 40)[0])

    def _copy_out(self, start: int, length: int) -> bytes:
        cap = self.capacity
        off = start % cap
        first = min(length, cap - off)
        base = HEADER_SIZE
        out = bytes(self.buf[base + off : base + off + first])
        if first < length:
            out += bytes(self.buf[base : base + length - first])
        return out

    def drain(self) -> list[Record]:
        write = int(self._write.value)
        read = int(self._read.value)
        if write == read:
            return []
        if write - read > self.capacity:
            raise RingError("writer overran the reader")
        blob = self._copy_out(read, write - read)
        records = list(decode_records(blob))
        self._read.value = write
        return records

    def push(self, record: Record) -> bool:
        """Writer side (used by tests and the passive tap); returns False when dropped."""
        data = encode_record(record)
        write = int(self._write.value)
        read = int(self._read.value)
        if self.capacity - (write - read) < len(data):
            self._dropped.value += 1
            return False
        cap = self.capacity
        off = write % cap
        first = min(len(data), cap - off)
        self.buf[HEADER_SIZE + off : HEADER_SIZE + off + first] = data[:first]
        if first < len(data):
            self.buf[HEADER_SIZE : HEADER_SIZE + len(data) - first] = data[first:]
        self._write.value = write + len(data)
        return True
