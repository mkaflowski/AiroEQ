"""Run Airoha's libnative-peq.so (arm64) under Unicorn to generate PEQ NVKEY data exactly like the app.

API of the library (internal C functions, all take a "thread_id"/handle first):
  setParam(h, double fs, int total_band, int mode, int debug)
  set_peq_point(h, int idx, double f, double gain_db, double q)
  generate_cofe(h) -> int
  get_cofe_count(h) -> u16 byte count, get_cofe_param(h) -> pointer to bytes
"""
import math, os, struct, sys
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UcError
from unicorn.arm64_const import *

sys.path.insert(0, os.path.dirname(__file__))
from elf import Elf

LIB = os.path.join(os.path.dirname(__file__), '..', 'native', 'libnative-peq.so')
PAGE = 0x1000
TRAP_BASE = 0x7000_0000
HEAP_BASE, HEAP_SIZE = 0x4000_0000, 0x0800_0000
STACK_BASE, STACK_SIZE = 0x6000_0000, 0x0010_0000
TLS_BASE = 0x6800_0000
RET_ADDR = TRAP_BASE + 0xFFF0


def _al(x, a=PAGE):
    return (x + a - 1) & ~(a - 1)


class PeqLib:
    def __init__(self, path=LIB, verbose=False):
        self.verbose = verbose
        self.elf = e = Elf(path)
        self.uc = uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        top = max(s['vaddr'] + s['memsz'] for s in e.segments if s['type'] == 1)
        uc.mem_map(0, _al(top))
        for s in e.segments:
            if s['type'] == 1 and s['filesz']:
                uc.mem_write(s['vaddr'], e.d[s['off']:s['off'] + s['filesz']])
        uc.mem_map(HEAP_BASE, HEAP_SIZE)
        uc.mem_map(STACK_BASE, STACK_SIZE)
        uc.mem_map(TLS_BASE, PAGE)
        uc.mem_map(TRAP_BASE, 0x10000)
        uc.mem_write(TRAP_BASE, b'\xc0\x03\x5f\xd6' * (0x10000 // 4))  # ret everywhere
        uc.reg_write(UC_ARM64_REG_TPIDR_EL0, TLS_BASE)
        uc.mem_write(TLS_BASE + 0x28, struct.pack('<Q', 0x1122334455667788))  # stack canary
        # enable FP/SIMD
        uc.reg_write(UC_ARM64_REG_CPACR_EL1, 0x300000)
        self.heap_ptr = HEAP_BASE
        self.traps = {}
        names = {}
        for r in e.relocs:
            sym = e.syms[r['sym']] if r['sym'] else None
            if r['type'] == 1027:  # RELATIVE
                uc.mem_write(r['off'], struct.pack('<Q', r['addend'] & (2**64 - 1)))
            elif sym and sym['shndx']:
                uc.mem_write(r['off'], struct.pack('<Q', sym['value'] + (r['addend'] if r['type'] == 257 else 0)))
            elif sym:
                n = sym['name']
                if n not in names:
                    names[n] = TRAP_BASE + 4 * len(names)
                    self.traps[names[n]] = n
                uc.mem_write(r['off'], struct.pack('<Q', names[n]))
        uc.hook_add(UC_HOOK_CODE, self._trap, begin=TRAP_BASE, end=TRAP_BASE + 0xFFFF)

    # ---- libc stubs -------------------------------------------------------
    def _malloc(self, n):
        p = _al(self.heap_ptr, 16)
        self.heap_ptr = p + max(n, 16)
        if self.heap_ptr > HEAP_BASE + HEAP_SIZE:
            raise MemoryError('emulated heap exhausted')
        self.uc.mem_write(p, b'\0' * n)
        return p

    def _d(self, i):
        return struct.unpack('<d', struct.pack('<Q', self.uc.reg_read(UC_ARM64_REG_D0 + i)))[0]

    def _set_d0(self, v):
        self.uc.reg_write(UC_ARM64_REG_D0, struct.unpack('<Q', struct.pack('<d', v))[0])

    def _x(self, i):
        return self.uc.reg_read(UC_ARM64_REG_X0 + i)

    def _trap(self, uc, addr, size, _):
        if addr == RET_ADDR:
            uc.emu_stop(); return
        n = self.traps.get(addr)
        if n is None:
            return
        x = self._x
        if n == 'malloc':
            uc.reg_write(UC_ARM64_REG_X0, self._malloc(x(0)))
        elif n == 'calloc':
            uc.reg_write(UC_ARM64_REG_X0, self._malloc(x(0) * x(1)))
        elif n == 'posix_memalign':
            uc.mem_write(x(0), struct.pack('<Q', self._malloc(x(2)))); uc.reg_write(UC_ARM64_REG_X0, 0)
        elif n in ('free', '__android_log_print', 'pthread_mutex_lock', 'pthread_mutex_unlock', '__cxa_atexit'):
            if n == '__android_log_print' and self.verbose:
                fmt = uc.mem_read(x(2), 200).split(b'\0')[0]
                print('  [lib]', fmt.decode(errors='replace'), file=sys.stderr)
            uc.reg_write(UC_ARM64_REG_X0, 0)
        elif n == 'memset':
            uc.mem_write(x(0), bytes([x(1) & 0xff]) * x(2))
        elif n in ('memcpy', 'memmove', '__memcpy_chk', '__memmove_chk'):
            uc.mem_write(x(0), bytes(uc.mem_read(x(1), x(2))))
        elif n == 'pow':
            self._set_d0(math.pow(self._d(0), self._d(1)))
        elif n == 'log10':
            self._set_d0(math.log10(self._d(0)) if self._d(0) > 0 else -math.inf)
        elif n == 'log':
            self._set_d0(math.log(self._d(0)) if self._d(0) > 0 else -math.inf)
        elif n == 'exp':
            self._set_d0(math.exp(self._d(0)))
        elif n == 'atan2':
            self._set_d0(math.atan2(self._d(0), self._d(1)))
        elif n == 'hypot':
            self._set_d0(math.hypot(self._d(0), self._d(1)))
        elif n == 'sincos':
            v = self._d(0)
            uc.mem_write(x(0), struct.pack('<d', math.sin(v)))
            uc.mem_write(x(1), struct.pack('<d', math.cos(v)))
        elif n == 'qsort':
            self._qsort(x(0), x(1), x(2), x(3))
        elif n == '__stack_chk_fail':
            raise RuntimeError('stack check failed in emulated lib')
        else:
            raise NotImplementedError(f'import {n} called from lib')

    def _qsort(self, base, n, size, cmp):
        """The lib only uses two comparators; reproduce them in Python."""
        import functools
        items = [self.read(base + i * size, size) for i in range(n)]
        code = self.read(cmp, 16)
        if code == bytes.fromhex('200440fd010440fd0038611e0000781e'):    # compare1: (int)(b.d@8 - a.d@8)
            key = lambda a, b: int(struct.unpack_from('<d', b, 8)[0] - struct.unpack_from('<d', a, 8)[0])
        elif code[:12] == bytes.fromhex('080040b9290040b90001094b'):          # compare2: a.i@0 - b.i@0
            key = lambda a, b: struct.unpack_from('<i', a)[0] - struct.unpack_from('<i', b)[0]
        else:
            raise NotImplementedError(f'qsort comparator at {cmp:#x}: {code.hex()}')
        items.sort(key=functools.cmp_to_key(key))
        self.uc.mem_write(base, b''.join(items))

    # ---- calling ------------------------------------------------------------
    def call(self, name, ints=(), doubles=(), ret='int'):
        uc = self.uc
        for i, v in enumerate(ints):
            uc.reg_write(UC_ARM64_REG_X0 + i, v & (2**64 - 1))
        for i, v in enumerate(doubles):
            uc.reg_write(UC_ARM64_REG_D0 + i, struct.unpack('<Q', struct.pack('<d', v))[0])
        uc.reg_write(UC_ARM64_REG_SP, STACK_BASE + STACK_SIZE - 0x100)
        uc.reg_write(UC_ARM64_REG_LR, RET_ADDR)
        uc.emu_start(self.elf.sym(name)['value'], RET_ADDR)
        if ret == 'double':
            return self._d(0)
        v = uc.reg_read(UC_ARM64_REG_X0)
        return v if ret == 'ptr' else struct.unpack('<i', struct.pack('<I', v & 0xffffffff))[0]

    def read(self, addr, n):
        return bytes(self.uc.mem_read(addr, n))

    # ---- high level ---------------------------------------------------------
    def generate(self, bands, fs=48000.0, mode=0, debug=0, h=0):
        """bands: list of (freq Hz, gain dB, Q). Returns NVKEY payload bytes."""
        self.call('setParam', ints=(h, len(bands), mode, debug), doubles=(fs,))
        for i, (f, g, q) in enumerate(bands):
            self.call('set_peq_point', ints=(h, i), doubles=(f, g, q))
        rc = self.call('generate_cofe', ints=(h,))
        if rc < 0:
            raise ValueError(f'generate_cofe failed: {rc}')
        # Final word of the table = output rescale, derived from the response peak.
        # Factory presets match: peak <= 8 dB -> peak, peak > 8 dB -> peak - 8.
        peak = self.call('get_freq_resp_max', ints=(h,), ret='double')
        self.call('change_rescale_cofe', ints=(h,), doubles=(peak - 8 if peak > 8 else peak,))
        n = self.call('get_cofe_count', ints=(h,)) & 0xffff   # in 16-bit words
        p = self.call('get_cofe_param', ints=(h,), ret='ptr')
        return self.read(p, 2 * n), rc

    def build_nvkey(self, bands):
        """Full PEQ NVKEY (as stored at 0xF260+N): tables for 44.1 kHz (id 1) and 48 kHz (id 2)."""
        out = struct.pack('<HH', 2, 0)
        for sr_id, fs in ((1, 44100.0), (2, 48000.0)):
            data, _ = self.generate(bands, fs=fs)
            out += struct.pack('<HH', sr_id, len(data) // 2) + data
        return out


if __name__ == '__main__':
    lib = PeqLib(verbose='-v' in sys.argv)
    preset0 = [(2000, 3, 2), (100, 2, 2), (11500, 2, 3), (180, -2, 2), (6000, -6, 2), (8000, -6, 2), (600, -15, 0.4)]
    data, rc = lib.generate(preset0)
    print('rc', rc, 'len', len(data))
    print(data.hex(' '))
