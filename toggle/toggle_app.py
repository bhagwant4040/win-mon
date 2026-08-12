"""
winMon Privacy Toggle — lets the employee pause ALL winMon monitoring on
this PC with one click: activity tracking, screenshots, camera, microphone,
and the heartbeat itself — the PC shows as offline on the HQ dashboard
while blocked, not just "camera/mic off".

Purely local: flips a value under HKEY_CURRENT_USER that winMon.exe checks
at the top of its main loop (see _locally_blocked() in agent/agent.py). No
server round-trip, no admin action needed, doesn't require winMon to be
running to flip the switch — it takes effect within one loop pass (a few
seconds) the next time winMon itself checks, in both directions.
"""
import tkinter as tk
import winreg

REG_KEY = r'Software\winMon\Privacy'
REG_VALUE = 'CamMicBlocked'


def is_blocked():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY)
        try:
            val, _ = winreg.QueryValueEx(k, REG_VALUE)
            return bool(val)
        finally:
            winreg.CloseKey(k)
    except Exception:
        return False


def set_blocked(blocked):
    k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY)
    try:
        winreg.SetValueEx(k, REG_VALUE, 0, winreg.REG_DWORD, 1 if blocked else 0)
    finally:
        winreg.CloseKey(k)


class App:
    def __init__(self, root):
        self.root = root
        root.title('winMon Privacy Toggle')
        root.geometry('360x240')
        root.resizable(False, False)
        root.configure(bg='#0d1b2a')

        tk.Label(root, text='winMon Monitoring', font=('Segoe UI', 15, 'bold'),
                 bg='#0d1b2a', fg='#ffffff').pack(pady=(28, 6))
        self.status_lbl = tk.Label(root, text='', font=('Segoe UI', 13, 'bold'), bg='#0d1b2a')
        self.status_lbl.pack(pady=(0, 20))
        self.btn = tk.Button(root, text='', font=('Segoe UI', 12, 'bold'),
                              width=22, height=2, relief='flat', cursor='hand2',
                              command=self.toggle)
        self.btn.pack()
        tk.Label(root, text='Blocked = all monitoring paused on this PC (shows offline).\nOnly affects this PC.',
                 font=('Segoe UI', 8), bg='#0d1b2a', fg='#8899aa', justify='center',
                 wraplength=300).pack(pady=(20, 0))
        self.refresh()

    def refresh(self):
        blocked = is_blocked()
        if blocked:
            self.status_lbl.config(text='● BLOCKED', fg='#ff5555')
            self.btn.config(text='Turn ON (allow)', bg='#1a7a3a', fg='#ffffff',
                             activebackground='#22994a', activeforeground='#ffffff')
        else:
            self.status_lbl.config(text='● ALLOWED', fg='#3ddc84')
            self.btn.config(text='Turn OFF (block)', bg='#c0392b', fg='#ffffff',
                             activebackground='#e0473a', activeforeground='#ffffff')

    def toggle(self):
        set_blocked(not is_blocked())
        self.refresh()


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
