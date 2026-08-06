"""
winMon — Windows activity agent for Rajiv Syndicate.

Samples the foreground window (app + title + website + idle) every few seconds,
aggregates contiguous use into segments, and uploads them in batches to the EMS
"Computer Activity" backend. First run shows a small setup dialog to enter the
employee's EMS credentials; the admin approves the PC in the dashboard, after
which the agent runs silently in the background.

Dependencies (see requirements.txt): requests, psutil, pywin32, uiautomation.
Runs on Windows only. Package to a single .exe with PyInstaller (see build.bat).
"""
import os
import sys
import json
import time
import uuid
import socket
import getpass
import threading
import subprocess
import queue as _queue

import requests

APP_VERSION = '2.7'
DEFAULT_SERVER = 'https://ems.rajivsyndicate.com'

# Self-update: the exe is published as a GitHub Release; the agent checks the
# latest release and replaces itself when a newer version is available.
UPDATE_REPO = 'bhagwant4040/win-mon'

# Config lives in %APPDATA%\winMon so it's writable even when the .exe is in
# Program Files. Holds server url, agent_id (persistent), employee_id, token.
_CFG_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'winMon')
CONFIG_PATH = os.path.join(_CFG_DIR, 'config.json')


def load_cfg():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg):
    """Write config atomically so a crash mid-write can't corrupt/erase it."""
    os.makedirs(_CFG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)


def _register(cfg):
    """Silently (re)register this PC with its saved office/name/agent_id — no dialog.
    Used on first setup and whenever the server has lost our record. Returns True on ok."""
    server = (cfg.get('server') or DEFAULT_SERVER).rstrip('/')
    try:
        r = requests.post(server + '/api/win/register', json={
            'agent_id': _agent_id(cfg),
            'office': cfg.get('office', ''), 'name': cfg.get('name', ''),
            'hostname': socket.gethostname(), 'os_user': getpass.getuser(),
            'app_version': APP_VERSION,
        }, timeout=15)
        return r.ok
    except Exception:
        return False


# ── Remote commands (lock / logoff / restart / shutdown) ──────────────────────
def _run_remote_command(cmd):
    """Execute an admin remote command on this PC. Windows only."""
    try:
        flags = 0x08000000 if os.name == 'nt' else 0   # CREATE_NO_WINDOW
        if cmd == 'lock':
            try:
                import ctypes
                ctypes.windll.user32.LockWorkStation()
            except Exception:
                subprocess.Popen(['rundll32.exe', 'user32.dll,LockWorkStation'], creationflags=flags)
        elif cmd == 'logoff':
            subprocess.Popen(['shutdown', '/l'], creationflags=flags)
        elif cmd == 'restart':
            subprocess.Popen(['shutdown', '/r', '/t', '0', '/f'], creationflags=flags)
        elif cmd == 'shutdown':
            subprocess.Popen(['shutdown', '/s', '/t', '0', '/f'], creationflags=flags)
    except Exception:
        pass


# ── Two-way voice (admin ↔ this PC) ───────────────────────────────────────────
_talk = {'session': None}


def _talk_ws_url(server, token):
    base = (server or DEFAULT_SERVER).rstrip('/')
    if base.startswith('https://'):
        base = 'wss://' + base[len('https://'):]
    elif base.startswith('http://'):
        base = 'ws://' + base[len('http://'):]
    return base + '/talkws/talk/agent/' + token


class TalkSession:
    """Live 16kHz mono voice: streams the mic up and plays received audio on the
    speaker, over a WebSocket to the relay. A small on-screen banner shows it's live."""
    RATE = 16000
    BLK = 1600   # 100 ms frames

    def __init__(self, url):
        self.url = url
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.ws = None
        self.in_stream = None
        self.out_stream = None
        self.alive = False
        self._ind = None
        self._warn = ''
        self.in_sr = 16000
        self.out_sr = 16000

    def start(self):
        import sounddevice as sd
        import numpy as np
        import websocket
        self._np = np
        self.alive = True

        def on_message(ws, message):
            if isinstance(message, (bytes, bytearray)):
                with self.lock:
                    self.buf.extend(message)
                    cap = self.RATE * 2 * 3          # keep at most ~3 s backlog
                    if len(self.buf) > cap:
                        del self.buf[:len(self.buf) - self.RATE * 2 * 2]

        def on_close(ws, *a):
            self.alive = False

        self.ws = websocket.WebSocketApp(self.url, on_message=on_message, on_close=on_close)
        threading.Thread(target=lambda: self.ws.run_forever(ping_interval=20), daemon=True).start()

        # Open at each device's NATIVE samplerate (WASAPI shared-mode mix rate) and
        # resample to/from the 16 kHz wire format in software. Forcing 16 kHz on the
        # host API (esp. MME) is what raised PaError -9999 / "MME error" on some PCs.
        self.in_sr, in_dev = self._pick(sd, 'input')
        self.out_sr, out_dev = self._pick(sd, 'output')

        def in_cb(indata, frames, tinfo, status):
            try:
                mono = indata[:, 0] if getattr(indata, 'ndim', 1) > 1 else indata
                ds = self._resample(mono, self.in_sr, self.RATE)
                if self.ws and self.ws.sock and self.ws.sock.connected:
                    self.ws.send(ds.tobytes(), websocket.ABNF.OPCODE_BINARY)
            except Exception:
                pass

        def out_cb(outdata, frames, tinfo, status):
            need16 = int(round(frames * self.RATE / self.out_sr))
            with self.lock:
                take = bytes(self.buf[:need16 * 2]); del self.buf[:len(take)]
            src = self._np.frombuffer(take, dtype='int16')
            up = self._resample(src, self.RATE, self.out_sr)
            if len(up) < frames:
                up = self._np.concatenate([up, self._np.zeros(frames - len(up), dtype='int16')])
            outdata[:, 0] = up[:frames]

        errs = []
        # Speaker first (admin → PC), so the admin can still talk even if the mic is blocked.
        try:
            self.out_stream = sd.OutputStream(samplerate=self.out_sr, channels=1, dtype='int16',
                                              blocksize=max(256, int(self.out_sr * 0.1)),
                                              device=out_dev, callback=out_cb)
            self.out_stream.start()
        except Exception as ex:
            self.out_stream = None
            errs.append('speaker: ' + self._friendly(ex))
        # Mic (PC → admin)
        try:
            self.in_stream = sd.InputStream(samplerate=self.in_sr, channels=1, dtype='int16',
                                            blocksize=max(256, int(self.in_sr * 0.1)),
                                            device=in_dev, callback=in_cb)
            self.in_stream.start()
        except Exception as ex:
            self.in_stream = None
            errs.append('mic: ' + self._friendly(ex))

        if not self.in_stream and not self.out_stream:
            self.alive = False
            raise RuntimeError('; '.join(errs) or 'no audio devices')
        self._warn = '; '.join(errs)          # non-fatal: one direction still works
        threading.Thread(target=self._indicator, daemon=True).start()

    # ── device selection + resampling (keeps the 16 kHz wire format host-agnostic) ──
    def _pick(self, sd, kind):
        """Return (samplerate, device_index) for 'input'/'output', preferring WASAPI."""
        ch_key = 'max_input_channels' if kind == 'input' else 'max_output_channels'
        cands = []
        try:
            for a in sd.query_hostapis():
                if 'wasapi' in a.get('name', '').lower():
                    dd = a.get('default_' + kind + '_device', -1)
                    if isinstance(dd, int) and dd >= 0:
                        cands.append(dd)
        except Exception:
            pass
        try:
            gd = sd.default.device[0 if kind == 'input' else 1]
            if isinstance(gd, int) and gd >= 0:
                cands.append(gd)
        except Exception:
            pass
        try:
            for i, d in enumerate(sd.query_devices()):
                if d.get(ch_key, 0) > 0:
                    cands.append(i)
        except Exception:
            pass
        for idx in cands:
            try:
                d = sd.query_devices(idx)
                if d.get(ch_key, 0) > 0:
                    sr = int(d.get('default_samplerate') or 48000)
                    return (sr if sr > 0 else 48000), idx
            except Exception:
                continue
        return 48000, None                     # let PortAudio use its default device

    def _resample(self, data, sr_from, sr_to):
        np = self._np
        if data is None or len(data) == 0:
            return np.zeros(0, dtype='int16')
        if sr_from == sr_to:
            return data.astype('int16')
        n_out = int(round(len(data) * sr_to / sr_from))
        if n_out <= 0:
            return np.zeros(0, dtype='int16')
        xp = np.arange(len(data))
        x = np.linspace(0, len(data) - 1, n_out)
        return np.interp(x, xp, data.astype(np.float32)).astype('int16')

    @staticmethod
    def _friendly(ex):
        s = str(ex); low = s.lower()
        if ('-9999' in s or 'host error' in low or 'mme' in low
                or 'unanticipated' in low or 'invalid device' in low):
            return ('device blocked or in use — allow desktop apps in Windows '
                    'Settings ▸ Privacy & security ▸ Microphone, or close the app using it')
        return s

    def _indicator(self):
        try:
            import tkinter as tk
            r = tk.Tk(); r.overrideredirect(True); r.attributes('-topmost', True)
            try: r.attributes('-alpha', 0.92)
            except Exception: pass
            r.configure(bg='#dc2626')
            tk.Label(r, text='\U0001F3A4  Live audio with admin', bg='#dc2626', fg='white',
                     font=('Segoe UI', 10, 'bold'), padx=14, pady=6).pack()
            r.update_idletasks()
            r.geometry('+%d+%d' % (r.winfo_screenwidth() - r.winfo_width() - 20, 20))
            self._ind = r

            def poll():
                if not self.alive:
                    try: r.destroy()
                    except Exception: pass
                    return
                r.after(400, poll)
            poll(); r.mainloop()
        except Exception:
            pass

    def stop(self):
        self.alive = False
        for s in (self.in_stream, self.out_stream):
            try: s.stop(); s.close()
            except Exception: pass
        try: self.ws.close()
        except Exception: pass
        try:
            if self._ind: self._ind.after(0, self._ind.destroy)
        except Exception: pass


