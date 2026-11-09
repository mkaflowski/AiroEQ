"""Airoha RACE client for FlyCC earbuds over Bluetooth Classic RFCOMM (Windows/Linux).

Close the FlyCC app on the phone first: it holds the RACE channel.

Usage:
  python race_tool.py extract-lib FlyCC.apk    # one-time: take libnative-peq.so from the FlyCC APK
  python race_tool.py devices                  # list paired Bluetooth devices
  python race_tool.py --addr XX:XX:XX:XX:XX:XX info   # address is remembered afterwards
  python race_tool.py eq                       # decode the 5 preset curves
  python race_tool.py read F2B0 F2B5 ...
  python race_tool.py scan F000 FFFF [-o scan.json]
  python race_tool.py preset 1..5              # select preset slot (same command as the app)
  python race_tool.py peq 2 60:6:0.7 3000:-2:1 10000:3:2 [--dry-run]
                                               # write own curve (Hz:dB:Q per band) into slot 2
  python race_tool.py restore 2                # write factory curve back (from factory_backup.json)
Options: --addr  --channel 3  -v
"""
import argparse, json, math, os, re, socket, struct, subprocess, sys, time, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, 'airoeq_config.json')
LIB_PATH = os.path.join(HERE, 'native', 'libnative-peq.so')
APK_LIB = 'lib/arm64-v8a/libnative-peq.so'
DEFAULT_CHANNEL = 3


