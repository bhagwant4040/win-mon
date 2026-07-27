# winMon — Windows Computer-Activity Monitor

A background Windows agent that tracks the **foreground app, window title, active
website, and time** per employee (with idle detection), and reports to the EMS
**Computer Activity** dashboard. Same approve-first model as locEMS.

## Parts
- **Agent** (`agent/agent.py`) — runs on each employee PC. Packaged to `winMon.exe`.
- **Backend + dashboard** — inside EMS (`ems.rajivsyndicate.com`):
  - Device API: `/api/win/enroll`, `/api/win/session`, `/api/win/activity`, `/api/win/heartbeat`, `/api/win/config`
  - Admin: **EMS → Computer Activity** (Overview / Activity / PC Report + Excel export)

## What it captures
- Active application (process) + window title
- Active website (best-effort, via UI Automation on the browser address bar)
- Time per app / per website, active vs idle, first/last active
- **Activity intensity** — keystroke & mouse-click **counts** per segment (counts only, never the actual keys/content)
- **Screenshots** — periodic desktop captures (admin toggle; off by default)
- **Events** — USB drive connect/remove, new software installs, print jobs
- **Policies** — block or allow-only apps/sites, per-PC or all PCs; blocked apps are closed, blocked sites go in the hosts file (agent must run as admin for site blocking)
- **System health** — CPU/RAM/disk/battery/uptime/OS with good/warning/critical flags
- Full keystroke *logging* (actual text) is intentionally NOT done.

## Admin controls (EMS → Computer Activity)
Overview (approvals + monitoring settings) · System Status · Activity · Screenshots · Events · PC Report (+Excel) · App/Site Policies. Turn **screenshots** and **intensity** on/off in **Overview → Monitoring Settings**.

## Build the agent (on Windows)
```
cd agent
build.bat
```
Produces `agent\dist\winMon.exe` (single file, runs silently — no console window).

## Install on an employee PC
1. Copy `winMon.exe` to the PC (e.g. `C:\winMon\winMon.exe`).
2. Run it once → a setup box asks for **Server** (default `https://ems.rajivsyndicate.com`),
   **Employee ID**, and **EMS Password**. Submit.
3. In **EMS → Computer Activity → Overview**, the PC shows as **Pending** → click **Approve**.
4. Monitoring starts automatically and continues in the background.

### Auto-start on login (so it always runs)
Put a shortcut to `winMon.exe` in the Startup folder:
- Press `Win+R` → `shell:startup` → paste a shortcut to `winMon.exe`.
- Or (all users, needs admin): copy the shortcut into
  `C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp`.

## Notes
- Config is stored per-user at `%APPDATA%\winMon\config.json` (server, agent id, token).
- If the admin **Revokes** the PC in the dashboard, the agent stops reporting and
  waits for re-approval.
- Website capture depends on the browser exposing its address bar to UI Automation;
  app + title + time always work. A browser extension can be added later for
  rock-solid URL capture.
- **Tell staff their work PCs are monitored** — required by law in many places.