# ── Admin chat + notices (on-screen, Windows) ─────────────────────────────────
class ChatUI:
    """PC-side windows for admin chat and notices. Runs one Tk root in its own
    thread; the heartbeat thread feeds messages/notices via a thread-safe queue.

    Chat: a window that stays on top until the user replies. Notices: a window
    per notice that can't be closed until the user clicks 'I have read this'."""
    def __init__(self, server, token):
        self.server = server
        self.token = token
        self.q = _queue.Queue()
        self.root = None
        self._tk = None
        self.txt = None
        self.entry = None
        self._chat_ids = set()      # admin message ids already shown
        self._pending = 0           # unreplied admin messages → keep chat on top
        self._notice_wins = {}      # notice_id -> Toplevel
        self._shown_notices = set()
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._ui_thread, daemon=True).start()

    def feed(self, chat, notices):
        """Called from the heartbeat thread — never touches widgets directly."""
        self.q.put((chat or [], notices or []))

    def _headers(self):
        return {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'}

    def _ui_thread(self):
        try:
            import tkinter as tk
            self._tk = tk
            self.root = tk.Tk()
            self.root.withdraw()          # hidden until there's something to show
            self._build_chat()
            self.root.after(300, self._drain)
            self.root.mainloop()
        except Exception:
            pass

    def _build_chat(self):
        tk = self._tk
        w = self.root
        w.title('Message from Admin')
        w.configure(bg='#0f172a')
        w.geometry('380x480')
        w.minsize(340, 400)
        tk.Label(w, text='  Admin Chat', bg='#0088FF', fg='white', anchor='w',
                 font=('Segoe UI', 11, 'bold'), pady=6).pack(side='top', fill='x')
        # IMPORTANT: pin the reply bar to the BOTTOM *first* so pack can never clip
        # it off-screen when the window is short; the conversation fills the rest.
        bar = tk.Frame(w, bg='#0f172a'); bar.pack(side='bottom', fill='x')
        tk.Label(bar, text='Your reply:', bg='#0f172a', fg='#cbd5e1',
                 font=('Segoe UI', 9)).pack(side='top', anchor='w', padx=8, pady=(6, 0))
        row = tk.Frame(bar, bg='#0f172a'); row.pack(side='top', fill='x')
        self.entry = tk.Entry(row, font=('Segoe UI', 11))
        self.entry.pack(side='left', fill='x', expand=True, padx=(8, 4), pady=8, ipady=4)
        self.entry.bind('<Return>', lambda e: self._send())
        tk.Button(row, text='Send', command=self._send, bg='#0088FF', fg='white', bd=0,
                  activebackground='#0069cc', font=('Segoe UI', 10, 'bold'),
                  padx=16, pady=4).pack(side='right', padx=(4, 8), pady=8)
        self.txt = tk.Text(w, wrap='word', state='disabled', bg='#f6f8fb', fg='#111',
                           font=('Segoe UI', 10), bd=0, padx=10, pady=10)
        self.txt.tag_configure('admin_h', foreground='#0088FF', font=('Segoe UI', 10, 'bold'))
        self.txt.tag_configure('me_h', foreground='#16a34a', font=('Segoe UI', 10, 'bold'))
        self.txt.pack(side='top', fill='both', expand=True)
        w.protocol('WM_DELETE_WINDOW', self._hide_chat)

    def _hide_chat(self):
        if self._pending > 0:
            self._raise_chat()            # must reply first — force it back
        else:
            try: self.root.withdraw()
            except Exception: pass

    def _raise_chat(self):
        try:
            self.root.deiconify(); self.root.lift()
            self.root.attributes('-topmost', True)
            self.entry.focus_force()
        except Exception:
            pass

    def _append(self, who, body):
        try:
            self.txt.configure(state='normal')
            self.txt.insert('end', 'Admin: ' if who == 'admin' else 'You: ',
                            ('admin_h',) if who == 'admin' else ('me_h',))
            self.txt.insert('end', (body or '') + '\n\n')
            self.txt.configure(state='disabled'); self.txt.see('end')
        except Exception:
            pass

    def _send(self):
        try:
            body = self.entry.get().strip()
        except Exception:
            return
        if not body:
            return
        self.entry.delete(0, 'end')
        self._append('me', body)
        self._pending = 0                 # replying releases the on-top hold
        threading.Thread(target=self._post_reply, args=(body,), daemon=True).start()

    def _post_reply(self, body):
        try:
            requests.post(self.server + '/api/win/chat', headers=self._headers(),
                          json={'body': body}, timeout=10)
        except Exception:
            pass

    def _drain(self):
        try:
            while True:
                chat, notices = self.q.get_nowait()
                self._on_chat(chat)
                self._on_notices(notices)
        except _queue.Empty:
            pass
        except Exception:
            pass
        if self._pending > 0:
            self._raise_chat()
        try:
            self.root.after(1000, self._drain)
        except Exception:
            pass

    def _on_chat(self, chat):
        new = [m for m in chat if m.get('id') not in self._chat_ids]
        for m in new:
            self._chat_ids.add(m.get('id'))
            self._append('admin', m.get('body', ''))
        self._pending = len(chat)         # server keeps returning until user replies
        if new:
            self._raise_chat()

    def _on_notices(self, notices):
        for n in notices:
            nid = n.get('id')
            if nid in self._shown_notices:
                continue
            self._shown_notices.add(nid)
            self._show_notice(n)

    def _show_notice(self, n):
        tk = self._tk
        try:
            top = tk.Toplevel(self.root)
        except Exception:
            return
        nid = n.get('id')
        top.title('Notice')
        top.configure(bg='#ffffff'); top.geometry('440x340'); top.minsize(360, 260)
        top.attributes('-topmost', True)
        tk.Label(top, text=n.get('title', 'Notice'), bg='#dc2626', fg='white', anchor='w',
                 font=('Segoe UI', 12, 'bold'), padx=14, pady=9).pack(side='top', fill='x')

        def ack():
            threading.Thread(target=self._post_ack, args=(nid,), daemon=True).start()
            self._notice_wins.pop(nid, None)
            try: top.destroy()
            except Exception: pass

        # pin the acknowledge button to the bottom FIRST so it's never clipped
        tk.Button(top, text='I have read this', command=ack, bg='#12b34a', fg='white', bd=0,
                  activebackground='#0f9a40', font=('Segoe UI', 11, 'bold'), pady=9).pack(side='bottom', fill='x')
        body = tk.Text(top, wrap='word', font=('Segoe UI', 11), bd=0, padx=14, pady=14)
        body.insert('1.0', n.get('body', '')); body.configure(state='disabled')
        body.pack(side='top', fill='both', expand=True)
        # can't dismiss without acknowledging
        top.protocol('WM_DELETE_WINDOW', lambda: (top.lift(), top.attributes('-topmost', True)))
        self._notice_wins[nid] = top

    def _post_ack(self, nid):
        try:
            requests.post(self.server + '/api/win/notice-ack', headers=self._headers(),
                          json={'notice_id': nid}, timeout=10)
        except Exception:
            pass


# ── Self-update (silent, from GitHub Releases) ────────────────────────────────
def _ver_tuple(v):
    out = []
    for p in str(v).lstrip('vV').split('.'):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


def _check_update():
    """Return (version, download_url) if a newer release exists, else None."""
    try:
        r = requests.get('https://api.github.com/repos/%s/releases/latest' % UPDATE_REPO,
                         headers={'Accept': 'application/vnd.github+json'}, timeout=15)
        if not r.ok:
            return None
        d = r.json()
        tag = (d.get('tag_name') or '').lstrip('vV')
        if not tag or _ver_tuple(tag) <= _ver_tuple(APP_VERSION):
            return None
        url = None
        for a in (d.get('assets') or []):
            if (a.get('name') or '').lower().endswith('.exe'):
                url = a.get('browser_download_url'); break
        if not url:
            url = 'https://github.com/%s/releases/latest/download/winMon.exe' % UPDATE_REPO
        return (tag, url)
    except Exception:
        return None


def _apply_update(url):
    """Download and install the new exe, then relaunch it. Windows only.
    Uses the rename trick: a running .exe can be RENAMED (not overwritten) while
    running, so we move ourselves aside and drop the new exe into place — no batch
    file, no console window, no waiting/retry loop.

    A corrupt/truncated download here is catastrophic: once we exit, nothing is
    left running to self-heal (no OS-level autostart is registered), and the
    machine goes dark with no remote recovery path. So this verifies BOTH the
    exact byte count (against Content-Length) AND the PE header before ever
    swapping the file in, and rolls back if the relaunched process dies immediately."""
    if not getattr(sys, 'frozen', False):
        return False  # only update the packaged .exe, not a python/dev run
    exe = sys.executable
    newexe = exe + '.new'
    oldexe = exe + '.old'
    # 1) download the new exe alongside the current one
    try:
        with requests.get(url, stream=True, timeout=180) as r:
            r.raise_for_status()
            expected = r.headers.get('Content-Length')
            expected = int(expected) if expected and expected.isdigit() else None
            got = 0
            with open(newexe, 'wb') as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk); got += len(chunk)
        # Reject anything that isn't a complete, valid Windows executable —
        # partial/corrupt downloads must never be installed.
        if expected is not None and got != expected:
            os.remove(newexe); return False
        if got < 40_000_000:            # sanity floor (current exe is ~83 MB)
            os.remove(newexe); return False
        with open(newexe, 'rb') as f:
            if f.read(2) != b'MZ':      # valid PE/DOS header
                os.remove(newexe); return False
    except Exception:
        try: os.remove(newexe)
        except OSError: pass
        return False
    # 2) swap by renaming (allowed while the exe is running)
    try:
        if os.path.exists(oldexe):
            try: os.remove(oldexe)
            except OSError: pass
        os.rename(exe, oldexe)     # move the running exe aside
        os.rename(newexe, exe)     # install the new exe under the real name
    except OSError:
        try:                       # roll back if we moved ourselves but couldn't install
            if not os.path.exists(exe) and os.path.exists(oldexe):
                os.rename(oldexe, exe)
        except OSError: pass
        try:
            if os.path.exists(newexe): os.remove(newexe)
        except OSError: pass
        return False
    # 3) launch the freshly installed exe (detached, windowless).
    #    Strip PyInstaller's _MEIPASS2 / _PYI_* env vars so the child does a FRESH
    #    onefile extraction instead of reusing our about-to-be-deleted temp dir —
    #    otherwise the child crashes with "No module named '_socket'".
    try:
        child_env = {k: v for k, v in os.environ.items()
                     if not (k.startswith('_MEI') or k.startswith('_PYI'))}
        proc = subprocess.Popen([exe, '--relaunch'], creationflags=0x00000008,  # DETACHED
                                close_fds=True, env=child_env)
    except Exception:
        proc = None
    # 4) give the new process a moment to get past its earliest crash window
    # (e.g. a corrupted exe that IS a valid PE header but fails on load) before
    # we commit to exiting. If it already died, roll back to the known-good exe
    # instead of leaving nothing running.
    if proc is not None:
        time.sleep(2.5)
        if proc.poll() is not None:     # already exited — launch failed
            try:
                os.remove(exe)
                os.rename(oldexe, exe)
            except OSError:
                pass
            return False
    os._exit(0)


