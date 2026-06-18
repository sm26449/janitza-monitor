"""Round-trip proof for RegisterEncoder against STANDARD Modbus word order.

The virtual meter's OUTPUT must match what the consumer (Victron's
dbus-modbus-client) decodes: registers are 16-bit big-endian on the wire;
`Reg_*b` = big word order (high word first), `Reg_*l` = little (low word first).
We validate against that standard — NOT janitza's RegisterParser, whose
little-endian signed-int path is internally inconsistent (a separate issue in
the Janitza *input* reader, irrelevant to the *output* encoder).
"""
import math
import struct
import pytest
from janitza.encoder import RegisterEncoder

ORDERS = ['big', 'little']
INT_CASES = {
    'int16':  [0, 1, -1, 32767, -32768, 1234],
    'uint16': [0, 1, 65535, 4096],
    'int32':  [0, 1, -1, 2147483647, -2147483648, -57940, 123456],
    'uint32': [0, 1, 4294967295, 87990],
    'int64':  [0, -1, 9223372036854775807, -9223372036854775808],
    'uint64': [0, 1, 18446744073709551615],
}


def ref_decode_int(regs, dtype, order):
    words = regs if order == 'big' else list(reversed(regs))   # → [high..low]
    u = 0
    for w in words:
        u = (u << 16) | (w & 0xffff)
    bits = len(regs) * 16
    if not dtype.startswith('u') and u >= (1 << (bits - 1)):
        u -= (1 << bits)
    return u


def ref_decode_float(regs, dtype, order):
    words = regs if order == 'big' else list(reversed(regs))
    raw = b''.join(int(w & 0xffff).to_bytes(2, 'big') for w in words)
    return struct.unpack('>f' if dtype != 'double' else '>d', raw)[0]


@pytest.mark.parametrize('order', ORDERS)
@pytest.mark.parametrize('dtype', list(INT_CASES))
def test_int_roundtrip(order, dtype):
    enc = RegisterEncoder(order)
    for v in INT_CASES[dtype]:
        regs = enc.encode(v, dtype)
        assert ref_decode_int(regs, dtype, order) == v, f"{dtype}/{order} v={v} regs={regs}"


@pytest.mark.parametrize('order', ORDERS)
@pytest.mark.parametrize('dtype', ['float', 'double'])
def test_float_roundtrip(order, dtype):
    enc = RegisterEncoder(order)
    for v in [0.0, 1.5, -1.5, 236.816, -5794.778, 49.995, 12345.678]:
        regs = enc.encode(v, dtype)
        back = ref_decode_float(regs, dtype, order)
        assert math.isclose(back, v, rel_tol=1e-6, abs_tol=1e-3), f"{dtype}/{order} v={v} back={back}"


def test_scale_convention():
    # EM24 Reg_s32l scale=10: -5794 W -> raw -57940 -> consumer reads /10 = -5794
    enc = RegisterEncoder('little')
    regs = enc.encode(-5794, 'int32', scale=10)
    assert ref_decode_int(regs, 'int32', 'little') == -57940


def test_clamp_no_overflow():
    enc = RegisterEncoder('big')
    regs = enc.encode(10**12, 'int16')              # far over range
    assert ref_decode_int(regs, 'int16', 'big') == 32767   # clamped, no overflow


def test_string_roundtrip():
    enc = RegisterEncoder('big')
    regs = enc.encode_string('JNZ001', 7)
    assert len(regs) == 7
    raw = b''.join(int(r).to_bytes(2, 'big') for r in regs)
    assert raw.rstrip(b'\x00').decode('ascii') == 'JNZ001'
