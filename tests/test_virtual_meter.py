"""Conformance: the standalone EM24 reader (Victron-equivalent decode) reads the
virtual meter and gets back exactly the values we fed in. Template-derived
synthetic sources so it survives source-name changes."""
import time
from janitza.virtual_meter import load_template, VirtualMeter
from tools.read_em24 import read_em24

PORT = 15022


def test_em24_conformance():
    t = load_template('config/templates/em24_av53.yaml')
    t.transport['port'] = PORT
    # synthetic value per live source name in the template
    fed = {'_G_P_SUM3': -20049.0, '_G_FREQ': 49.99, '_WH_V[4]': 87992.0,
           '_WH_Z[4]': 27913246.0, '_G_ULN[0]': 239.1, '_G_ULN[1]': 238.2,
           '_G_ULN[2]': 238.0, '_ILN[0]': 27.96, '_ILN[1]': 28.3, '_ILN[2]': 28.1,
           '_PLN[0]': -6659.0, '_PLN[1]': -6716.0, '_PLN[2]': -6656.0}
    now = time.time()
    vm = VirtualMeter(t, lambda n: (fed[n], now) if n in fed else None,
                      stale_after_s=60, update_interval_s=0.3)
    vm.start()
    try:
        for _ in range(40):
            time.sleep(0.25)
            try:
                got = read_em24('127.0.0.1', PORT, 1)
                break
            except SystemExit:
                continue
        else:
            raise AssertionError("virtual meter did not come up")

        assert got['model_id'] == 1651
        assert got['application'] == 7
        assert abs(got['power_total_W'] - (-20049.0)) < 0.5
        assert abs(got['frequency_Hz'] - 49.99) < 0.15        # 0.1 Hz resolution
        assert abs(got['L1_V'] - 239.1) < 0.2
        assert abs(got['L1_A'] - 27.96) < 0.01
        assert abs(got['L1_W'] - (-6659.0)) < 0.5
        assert abs(got['energy_export_kWh'] - 27913.246) < 0.2   # Wh->kWh*10 scale
    finally:
        vm.stop()