def _cleanup_stale_update():
    """Remove leftover .new / .old files from a previous update (Windows)."""
    if not getattr(sys, 'frozen', False):
        return
    for suffix in ('.new', '.old'):
        try:
            p = sys.executable + suffix
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


# ── Windows foreground / idle / browser-url helpers ───────────────────────────
try:
    import win32gui
    import win32process
    import win32api
    import psutil
    _WIN = True
except Exception:
    _WIN = False

try:
    import uiautomation as _uia
    _UIA = True
except Exception:
    _UIA = False


def get_foreground():
    """Returns (app_process_name, window_title, pid) for the focused window."""
    if not _WIN:
        return ('', '', 0)
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return ('', '', 0)
        title = win32gui.GetWindowText(hwnd) or ''
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        app = ''
        try:
            app = psutil.Process(pid).name()
        except Exception:
            app = ''
        return (app, title, pid)
    except Exception:
        return ('', '', 0)


def kill_pid(pid):
    try:
        psutil.Process(pid).terminate()
        return True
    except Exception:
        return False


def _domain(u):
    return (u or '').replace('https://', '').replace('http://', '').split('/')[0].lower()


# Managed block in the Windows hosts file (needs admin). Blocked domains resolve
# to 0.0.0.0 so no browser can reach them.
_HOSTS = r'C:\Windows\System32\drivers\etc\hosts'
_HOSTS_BEGIN = '# winMon-block-begin'
_HOSTS_END = '# winMon-block-end'


