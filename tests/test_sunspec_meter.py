"""Conformance: SunSpec reader decodes the virtual Fronius meter correctly."""
import time
from janitza.virtual_meter import load_template, VirtualMeter
from tools.read_sunspec import read_sunspec

PORT = 15052


def test_sunspec_conformance():
    t = load_template('config/templates/fronius_sunspec_meter.yaml')
    t.transport['port'] = PORT
    fed = {'_I_SUM3': 1.3, '_ILN[0]': 27.9, '_ILN[1]': 28.3, '_ILN[2]': 28.1,
           '_G_ULN[0]': 235.9, '_G_ULN[1]': 234.5, '_G_ULN[2]': 234.0,
           '_G_FREQ': 50.01, '_G_P_SUM3': -20000.0, '_PLN[0]': -6659.0,
           '_PLN[1]': -6716.0, '_PLN[2]': -6656.0,
           '_WH_Z[4]': 27922776.0, '_WH_V[4]': 88052.0}
    now = time.time()
    vm = VirtualMeter(t, lambda n: (fed[n], now) if n in fed else None,
                      stale_after_s=60, update_interval_s=0.3)
    vm.start()
    try:
        for _ in range(40):
            time.sleep(0.25)
            try:
                got = read_sunspec('127.0.0.1', PORT, 1); break
            except SystemExit:
                continue
        else:
            raise AssertionError("sunspec meter did not come up")
        assert got['SunS'] == 'SunS'
        assert got['model_id'] == 213                   # float 3-phase (Fronius requires it)
        assert got['Manufacturer'] == 'Fronius'         # DataManager checks this for detection
        assert abs(got['V_L1'] - 235.9) < 0.1           # float — exact
        assert abs(got['Hz'] - 50.01) < 0.01
        assert abs(got['W_total'] - (-20000)) < 1       # float — exact
        assert abs(got['Wh_export'] - 27922776) < 4     # float32 precision at 2.8e7
        assert abs(got['Wh_import'] - 88052) < 1
    finally:
        vm.stop()
