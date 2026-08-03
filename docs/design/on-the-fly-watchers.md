# On-the-fly watchers: rule-based room matching + lazy watcher lifecycle

Status: **design only — not yet implemented.** Captures a 2026-08-02 design
discussion. Nothing in this doc has landed in code yet. Implementation
requires coordinating a production test window with the repo owner first
(see "Rollout" below) — do not schedule this on macbook-server without
checking in.

## Problem

`WatcherConfig.room: str` is a fixed 1:1 mapping — one watcher, one room,
enumerated statically in `config.yaml`, all created eagerly at gateway
startup (`gateway/core/config.py`'s `WatcherConfig` docstring: "the gateway
starts all configured watchers on startup — no runtime add-watcher commands
are needed").

This doesn't match a common real-world usage pattern: a team creates an
ad-hoc room for a live incident and pulls in whoever/whatever is needed,
including an ACG agent — with no expectation that someone edits
`config.yaml` and restarts the gateway first. ACG currently has no way to
notice a new room exists and start watching it without a config touch +
restart.

## Design goals

1. Support "listen to room X, Y, Z" (current behavior — unchanged), "listen
   to all rooms I have access to," and "listen to all rooms except A, B, C"
   from a single room-matching mechanism.
2. New rooms (created after the gateway started) get picked up automatically
   when possible, with a working fallback when not.
3. Preserve the **1 session : 1 room** invariant. A rule matching many rooms
   still spins up one independent watcher/session per matched room — never
   one session serving multiple rooms. (Explicitly confirmed as a hard
   requirement, not a nice-to-have: the risk of an agent answering a room-1
   question into room-2 is not something we're willing to accept the risk of
   without first validating agent behavior in isolation — out of scope for
   this change.)
4. Don't keep unbounded numbers of idle watchers around forever; don't
   eagerly pay startup cost for rooms nobody's messaged in months.

## Config schema

- `room: roomX` (exact match) stays supported unchanged.
- New: `room: "*"` + optional `exclude_room: [...]`. `#2` (all rooms) and
  `#3` (all except some) are the same mechanism — `#3` is `#2` with an
  extra filter step, not a separate implementation.
- Two new TTL settings, living on `AgentConfig` (inheritable via
  `agent_templates:`/`inherits:` — the existing pattern, no new config
  layer invented), overridable per watcher/rule entry for edge cases:
  - `session_idle_days` — after this many days with no new message, drop
    the **runtime** watcher object (processor, connector subscription) but
    keep the persisted session ID. Framed for the user as "when do we
    consider this session idle and put it on the back burner to save
    resources" (not "unload," per feedback — friendlier framing).
  - `session_expire_days` — after this many days, drop the session
    entirely. Next message starts fresh.
- `AgentBackend` gets a new method, `typical_session_retention_days() -> int
  | None` (`None` = no known agent-side limit). Effective expiry = `min(configured
  session_expire_days, agent's declared value)` when the agent declares one.
  Confirmed values (2026-08-02 research, cited below): **Claude Code: 30**
  (default `cleanupPeriodDays` in `settings.json`, user-adjustable per
  machine — treat 30 as a default assumption, not a guaranteed constant
  across every install). **OpenCode: `None`** (no automatic expiry exists at
  all — confirmed via source, not just docs; sessions persist indefinitely
  in SQLite).
- **Retiring `WatcherConfig.session_id` (sticky session pinning) and
  `online_notification`/`offline_notification` as part of this change** —
  see "Also removing" below.

## Room discovery + membership-event hooks

Startup: enumerate currently-accessible rooms via `subscriptions.get` (RC)
/ `GET /api/v4/users/me/channels` (MM), filter through the rule
(wildcard/exclude), and record the matching set — but do **not** eagerly
instantiate a watcher for all of them (see "Lazy creation" below).

**Real-time hooks, confirmed to exist on both connectors (2026-08-02
research):**

| | Bot added to room | Bot removed from room |
|---|---|---|
| **RC** | `stream-notify-user` → `{userId}/subscriptions-changed`, `clientAction === 'inserted'`. Payload has `rid`/type/name — enough to subscribe immediately, no follow-up REST call needed. No ordering race (DB write commits before the broadcast fires). | Same stream, `clientAction === 'removed'`. Server also proactively tears down that room's `stream-room-messages` subscription itself. |
| **MM** | `user_added` websocket event — sent as a **dedicated copy targeted at the new member directly** (bypasses membership-cache checks), not just a channel-wide broadcast the bot has to filter. Has `user_id`/`team_id`/`channel_id`; full channel metadata needs one follow-up REST call, non-blocking. | Same pattern: `user_removed` sent as a targeted copy to the removed user, distinct from the channel-wide broadcast. |

**Fallback (required, not optional):** MM's websocket has no gap-detection
or replay on reconnect (confirmed via issue #23332) — a missed add/remove
event during a disconnect is simply gone. Both connectors' REST list
endpoints (`subscriptions.get` w/ `updatedSince`; `GET
/api/v4/users/me/channels`) double as periodic-poll fallbacks and catch
whatever the push event missed.

**Design pattern:** hook the event when the connector supports it (both do
today) → pre-emptively create/remove the watcher on the push signal. If a
given connector doesn't have this wired up (future connectors might not),
or the event gets missed, fall back to the two safety nets that always
work regardless of platform: "new message from an unrecognized-but-matching
room" (lazy creation) and idle/expire TTLs (removal).

## Lazy watcher creation

Whether a room is genuinely brand-new or just a pre-existing room nobody's
messaged in a while, the mechanism is identical: **the first time a
message arrives for a room matching an active rule with no in-memory
processor yet, create one on the spot** — check `state.json` for an
existing (possibly dormant) session ID to resume first, else start fresh.

**Exception — startup pre-warming:** rooms with a recent
`last_processed_ts` in `state.json` (this field already exists — no schema
addition needed there) get warmed up eagerly at startup, same as today.
Purely-lazy creation is reserved for rooms with no prior activity — this
avoids adding first-message latency to rooms that are already in active
use, which would otherwise be a real regression for something like the
`#nest` room.

**Trigger point in existing code:** `MessageDispatcher.dispatch()`
(`gateway/core/dispatch.py`) currently does
`self._room_processors.get(msg.room.id, [])` and, on a miss, just logs
"No processor found for room_id=%s" and drops the message. That miss path
*is* the lazy-create trigger going forward: instead of logging-and-dropping,
check whether `room.id` matches an active rule and, if so, create the
watcher there before proceeding.

**Concurrency:** a miss on `_room_processors` can happen more than once for
the same room before creation finishes — a burst of messages right after
the room goes active, or a membership-event hook and a message arriving for
the same room at nearly the same time. Creation needs a per-room lock so
only one creation happens, with the same shape as the existing per-session
lock in `injected_context_builder.py` (`self._locks: dict[str,
asyncio.Lock]`, lazily created per key). Messages that arrive while creation
is in flight for that room should wait on the lock and get dispatched to the
newly-created processor once it's ready, rather than being dropped.

## Idle-expiry / auto-remove

- Track `last_processed_ts` (already exists) per watcher; compare against
  `session_idle_days` / effective `session_expire_days` on each check.
- Real-time removal triggers immediately on a confirmed "bot removed from
  room" event (see table above) — no reason to keep a watcher around for a
  room the bot literally can't read/post in anymore, don't wait for the
  idle timer.
- **Session-not-found handling:** if ACG tries to resume a session (dormant
  reactivation, or a session past what the agent itself would have
  retained) and the underlying agent reports it's gone — log a warning and
  auto-create a fresh session rather than hard-failing. Preserving context
  is the goal when possible, but its absence shouldn't block the watcher
  from working.

## CLI

- `agent-chat-gateway list` — defaults to showing only in-memory (active)
  watchers.
- `agent-chat-gateway list --all` — also shows dormant watchers (state
  persisted, no runtime object) and rule-matched rooms with no watcher ever
  created yet.
- New: `agent-chat-gateway expire <watcher>` — **resolved 2026-08-02.**
  Manually forces a watcher straight to the same end-state it would
  eventually reach on its own via `session_expire_days`: removes the
  in-memory watcher/processor if one is currently active, and deletes the
  persisted session ID from `state.json`. It does **not** touch rule
  matching and does **not** prevent the watcher from coming back — this is
  a cleanup/reset shortcut ("expire this now instead of waiting X days"),
  not a removal or a block. If a new message arrives for that room
  afterward, lazy creation kicks in exactly as it would for any other
  matching room with no active watcher, and a fresh session is created (no
  attempt to resume the just-deleted one). No static/dynamic distinction —
  applies uniformly to every watcher, since post-migration there's no
  structural boundary left to gate it on.

## Also removing, as part of this change

- **`WatcherConfig.session_id` (sticky session pinning).** Original use case
  ("attach to an existing offline session and continue via RC/MM") turned
  out to be unused in practice. Removing it also deletes an edge case this
  design would otherwise have needed to special-case (a pinned session
  would need explicit "never idle-expire" protection under the new
  lifecycle — moot once the field is gone). If the underlying need ever
  resurfaces, the better shape is a proper handoff capability (summarize +
  reinject into a **new** session, same family as `history_handoff` /
  issue #15's watcher-reset-continuity design) rather than literally
  pointing at someone else's session object.
- **`online_notification`/`offline_notification`.** Most IM platforms
  already show member online/offline status natively; ACG doesn't need to
  duplicate it, and it sidesteps having to design notification-suppression
  rules for routine idle-unload/reactivate cycles (which shouldn't read as
  "the agent went offline" to a room's members).
- Both are breaking config-schema changes. Follow the existing precedent
  from the `agent_defaults` → `agent_templates` migration (PR #71): a
  leftover old field/value is a hard `ValueError` at config-load time (no
  silent ignore), documented in a migration guide alongside
  `docs/migration-0.2.md`/`docs/migration-0.3.md`.

## Rollout

**Decision: single-shot migration.** Static `room: roomX` watchers migrate to
the new rule engine (a single-room rule is a strict degenerate case of the
general mechanism, so this is mechanically safe) in the same release as the
rest of this design — no additive-first staging, no separate follow-up
release for the migration. The 2026-08-02 production incidents that
motivated considering a staged rollout were caused by testing directly
against production at an unlucky moment (an unrelated active task running in
another room at the same time), not by anything about the migration approach
itself — so staging the migration wouldn't have prevented them. A separate
e2e test environment is planned for the future so testing won't need to
touch production directly at all.

**Standing constraint, regardless of scope:** do not deploy/test any part of
this on macbook-server without explicitly coordinating a low-traffic window
with the repo owner first. This applies for the life of this change, not as
a one-time reminder.

## Open items / not yet resolved

- `session_idle_days`/`session_expire_days` exact default values — not yet
  chosen. **Invariant to validate at config-load time, not just a value to
  pick later:** `session_idle_days` must be strictly less than the
  effective `session_expire_days` (which itself is `min(configured
  session_expire_days, agent's declared typical_session_retention_days())`
  when the agent declares one, e.g. Claude's 30) — a config where idle >=
  expire means the watcher jumps straight from active to gone, skipping the
  back-burner state entirely, which is silently confusing rather than
  useful. Reject at load time with a clear error, same posture as the
  breaking-change `ValueError`s elsewhere in this doc.
- Whether ACG should attempt to read the *actual* configured
  `cleanupPeriodDays` from a target machine's `~/.claude/settings.json`
  (more accurate) vs. always assuming the documented default of 30
  (simpler, but wrong if the user changed it) — flagged, not decided.
- Exact `AgentBackend.typical_session_retention_days()` interface shape
  (sync property vs async method, where OpenCode's `None` and Claude's `30`
  get wired in) — not yet designed at the code level.
- Migration guide content for the `session_id`/`online_notification`/
  `offline_notification` removals — not yet written.

## Sources (2026-08-02 research)

- RC subscription-change events, `subscriptions.get` REST fallback:
  `apps/meteor/server/lib/notifyListener.ts`, `listeners.module.ts`,
  `rooms/removeUserFromRoom.ts`, `rooms/createRoom.ts`,
  `server/api/v1/subscriptions.ts` (RocketChat/Rocket.Chat).
- MM `user_added`/`user_removed` targeted websocket events:
  `server/channels/app/channel.go`, `server/channels/app/platform/web_conn.go`,
  `server/channels/app/platform/cluster.go` (mattermost/mattermost); gap-
  detection caveat via GitHub issue #23332.
- Claude Code `cleanupPeriodDays` (default 30, `settings.json`):
  code.claude.com/docs/en/settings; corroborating GitHub issues #62959,
  #59248, #23710 on anthropics/claude-code.
- OpenCode has no session expiry (SQLite, unbounded):
  `packages/opencode/src/session/compaction.ts`,
  `tool/truncate.ts`, `snapshot/index.ts`, `packages/core/src/database/database.ts`
  (anomalyco/opencode); GitHub issues #16101, #22110, #9290.