def apply_hosts_block(sites):
    try:
        with open(_HOSTS, 'r', encoding='utf-8', errors='ignore') as f:
            txt = f.read()
        # strip any previous managed block
        if _HOSTS_BEGIN in txt and _HOSTS_END in txt:
            pre = txt.split(_HOSTS_BEGIN)[0]
            post = txt.split(_HOSTS_END, 1)[1]
            txt = pre.rstrip() + '\n' + post.lstrip()
        block = ''
        if sites:
            lines = [_HOSTS_BEGIN]
            for s in sites:
                d = _domain(s)
                if d:
                    lines.append('0.0.0.0 ' + d)
                    lines.append('0.0.0.0 www.' + d)
            lines.append(_HOSTS_END)
            block = '\n' + '\n'.join(lines) + '\n'
        with open(_HOSTS, 'w', encoding='utf-8') as f:
            f.write(txt.rstrip() + '\n' + block)
        return True
    except Exception:
        return False   # not admin / not writable — rely on detection+log only


def collect_health():
    """A system status/health snapshot via psutil."""
    if not _WIN:
        return {}
    import platform
    try:
        vm = psutil.virtual_memory()
        # system drive (usually C:)
        sysdrive = os.environ.get('SystemDrive', 'C:') + '\\'
        du = psutil.disk_usage(sysdrive)
        try:
            bat = psutil.sensors_battery()
        except Exception:
            bat = None
        net = psutil.net_io_counters()
        snap = {
            'cpu_pct': psutil.cpu_percent(interval=None),
            'mem_pct': vm.percent, 'mem_total_mb': int(vm.total / 1048576),
            'disk_pct': du.percent, 'disk_free_gb': round(du.free / 1073741824, 1),
            'uptime_s': int(time.time() - psutil.boot_time()),
            'battery_pct': (int(bat.percent) if bat else None),
            'battery_plugged': (bool(bat.power_plugged) if bat else None),
            'os_name': platform.platform(),
            'num_procs': len(psutil.pids()),
            'net_sent_mb': round(net.bytes_sent / 1048576, 1),
            'net_recv_mb': round(net.bytes_recv / 1048576, 1),
        }
        snap.update(_device_caps())
        return snap
    except Exception:
        return {}


_caps_cache = None


def _device_caps():
    """Detect presence of microphone / speaker / camera (cached). Camera check
    uses Windows PnP so the webcam is never opened (no LED)."""
    global _caps_cache
    if _caps_cache is not None:
        return _caps_cache
    caps = {'has_mic': None, 'has_speaker': None, 'has_camera': None}
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        caps['has_mic'] = bool(any(d.get('max_input_channels', 0) > 0 for d in devs))
        caps['has_speaker'] = bool(any(d.get('max_output_channels', 0) > 0 for d in devs))
    except Exception:
        pass
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "@(Get-CimInstance Win32_PnPEntity -Filter \"PNPClass='Camera' OR PNPClass='Image'\").Count"],
            capture_output=True, text=True, timeout=15,
            creationflags=(0x08000000 if os.name == 'nt' else 0))
        n = (out.stdout or '').strip()
        caps['has_camera'] = bool(n.isdigit() and int(n) > 0)
    except Exception:
        pass
    _caps_cache = caps
    return caps


def get_idle_seconds():
    """Seconds since the last keyboard/mouse input."""
    if not _WIN:
        return 0
    try:
        millis = win32api.GetTickCount() - win32api.GetLastInputInfo()
        return max(0, millis // 1000)
    except Exception:
        return 0


_BROWSERS = ('chrome', 'msedge', 'brave', 'firefox', 'opera', 'vivaldi')


def get_browser_url(app):
    """Best-effort: read the active browser's address bar via UI Automation.
    Returns a domain-ish string or ''. Fragile across browser versions — the
    dashboard treats a missing URL gracefully."""
    if not (_UIA and app):
        return ''
    a = app.lower()
    if not any(b in a for b in _BROWSERS):
        return ''
    try:
        hwnd = win32gui.GetForegroundWindow()
        ctrl = _uia.ControlFromHandle(hwnd)
        if not ctrl:
            return ''
        # Chromium: an Edit named "Address and search bar". Firefox: "Search with…".
        edit = ctrl.EditControl(searchDepth=25)
        if edit and edit.Exists(0.2, 0):
            val = ''
            try:
                val = edit.GetValuePattern().Value or ''
            except Exception:
                val = edit.Name or ''
            val = (val or '').strip()
            if val and ' ' not in val and '.' in val:
                return val[:255]
    except Exception:
        return ''
    return ''


# ── activity intensity (keystroke + mouse-click COUNTS only, never content) ────
_key_count = 0
_click_count = 0
_intensity_on = False


def start_intensity():
    global _intensity_on
    if _intensity_on:
        return
    try:
        from pynput import keyboard, mouse

        def on_press(_k):
            global _key_count
            _key_count += 1

        def on_click(_x, _y, _button, pressed):
            global _click_count
            if pressed:
                _click_count += 1

        keyboard.Listener(on_press=on_press).start()
        mouse.Listener(on_click=on_click).start()
        _intensity_on = True
    except Exception:
        pass


_shot_err = ''


def capture_jpeg(max_w=1366, quality=55):
    """Grab the screen → downscaled JPEG in a BytesIO buffer. On failure records
    the reason in _shot_err and returns None."""
    global _shot_err
    try:
        from PIL import ImageGrab
        import io
        img = ImageGrab.grab(all_screens=True)
        if img is None:
            _shot_err = 'ImageGrab.grab() returned None'; return None
        if img.width > max_w:
            r = max_w / float(img.width)
            img = img.resize((max_w, int(img.height * r)))
        buf = io.BytesIO()
        img.convert('RGB').save(buf, 'JPEG', quality=quality)
        buf.seek(0)
        _shot_err = ''
        return buf
    except Exception as ex:
        _shot_err = '%s: %s' % (type(ex).__name__, ex)
        return None


def list_removable_drives():
    out = set()
    try:
        import win32file
        import string
        for letter in string.ascii_uppercase:
            root = letter + ':\\'
            try:
                if win32file.GetDriveType(root) == win32file.DRIVE_REMOVABLE:
                    out.add(root)
            except Exception:
                pass
    except Exception:
        pass
    return out


def list_installed_software():
    names = set()
    try:
        import winreg
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'),
            (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
        ]
        for hive, path in roots:
            try:
                key = winreg.OpenKey(hive, path)
            except Exception:
                continue
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i); i += 1
                except OSError:
                    break
                try:
                    sk = winreg.OpenKey(key, sub)
                    dn, _ = winreg.QueryValueEx(sk, 'DisplayName')
                    if dn:
                        names.add(str(dn))
                except Exception:
                    pass
    except Exception:
        pass
    return names


