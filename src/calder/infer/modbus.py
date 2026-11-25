"""Modbus recognition (Modbus Application Protocol v1.1b3; Modbus over serial line v1.02).

* RTU: frames separated by silent intervals, a constant unit address, a valid
  function code and a CRC-16/MODBUS stored little-endian.
* ASCII: ``:``-prefixed upper-case hex lines ending in CRLF with an LRC.
* TCP: MBAP header with protocol identifier 0 and a length field that covers
  the unit identifier and PDU.
"""

from __future__ import annotations

from dataclasses import dataclass

from calder.runtime.checksums import CRC_BY_NAME, crc, sum8_twos
from calder.runtime.framing import FramingError, modbus_rtu_length

FUNCTIONS: dict[int, str] = {
    0x01: "read_coils",
    0x02: "read_discrete_inputs",
    0x03: "read_holding_registers",
    0x04: "read_input_registers",
    0x05: "write_single_coil",
    0x06: "write_single_register",
    0x0F: "write_multiple_coils",
    0x10: "write_multiple_registers",
}
READ_FUNCTIONS = frozenset({0x01, 0x02, 0x03, 0x04})

_MODBUS_CRC = CRC_BY_NAME["CRC-16/MODBUS"]


def rtu_crc_ok(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    return crc(_MODBUS_CRC, frame[:-2]) == int.from_bytes(frame[-2:], "little")


@dataclass(frozen=True)
class Recognition:
    family: str
    ratio: float
    address: int | None
    frames: int
    notes: str


def recognize_rtu(tx_frames: list[bytes], rx_frames: list[bytes], threshold: float = 0.95) -> Recognition | None:
    frames = tx_frames + rx_frames
    if len(frames) < 4:
        return None
    good = 0
    addresses: set[int] = set()
    for f in frames:
        if not rtu_crc_ok(f) or (f[1] & 0x7F) not in FUNCTIONS:
            continue
        try:
            expected = modbus_rtu_length(f, request=f in tx_frames)
        except FramingError:
            continue
        if expected is not None and expected != len(f):
            continue
        good += 1
        addresses.add(f[0])
    ratio = good / len(frames)
    if ratio < threshold or len(addresses) != 1:
        return None
    return Recognition("modbus_rtu", ratio, addresses.pop(), len(frames), "CRC-16/MODBUS validates frames")


def ascii_lrc_ok(line: bytes) -> bool:
    text = line.strip()
    if not text.startswith(b":") or len(text) < 7 or len(text) % 2 == 0:
        return False
    try:
        payload = bytes.fromhex(text[1:].decode("ascii"))
    except ValueError:
        return False
    return sum8_twos(payload[:-1]) == payload[-1]


def recognize_ascii(lines: list[bytes], threshold: float = 0.95) -> Recognition | None:
    if len(lines) < 4:
        return None
    good = [ln for ln in lines if ascii_lrc_ok(ln)]
    ratio = len(good) / len(lines)
    if ratio < threshold:
        return None
    addresses = {bytes.fromhex(ln.strip()[1:3].decode())[0] for ln in good}
    address = addresses.pop() if len(addresses) == 1 else None
    return Recognition("modbus_ascii", ratio, address, len(lines), "':' framing with valid LRC")


def recognize_tcp(frames: list[bytes], threshold: float = 0.95) -> Recognition | None:
    if len(frames) < 4:
        return None
    good = 0
    for f in frames:
        if len(f) < 8:
            continue
        proto = int.from_bytes(f[2:4], "big")
        length = int.from_bytes(f[4:6], "big")
        if proto == 0 and length == len(f) - 6 and (f[7] & 0x7F) in FUNCTIONS:
            good += 1
    ratio = good / len(frames)
    if ratio < threshold:
        return None
    return Recognition("modbus_tcp", ratio, None, len(frames), "MBAP header with protocol id 0")


def request_key(pdu: bytes) -> str:
    """Message type key from a request PDU: function code and register address."""
    func = pdu[0]
    if func in READ_FUNCTIONS and len(pdu) >= 5:
        return f"{func:02x}:{int.from_bytes(pdu[1:3], 'big'):04x}x{int.from_bytes(pdu[3:5], 'big')}"
    if len(pdu) >= 3:
        return f"{func:02x}:{int.from_bytes(pdu[1:3], 'big'):04x}"
    return f"{func:02x}"


def request_name(pdu: bytes) -> str:
    func = pdu[0]
    base = FUNCTIONS.get(func, f"function_{func:02x}")
    if len(pdu) >= 3:
        reg = int.from_bytes(pdu[1:3], "big")
        base = base.replace("registers", "register").replace("coils", "coil")
        return f"{base}_{reg:04x}"
    return base
