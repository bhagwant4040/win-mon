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

import requests

APP_VERSION = '1.0'
DEFAULT_SERVER = 'https://ems.rajivsyndicate.com'

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
    os.makedirs(_CFG_DIR, exist_ok=True)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f)


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
        return {
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
    except Exception:
        return {}


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


def capture_jpeg(max_w=1366, quality=55):
    """Grab the screen → downscaled JPEG in a BytesIO buffer."""
    try:
        from PIL import ImageGrab
        import io
        img = ImageGrab.grab()
        if img.width > max_w:
            r = max_w / float(img.width)
            img = img.resize((max_w, int(img.height * r)))
        buf = io.BytesIO()
        img.convert('RGB').save(buf, 'JPEG', quality=quality)
        buf.seek(0)
        return buf
    except Exception:
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


def setup_dialog(cfg):
    """Tiny Tkinter dialog to capture server + EMS credentials. Returns True if
    enrollment was submitted successfully (agent now pending approval)."""
    import tkinter as tk
    from tkinter import messagebox

    result = {'ok': False}
    root = tk.Tk()
    root.title('winMon Setup — Rajiv Syndicate')
    root.geometry('380x260')
    root.resizable(False, False)

    tk.Label(root, text='Computer Monitoring Setup', font=('Segoe UI', 13, 'bold')).pack(pady=(16, 4))
    tk.Label(root, text='Sign in with your EMS credentials', fg='#666').pack()

    frm = tk.Frame(root); frm.pack(pady=12, padx=20, fill='x')
    tk.Label(frm, text='Server').grid(row=0, column=0, sticky='w', pady=4)
    e_srv = tk.Entry(frm, width=28); e_srv.grid(row=0, column=1, pady=4)
    e_srv.insert(0, cfg.get('server') or DEFAULT_SERVER)
    tk.Label(frm, text='Employee ID').grid(row=1, column=0, sticky='w', pady=4)
    e_eid = tk.Entry(frm, width=28); e_eid.grid(row=1, column=1, pady=4)
    tk.Label(frm, text='Password').grid(row=2, column=0, sticky='w', pady=4)
    e_pw = tk.Entry(frm, width=28, show='•'); e_pw.grid(row=2, column=1, pady=4)

    status = tk.Label(root, text='', fg='#c00'); status.pack()

    def submit():
        srv = e_srv.get().strip().rstrip('/') or DEFAULT_SERVER
        eid = e_eid.get().strip()
        pw = e_pw.get()
        if not eid or not pw:
            status.config(text='Enter Employee ID and password.'); return
        try:
            r = requests.post(srv + '/api/win/enroll', json={
                'employee_id': eid, 'password': pw, 'agent_id': _agent_id(cfg),
                'hostname': socket.gethostname(), 'os_user': getpass.getuser(),
                'app_version': APP_VERSION,
            }, timeout=15)
            if r.status_code == 401:
                status.config(text='Invalid EMS credentials.'); return
            if not r.ok:
                status.config(text='Enroll failed (%s).' % r.status_code); return
            cfg['server'] = srv; cfg['employee_id'] = eid
            save_cfg(cfg)
            result['ok'] = True
            messagebox.showinfo('winMon', 'Submitted. Ask Head Office to approve this PC.\nMonitoring will start automatically once approved.')
            root.destroy()
        except Exception as ex:
            status.config(text='Network error: %s' % ex)

    tk.Button(root, text='Enroll', command=submit, width=16,
              bg='#2563eb', fg='white', font=('Segoe UI', 10, 'bold')).pack(pady=10)
    root.mainloop()
    return result['ok']


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
        self._hosts_sig = None     # last applied hosts set (avoid rewriting each cycle)
        self.screenshots_enabled = False
        self.shot_int = 300
        self.intensity_enabled = True
        self._drives = None        # detector baselines
        self._installed = None
        self._pjobs = None

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

    def wait_for_approval(self):
        """Poll /session until the admin approves; returns the token."""
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
                        return
                    if st in ('denied', 'revoked', 'none'):
                        # need to (re-)enroll
                        return None
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
            return
        app, title, _pid = get_foreground()
        try:
            requests.post(self.server + '/api/win/screenshot', headers=self._headers(),
                          files={'image': ('shot.jpg', buf, 'image/jpeg')},
                          data={'app': app, 'title': title}, timeout=25)
        except Exception:
            pass

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
                requests.post(self.server + '/api/win/heartbeat', headers=self._headers(), timeout=10)
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
        self.fetch_config()
        self.fetch_policy()
        self.send_health()
        self.run_detectors()      # establish baselines (no events on first pass)
        self.scan_software()
        last_upload = last_health = last_shot = last_sw = time.time()
        while self.token:
            self.sample()
            now = time.time()
            self.run_detectors()   # cheap: USB + print each sample
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
            time.sleep(self.sample_int)


def main():
    if not _WIN:
        print('winMon runs on Windows only.')
        return
    cfg = load_cfg()
    _agent_id(cfg)
    # First run (or after deny/revoke): capture credentials + enroll.
    while True:
        if not cfg.get('employee_id'):
            if not setup_dialog(cfg):
                return
            cfg = load_cfg()
        t = Tracker(cfg)
        if not t.token:
            res = t.wait_for_approval()
            if res is None:
                # denied/revoked → force re-enroll
                cfg['employee_id'] = None; cfg['token'] = None; save_cfg(cfg)
                cfg = load_cfg()
                continue
            cfg = load_cfg(); t.token = cfg.get('token')
        try:
            t.run()
        except Exception:
            pass
        # token dropped (revoked) → loop back and wait for re-approval
        cfg = load_cfg()
        if not cfg.get('token'):
            cfg['employee_id'] = cfg.get('employee_id')  # keep id, just re-wait
        time.sleep(5)


if __name__ == '__main__':
    main()
