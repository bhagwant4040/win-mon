#!/usr/bin/env python3
"""
winMon two-way voice relay.

A tiny WebSocket relay that pairs an admin's EMS browser with a winMon PC agent
into a "room" (keyed by agent_id) and forwards audio frames both ways in
real time. Runs on the VPS behind nginx (which proxies /talkws/).

Auth (read-only, against the live EMS SQLite DB):
  • agent  connects to  /talkws/agent/<token>      → token must match win_agent.token (active)
  • admin  connects to  /talkws/admin/<ticket>     → ticket must match win_agent.talk_ticket
                                                       and talk_until must not be expired

Only voice/control frames are relayed between the two peers; the relay never
stores audio. Start under supervisor. Requires: pip install websockets
"""
import asyncio
import json
import sqlite3
from datetime import datetime

import websockets

DB_PATH = '/home/ems_user/ems_rs/instance/employee_management.db'
HOST, PORT = '127.0.0.1', 5010

rooms = {}   # agent_id -> {'agent': ws, 'admin': ws}


def _db():
    return sqlite3.connect('file:%s?mode=ro' % DB_PATH, uri=True, timeout=5)


def _auth_agent(token):
    if not token:
        return None
    try:
        c = _db()
        r = c.execute("SELECT agent_id FROM win_agent WHERE token=? AND status='active'",
                      (token,)).fetchone()
        c.close()
        return r[0] if r else None
    except Exception:
        return None


def _auth_admin(ticket):
    if not ticket:
        return None
    try:
        c = _db()
        r = c.execute("SELECT agent_id, talk_until FROM win_agent WHERE talk_ticket=?",
                      (ticket,)).fetchone()
        c.close()
        if not r:
            return None
        aid, until = r
        if until:
            try:
                dt = datetime.fromisoformat(str(until))
                if (datetime.utcnow() - dt).total_seconds() > 0:   # expired
                    return None
            except Exception:
                pass
        return aid
    except Exception:
        return None


def _path_of(ws):
    # websockets version-agnostic path lookup
    for attr in ('path',):
        p = getattr(ws, attr, None)
        if p:
            return p
    req = getattr(ws, 'request', None)
    return getattr(req, 'path', '') if req else ''


async def handler(ws, *args):
    path = (args[0] if args else None) or _path_of(ws)
    parts = [p for p in (path or '').split('/') if p]
    # strip an optional leading 'talkws'
    if parts and parts[0] == 'talkws':
        parts = parts[1:]
    if len(parts) < 3 or parts[0] != 'talk':
        await ws.close(); return
    role, cred = parts[1], parts[2]
    aid = _auth_agent(cred) if role == 'agent' else (_auth_admin(cred) if role == 'admin' else None)
    if not aid:
        await ws.close(code=4001); return

    room = rooms.setdefault(aid, {})
    room[role] = ws
    peer = 'admin' if role == 'agent' else 'agent'
    # tell each side whether its peer is present
    try:
        await ws.send(json.dumps({'sys': 'ready', 'peer': bool(room.get(peer))}))
    except Exception:
        pass
    if room.get(peer):
        try:
            await room[peer].send(json.dumps({'sys': 'peer', 'state': 'joined'}))
        except Exception:
            pass
    try:
        async for msg in ws:
            p = room.get(peer)
            if p:
                try:
                    await p.send(msg)     # forward audio (binary) or control (text)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        if room.get(role) is ws:
            room.pop(role, None)
        p = room.get(peer)
        if p:
            try:
                await p.send(json.dumps({'sys': 'peer', 'state': 'left'}))
            except Exception:
                pass
        if not room:
            rooms.pop(aid, None)


async def main():
    async with websockets.serve(handler, HOST, PORT, max_size=2 ** 20, ping_interval=20):
        print('winMon talk relay listening on %s:%d' % (HOST, PORT))
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
