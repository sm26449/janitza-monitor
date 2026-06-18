#!/usr/bin/env python3
"""Standalone Carlo Gavazzi EM24 reader — decodes the EM24 register map exactly
as Victron's dbus-modbus-client does (raw / scale, Reg_s32l = low word first).

Point it at our virtual meter OR a real EM24 to confirm the data is correct:
    python tools/read_em24.py <host> [port] [unit]
"""
import sys
from pymodbus.client import ModbusTcpClient

MODELS = {1648: 'EM24DINAV23XE1X', 1651: 'EM24DINAV53XE1X', 1652: 'EM24DINAV53XE1PFA'}


def _s32l(regs):                       # Reg_s32l: low word first, signed
    u = ((regs[1] & 0xffff) << 16) | (regs[0] & 0xffff)
    return u - (1 << 32) if u >= (1 << 31) else u


def read_em24(host: str, port: int = 1502, unit: int = 1) -> dict:
    c = ModbusTcpClient(host, port=port)
    if not c.connect():
        raise SystemExit(f"cannot connect to {host}:{port}")

    def rd(addr, count):
        rr = c.read_holding_registers(addr, count=count, slave=unit)
        if rr.isError():
            raise SystemExit(f"read error @0x{addr:04x}: {rr}")
        return rr.registers

    out = {}
    model = rd(0x000b, 1)[0]
    out['model_id'] = model
    out['model'] = MODELS.get(model, f'unknown({model})')
    out['application'] = rd(0xa000, 1)[0]
    out['power_total_W'] = _s32l(rd(0x0028, 2)) / 10
    out['frequency_Hz'] = rd(0x0033, 1)[0] / 10
    out['energy_import_kWh'] = _s32l(rd(0x0034, 2)) / 10
    out['energy_export_kWh'] = _s32l(rd(0x004e, 2)) / 10
    for ph, vb, ib, pb in [(1, 0x0000, 0x000c, 0x0012),
                           (2, 0x0002, 0x000e, 0x0014),
                           (3, 0x0004, 0x0010, 0x0016)]:
        out[f'L{ph}_V'] = _s32l(rd(vb, 2)) / 10
        out[f'L{ph}_A'] = _s32l(rd(ib, 2)) / 1000
        out[f'L{ph}_W'] = _s32l(rd(pb, 2)) / 10
    c.close()
    return out


if __name__ == '__main__':
    host = sys.argv[1] if len(sys.argv) > 1 else '127.0.0.1'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 1502
    unit = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    vals = read_em24(host, port, unit)
    print(f"=== EM24 @ {host}:{port} unit {unit} ===")
    for k, v in vals.items():
        print(f"  {k:20} {v}")