def list_print_jobs():
    jobs, details = set(), {}
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        for p in win32print.EnumPrinters(flags):
            name = p[2]
            try:
                h = win32print.OpenPrinter(name)
                for j in win32print.EnumJobs(h, 0, 999, 1):
                    jid = (name, j.get('JobId'))
                    jobs.add(jid)
                    details[jid] = (j.get('pDocument') or 'document') + ' -> ' + name
                win32print.ClosePrinter(h)
            except Exception:
                pass
    except Exception:
        pass
    return jobs, details


# Never terminate these under an app allow-list (would brick Windows / the agent).
_SAFE_APPS = {
    'explorer.exe', 'winlogon.exe', 'csrss.exe', 'services.exe', 'svchost.exe',
    'lsass.exe', 'wininit.exe', 'dwm.exe', 'taskhostw.exe', 'sihost.exe',
    'ctfmon.exe', 'searchhost.exe', 'startmenuexperiencehost.exe',
    'shellexperiencehost.exe', 'runtimebroker.exe', 'fontdrvhost.exe',
    'winmon.exe', 'python.exe', 'pythonw.exe', 'systemsettings.exe', 'applicationframehost.exe',
}


# ── enrollment (first-run setup dialog) ───────────────────────────────────────
def _agent_id(cfg):
    if not cfg.get('agent_id'):
        cfg['agent_id'] = 'win-' + str(uuid.uuid4())
        save_cfg(cfg)
    return cfg['agent_id']


def _fetch_offices(server):
    try:
        r = requests.get(server + '/api/win/offices', timeout=10)
        if r.ok:
            return r.json().get('offices') or []
    except Exception:
        pass
    return []


def setup_dialog(cfg):
    """First-run: pick an Office/location + type a Name, then register this PC.
    The unique ID is shown so the admin can identify it. Returns True on submit."""
    import tkinter as tk
    from tkinter import ttk, messagebox

    server = (cfg.get('server') or DEFAULT_SERVER).rstrip('/')
    agent_id = _agent_id(cfg)
    offices = _fetch_offices(server)
    result = {'ok': False}

    root = tk.Tk()
    root.title('winMon Setup — Rajiv Syndicate')
    root.geometry('440x440'); root.resizable(False, False)

    tk.Label(root, text='Computer Monitoring — Setup', font=('Segoe UI', 13, 'bold')).pack(pady=(16, 2))
    tk.Label(root, text='This PC will be registered for monitoring.', fg='#666').pack()

    frm = tk.Frame(root); frm.pack(pady=12, padx=22, fill='x')
    tk.Label(frm, text='Server').grid(row=0, column=0, sticky='w', pady=5)
    e_srv = tk.Entry(frm, width=30); e_srv.grid(row=0, column=1, pady=5); e_srv.insert(0, server)
    tk.Label(frm, text='Office / Location').grid(row=1, column=0, sticky='w', pady=5)
    off_var = tk.StringVar()
    # Choose from the office list set in EMS. Only allow free typing if the
    # server returned no offices (so setup never gets blocked).
    cb = ttk.Combobox(frm, width=27, textvariable=off_var, values=offices,
                      state=('readonly' if offices else 'normal'))
    cb.grid(row=1, column=1, pady=5)
    if offices:
        cb.set('— choose office —')
    tk.Label(frm, text='Name (this PC)').grid(row=2, column=0, sticky='w', pady=5)
    e_name = tk.Entry(frm, width=30); e_name.grid(row=2, column=1, pady=5)

    tk.Label(root, text='Unique ID: ' + agent_id, font=('Consolas', 9), fg='#2563eb').pack()

    # Consent — must be ticked to register (recorded on the server with a timestamp).
    consent_var = tk.BooleanVar(value=False)
    cframe = tk.Frame(root, bd=1, relief='solid'); cframe.pack(fill='x', padx=22, pady=(10, 2))
    tk.Checkbutton(cframe, variable=consent_var, justify='left', anchor='w', wraplength=380,
                   font=('Segoe UI', 9), padx=6, pady=6,
                   text=('I consent to monitoring of this company PC — screen, activity, and '
                         '(when enabled by the administrator) camera and microphone.')).pack(anchor='w')

    status = tk.Label(root, text='', fg='#c00', wraplength=390); status.pack()

    def submit():
        srv = e_srv.get().strip().rstrip('/') or DEFAULT_SERVER
        office = off_var.get().strip()
        name = e_name.get().strip()
        if office.startswith('—'):
            office = ''
        if not office:
            status.config(text='Choose the office/location for this PC.'); return
        if not name:
            status.config(text='Enter a name for this PC.'); return
        if not consent_var.get():
            status.config(text='Please tick the consent box to continue.'); return
        try:
            r = requests.post(srv + '/api/win/register', json={
                'agent_id': agent_id, 'office': office, 'name': name,
                'hostname': socket.gethostname(), 'os_user': getpass.getuser(),
                'app_version': APP_VERSION, 'consent': True,
            }, timeout=15)
            if not r.ok:
                status.config(text='Registration failed (%s).' % r.status_code); return
            cfg['server'] = srv; cfg['office'] = office; cfg['name'] = name
            save_cfg(cfg)
            result['ok'] = True
            messagebox.showinfo('winMon',
                'Registered!\n\nUnique ID: %s\n\nAsk Head Office to approve this PC in EMS.\n'
                'Monitoring starts automatically once approved.' % agent_id)
            root.destroy()
        except Exception as ex:
            status.config(text='Network error: %s' % ex)

    tk.Button(root, text='Register this PC', command=submit, width=20,
              bg='#2563eb', fg='white', font=('Segoe UI', 10, 'bold')).pack(pady=12)
    root.mainloop()
    return result['ok']