def load_config():
    try:
        return json.load(open(CONFIG, encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_config(**kw):
    cfg = load_config()
    cfg.update(kw)
    json.dump(cfg, open(CONFIG, 'w', encoding='utf-8'), indent=1)


def extract_lib(apk_path):
    """Copy Airoha's libnative-peq.so (arm64) out of the FlyCC APK. Not redistributed with AiroEQ."""
    with zipfile.ZipFile(apk_path) as z:
        if APK_LIB not in z.namelist():
            raise RuntimeError(f'{os.path.basename(apk_path)} does not contain {APK_LIB}')
        data = z.read(APK_LIB)
    os.makedirs(os.path.dirname(LIB_PATH), exist_ok=True)
    with open(LIB_PATH, 'wb') as f:
        f.write(data)
    return LIB_PATH


def paired_devices():
    """[(name, 'AA:BB:CC:DD:EE:FF')] of paired Bluetooth Classic devices (Windows / Linux bluetoothctl)."""
    out = []
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        if sys.platform == 'win32':
            ps = ("Get-PnpDevice -Class Bluetooth | Where-Object { $_.InstanceId -match 'BTHENUM\\\\DEV_' } | "
                  "ForEach-Object { $_.FriendlyName + '|' + $_.InstanceId }")
            txt = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True,
                                 timeout=20, creationflags=flags).stdout
            for line in txt.splitlines():
                m = re.search(r'^(.*)\|.*DEV_([0-9A-F]{12})', line.strip(), re.I)
                if m:
                    a = m.group(2).upper()
                    out.append((m.group(1).strip(), ':'.join(a[i:i + 2] for i in range(0, 12, 2))))
        else:
            txt = subprocess.run(['bluetoothctl', 'devices', 'Paired'], capture_output=True, text=True, timeout=20).stdout
            for line in txt.splitlines():
                m = re.match(r'Device\s+([0-9A-F:]{17})\s+(.*)', line.strip(), re.I)
                if m:
                    out.append((m.group(2), m.group(1).upper()))
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(set(out))

CMD, RESP, CMD_NR, IND = 0x5A, 0x5B, 0x5C, 0x5D
ID_NVKEY_READ = 0x0A00
ID_NVKEY_WRITE = 0x0A01
ID_RELAY = 0x0D01            # tunnel a RACE command to another earbud
ID_AVAILABLE_DST = 0x0D00   # lists relay destinations: 2 bytes, then (type, id) pairs
DST_PARTNER_TYPE = 0x05      # the other earbud; its id changes (seen 0x08, 0x38) - always query it
ID_VERSION = ID_AVAILABLE_DST
ID_DEVICE_INFO = 0x1C09
ID_VENDOR = 0x2100


class Race:
    def __init__(self, addr, channel, verbose=False, timeout=2.0):
        self.verbose = verbose
        self.timeout = timeout
        self.buf = b''
        if not hasattr(socket, 'AF_BLUETOOTH'):
            raise OSError('this Python has no Bluetooth RFCOMM sockets (macOS is not supported yet; '
                          'use Windows or Linux)')
        self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        self.sock.settimeout(10)
        self.sock.connect((addr, channel))

    def close(self):
        self.sock.close()

    def partner(self):
        """Relay destination of the other earbud, or None if it is not reachable (e.g. in the case)."""
        pl = self.request(ID_AVAILABLE_DST)
        if pl:
            for i in range(2, len(pl) - 1, 2):
                if pl[i] == DST_PARTNER_TYPE:
                    return pl[i:i + 2]
        return None

    def _frames(self):
        """Yield complete RACE frames (type, id, payload) from the receive buffer."""
        while True:
            i = self.buf.find(b'\x05')
            if i < 0:
                self.buf = b''
                return
            self.buf = self.buf[i:]
            if len(self.buf) < 6:
                return
            if self.buf[1] not in (CMD, RESP, CMD_NR, IND):
                self.buf = self.buf[1:]
                continue
            ln, = struct.unpack_from('<H', self.buf, 2)
            if len(self.buf) < 4 + ln:
                return
            typ, rid = self.buf[1], struct.unpack_from('<H', self.buf, 4)[0]
            payload = self.buf[6:4 + ln]
            self.buf = self.buf[4 + ln:]
            yield typ, rid, payload

    def request(self, rid, payload=b'', match=None, relay=None):
        """Send a command and return the payload of the first matching RESP/IND.

        relay: destination bytes (from partner()) to tunnel the command to the other earbud via 0x0D01.
        """
        pkt = struct.pack('<BBHH', 0x05, CMD, len(payload) + 2, rid) + payload
        if relay is not None:
            pkt = struct.pack('<BBHH', 0x05, CMD, len(pkt) + len(relay) + 2, ID_RELAY) + relay + pkt
            self.buf = b''
            pl = self._send_wait(pkt, ID_RELAY, relay, typ_ok=(IND,))
            if pl is None:
                return None
            inner = pl[len(relay):]
            saved, self.buf = self.buf, inner
            for typ, got, ipl in self._frames():
                if got == rid and (match is None or ipl.startswith(match)):
                    self.buf = saved
                    return ipl
            self.buf = saved
            return None
        return self._send_wait(pkt, rid, match)

    def _send_wait(self, pkt, rid, match=None, typ_ok=(RESP, IND)):
        if self.verbose:
            print('->', pkt.hex(' '), file=sys.stderr)
        self.sock.sendall(pkt)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            self.sock.settimeout(max(0.05, deadline - time.time()))
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                raise ConnectionError('connection closed')
            self.buf += chunk
            for typ, got, pl in self._frames():
                if self.verbose:
                    print(f'<- {typ:02X} id=0x{got:04X} {pl.hex(" ")}', file=sys.stderr)
                if got == rid and typ in typ_ok and (match is None or pl.startswith(match)):
                    return pl
        return None

    def read_nvkey(self, key, maxlen=1000, relay=None):
        pl = self.request(ID_NVKEY_READ, struct.pack('<HH', key, maxlen), relay=relay)
        if pl is None or len(pl) < 2:
            return None
        n, = struct.unpack_from('<H', pl)
        data = pl[2:2 + n]
        return data if n and len(data) == n else (b'' if n == 0 else pl)

    def write_nvkey(self, key, data, relay=None):
        """RACE 0x0A01 write full NVKEY. Returns status byte (0 = OK) or None on no answer."""
        pl = self.request(ID_NVKEY_WRITE, struct.pack('<H', key) + data, relay=relay)
        return pl[0] if pl else None


def hexdump(data, indent='    '):
    for o in range(0, len(data), 16):
        row = data[o:o + 16]
        txt = ''.join(chr(b) if 32 <= b < 127 else '.' for b in row)
        print(f'{indent}{o:04x}  {row.hex(" "):<47}  {txt}')


def cmd_info(r, a):
    name = r.read_nvkey(0x1002)
    print('chip     :', name.decode(errors='replace') if name else '?')
    v = r.request(ID_VERSION)
    print('0x0D00   :', v.hex(' ') if v else '-')
    d = r.request(ID_DEVICE_INFO, b'\x01\xff')
    if d:
        print('0x1C09   :')
        hexdump(d)
    s = r.request(ID_VENDOR, b'\x0a', match=b'\x0a')
    print('status   :', s.hex(' ') if s else '-')


def cmd_read(r, a):
    for k in a.keys:
        key = int(k, 16)
        data = r.read_nvkey(key)
        if data is None:
            print(f'NVKEY 0x{key:04X}: no answer')
        else:
            print(f'NVKEY 0x{key:04X}: {len(data)} B')
            hexdump(data)


def cmd_scan(r, a):
    start, end = int(a.start, 16), int(a.end, 16)
    found = {}
    t0 = time.time()
    for key in range(start, end + 1):
        data = r.read_nvkey(key)
        if data:
            found[f'{key:04X}'] = data.hex()
            print(f'0x{key:04X}: {len(data):4d} B  {data[:24].hex(" ")}{" ..." if len(data) > 24 else ""}', flush=True)
        if key % 256 == 255:
            print(f'  .. 0x{key:04X} ({time.time() - t0:.0f} s)', file=sys.stderr, flush=True)
    with open(a.out, 'w') as f:
        json.dump(found, f, indent=1)
    print(f'{len(found)} keys saved to {a.out}')


PEQ_KEYS = [0xF260, 0xF261, 0xF262, 0xF263, 0xF264]   # preset 0..4
SAMPLE_RATES = {1: 44100, 2: 48000}


def _coef(b):
    """32-bit coefficient stored as two LE halfwords, high half first."""
    hi, lo = struct.unpack('<hH', b)
    return (hi << 16) | lo


def decode_peq(data):
    """Parse a PEQ NVKEY. Returns list of tables: {fs, bands: [(b0,b1,b2,a1,a2) floats], tail}.

    Layout: u16 table_count, u16 0, then per table: u16 sr_id, u16 len16, u16 bands,
    bands * 5 coefs (b0,b1,b2,a1,a2), 1 trailing word. b0/b2/a2 are Q31, b1/a1 are Q30.
    """
    count, = struct.unpack_from('<H', data, 0)
    o, tables = 4, []
    for _ in range(count):
        sr, ln, nb = struct.unpack_from('<HHH', data, o)
        p, bands = o + 6, []
        for _ in range(nb):
            c = [_coef(data[p + 4 * i:p + 4 * i + 4]) for i in range(5)]
            bands.append((c[0] / 2**31, c[1] / 2**30, c[2] / 2**31, c[3] / 2**30, c[4] / 2**31))
            p += 20
        tables.append({'fs': SAMPLE_RATES.get(sr, sr), 'bands': bands, 'tail': _coef(data[p:p + 4])})
        o += 4 + 2 * ln
    return tables


def biquad_to_peak(b0, b1, b2, a1, a2, fs):
    """Invert an RBJ peaking biquad -> (f0 Hz, gain dB, Q, linear pre-gain)."""
    g = b0
    nb1, nb2 = b1 / g, b2 / g
    aA, a_A = (1 - nb2) / (1 + nb2), (1 - a2) / (1 + a2)
    A, alpha = math.sqrt(aA / a_A), math.sqrt(aA * a_A)
    w0 = math.acos(max(-1.0, min(1.0, -nb1 * (1 + aA) / 2)))
    return w0 * fs / (2 * math.pi), 40 * math.log10(A), math.sin(w0) / (2 * alpha), g


def cmd_eq(r, a):
    for i, key in enumerate(PEQ_KEYS):
        data = r.read_nvkey(key)
        if not data:
            print(f'slot {i + 1} (0x{key:04X}): missing'); continue
        t = decode_peq(data)[0]
        print(f'slot {i + 1} (NVKEY 0x{key:04X}), {len(t["bands"])} bands:')
        for j, bq in enumerate(t['bands']):
            f0, gain, q, g = biquad_to_peak(*bq, t['fs'])
            pre = f'   pre-gain {20 * math.log10(g):+.2f} dB' if abs(g - 1) > 1e-3 else ''
            print(f'   {j + 1}: {f0:7.0f} Hz  {gain:+6.2f} dB  Q {q:4.2f}{pre}')


BACKUP = os.path.join(HERE, 'factory_backup.json')   # this device's original preset curves


def load_factory_backup():
    try:
        return json.load(open(BACKUP, encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def ensure_factory_backup(r, log=print):
    """Before the very first write, save all 5 original preset curves of this earbud."""
    if all(f'{k:04X}' in load_factory_backup() for k in PEQ_KEYS):
        return
    saved = {}
    for k in PEQ_KEYS:
        data = r.read_nvkey(k)
        if not data:
            raise RuntimeError('Could not read the original presets for the factory backup - nothing was written')
        saved[f'{k:04X}'] = data.hex()
    json.dump(saved, open(BACKUP, 'w', encoding='utf-8'), indent=1)
    log(f'Factory backup saved to {os.path.basename(BACKUP)}')
LIMITS = dict(bands=(1, 10), f=(20, 20000), gain=(-15, 12), q=(0.1, 10))


def parse_bands(specs):
    bands = []
    for s in specs:
        try:
            f, g, q = (float(x) for x in s.split(':'))
        except ValueError:
            sys.exit(f'Bad band "{s}" - format is Hz:dB:Q, e.g. 1000:-3:1.4')
        for name, v in (('f', f), ('gain', g), ('q', q)):
            lo, hi = LIMITS[name]
            if not lo <= v <= hi:
                sys.exit(f'Band "{s}": {name}={v} out of range {lo}..{hi}')
        bands.append((f, g, q))
    lo, hi = LIMITS['bands']
    if not lo <= len(bands) <= hi:
        sys.exit(f'Number of bands must be {lo}..{hi}')
    return bands


def print_curve(data):
    t = decode_peq(data)[0]
    for j, bq in enumerate(t['bands']):
        f0, gain, q, g = biquad_to_peak(*bq, t['fs'])
        pre = f'   pre-gain {20 * math.log10(g):+.2f} dB' if abs(g - 1) > 1e-3 else ''
        print(f'   {j + 1}: {f0:7.0f} Hz  {gain:+6.2f} dB  Q {q:4.2f}{pre}')




def select_preset(r, index):
    return r.request(ID_VENDOR, bytes([0x0f, 0x0a, 0x00, 0x00, index, 0x00]), match=b'\x0f\x0a') is not None


def write_preset_data(r, index, data, log=print):
    """Back up, write to both earbuds, verify, then re-select the preset so the DSP reloads it.
    Raises RuntimeError on failure."""
    key = PEQ_KEYS[index]
    partner = r.partner()
    if partner is None:
        raise RuntimeError('The other earbud is not reachable (in the case or disconnected). '
                           'Take both earbuds out and try again - nothing was written.')
    targets = [('this earbud', None), ('other earbud', partner)]
    ensure_factory_backup(r, log)
    bdir = os.path.join(HERE, 'backups')          # previous curve of each slot, overwritten on every write
    os.makedirs(bdir, exist_ok=True)
    for name, relay in targets:
        old = r.read_nvkey(key, relay=relay)
        if old is None:
            raise RuntimeError(f'{name}: no answer while reading, nothing was written')
        path = os.path.join(bdir, f'slot{index + 1}_{"other" if relay else "this"}_earbud_previous.bin')
        with open(path, 'wb') as f:
            f.write(old)
    for name, relay in targets:
        st = r.write_nvkey(key, data, relay=relay)
        back = r.read_nvkey(key, relay=relay)
        ok = st == 0 and back == data
        log(f'{name}: status {st}, verify {"OK" if ok else "FAILED"}')
        if not ok:
            raise RuntimeError(f'{name}: write failed (status {st}) - use "Restore factory"')
    log(f'slot {index + 1} re-selected: ' + ('OK' if select_preset(r, index) else 'no answer'))


def write_preset(r, index, data, dry_run=False):
    key = PEQ_KEYS[index]
    print(f'slot {index + 1} -> NVKEY 0x{key:04X}, {len(data)} B:')
    print_curve(data)
    if dry_run:
        print('(--dry-run: nothing written)'); return
    try:
        write_preset_data(r, index, data)
    except RuntimeError as e:
        sys.exit(str(e))


def _peqlib():
    if not os.path.exists(LIB_PATH):
        sys.exit('Airoha PEQ library missing - run once: python race_tool.py extract-lib <FlyCC.apk>')
    sys.path.insert(0, os.path.join(HERE, 'emu'))
    from peqlib import PeqLib
    return PeqLib(LIB_PATH)


def cmd_peq(r, a):
    bands = parse_bands(a.bands)
    print('Computing coefficients (emulated Airoha library)...')
    data = _peqlib().build_nvkey(bands)
    write_preset(r, a.index, data, a.dry_run)


def cmd_restore(r, a):
    key = f'{PEQ_KEYS[a.index]:04X}'
    src = json.load(open(a.source)) if os.path.exists(a.source) else {}
    if key not in src:
        sys.exit(f'No factory backup for slot {a.index + 1} in {a.source}')
    write_preset(r, a.index, bytes.fromhex(src[key]), a.dry_run)


def cmd_preset(r, a):
    pl = r.request(ID_VENDOR, bytes([0x0f, 0x0a, 0x00, 0x00, a.index, 0x00]), match=b'\x0f\x0a')
    print('answer:', pl.hex(' ') if pl else 'none')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cfg = load_config()
    p.add_argument('--addr', default=cfg.get('address'), help='earbuds Bluetooth address (remembered)')
    p.add_argument('--channel', type=int, default=cfg.get('channel', DEFAULT_CHANNEL))
    p.add_argument('-v', '--verbose', action='store_true')
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('extract-lib', help='copy libnative-peq.so out of the FlyCC APK'); s.add_argument('apk')
    sub.add_parser('devices', help='list paired Bluetooth devices')
    sub.add_parser('info')
    sub.add_parser('eq', help='read and decode the preset curves')
    s = sub.add_parser('read'); s.add_argument('keys', nargs='+')
    s = sub.add_parser('scan'); s.add_argument('start'); s.add_argument('end'); s.add_argument('-o', '--out', default='scan.json')
    s = sub.add_parser('preset'); s.add_argument('slot', type=int, choices=range(1, 6))
    s = sub.add_parser('peq'); s.add_argument('slot', type=int, choices=range(1, 6))
    s.add_argument('bands', nargs='+', help='Hz:dB:Q'); s.add_argument('--dry-run', action='store_true')
    s = sub.add_parser('restore'); s.add_argument('slot', type=int, choices=range(1, 6))
    s.add_argument('--source', default=BACKUP); s.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    if hasattr(a, 'slot'):
        a.index = a.slot - 1                  # CLI and GUI use slots 1..5

    if a.cmd == 'extract-lib':
        print('Saved', extract_lib(a.apk)); return
    if a.cmd == 'devices':
        for name, addr in paired_devices():
            print(f'{addr}  {name}')
        return
    if a.cmd == 'peq' and a.dry_run:          # no earbuds needed
        bands = parse_bands(a.bands)
        write_preset(None, a.index, _peqlib().build_nvkey(bands), True)
        return
    if not a.addr:
        sys.exit('No earbuds address yet - find it with "python race_tool.py devices", then pass --addr once.')

    try:
        r = Race(a.addr, a.channel, a.verbose)
    except OSError as e:
        sys.exit(f'Cannot connect to {a.addr} channel {a.channel}: {e}\n'
                 'The earbuds must be paired with this PC and switched on.')
    save_config(address=a.addr, channel=a.channel)
    try:
        {'peq': cmd_peq, 'restore': cmd_restore, 'info': cmd_info, 'eq': cmd_eq, 'read': cmd_read, 'scan': cmd_scan, 'preset': cmd_preset}[a.cmd](r, a)
    finally:
        r.close()


if __name__ == '__main__':
    main()
