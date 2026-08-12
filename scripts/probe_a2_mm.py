#!/usr/bin/env python3
"""A2: does Mattermost's websocket track channel MEMBERSHIP or mere readability?

Self-driving experiment for assumption A2 in docs/design/dynamic-watcher-design.md.
The whole Mattermost approach rests on a docstring claim that one socket
carries `posted` for every channel the bot is a member of; nothing in the repo
proves it, and the distinction that matters for routing is membership vs
readability — if delivery follows readability, a membership gate is needed and
Mattermost has no per-message signal for it.

Cases:
  1. post in a channel the probe user IS a member of  -> expect delivered
  2. post in a PUBLIC channel the probe user is NOT in -> the question
  3. post BY the probe user itself                     -> own-message echo
  4. add a user to a channel                           -> system message
  5. DM to the probe user                              -> DM delivery

Also records, per event, whether `data.channel_name`, `data.channel_type` and
`broadcast.team_id` are populated — if they are, routing can skip a REST
lookup, which matters for keeping work off the handler path.

Usage:
    uv run python scripts/probe_a2_mm.py --url https://mm.labpig.com --team lab \
        --probe-user probe-bot --probe-password '...' \
        --admin-user glin --admin-password '...' \
        --member-channel sandbox --outside-channel probe-outside
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime

import httpx
import websockets


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {m}", flush=True)


class MM:
    def __init__(self, url: str):
        self.c = httpx.AsyncClient(base_url=url.rstrip("/") + "/api/v4", timeout=20)
        self.h: dict = {}
        self.uid = ""

    async def login(self, user: str, pw: str) -> str:
        r = await self.c.post("users/login", json={"login_id": user, "password": pw})
        r.raise_for_status()
        self.token = r.headers["Token"]
        self.h = {"Authorization": f"Bearer {self.token}"}
        self.uid = r.json()["id"]
        return self.token

    async def team_id(self, team: str) -> str:
        r = await self.c.get(f"teams/name/{team}", headers=self.h)
        r.raise_for_status()
        return r.json()["id"]

    async def channel_id(self, team_id: str, name: str) -> str:
        r = await self.c.get(f"teams/{team_id}/channels/name/{name}", headers=self.h)
        r.raise_for_status()
        return r.json()["id"]

    async def post(self, channel_id: str, text: str) -> int:
        r = await self.c.post("posts", headers=self.h,
                              json={"channel_id": channel_id, "message": text})
        return r.status_code

    async def add_member(self, channel_id: str, user_id: str) -> int:
        r = await self.c.post(f"channels/{channel_id}/members", headers=self.h,
                              json={"user_id": user_id})
        return r.status_code

    async def remove_member(self, channel_id: str, user_id: str) -> int:
        r = await self.c.delete(f"channels/{channel_id}/members/{user_id}", headers=self.h)
        return r.status_code

    async def user_id_of(self, username: str) -> str:
        r = await self.c.get(f"users/username/{username}", headers=self.h)
        r.raise_for_status()
        return r.json()["id"]

    async def dm_channel(self, a: str, b: str) -> str:
        r = await self.c.post("channels/direct", headers=self.h, json=[a, b])
        r.raise_for_status()
        return r.json()["id"]

    async def close(self) -> None:
        await self.c.aclose()


async def driver(a, ready: asyncio.Event) -> None:
    await ready.wait()
    await asyncio.sleep(1.5)

    admin = MM(a.url)
    await admin.login(a.admin_user, a.admin_password)
    probe = MM(a.url)
    await probe.login(a.probe_user, a.probe_password)

    tid = await admin.team_id(a.team)
    member_ch = await admin.channel_id(tid, a.member_channel)
    outside_ch = await admin.channel_id(tid, a.outside_channel)
    probe_uid = await admin.user_id_of(a.probe_user)
    extra_uid = await admin.user_id_of(a.admin_user)

    log(f"CASE 1: admin posts in '{a.member_channel}' (probe IS member) ch={member_ch}")
    log(f"  -> {await admin.post(member_ch, 'A2 case1 member-channel')}")
    await asyncio.sleep(3)

    log(f"CASE 2: admin posts in PUBLIC '{a.outside_channel}' (probe NOT member) ch={outside_ch}")
    log(f"  -> {await admin.post(outside_ch, 'A2 case2 outside-channel')}")
    await asyncio.sleep(3)

    log(f"CASE 3: probe posts in '{a.member_channel}' (own message)")
    log(f"  -> {await probe.post(member_ch, 'A2 case3 own-message')}")
    await asyncio.sleep(3)

    log(f"CASE 4: system message — re-add {a.admin_user} to '{a.member_channel}'")
    await admin.remove_member(member_ch, extra_uid)
    await asyncio.sleep(2)
    log(f"  -> add: {await admin.add_member(member_ch, extra_uid)}")
    await asyncio.sleep(3)

    log(f"CASE 5: DM to {a.probe_user}")
    dm = await admin.dm_channel(admin.uid, probe_uid)
    log(f"  -> dm ch={dm} post: {await admin.post(dm, 'A2 case5 direct-message')}")
    await asyncio.sleep(3)

    await admin.close()
    await probe.close()
    log("driver finished")


async def listener(a, ready: asyncio.Event) -> int:
    probe = MM(a.url)
    token = await probe.login(a.probe_user, a.probe_password)
    await probe.close()

    ws_url = a.url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/api/v4/websocket"
    log(f"connecting {ws_url}")
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"seq": 1, "action": "authentication_challenge",
                                  "data": {"token": token}}))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + a.seconds
        n = 0
        summary = []
        authed = False

        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - loop.time()))
            except (asyncio.TimeoutError, websockets.ConnectionClosed) as e:
                log(f"stream ended: {type(e).__name__}")
                break
            f = json.loads(raw)

            if not authed and (f.get("event") == "hello" or f.get("status") == "OK"):
                authed = True
                log(f"authenticated: {json.dumps(f)[:200]}")
                ready.set()
                continue

            ev = f.get("event")
            if ev != "posted":
                continue

            n += 1
            data = f.get("data") or {}
            bc = f.get("broadcast") or {}
            post = json.loads(data.get("post", "{}"))
            print("-" * 74, flush=True)
            log(f"POSTED #{n}")
            print(f"  data.channel_name  : {data.get('channel_name')!r}", flush=True)
            print(f"  data.channel_type  : {data.get('channel_type')!r}", flush=True)
            print(f"  data.team_id       : {data.get('team_id')!r}", flush=True)
            print(f"  broadcast.team_id  : {bc.get('team_id')!r}", flush=True)
            print(f"  broadcast.channel_id: {bc.get('channel_id')!r}", flush=True)
            print(f"  post.type (system) : {post.get('type')!r}  <-- non-empty = SYSTEM", flush=True)
            print(f"  data.sender_name   : {data.get('sender_name')!r}", flush=True)
            print(f"  post.message       : {str(post.get('message'))[:60]!r}", flush=True)
            print(f"  RAW: {json.dumps(f)[:700]}", flush=True)
            summary.append({
                "n": n, "chan": data.get("channel_name"), "ctype": data.get("channel_type"),
                "team": data.get("team_id"), "bteam": bc.get("team_id"),
                "type": post.get("type"), "from": data.get("sender_name"),
                "text": str(post.get("message"))[:40],
            })

        print("-" * 74, flush=True)
        log(f"captured {n} posted event(s)")
        print("\nSUMMARY", flush=True)
        for s in summary:
            print(f"  #{s['n']} chan={s['chan']!r} ctype={s['ctype']!r} "
                  f"team={s['team']!r} bteam={s['bteam']!r} type={s['type']!r} "
                  f"from={s['from']!r} text={s['text']!r}", flush=True)
        return 0


async def main_async(a) -> int:
    ready = asyncio.Event()
    t = asyncio.create_task(driver(a, ready))
    rc = await listener(a, ready)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    return rc


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--team", default="lab")
    p.add_argument("--probe-user", required=True)
    p.add_argument("--probe-password", required=True)
    p.add_argument("--admin-user", required=True)
    p.add_argument("--admin-password", required=True)
    p.add_argument("--member-channel", default="sandbox")
    p.add_argument("--outside-channel", default="probe-outside")
    p.add_argument("--seconds", type=int, default=45)
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