def show_status_dialog(cfg):
    """Shown when the user re-opens the app: displays the unique ID + status so
    they can read it out to the admin, and whether it's approved / has a problem."""
    import tkinter as tk
    server = (cfg.get('server') or DEFAULT_SERVER).rstrip('/')
    agent_id = _agent_id(cfg)
    status_txt, color = 'Checking…', '#666'
    try:
        r = requests.get(server + '/api/win/session', params={'agent_id': agent_id}, timeout=10)
        st = (r.json() or {}).get('status') if r.ok else None
        status_txt, color = {
            'active': ('Approved — monitoring is running', '#16a34a'),
            'pending': ('Waiting for Head Office approval', '#d97706'),
            'denied': ('Denied by Head Office', '#dc2626'),
            'revoked': ('Revoked by Head Office', '#dc2626'),
            'none': ('Not registered', '#dc2626'),
        }.get(st, (str(st), '#666'))
    except Exception:
        status_txt, color = 'Cannot reach server — check network', '#dc2626'

    root = tk.Tk(); root.title('winMon — This PC'); root.geometry('440x300'); root.resizable(False, False)
    tk.Label(root, text='Computer Monitoring', font=('Segoe UI', 13, 'bold')).pack(pady=(16, 2))
    tk.Label(root, text=(cfg.get('name') or '') + ('  ·  ' + cfg.get('office') if cfg.get('office') else ''),
             fg='#333', font=('Segoe UI', 10)).pack()
    tk.Label(root, text='Unique ID', fg='#666', font=('Segoe UI', 9)).pack(pady=(12, 0))
    idbox = tk.Entry(root, width=42, font=('Consolas', 10), justify='center', bd=1, relief='solid')
    idbox.pack(pady=2); idbox.insert(0, agent_id); idbox.config(state='readonly')

    def copyid():
        root.clipboard_clear(); root.clipboard_append(agent_id)
    tk.Button(root, text='Copy ID', command=copyid, width=12).pack(pady=6)
    tk.Label(root, text=status_txt, fg=color, font=('Segoe UI', 10, 'bold'), wraplength=400).pack(pady=6)

    # Current version + live update status
    ver_lbl = tk.Label(root, text='Version %s · checking for updates…' % APP_VERSION,
                       fg='#666', font=('Segoe UI', 9))
    ver_lbl.pack(pady=(2, 0))

    def _upd_check():
        upd = _check_update()
        txt = ('Version %s · update available (v%s) — installs automatically'
               % (APP_VERSION, upd[0])) if upd else ('Version %s · up to date' % APP_VERSION)
        try:
            root.after(0, lambda: ver_lbl.config(text=txt))
        except Exception:
            pass
    threading.Thread(target=_upd_check, daemon=True).start()

    tk.Button(root, text='Close', command=root.destroy, width=12).pack(pady=8)
    root.mainloop()


