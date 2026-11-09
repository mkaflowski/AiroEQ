"""Minimal ELF64 (aarch64) reader: sections, dynamic symbols, relocations."""
import struct

class Elf:
    def __init__(self, path):
        self.d = d = open(path, 'rb').read()
        (self.shoff,) = struct.unpack_from('<Q', d, 0x28)
        (self.phoff,) = struct.unpack_from('<Q', d, 0x20)
        self.phentsize, self.phnum, self.shentsize, self.shnum, self.shstrndx = struct.unpack_from('<HHHHH', d, 0x36)
        self.sections = []
        for i in range(self.shnum):
            f = struct.unpack_from('<IIQQQQIIQQ', d, self.shoff + i * self.shentsize)
            self.sections.append(dict(name_off=f[0], type=f[1], addr=f[3], off=f[4], size=f[5], link=f[6], entsize=f[9]))
        strtab = self.sections[self.shstrndx]
        for s in self.sections:
            e = d.index(b'\0', strtab['off'] + s['name_off'])
            s['name'] = d[strtab['off'] + s['name_off']:e].decode()
        self.secmap = {s['name']: s for s in self.sections}
        self.segments = []
        for i in range(self.phnum):
            f = struct.unpack_from('<IIQQQQQQ', d, self.phoff + i * self.phentsize)
            self.segments.append(dict(type=f[0], flags=f[1], off=f[2], vaddr=f[3], filesz=f[5], memsz=f[6]))
        ds, dstr = self.secmap['.dynsym'], self.secmap['.dynstr']
        self.syms = []
        for i in range(ds['size'] // 24):
            nm, info, oth, shn, val, sz = struct.unpack_from('<IBBHQQ', d, ds['off'] + i * 24)
            e = d.index(b'\0', dstr['off'] + nm)
            self.syms.append(dict(name=d[dstr['off'] + nm:e].decode(), value=val, size=sz, shndx=shn, type=info & 0xf))
        self.relocs = []
        for s in self.sections:
            if s['type'] == 4:  # SHT_RELA
                for i in range(s['size'] // 24):
                    off, info, add = struct.unpack_from('<QQq', d, s['off'] + i * 24)
                    self.relocs.append(dict(off=off, type=info & 0xffffffff, sym=info >> 32, addend=add))

    def sym(self, name):
        return next(s for s in self.syms if s['name'] == name)
