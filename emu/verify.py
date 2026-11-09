"""Check that the emulated Airoha library reproduces the factory presets byte-for-byte.

Usage: python emu/verify.py [factory_backup.json]
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from peqlib import PeqLib
import race_tool as rt

ref = {k: bytes.fromhex(v) for k, v in json.load(open(sys.argv[1] if len(sys.argv) > 1 else rt.BACKUP)).items()}
lib = PeqLib(rt.LIB_PATH)
for i, key in enumerate(rt.PEQ_KEYS):
    data = ref[f'{key:04X}']
    t = rt.decode_peq(data)[0]
    bands = []
    for bq in t['bands']:
        f0, g, q, _ = rt.biquad_to_peak(*bq, t['fs'])
        bands.append((round(f0), round(g * 2) / 2, round(q * 100) / 100))
    out = lib.build_nvkey(bands)
    diff = sum(a != b for a, b in zip(out, data)) + abs(len(out) - len(data))
    print(f'preset {i + 1}: {len(bands)} bands ->', 'IDENTICAL' if out == data else f'{diff} bytes differ (rounded params)')