# ── main tracking loop ─────────────────────────────────────────────────────────
class Tracker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.server = cfg.get('server') or DEFAULT_SERVER
        self.agent_id = _agent_id(cfg)
        self.token = cfg.get('token')
        self.sample_int = 5
        self.upload_int = 60
        self.idle_after = 120
        self.health_int = 300
        self.queue = []
        self.cur = None            # {app,title,url,idle,start}
        self.lock = threading.Lock()
        self.blocked_apps = []
        self.blocked_sites = []
        self.allowed_apps = []
        self.allowed_sites = []
        self.allow_only_apps = False
        self.allow_only_sites = False
        self.viol_queue = []
        self.event_queue = []
        self.chat_ui = None        # ChatUI (admin chat + notices), created lazily
        self._hosts_sig = None     # last applied hosts set (avoid rewriting each cycle)
        self.screenshots_enabled = False
        self.shot_int = 300
        self.intensity_enabled = True
        self._drives = None        # detector baselines
        self._installed = None
        self._pjobs = None
        self._live_until = 0       # stream live frames while > time.time()
        self._cam_until = 0        # stream the webcam (not screen) while > time.time()
        self._cam = None           # open cv2.VideoCapture during camera mode

    def _headers(self):
        return {'Authorization': 'Bearer ' + self.token} if self.token else {}

    def fetch_config(self):
        try:
            r = requests.get(self.server + '/api/win/config', timeout=10)
            if r.ok:
                d = r.json()
                self.sample_int = int(d.get('sample_interval_s', 5))
                self.upload_int = int(d.get('upload_interval_s', 60))
                self.idle_after = int(d.get('idle_after_s', 120))
                self.health_int = int(d.get('health_interval_s', 300))
                self.screenshots_enabled = bool(d.get('screenshots_enabled'))
                self.shot_int = int(d.get('screenshot_interval_s', 300))
                self.intensity_enabled = bool(d.get('intensity_enabled', True))
                if self.intensity_enabled:
                    start_intensity()
        except Exception:
            pass

    def send_health(self):
        snap = collect_health()
        if not snap:
            return
        try:
            requests.post(self.server + '/api/win/health', headers=self._headers(),
                          json=snap, timeout=15)
        except Exception:
            pass

    def report_error(self, msg):
        """Push an agent problem to EMS so it shows on the dashboard."""
        try:
            requests.post(self.server + '/api/win/error', headers=self._headers(),
                          json={'error': str(msg)[:500]}, timeout=10)
        except Exception:
            pass

    def maybe_update(self):
        """Check for a newer released exe and self-update silently if found."""
        upd = _check_update()
        if upd:
            _apply_update(upd[1])   # exits & relaunches the new version if it succeeds

    def check_pending(self):
        """Heartbeat + honour admin requests: on-demand screenshot and remote
        commands (lock / logoff / restart / shutdown)."""
        try:
            r = requests.post(self.server + '/api/win/heartbeat',
                              headers=self._headers(),
                              json={'app_version': APP_VERSION}, timeout=10)
            if not r.ok:
                return
            d = r.json() or {}
            if d.get('shot_now'):
                self.capture_screenshot()   # captures & uploads regardless of the periodic toggle
            if d.get('live'):
                self._live_until = time.time() + 20   # keep streaming until re-confirmed
            self._cam_until = (time.time() + 20) if d.get('cam') else 0
            if not d.get('cam'):
                self._release_cam()               # turn the webcam (LED) back off promptly
            if d.get('talk'):
                self._ensure_talk()
            else:
                self._stop_talk()
            cmd = d.get('cmd')
            if cmd == 'update':
                self.maybe_update()         # force an update check now (exits if updating)
            elif cmd:
                _run_remote_command(cmd)
            # Admin chat + notices — spin up the on-screen UI only when there's
            # something to show, then feed every heartbeat so it can re-raise / clear.
            chat = d.get('chat') or []
            notices = d.get('notices') or []
            if (chat or notices) and self.chat_ui is None and self.token:
                self.chat_ui = ChatUI(self.server, self.token)
                self.chat_ui.start()
            if self.chat_ui:
                self.chat_ui.feed(chat, notices)
        except Exception:
            pass

    def _ensure_talk(self):
        if _talk['session'] and _talk['session'].alive:
            return
        try:
            s = TalkSession(_talk_ws_url(self.server, self.token))
            s.start()
            _talk['session'] = s
            warn = getattr(s, '_warn', '')
            detail = 'Voice session started with admin' + ((' — ' + warn) if warn else '')
            self.event_queue.append({'type': 'voice', 'detail': detail, 'ts': time.time()})
            if warn:
                self.report_error('voice partial: ' + warn)   # one direction only
        except Exception as ex:
            self.report_error('voice start failed: %s' % TalkSession._friendly(ex))

    def _stop_talk(self):
        if _talk['session']:
            try: _talk['session'].stop()
            except Exception: pass
            _talk['session'] = None

    def wait_for_approval(self):
        """Poll /session until the admin approves. Returns the token when active,
        or None only when the admin explicitly denied/revoked this PC. If the
        server has no record of us ('none'), silently re-register and keep waiting
        — we never pop the setup dialog again once the PC has been configured."""
        while True:
            try:
                r = requests.get(self.server + '/api/win/session',
                                 params={'agent_id': self.agent_id}, timeout=10)
                if r.ok:
                    d = r.json()
                    st = d.get('status')
                    if st == 'active' and d.get('token'):
                        self.token = d['token']
                        self.cfg['token'] = self.token; save_cfg(self.cfg)
                        return self.token
                    if st in ('denied', 'revoked'):
                        return None            # admin removed this PC
                    if st == 'none':
                        _register(self.cfg)    # server lost us → re-register, stay pending
            except Exception:
                pass
            time.sleep(20)

    def _key(self, seg):
        return (seg['app'], seg['title'], seg['url'], seg['idle'])

    def fetch_policy(self):
        """Pull effective block-lists; (re)apply the hosts-file site block."""
        try:
            r = requests.get(self.server + '/api/win/policy', headers=self._headers(), timeout=10)
            if r.ok:
                d = r.json()
                self.blocked_apps = [a.lower() for a in (d.get('blocked_apps') or [])]
                self.blocked_sites = [s.lower() for s in (d.get('blocked_sites') or [])]
                self.allowed_apps = [a.lower() for a in (d.get('allowed_apps') or [])]
                self.allowed_sites = [s.lower() for s in (d.get('allowed_sites') or [])]
                self.allow_only_apps = bool(d.get('allow_only_apps'))
                self.allow_only_sites = bool(d.get('allow_only_sites'))
                sig = tuple(sorted(self.blocked_sites))
                if sig != self._hosts_sig:
                    self._hosts_ok = apply_hosts_block(self.blocked_sites)
                    self._hosts_sig = sig
        except Exception:
            pass

    def _enforce(self, app, title, url, pid):
        """Kill blocked / not-allowed apps; flag blocked/not-allowed sites.
        Returns True if the foreground app was killed."""
        al = (app or '').lower()
        # explicit app block
        for pat in self.blocked_apps:
            if pat and pat in al:
                killed = kill_pid(pid)
                self.viol_queue.append({'kind': 'app', 'pattern': pat, 'detail': title or app,
                                        'action': 'blocked' if killed else 'logged', 'ts': time.time()})
                return killed
        # app allow-only: kill anything not explicitly allowed (never a safe system app)
        if self.allow_only_apps and al and al not in _SAFE_APPS:
            if not any(a in al for a in self.allowed_apps):
                killed = kill_pid(pid)
                self.viol_queue.append({'kind': 'app', 'pattern': 'not-allowed', 'detail': title or app,
                                        'action': 'blocked' if killed else 'logged', 'ts': time.time()})
                return killed
        dom = _domain(url)
        if dom:
            for pat in self.blocked_sites:
                if pat and pat in dom:
                    self.viol_queue.append({'kind': 'site', 'pattern': pat, 'detail': url,
                                            'action': 'blocked' if getattr(self, '_hosts_ok', False) else 'logged',
                                            'ts': time.time()})
                    return False
            if self.allow_only_sites and not any(a in dom for a in self.allowed_sites):
                self.viol_queue.append({'kind': 'site', 'pattern': 'not-allowed', 'detail': url,
                                        'action': 'logged', 'ts': time.time()})
        return False

    def sample(self):
        idle_s = get_idle_seconds()
        idle = idle_s >= self.idle_after
        if idle:
            app, title, url = '', '', ''
        else:
            app, title, pid = get_foreground()
            url = get_browser_url(app)
            if self._enforce(app, title, url, pid):
                # app was blocked/closed — don't log it as normal active use
                app, title, url = app, '[blocked] ' + (title or ''), url
        now = time.time()
        newseg = {'app': app, 'title': title, 'url': url, 'idle': idle, 'start': now,
                  'kb': _key_count, 'cb': _click_count}   # intensity baselines
        with self.lock:
            if self.cur is None:
                self.cur = newseg
            elif self._key(self.cur) != self._key(newseg):
                self._close(now)
                self.cur = newseg

    def _close(self, end_ts):
        if self.cur and end_ts > self.cur['start'] + 0.5:
            keys = max(0, _key_count - self.cur.get('kb', _key_count))
            clicks = max(0, _click_count - self.cur.get('cb', _click_count))
            self.queue.append({
                'app': self.cur['app'], 'title': self.cur['title'], 'url': self.cur['url'],
                'idle': self.cur['idle'], 'start': self.cur['start'], 'end': end_ts,
                'keys': keys, 'clicks': clicks,
            })

    def _flush_violations(self):
        if not self.viol_queue:
            return
        batch = self.viol_queue[:]
        self.viol_queue = []
        try:
            r = requests.post(self.server + '/api/win/violation', headers=self._headers(),
                              json={'violations': batch}, timeout=15)
            if not r.ok and r.status_code != 401:
                self.viol_queue = batch + self.viol_queue
        except Exception:
            self.viol_queue = batch + self.viol_queue

    def capture_screenshot(self):
        buf = capture_jpeg()
        if not buf:
            self.report_error('screenshot capture failed — ' + (_shot_err or 'unknown'))
            return
        app, title, _pid = get_foreground()
        try:
            r = requests.post(self.server + '/api/win/screenshot', headers=self._headers(),
                              files={'image': ('shot.jpg', buf, 'image/jpeg')},
                              data={'app': app, 'title': title}, timeout=25)
            if not r.ok and r.status_code != 401:
                self.report_error('screenshot upload failed: HTTP %s' % r.status_code)
        except Exception as ex:
            self.report_error('screenshot upload error: %s' % ex)

    def send_live_frame(self):
        """Upload one live-view frame — the screen, or the WEBCAM in camera mode."""
        if time.time() < self._cam_until:
            buf = self._webcam_frame()
        else:
            self._release_cam()
            buf = capture_jpeg(max_w=1100, quality=42)
        if not buf:
            return
        try:
            requests.post(self.server + '/api/win/live', headers=self._headers(),
                          files={'image': ('live.jpg', buf, 'image/jpeg')}, timeout=10)
        except Exception:
            pass

    def _webcam_frame(self):
        """Capture one JPEG frame from the webcam (opens the camera; LED lights)."""
        try:
            import cv2, io
            if self._cam is None:
                self._cam = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            ok, frame = self._cam.read()
            if not ok or frame is None:
                return None
            ok2, enc = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 55])
            if not ok2:
                return None
            return io.BytesIO(enc.tobytes())
        except Exception as ex:
            self.report_error('webcam failed: %s' % ex)
            self._release_cam()
            return None

    def _release_cam(self):
        if self._cam is not None:
            try: self._cam.release()
            except Exception: pass
            self._cam = None

    def _flush_events(self):
        if not self.event_queue:
            return
        batch = self.event_queue[:]
        self.event_queue = []
        try:
            r = requests.post(self.server + '/api/win/event', headers=self._headers(),
                              json={'events': batch}, timeout=15)
            if not r.ok and r.status_code != 401:
                self.event_queue = batch + self.event_queue
        except Exception:
            self.event_queue = batch + self.event_queue

    def run_detectors(self):
        """USB insert/remove, new software installs, print jobs → events."""
        now = time.time()
        try:
            drives = list_removable_drives()
            if self._drives is not None:
                for d in drives - self._drives:
                    self.event_queue.append({'type': 'usb_connected', 'detail': 'USB drive ' + d, 'ts': now})
                for d in self._drives - drives:
                    self.event_queue.append({'type': 'usb_removed', 'detail': 'USB drive ' + d, 'ts': now})
            self._drives = drives
        except Exception:
            pass
        try:
            jobs, details = list_print_jobs()
            if self._pjobs is not None:
                for j in jobs - self._pjobs:
                    self.event_queue.append({'type': 'print_job', 'detail': details.get(j, 'print'), 'ts': now})
            self._pjobs = jobs
        except Exception:
            pass

    def scan_software(self):
        try:
            inst = list_installed_software()
            if self._installed is not None:
                for name in inst - self._installed:
                    self.event_queue.append({'type': 'software_installed', 'detail': name, 'ts': time.time()})
            self._installed = inst
        except Exception:
            pass

    def upload(self):
        self._flush_violations()
        self._flush_events()
        now = time.time()
        with self.lock:
            # flush the open segment up to now, then continue it from now
            self._close(now)
            if self.cur:
                self.cur['start'] = now
                self.cur['kb'] = _key_count
                self.cur['cb'] = _click_count
            batch = self.queue[:]
            self.queue = []
        if not batch:
            # heartbeat so the dashboard shows the PC online even when idle
            try:
                requests.post(self.server + '/api/win/heartbeat', headers=self._headers(),
                              json={'app_version': APP_VERSION}, timeout=10)
            except Exception:
                pass
            return
        try:
            r = requests.post(self.server + '/api/win/activity',
                              headers=self._headers(), json={'segments': batch}, timeout=20)
            if r.status_code == 401:
                # token revoked — drop it so the outer loop re-enrolls/waits
                self.token = None; self.cfg['token'] = None; save_cfg(self.cfg)
            elif not r.ok:
                with self.lock:
                    self.queue = batch + self.queue   # requeue on failure
        except Exception:
            with self.lock:
                self.queue = batch + self.queue

    def run(self):
        self.maybe_update()       # self-update on startup if a newer exe is out
        self.fetch_config()
        self.fetch_policy()
        self.send_health()
        self.run_detectors()      # establish baselines (no events on first pass)
        self.scan_software()
        last_upload = last_health = last_shot = last_sw = last_pcheck = time.time()
        last_upd = time.time()
        while self.token:
            try:
                self.sample()
                now = time.time()
                self.run_detectors()   # cheap: USB + print each sample
                # Poll for admin requests (screenshot / command / live). Poll faster
                # while a Live View is active so it feels responsive.
                live = now < self._live_until
                if now - last_pcheck >= (5 if live else 10):
                    self.check_pending()
                    last_pcheck = now
                    live = now < self._live_until
                if live:
                    self.send_live_frame()
                if self.screenshots_enabled and self.shot_int > 0 and now - last_shot >= self.shot_int:
                    self.capture_screenshot()
                    last_shot = now
                if now - last_sw >= 300:               # software scan every 5 min
                    self.scan_software()
                    last_sw = now
                if now - last_upload >= self.upload_int:
                    self.upload()
                    self.fetch_config()   # pick up screenshot/intensity toggles
                    self.fetch_policy()   # refresh block/allow lists
                    last_upload = now
                if now - last_health >= self.health_int:
                    self.send_health()
                    last_health = now
                if now - last_upd >= 6 * 3600:        # check for a new exe every 6h
                    self.maybe_update()
                    last_upd = now
            except Exception as ex:
                self.report_error('%s: %s' % (type(ex).__name__, ex))
            time.sleep(1.5 if time.time() < self._live_until else self.sample_int)


