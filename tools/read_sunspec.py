#!/usr/bin/env python3
"""Standalone SunSpec meter reader — decodes the meter the way a SunSpec
consumer (Fronius DataManager) does: SunS marker, common model (Mn/Md),
then the meter model. Handles model 213 (3-phase FLOAT — what the Fronius
DataManager requires) and legacy model 203 (int+SF). Point at the virtual
meter or a real SunSpec meter:
    python tools/read_sunspec.py <host> [port] [unit]
"""
import struct
import sys

from pymodbus.client import ModbusTcpClient


def _i16(r):
    return r - 0x10000 if r >= 0x8000 else r


def read_sunspec(host, port=1502, unit=1):
    c = ModbusTcpClient(host, port=port)
    if not c.connect():
        raise SystemExit(f"cannot connect to {host}:{port}")

    def rd(addr, count):
        rr = c.read_holding_registers(addr, count=count, slave=unit)
        if rr.isError():
            raise SystemExit(f"read error @{addr}: {rr}")
        return rr.registers

    def _str(addr, n):
        regs = rd(addr, n)
        bs = b"".join(struct.pack(">H", w) for w in regs)
        return bs.split(b"\x00")[0].decode("latin1").strip()

    out = {}
    marker = rd(40000, 2)
    out['SunS'] = "".join(chr((w >> 8) & 0xff) + chr(w & 0xff) for w in marker)
    out['Manufacturer'] = _str(40004, 16)
    out['Model'] = _str(40020, 16)
    hdr = rd(40070, 2)
    out['model_id'] = hdr[0]
    out['model_len'] = hdr[1]

    if hdr[0] == 213:                                    # 3-phase FLOAT (Fronius)
        blk = rd(40072, 76)                              # data 40072..40147
        def f(addr):
            i = addr - 40072
            return struct.unpack(">f", struct.pack(">HH", blk[i], blk[i + 1]))[0]
        out['A_total'] = round(f(40072), 3)
        out['V_L1'] = round(f(40082), 1)
        out['V_L2'] = round(f(40084), 1)
        out['V_L3'] = round(f(40086), 1)
        out['Hz'] = round(f(40096), 3)
        out['W_total'] = round(f(40098), 1)
        out['W_L1'] = round(f(40100), 1)
        out['Wh_export'] = round(f(40130))
        out['Wh_import'] = round(f(40138))
    else:                                                # legacy model 203 int+SF
        blk = rd(40072, 53)
        b = lambda off: blk[off - 40072]
        a_sf, v_sf, hz_sf, w_sf = _i16(b(40076)), _i16(b(40085)), _i16(b(40087)), _i16(b(40092))
        out['A_total'] = round(_i16(b(40072)) * 10 ** a_sf, 2)
        out['V_L1'] = round(_i16(b(40077)) * 10 ** v_sf, 1)
        out['V_L2'] = round(_i16(b(40079)) * 10 ** v_sf, 1)
        out['V_L3'] = round(_i16(b(40080)) * 10 ** v_sf, 1)
        out['Hz'] = round(_i16(b(40086)) * 10 ** hz_sf, 2)
        out['W_total'] = round(_i16(b(40088)) * 10 ** w_sf, 1)
        out['W_L1'] = round(_i16(b(40089)) * 10 ** w_sf, 1)
        wh_sf = _i16(b(40124))
        exp = rd(40108, 2); imp = rd(40116, 2)
        out['Wh_export'] = ((exp[0] << 16) | exp[1]) * 10 ** wh_sf
        out['Wh_import'] = ((imp[0] << 16) | imp[1]) * 10 ** wh_sf
    c.close()
    return out


if __name__ == '__main__':
    host = sys.argv[1] if len(sys.argv) > 1 else '127.0.0.1'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 1502
    unit = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    for k, v in read_sunspec(host, port, unit).items():
        print(f"  {k:14} {v}")
