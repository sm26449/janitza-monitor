"""Register encoder — engineering value → Modbus registers.

Turns an engineering value into the 16-bit Modbus registers a consumer expects,
honouring data type, word/byte order and scale. The word order follows the
**standard Modbus convention used by real consumers** (Victron dbus-modbus-client
`Reg_s32l`/`Reg_*b`: little = low word at the lower address). This is the
authority — NOT `RegisterParser`, whose little-endian *integer* read path is a
known, internally-inconsistent bug (see tests/test_encoder.py). Do not "fix" the
encoder to match that parser: it would silently invert every little-endian value
(e.g. grid power) fed into a control loop. Conformance is verified in tests
against the consumer convention, not against the parser.

Scale convention (matches Victron's dbus-modbus-client `Reg_*` definitions):
    raw_register = round(engineering_value * scale)
    consumer reads back: engineering_value = raw_register / scale
e.g. EM24 power Reg_s32l(..., scale=10): W=-5794 -> raw=-57940 -> reads -5794 W.
"""
from __future__ import annotations

import struct
from typing import Any

from .register_parser import RegisterParser

_INT_RANGES = {
    'int16':  (-32768, 32767),          'short':  (-32768, 32767),
    'uint16': (0, 65535),
    'int32':  (-2**31, 2**31 - 1),      'uint32': (0, 2**32 - 1),
    'int64':  (-2**63, 2**63 - 1),      'long64': (-2**63, 2**63 - 1),
    'uint64': (0, 2**64 - 1),
}


class RegisterEncoder:
    REGISTER_COUNTS = RegisterParser.REGISTER_COUNTS

    def __init__(self, byte_order: str = 'big'):
        self.byte_order = byte_order

    def register_count(self, data_type: str) -> int:
        return self.REGISTER_COUNTS.get(data_type.lower(), 2)

    def encode(self, value: Any, data_type: str, scale: float = 1.0) -> list[int]:
        """Encode ``value`` into a list of 16-bit registers for ``data_type``."""
        dt = data_type.lower()
        if dt in ('float', 'float32'):
            return self._enc_float(float(value) * scale)
        if dt == 'double':
            return self._enc_double(float(value) * scale)

        raw = int(round(float(value) * scale))
        lo, hi = _INT_RANGES.get(dt, _INT_RANGES['int32'])
        raw = max(lo, min(hi, raw))            # clamp to type range (no overflow)

        if dt in ('int16', 'short', 'uint16'):
            return [raw & 0xffff]
        if dt in ('int32', 'uint32'):
            return self._split32(raw & 0xffffffff)
        if dt in ('int64', 'long64', 'uint64'):
            return self._split64(raw & 0xffffffffffffffff)
        return self._split32(raw & 0xffffffff)

    # ── helpers (mirror RegisterParser's word/byte layout) ─────────────────
    def _split32(self, u: int) -> list[int]:
        hi, lo = (u >> 16) & 0xffff, u & 0xffff
        # parser big: r0=hi, r1=lo ; little: value from pack('<HH', r1, r0) → r1=hi? no:
        # parser little does (registers[1]<<16)|registers[0] → r1=hi, r0=lo
        return [hi, lo] if self.byte_order == 'big' else [lo, hi]

    def _split64(self, u: int) -> list[int]:
        w = [(u >> 48) & 0xffff, (u >> 32) & 0xffff, (u >> 16) & 0xffff, u & 0xffff]
        return w if self.byte_order == 'big' else list(reversed(w))

    def _enc_float(self, v: float) -> list[int]:
        # Standard Modbus: each 16-bit register big-endian; word order per
        # byte_order (big = high word first, little = low word first).
        regs = list(struct.unpack('>HH', struct.pack('>f', v)))
        return regs if self.byte_order == 'big' else list(reversed(regs))

    def _enc_double(self, v: float) -> list[int]:
        regs = list(struct.unpack('>HHHH', struct.pack('>d', v)))
        return regs if self.byte_order == 'big' else list(reversed(regs))

    def encode_string(self, text: str, length_regs: int) -> list[int]:
        """ASCII string into ``length_regs`` registers (2 chars/reg, null-padded)."""
        raw = text.encode('ascii', 'replace')[: length_regs * 2]
        raw = raw.ljust(length_regs * 2, b'\x00')
        return [int.from_bytes(raw[i:i + 2], 'big') for i in range(0, len(raw), 2)]
