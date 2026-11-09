"""AiroEQ - slider equalizer for Airoha-based earbuds (tested with FlyCC), PC window.

Run:  python airoeq.py
The earbuds must be paired with this PC. FlyCC on the phone must be closed (the window can do it via adb).
"""
import cmath, json, math, os, queue, re, shutil, subprocess, sys, threading, zipfile
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_NAME = 'AiroEQ'
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import race_tool as rt

NBANDS = 10
DEFAULT_FREQS = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
DEFAULT_Q = 1.4
GMIN, GMAX = rt.LIMITS['gain']
FMIN, FMAX = rt.LIMITS['f']
QMIN, QMAX = rt.LIMITS['q']
PLOT_DB = (-16, 13)
FS = 48000.0
FACTORY_FILE = os.path.join(HERE, 'factory_presets.json')


def peak_db(f, f0, gain, q, fs=FS):
    """Magnitude (dB) of an RBJ peaking filter at frequency f."""
    if abs(gain) < 1e-9:
        return 0.0
    A = 10 ** (gain / 40)
    w0 = 2 * math.pi * f0 / fs
    al = math.sin(w0) / (2 * q)
    b = (1 + al * A, -2 * math.cos(w0), 1 - al * A)
    a = (1 + al / A, -2 * math.cos(w0), 1 - al / A)
    z = cmath.exp(-1j * 2 * math.pi * f / fs)
    h = (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    return 20 * math.log10(abs(h))


def fmt_f(f):
    return f'{f / 1000:g}k' if f >= 1000 else f'{f:g}'


class Band:
    def __init__(self, parent, idx, on_change):
        self.on_change = on_change
        fr = ttk.Frame(parent, padding=(4, 2))
        fr.grid(row=0, column=idx, sticky='ns')
        self.gain = tk.DoubleVar(value=0.0)
        self.freq = tk.StringVar(value=str(DEFAULT_FREQS[idx]))
        self.q = tk.StringVar(value=str(DEFAULT_Q))
        self.title = ttk.Label(fr, text=fmt_f(DEFAULT_FREQS[idx]) + ' Hz', width=8, anchor='center',
                               font=('Segoe UI', 9, 'bold'))
        self.title.pack()
        self.val = ttk.Label(fr, text='0.0 dB', width=8, anchor='center')
        self.val.pack()
        self.scale = tk.Scale(fr, from_=GMAX, to=GMIN, resolution=0.5, orient='vertical', length=190,
                              variable=self.gain, showvalue=False, command=lambda _v: self._changed(),
                              width=18, sliderlength=22)
        self.scale.pack(pady=2)
        self.scale.bind('<Double-Button-1>', lambda e: (self.gain.set(0), self._changed()))
        ttk.Label(fr, text='Hz').pack()
        ef = ttk.Entry(fr, textvariable=self.freq, width=7, justify='center')
        ef.pack()
        ttk.Label(fr, text='Q').pack()
        eq = ttk.Entry(fr, textvariable=self.q, width=7, justify='center')
        eq.pack()
        for e in (ef, eq):
            e.bind('<Return>', lambda _e: self._changed())
            e.bind('<FocusOut>', lambda _e: self._changed())
        ef.bind('<MouseWheel>', lambda e: self._wheel_freq(e.delta))
        eq.bind('<MouseWheel>', lambda e: self._wheel_q(e.delta))

    def _wheel_freq(self, delta):
        f = self.get()[0] * (2 ** (1 / 6)) ** (1 if delta > 0 else -1)
        self.freq.set(f'{min(FMAX, max(FMIN, round(f))):g}')
        self._changed()

    def _wheel_q(self, delta):
        q = self.get()[2] * (1.1 if delta > 0 else 1 / 1.1)
        self.q.set(f'{min(QMAX, max(QMIN, round(q, 2))):g}')
        self._changed()

    def get(self):
        try:
            f = min(FMAX, max(FMIN, float(self.freq.get().replace(',', '.'))))
        except ValueError:
            f = 1000.0
        try:
            q = min(QMAX, max(QMIN, float(self.q.get().replace(',', '.'))))
        except ValueError:
            q = DEFAULT_Q
        return f, float(self.gain.get()), q

    def set(self, f, g, q):
        self.freq.set(f'{f:g}')
        self.q.set(f'{q:g}')
        self.gain.set(round(g * 2) / 2)
        self._changed(notify=False)

    def _changed(self, notify=True):
        f, g, q = self.get()
        self.title.config(text=fmt_f(round(f, 1)) + ' Hz')
        self.val.config(text=f'{g:+.1f} dB' if g else '0.0 dB')
        if notify:
            self.on_change()


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.minsize(900, 600)
        self.q = queue.Queue()
        self.lib = None
        self.busy = False
        self.factory = self._load_factory()
        self.cfg = rt.load_config()

        dev = ttk.Frame(root, padding=(8, 8, 8, 0))
        dev.pack(fill='x')
        ttk.Label(dev, text='Earbuds:').pack(side='left')
        self.device = ttk.Combobox(dev, width=40)
        self.device.pack(side='left', padx=(4, 4))
        if self.cfg.get('address'):
            self.device.set(self.cfg.get('name', 'Earbuds') + f' ({self.cfg["address"]})')
        ttk.Button(dev, text='Find paired', command=self.find_devices).pack(side='left')

        top = ttk.Frame(root, padding=8)
        top.pack(fill='x')
        ttk.Label(top, text='Earbuds slot:').pack(side='left')
        self.slot = ttk.Combobox(top, state='readonly', width=22,
                                 values=[f'{i + 1}: {p["name"]}' for i, p in enumerate(self.factory)])
        self.slot.current(4)
        self.slot.pack(side='left', padx=(4, 12))
        self.buttons = []
        for text, cmd in (('Read from earbuds', self.load_device), ('Send to earbuds', self.send),
                          ('Restore factory slot', self.restore)):
            b = ttk.Button(top, text=text, command=cmd)
            b.pack(side='left', padx=3)
            self.buttons.append(b)
        self.adb = tk.BooleanVar(value=shutil.which('adb') is not None)
        ttk.Checkbutton(top, text='Close FlyCC on phone (adb)', variable=self.adb).pack(side='right')

        pre = ttk.Frame(root, padding=(8, 0))
        pre.pack(fill='x')
        ttk.Label(pre, text='Presets:').pack(side='left')
        self.preset_buttons = []
        for i, p in enumerate(self.factory):
            b = ttk.Button(pre, text=p['name'], width=max(9, len(p['name']) + 3),
                           command=lambda i=i: self.activate_slot(i))
            b.pack(side='left', padx=2)
            self.preset_buttons.append(b)
            self.buttons.append(b)
        ttk.Button(pre, text='Flat', command=self.flat).pack(side='left', padx=(10, 2))
        ttk.Button(pre, text='Load profile…', command=self.load_profile).pack(side='right', padx=2)
        ttk.Button(pre, text='Save profile…', command=self.save_profile).pack(side='right', padx=2)

        self.canvas = tk.Canvas(root, height=230, bg='#14171c', highlightthickness=0)
        self.canvas.pack(fill='x', padx=8, pady=8)
        self.canvas.bind('<Configure>', lambda e: self.redraw())

        bands = ttk.Frame(root, padding=(8, 0))
        bands.pack()
        self.bands = [Band(bands, i, self.redraw) for i in range(NBANDS)]

        self.status = tk.StringVar(value='Ready. Bands at 0 dB are not sent. Double-click a slider = 0 dB, '
                                         'mouse wheel over a Hz/Q field changes the value.')
        ttk.Label(root, textvariable=self.status, padding=8, anchor='w').pack(fill='x', side='bottom')
        root.after(100, self._poll)

    # ---- data --------------------------------------------------------------
    def _load_factory(self):
        try:
            return json.load(open(FACTORY_FILE, encoding='utf-8'))
        except OSError:
            return [{'name': f'Slot {i + 1}', 'slot': i, 'bands': []} for i in range(5)]

    def current_bands(self):
        return [b.get() for b in self.bands]

    def active_bands(self):
        act = [(f, g, q) for f, g, q in self.current_bands() if abs(g) >= 0.05]
        return act or [(1000.0, 0.0, 1.0)]

    def apply_bands(self, bands, label=None):
        bands = sorted(tuple(b) for b in bands)[:NBANDS]
        used = {round(f) for f, _, _ in bands}
        free = [f for f in DEFAULT_FREQS if f not in used]
        rows = list(bands) + [(free.pop(0) if free else 1000, 0.0, DEFAULT_Q) for _ in range(NBANDS - len(bands))]
        rows.sort()
        for b, (f, g, q) in zip(self.bands, rows):
            b.set(f, g, q)
        self.redraw()
        if label:
            self.status.set(f'Loaded: {label}. "Send to earbuds" writes it to the selected slot.')

    def flat(self):
        for b, f in zip(self.bands, DEFAULT_FREQS):
            b.set(f, 0.0, DEFAULT_Q)
        self.redraw()

    def save_profile(self):
        path = filedialog.asksaveasfilename(initialdir=HERE, defaultextension='.json',
                                            filetypes=[('EQ profile', '*.json')])
        if path:
            json.dump({'bands': self.current_bands()}, open(path, 'w', encoding='utf-8'), indent=1)
            self.status.set(f'Profile saved: {os.path.basename(path)}')

    def load_profile(self):
        path = filedialog.askopenfilename(initialdir=HERE, filetypes=[('EQ profile', '*.json')])
        if path:
            self.apply_bands([tuple(b) for b in json.load(open(path, encoding='utf-8'))['bands']],
                             os.path.basename(path))

    # ---- plot --------------------------------------------------------------
    def redraw(self):
        c = self.canvas
        c.delete('all')
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 100)
        L, R, T, B = 44, 10, 10, 22
        lo, hi = PLOT_DB
        lx0, lx1 = math.log10(20), math.log10(20000)
        X = lambda f: L + (math.log10(f) - lx0) / (lx1 - lx0) * (w - L - R)
        Y = lambda d: T + (hi - min(hi, max(lo, d))) / (hi - lo) * (h - T - B)
        for d in range(-15, 13, 3):
            c.create_line(L, Y(d), w - R, Y(d), fill='#3a404a' if d == 0 else '#23272e')
            c.create_text(L - 6, Y(d), text=f'{d:+d}' if d else '0', anchor='e', fill='#8a93a0', font=('Segoe UI', 8))
        for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            c.create_line(X(f), T, X(f), h - B, fill='#23272e')
            c.create_text(X(f), h - B + 10, text=fmt_f(f), fill='#8a93a0', font=('Segoe UI', 8))
        bands = self.current_bands()
        pts = []
        for i in range(201):
            f = 10 ** (lx0 + (lx1 - lx0) * i / 200)
            pts += [X(f), Y(sum(peak_db(f, f0, g, q) for f0, g, q in bands))]
        c.create_line(*pts, fill='#4fb3ff', width=2, smooth=True)
        for f0, g, q in bands:
            if abs(g) >= 0.05:
                x, y = X(f0), Y(g)
                c.create_oval(x - 4, y - 4, x + 4, y + 4, fill='#ffb44f', outline='')

    # ---- device operations (background thread) -----------------------------
    def _run(self, title, fn):
        if self.busy:
            return
        self.busy = True
        for b in self.buttons:
            b.state(['disabled'])
        self.status.set(title + '…')

        def work():
            try:
                self.q.put(('ok', fn()))
            except Exception as e:
                self.q.put(('err', e))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == 'log':
                    self.status.set(val); continue
                self.busy = False
                for b in self.buttons:
                    b.state(['!disabled'])
                if kind == 'err':
                    self.status.set(f'Error: {val}')
                    messagebox.showerror(APP_NAME, str(val))
                elif callable(val):
                    val()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _log(self, msg):
        self.q.put(('log', msg))

    @property
    def backup(self):
        return rt.load_factory_backup()

    def find_devices(self):
        def fn():
            devs = rt.paired_devices()
            def done():
                self.device['values'] = [f'{n} ({a})' for n, a in devs]
                if devs and not self.device.get():
                    self.device.current(0)
                self.device.event_generate('<Down>')
                self.status.set(f'{len(devs)} paired device(s) found - pick your earbuds.')
            return done
        self._run('Looking for paired devices', fn)

    def _selected(self):
        txt = self.device.get().strip()
        m = re.search(r'([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})', txt)
        if not m:
            raise RuntimeError('Choose your earbuds first ("Find paired" lists the devices paired with this PC).')
        return (txt[:m.start()].strip(' (') or 'Earbuds'), m.group(1).upper()

    def _connect(self):
        name, addr = self._selected()
        channel = self.cfg.get('channel', rt.DEFAULT_CHANNEL)
        if self.adb.get():
            self._stop_flycc()
        try:
            r = rt.Race(addr, channel)
        except OSError as e:
            raise RuntimeError(f'Cannot connect to {name} ({e}).\n\nMake sure the earbuds are out of the case and '
                               'connected to this PC, and that FlyCC on the phone is closed.')
        rt.save_config(address=addr, name=name, channel=channel)
        return r

    def _ensure_lib(self):
        """The Airoha library is not shipped - extract it once from the FlyCC APK."""
        if os.path.exists(rt.LIB_PATH):
            return True
        messagebox.showinfo(APP_NAME, 'AiroEQ needs the Airoha PEQ library from the FlyCC Android app.\n\n'
                                      'Choose the FlyCC .apk file - it is only read, nothing is installed.')
        apk = filedialog.askopenfilename(title='FlyCC APK', filetypes=[('Android app', '*.apk')])
        if not apk:
            return False
        try:
            rt.extract_lib(apk)
        except (OSError, RuntimeError, zipfile.BadZipFile) as e:
            messagebox.showerror(APP_NAME, f'Could not extract the library: {e}')
            return False
        return True

    def _stop_flycc(self):
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        try:
            out = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=10, creationflags=flags).stdout
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) == 2 and parts[1] == 'device':
                    subprocess.run(['adb', '-s', parts[0], 'shell', 'am', 'force-stop', 'com.airoha.twshelper'],
                                   capture_output=True, timeout=10, creationflags=flags)
        except (OSError, subprocess.SubprocessError):
            pass

    def slot_index(self):
        return self.slot.current()

    def _read_slot(self, r, idx):
        data = r.read_nvkey(rt.PEQ_KEYS[idx])
        if not data:
            raise RuntimeError('The earbuds did not return a curve')
        t = rt.decode_peq(data)[0]
        bands = []
        for bq in t['bands']:
            f, g, q, _ = rt.biquad_to_peak(*bq, t['fs'])
            bands.append((round(f), round(g, 2), round(q, 3)))
        custom = data.hex() != self.backup.get(f'{rt.PEQ_KEYS[idx]:04X}')
        return bands, custom

    def mark_active(self, idx):
        for i, (b, p) in enumerate(zip(self.preset_buttons, self.factory)):
            b.config(text=('▶ ' if i == idx else '') + p['name'])

    def activate_slot(self, idx):
        """Preset button: switch the earbuds to this preset now and show its curve."""
        self.slot.current(idx)

        def fn():
            r = self._connect()
            try:
                if not rt.select_preset(r, idx):
                    raise RuntimeError('The earbuds did not confirm the preset change')
                bands, custom = self._read_slot(r, idx)
            finally:
                r.close()
            name = self.factory[idx]['name']

            def done():
                self.mark_active(idx)
                self.apply_bands(bands)
                self.status.set(f'Earbuds switched to {name}' + (' (your custom curve).' if custom else '.'))
            return done
        self._run(f'Switching to {self.factory[idx]["name"]}', fn)

    def load_device(self):
        idx = self.slot_index()

        def fn():
            r = self._connect()
            try:
                bands, custom = self._read_slot(r, idx)
            finally:
                r.close()
            return lambda: self.apply_bands(bands, f'slot {idx + 1} from earbuds' + (' (custom)' if custom else ''))
        self._run('Reading from earbuds', fn)

    def send(self):
        idx = self.slot_index()
        bands = self.active_bands()
        if not self._ensure_lib():
            return
        if not messagebox.askyesno(APP_NAME, f'Write this curve ({len(bands)} bands) to slot {idx + 1} '
                                               f'on both earbuds?\n\nThe factory curve can be brought back with '
                                               f'"Restore factory slot".'):
            return

        def fn():
            if self.lib is None:
                self._log('Starting the Airoha library…')
                sys.path.insert(0, os.path.join(HERE, 'emu'))
                from peqlib import PeqLib
                self.lib = PeqLib(rt.LIB_PATH)
            self._log('Computing coefficients…')
            data = self.lib.build_nvkey(bands)
            r = self._connect()
            try:
                rt.write_preset_data(r, idx, data, log=self._log)
            finally:
                r.close()
            return lambda: (self.mark_active(idx),
                            self.status.set(f'Written to slot {idx + 1} on both earbuds and preset activated.'))
        self._run('Sending', fn)

    def restore(self):
        idx = self.slot_index()
        key = f'{rt.PEQ_KEYS[idx]:04X}'
        backup = self.backup
        if key not in backup:
            messagebox.showerror(APP_NAME, 'No factory backup yet. It is saved automatically before the first '
                                           '"Send to earbuds", so this slot has never been changed by AiroEQ.')
            return
        if not messagebox.askyesno(APP_NAME, f'Restore the factory curve in slot {idx + 1}?'):
            return
        data = bytes.fromhex(backup[key])

        def fn():
            r = self._connect()
            try:
                rt.write_preset_data(r, idx, data, log=self._log)
            finally:
                r.close()
            return lambda: (self.mark_active(idx), self.apply_bands(self.factory[idx]['bands']),
                            self.status.set(f'Slot {idx + 1} restored to factory.'))
        self._run('Restoring', fn)


def main():
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    themes = ttk.Style().theme_names()
    ttk.Style().theme_use(next((t for t in ('vista', 'aqua') if t in themes), 'clam'))
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
