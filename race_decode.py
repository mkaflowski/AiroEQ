"""Decode Airoha RACE packets from an Android btsnoop_hci.log (RFCOMM/SPP)."""
import struct, sys

TYPES = {0x5A: 'CMD ', 0x5B: 'RESP', 0x5C: 'CMDN', 0x5D: 'IND '}

def records(path):
    d = open(path, 'rb').read()
    assert d[:8] == b'btsnoop\0'
    o = 16
    while o + 24 <= len(d):
        _, il, fl, _, ts = struct.unpack('>IIIIq', d[o:o + 24]); o += 24
        yield fl, ts, d[o:o + il]; o += il

def race_frames(data):
    i = data.find(b'\x05')
    while 0 <= i and i + 6 <= len(data):
        if data[i + 1] in TYPES:
            ln, = struct.unpack_from('<H', data, i + 2)
            if i + 4 + ln <= len(data):
                rid, = struct.unpack_from('<H', data, i + 4)
                yield data[i + 1], rid, data[i + 6:i + 4 + ln]
                i += 4 + ln
                i = data.find(b'\x05', i); continue
        i = data.find(b'\x05', i + 1)

t0 = None
for fl, ts, p in records(sys.argv[1]):
    if not p or p[0] != 2: continue
    t0 = t0 or ts
    l2 = p[5:]
    if len(l2) < 4: continue
    _, cid = struct.unpack_from('<HH', l2)
    for typ, rid, pl in race_frames(l2[4:]):
        print(f'{(ts - t0) / 1e6:8.3f} {"<-" if fl & 1 else "->"} {TYPES[typ]} id=0x{rid:04X} {pl.hex(" ")}')
