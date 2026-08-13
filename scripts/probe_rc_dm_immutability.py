#!/usr/bin/env python3
"""Can a Rocket.Chat 1:1 DM gain a member in place, keeping the same room id?

Backs §6.4 of docs/design/dynamic-watcher-design.md, and specifically the claim
in §2.2 that the room-kind cache needs no invalidation path.

The design caches "is this DM a 1:1 or a group DM?" keyed by room id, because
Rocket.Chat reports both as type `d` and telling them apart costs a REST lookup.
That cache is only safe if a room cannot change kind underneath it. The dangerous
case would be a 1:1 DM that gains a third participant *in place*: ordinary
message frames carry only type `d`, so nothing in the message stream would reveal
that the cached kind had gone stale.

So: is that reachable? This probe tries every route for adding a participant to
an existing type-`d` room, then re-reads the room to confirm nothing changed.

It runs entirely as the ADMIN account deliberately. Admin is strictly more
privileged than an ordinary DM member, so a route the server refuses to admin on
room-type grounds is refused to everyone — which makes the negative result
stronger than testing as a normal user, and avoids depending on probe-user login
(lab servers may enforce email 2FA on freshly created accounts).

Usage:
    uv run python scripts/probe_rc_dm_immutability.py --url https://rc.labpig.com \
        --admin-user glin --admin-password '<pw>' \
        --user-b probe-dm-b --user-c probe-dm-c

The two non-admin users must already exist; create them with the admin CLI:

    uv run python -m gateway.admin <profile> create-user probe-dm-b probe-dm-b@example.invalid '<pw>'
    uv run python -m gateway.admin <profile> create-user probe-dm-c probe-dm-c@example.invalid '<pw>'
"""

import argparse
import json
import sys

import httpx


def log(m: str) -> None:
    print(m, flush=True)


def show(label: str, status: int, body, trim: int = 700) -> None:
    log("=" * 72)
    log(f"### {label}\nHTTP {status}")
    log(json.dumps(body, indent=2, sort_keys=True)[:trim] if isinstance(body, (dict, list)) else str(body)[:trim])


class RC:
    def __init__(self, url: str):
        self.c = httpx.Client(base_url=url.rstrip("/"), timeout=25)
        self.h: dict = {}

    def call(self, path: str, method: str = "GET", body=None):
        r = self.c.request(method, path, headers=self.h, json=body)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text

    def login(self, user: str, pw: str) -> str:
        st, b = self.call("/api/v1/login", "POST", {"user": user, "password": pw})
        if not (isinstance(b, dict) and b.get("status") == "success"):
            log(f"login failed: HTTP {st} {b}")
            sys.exit(1)
        self.h = {"X-Auth-Token": b["data"]["authToken"], "X-User-Id": b["data"]["userId"]}
        return b["data"]["userId"]

    def method_call(self, name: str, params: list):
        """Invoke a Meteor method — the route the web UI itself uses."""
        msg = json.dumps({"msg": "method", "method": name, "id": "1", "params": params})
        st, b = self.call(f"/api/v1/method.call/{name}", "POST", {"message": msg})
        if isinstance(b, dict) and isinstance(b.get("message"), str):
            b = json.loads(b["message"])
        return st, b

    def user_id(self, username: str) -> str:
        st, b = self.call(f"/api/v1/users.info?username={username}")
        if st != 200:
            log(f"user '{username}' not found (HTTP {st}) — create it first, see the module docstring")
            sys.exit(1)
        return b["user"]["_id"]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--admin-user", required=True)
    p.add_argument("--admin-password", required=True)
    p.add_argument("--user-b", default="probe-dm-b")
    p.add_argument("--user-c", default="probe-dm-c")
    a = p.parse_args()

    rc = RC(a.url)
    admin_id = rc.login(a.admin_user, a.admin_password)
    log(f"[auth] admin={a.admin_user} id={admin_id}")

    st, b = rc.call("/api/info")
    if st == 200 and isinstance(b, dict) and "info" in b:
        log(f"[server] Rocket.Chat {b['info'].get('version')} commit={b['info'].get('commit', {}).get('hash')}")
    else:
        # /api/v1/info 404s on some deployments; the version is informational.
        log(f"[server] version unavailable (HTTP {st})")

    c_id = rc.user_id(a.user_c)

    # --- the 1:1 room ---
    st, b = rc.call("/api/v1/im.create", "POST", {"username": a.user_b})
    show(f'im.create {{"username": "{a.user_b}"}}  -> 1:1 DM', st, b)
    rid1 = b["room"]["_id"]
    log(f">>> rid1 = {rid1}  t={b['room'].get('t')}")

    st, before = rc.call(f"/api/v1/im.members?roomId={rid1}")
    log(f">>> rid1 members BEFORE: total={before.get('total')}")

    # --- a DM with a different member set is a different room, not a mutation ---
    st, b = rc.call("/api/v1/im.create", "POST", {"usernames": f"{a.user_b},{a.user_c}"})
    show(f'im.create {{"usernames": "{a.user_b},{a.user_c}"}}  -> group DM', st, b)
    rid2 = b.get("room", {}).get("_id")
    log(f">>> rid2 = {rid2}   same as rid1? {rid1 == rid2}")

    # --- every route for adding a member to rid1 ---
    log("\n" + "#" * 72 + "\n# in-place add attempts against the 1:1 room\n" + "#" * 72)
    for label, path, payload in [
        ("channels.invite (username)", "/api/v1/channels.invite", {"roomId": rid1, "username": a.user_c}),
        ("channels.invite (userId)", "/api/v1/channels.invite", {"roomId": rid1, "userId": c_id}),
        ("groups.invite", "/api/v1/groups.invite", {"roomId": rid1, "username": a.user_c}),
        ("im.invite", "/api/v1/im.invite", {"roomId": rid1, "username": a.user_c}),
        ("rooms.invite", "/api/v1/rooms.invite", {"roomId": rid1, "username": a.user_c}),
    ]:
        show(label, *rc.call(path, "POST", payload))

    for name, params in [
        ("addUsersToRoom", [{"rid": rid1, "users": [a.user_c]}]),
        ("addUserToRoom", [{"rid": rid1, "username": a.user_c}]),
    ]:
        show(f"method.call/{name}  (the web UI's own route)", *rc.method_call(name, params))

    # --- did anything actually change? ---
    st, after = rc.call(f"/api/v1/im.members?roomId={rid1}")
    st2, info = rc.call(f"/api/v1/rooms.info?roomId={rid1}")
    log("\n" + "#" * 72)
    log(f">>> rid1 members AFTER: total={after.get('total')} (was {before.get('total')})")
    log(f">>> rid1 usersCount={info.get('room', {}).get('usersCount')}")

    # --- and can it shrink? ---
    if rid2:
        show("method.call/removeUserFromRoom (shrink the group DM)",
             *rc.method_call("removeUserFromRoom", [{"rid": rid2, "username": a.user_c}]))

    unchanged = after.get("total") == before.get("total")
    distinct = bool(rid2) and rid1 != rid2
    log("\n" + "#" * 72)
    log(json.dumps({
        "rid_1to1": rid1,
        "rid_group": rid2,
        "group_is_a_distinct_room": distinct,
        "one_to_one_member_count_unchanged": unchanged,
        "verdict": ("IMMUTABLE — the kind cache cannot go stale"
                    if unchanged and distinct else
                    "MUTABLE — §2.2 needs the invalidation hook it describes"),
    }, indent=2))
    return 0 if (unchanged and distinct) else 2


if __name__ == "__main__":
    sys.exit(main())