def _acquire_single_instance(retries=1):
    """Return a bound socket if we are the first/only monitor process, else None.
    Retries briefly (used after a self-update, to wait for the old process to
    release the port before this fresh copy takes over)."""
    import socket as _s
    for i in range(max(1, retries)):
        try:
            srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
            srv.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 0)
            srv.bind(('127.0.0.1', 49517))
            srv.listen(1)
            return srv
        except OSError:
            if i < retries - 1:
                time.sleep(1)
    return None


def main():
    if not _WIN:
        print('winMon runs on Windows only.')
        return
    _cleanup_stale_update()   # clear any leftover .new/.old from a previous update
    cfg = load_cfg()
    _agent_id(cfg)

    # Not registered yet → first-run setup (office + name), then start monitoring.
    if not cfg.get('office') or not cfg.get('name'):
        if not setup_dialog(cfg):
            return
        cfg = load_cfg()

    # Already registered. If a monitor is already running, this launch is the
    # user asking "show my ID / status" → display it and exit. After a self-update
    # (--relaunch) we instead wait for the old copy to release the port.
    relaunch = '--relaunch' in sys.argv
    lock = _acquire_single_instance(retries=(15 if relaunch else 1))
    if lock is None:
        if relaunch:
            return          # old copy still holding on; autostart will recover
        show_status_dialog(cfg)
        return

    # We are the monitor process. Runs forever; only the admin can stop it (revoke).
    while True:
        t = Tracker(cfg)
        if not t.token:
            tok = t.wait_for_approval()
            if tok is None:
                # Admin denied/revoked this PC. Stop monitoring, but keep polling
                # quietly so it resumes automatically if re-approved. NEVER pop the
                # setup dialog — only the admin controls registration.
                cfg['token'] = None; save_cfg(cfg)
                time.sleep(60)
                cfg = load_cfg()
                continue
            cfg = load_cfg(); t.token = cfg.get('token')
        try:
            t.run()
        except Exception as ex:
            try:
                t.report_error('fatal: %s' % ex)
            except Exception:
                pass
        # token dropped (revoked mid-run) → loop back and wait for re-approval
        cfg = load_cfg()
        time.sleep(5)


if __name__ == '__main__':
    main()
