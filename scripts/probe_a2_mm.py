#!/usr/bin/env python3
"""A2: does Mattermost's websocket track channel MEMBERSHIP or mere readability?

Self-driving experiment for assumption A2 in docs/design/dynamic-watcher-design.md.
The whole Mattermost approach rests on a docstring claim that one socket
carries `posted` for every channel the bot is a member of; nothing in the repo
proves it, and the distinction that matters for routing is membership vs
readability — if delivery follows readability, a membership gate is needed and
Mattermost has no per-message signal for it.

Case 2 is preceded by a PREFLIGHT using the probe's own credentials, because
without it "no event arrived" is ambiguous — a channel the probe cannot read
looks exactly like one whose delivery is membership-gated. The preflight asserts
the probe really can read the channel (channel GET, public listing, posts) and
really is not a member (absent from its joined list, plus an admin-token
membership lookup returning 404; the probe's own lookup returns 403, since a
non-member cannot query even its own row).

Cases:
  1. post in a channel the probe user IS a member of  -> expect delivered
  2. post in a PUBLIC channel the probe user is NOT in -> the question
  3. post BY the probe user itself                     -> own-message echo
  4. add a user to a channel                           -> system message
  5. DM to the probe user                              -> DM delivery
  6. join the probe to the case-2 channel, post again -> isolates the variable

Case 6 exists because cases 1 and 2 differ in two ways at once (different
channel AND different membership), so alone they cannot rule out a
channel-specific quirk. Case 6 changes only the membership row, then removes
the probe again so the script stays re-runnable.

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

    async def channel_type(self, channel_id: str) -> tuple[int, str]:
        r = await self.c.get(f"channels/{channel_id}", headers=self.h)
        return r.status_code, r.json().get("type", "") if r.status_code == 200 else ""

    async def membership_status(self, channel_id: str, user_id: str) -> int:
        r = await self.c.get(f"channels/{channel_id}/members/{user_id}", headers=self.h)
        return r.status_code

    async def posts_status(self, channel_id: str) -> int:
        r = await self.c.get(f"channels/{channel_id}/posts", headers=self.h)
        return r.status_code

    async def public_channel_names(self, team_id: str) -> set[str]:
        r = await self.c.get(f"teams/{team_id}/channels", headers=self.h)
        r.raise_for_status()
        return {c["name"] for c in r.json()}

    async def joined_channel_names(self, team_id: str) -> set[str]:
        r = await self.c.get(f"users/{self.uid}/teams/{team_id}/channels", headers=self.h)
        r.raise_for_status()
        return {c["name"] for c in r.json()}

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

    # Preflight for CASE 2, using the PROBE's own credentials. Without it, "no
    # event arrived" is ambiguous: a channel the probe cannot read and one whose
    # delivery is membership-gated look identical from the listener's side. The
    # conclusion in the design doc's §6.2 rests on this distinction, so the
    # fixture is asserted rather than assumed.
    log(f"PREFLIGHT for CASE 2 on '{a.outside_channel}':")
    ch_st, ch_type = await probe.channel_type(outside_ch)
    log(f"  probe GET channel        -> {ch_st} type={ch_type!r}   (want 200 / 'O')")
    posts_st = await probe.posts_status(outside_ch)
    log(f"  probe GET channel posts  -> {posts_st}            (want 200: content really is readable)")
    try:
        in_public = a.outside_channel in await probe.public_channel_names(tid)
        in_joined = a.outside_channel in await probe.joined_channel_names(tid)
        listed = True
    except httpx.HTTPStatusError as e:
        # A public channel is only readable to team members; if the probe is not
        # on the team, that is the fixture being wrong, not a finding.
        log(f"  channel listing failed  -> {e.response.status_code} — is the probe a member of team '{a.team}'?")
        in_public = in_joined = False
        listed = False
    if listed:
        log(f"  in team's public list    -> {in_public}         (want True)")
        log(f"  in probe's joined list   -> {in_joined}        (want False)")
    own_ms = await probe.membership_status(outside_ch, probe.uid)
    admin_ms = await admin.membership_status(outside_ch, probe.uid)
    log(f"  probe's own membership   -> {own_ms}            (403 expected: a non-member cannot query even its own row)")
    log(f"  admin-token membership   -> {admin_ms}            (want 404 — this is the load-bearing non-membership proof)")

    if (ch_st == 200 and ch_type == "O" and posts_st == 200
            and listed and in_public and not in_joined and admin_ms == 404):
        log("  preflight OK: readable, and genuinely not a member")
    else:
        log("  *** PREFLIGHT FAILED — CASE 2 cannot distinguish membership gating "
            "from plain inaccessibility. Fix the fixture before trusting it. ***")

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

    # CASE 6 isolates the variable. Cases 1 and 2 differ in two ways at once —
    # different channels AND different membership — so on their own they cannot
    # rule out a channel-specific quirk. Joining the *same* channel that was
    # silent in case 2 and posting again changes only the membership row.
    log(f"CASE 6: join probe to the case-2 channel, then post there again ch={outside_ch}")
    log(f"  -> add probe: {await admin.add_member(outside_ch, probe_uid)}")
    await asyncio.sleep(2)
    log(f"  -> post: {await admin.post(outside_ch, 'A2 case6 same-channel-after-join')}")
    await asyncio.sleep(3)
    # Restore the fixture so the script is re-runnable: case 2 requires the probe
    # to be a non-member again.
    log(f"  -> cleanup, remove probe: {await admin.remove_member(outside_ch, probe_uid)}")

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
