# On-the-fly watchers: rule-based room matching + lazy watcher lifecycle

Status (updated 2026-08-05): **config schema landed (PR #77); rule-based
room matching + lazy creation landed for Mattermost.** RocketChat support,
`session_id`/`online_notification`/`offline_notification` retirement, and
the `expire` CLI are still design-only. Coordinating a low-traffic
production test window with the repo owner is still required before any of
this touches macbook-server (see "Rollout" below) — nothing in this feature
has been deployed to production yet, regardless of what has merged.

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

### 2026-08-05 revision: the real trigger point differs by connector, and is NOT `dispatch()`

Corrected after reading the actual connector code (an Explore-agent
investigation, not assumption) while starting implementation: the original
claim above — that `MessageDispatcher.dispatch()`'s `_room_processors.get()`
miss is the lazy-create trigger point — is only true in the sense that
`dispatch()` is never reached at all for an unwatched room on EITHER
connector today. The real gate is earlier, and differs by transport:

- **Mattermost**: no wire-protocol subscribe exists. The WebSocket already
  streams `posted` events for every channel the bot is a member of,
  regardless of whether ACG is watching it. `MattermostConnector.
  _on_posted_event()` (`gateway/connectors/mattermost/connector.py:643-645`)
  is what actually drops the event: `state = self._channels.get(channel_id);
  if not state: return`. This is a **local, in-process filter on an
  already-delivered event** — the data is right there, nothing further needs
  to be fetched over the wire to see it. This is the correct, and only,
  hook point for MM's lazy creation.
- **RocketChat**: DDP requires an explicit per-room `sub` before the
  **server** will ever emit a `stream-room-messages` "changed" event for
  that room — confirmed via `websocket.py`'s `subscribe_room()`/DDP `sub`
  frame and the RC server's own stream-scoping behavior. A room ACG never
  subscribed to produces **no event at all**, not a filtered one — there is
  nothing for a "miss" hook to react to. Reactive lazy creation is
  therefore **not achievable for RC** without something else first causing
  a subscribe call for that room's ID — which is exactly what the
  `stream-notify-user`/`subscriptions-changed` membership-event hook
  (documented below) is for. **This is why RC support is scoped to a
  separate follow-up** (2026-08-04/05 discussion) — it isn't optional
  polish, it's a different mechanism (event-hook-triggered subscribe, not
  message-triggered creation) requiring its own design pass.

**Concurrency (MM): already handled by existing architecture, verified —
not something this feature needs to add.** `MattermostWebSocketClient.
_dispatch()` (`websocket.py:198-216`) routes every decoded event into a
**per-channel `asyncio.Queue`**, lazily spinning up one dedicated
`_channel_worker` task per `channel_id` the first time it's ever seen —
including a channel ACG has no state for yet. Each worker processes its
own channel's queue strictly sequentially (`await self._handler(decoded)`
before pulling the next item). Consequences, verified by reading the code
rather than assumed:
- A slow lazy-creation call inside `_on_posted_event` (a full watcher
  start: agent session provisioning, history fetch, durable-context
  delivery — realistically several seconds, potentially 30+) only delays
  **that one new channel's own queue** — every other channel already has
  its own independent worker and is completely unaffected. No global
  connector stall.
- Two messages arriving back-to-back for the same brand-new channel are
  processed one at a time by the SAME worker — the second one is not even
  dequeued until the first (which does the lazy creation) finishes. No
  race is possible for the common case.
- The one path that calls `_on_posted_event` outside this queue is
  `_on_ws_reconnect`'s replay loop (`connector.py:223`) — but it only
  replays **already-tracked** channels (`self._channels`), so it can never
  hit the "no state yet" branch a genuinely new room takes. No race there
  either.
- A defensive per-room `asyncio.Lock` (same shape as
  `injected_context_builder.py`'s `self._locks: dict[str, asyncio.Lock]`)
  is still added in `WatcherLifecycle.try_lazy_create()` — cheap, and
  removes any dependency on this connector-specific queuing behavior
  continuing to hold, especially once RC (a different concurrency model)
  reuses the same lazy-creation core logic later.

### Scope decisions made while starting implementation (2026-08-05)

- **`GatewayConfig` gets a new, separate `watcher_rules: list[WatcherConfig]`
  field — not a flag on the existing `watchers` list.** A `room: "*"` entry
  is parsed into this list instead of `watchers`. Reasoning: `sync_watchers()`
  and `_start_watcher()` both operate on `watchers`/`_watcher_configs` and
  call `resolve_room(wc.room)` — if a rule ever ended up in that list,
  `"*"` would be resolved as a literal room name at every boot. A separate
  list makes that failure impossible by construction instead of relying on
  every call site remembering to filter on a flag.
- **At most one `room: "*"` rule per connector** — enforced as a config-load
  error. Kills the "which agent wins when two rules match the same room"
  question by construction; trivially relaxable later if a real use case
  needs it.
- **`room: "*"` matches channels only, not DMs, by default.** A wildcard
  binding an agent to every DM the bot account has is surprising and very
  unlikely to be the intent of "listen to all rooms I have access to." A
  DM is still reachable by naming it explicitly (`room: "@alice"`), same as
  today.
- **History-handoff double-delivery — investigated, deliberately NOT fixed
  in this pass (revised 2026-08-05, reversing the earlier "fixed" plan
  above).** A lazily-created watcher's history-handoff fetch (`hh.enabled`,
  default `True`) will include the very message that triggered its own
  creation, which then ALSO gets processed as the live prompt — the agent
  sees it twice (once folded into the history summary, once as the live
  turn). The original plan was an `exclude_msg_id` parameter on
  `_start_watcher()`, but `Connector.fetch_room_history()`'s return shape
  (`gateway/connectors/mattermost/connector.py`) is a platform-agnostic
  `{ts, username, role, room_name, text}` dict with **no message ID at
  all** — matching by `ts` instead would mean comparing independently-
  computed timestamp strings (one from `ts_ms_to_iso_local()` in the
  history path, one from `filter_mm_message()`'s `FilterResult.msg_ts` in
  the live path) for exact equality, which is fragile: a silent mismatch
  (rounding, timezone edge case) either leaves the duplicate in place
  anyway or, worse, could over-match and drop an unrelated message that
  happens to share a timestamp. Given the actual failure mode is cosmetic
  (the agent sees one extra line of context, not incorrect behavior) and
  every candidate fix touches a shared, platform-agnostic interface used
  by both connectors' eager-startup path too, this is being left as a
  known, accepted limitation rather than shipping a fragile heuristic.
  Revisit if `fetch_room_history()` ever gains a real message ID field for
  other reasons.
- **`_sanitize_room_for_name()`/`_auto_watcher_name()` move from
  `gateway/config.py` to `gateway/core/config.py`**, re-exported from
  `gateway/config.py` for existing callers/tests. `gateway/core/
  watcher_lifecycle.py` (where the lazy-creation logic lives) is in the
  `core` package, which does not import from the top-level `gateway.config`
  module (that module imports *from* `core` and re-exports — the
  dependency only ever goes one direction); the auto-naming helpers need to
  be reachable from both sides.
- **New `Connector` method, `resolve_room_by_id(room_id: str) -> Room`** —
  the reverse of `resolve_room(name)`. Needed because the connector only
  has a raw `channel_id` at the point a message arrives for an unwatched
  room; matching it against `exclude_room:` (which is name-based, like
  every other room field in this config) requires resolving the name
  first. MM implements it via `GET /api/v4/channels/{channel_id}`; default
  raises `NotImplementedError` (only MM needs it for now).
- **Explicitly out of scope for this pass:** RC support (see above — a
  different mechanism, own follow-up); startup pre-warming of a room that
  got a lazily-created watcher in a PRIOR gateway run (its `WatcherConfig`
  only lives in `WatcherLifecycle._watcher_configs` in-memory today, not
  reconstructed from `state.json` at boot — a restart means that room's
  *next* message re-triggers lazy creation from scratch, resuming the
  persisted session correctly, just without the eager-startup latency
  optimization — **this "resuming correctly" half was a genuine bug until
  the PR #79 review round below caught it; see `WatcherState.
  dynamically_created`**). Both are real gaps, not silently dropped —
  tracked here for a later pass, not assumed solved.

### PR #79 review round (2026-08-05) — four real findings, all fixed

Codex's review of the first Mattermost lazy-creation slice caught four
issues that hand-tracing missed; all four are fixed in the same PR, not
deferred:

1. **Multi-team bot accounts weren't scoped.** Mattermost's websocket
   delivers `posted` events for every team a bot account belongs to, not
   just the configured one — `get_channel_by_id()` had no check against
   `self.team_id`, so a wildcard rule could lazily bind an agent to a room
   in the wrong team. Fixed: rejects a channel whose `team_id` doesn't
   match (DMs exempted — Mattermost gives them no meaningful team scope,
   and they're excluded from wildcard matching anyway by room type).
2. **Fail-closed didn't cover the lazy path.** `sync_watchers()` refuses to
   start a static watcher whose agent's backend/permission broker failed
   at startup (`_blocked_agents`) — `try_lazy_create()` had no equivalent
   check, so a wildcard rule could start a processor with zero permission
   enforcement. Fixed: same check, same posture, before any lock/creation
   work.
3. **A paused (or otherwise not-running) watcher for the SAME room could
   get silently resumed.** The original collision check only rejected a
   name match against a *different* room — a same-room match (a paused
   static watcher, or a previously lazy-created one with no running
   processor) fell through to building and starting a brand-new
   `WatcherConfig`, defeating an explicit `pause`. Fixed: any existing
   config for the same room with no running processor now short-circuits
   to `False` — only `pause_watcher()`/`resume_watcher()`/`reset_watcher()`
   may bring an already-known watcher back to life.
4. **A lazy watcher's state didn't actually survive a restart.**
   `sync_watchers()`'s final `save()` only ever persists `self._states`,
   built solely from `_watcher_configs` — since a lazily-created watcher's
   `WatcherConfig` is never in that list at the next boot, its persisted
   session was silently dropped by the very first restart's save, despite
   the design (and the "Explicitly out of scope" bullet above, before this
   fix) claiming resume-across-restart worked. Fixed: `WatcherState` gained
   a `dynamically_created` flag; `sync_watchers()` now carries forward any
   persisted entry with that flag set instead of treating it as removed.

### PR #79 review, second round (2026-08-05) — three more findings, all fixed

A follow-up review pass (same PR, second commit) caught three more —
two of them direct extensions of round-one's fixes into cases the first
pass hadn't covered yet:

5. **Lazy creation ran before the system-message/own-message filters.**
   `_on_posted_event()`'s existing `post.get("type")` (system message) and
   `sender_id == bot_user_id` (own message) checks sat AFTER the
   lazy-creation gate, not before — so a `system_join_channel` event (e.g.
   the bot itself being added to a channel) or an echo of the bot's own
   post in an otherwise-unwatched channel could trigger a full watcher
   creation (subscribe, session, possibly an `online_notification` post)
   before ever being recognized as noise that `filter_mm_message` would
   have rejected anyway. Fixed: both checks moved before the lazy-creation
   gate — neither needs `state`, so neither needs to wait for it.
6. **Round one's "honor paused watchers" fix only covered the same-process
   case.** After a restart, a lazy watcher's `WatcherConfig` is gone from
   `_watcher_configs` (finding #4's fix only preserves the *state*, not the
   config — reconstructing the config itself was explicitly left out of
   scope, see above) — so `get_watcher_config()` returns `None` and finding
   #3's fix never fires. The loaded `WatcherState` (now correctly resumed
   thanks to finding #4) could still have `paused=True`, but nothing
   checked it before calling `_start_watcher()`, which has no
   "am I paused" gate of its own — it always starts unconditionally. Fixed:
   a second, explicit check on the loaded `state.paused` right before
   calling `_start_watcher()`, independent of the `_watcher_configs`-based
   check above.
7. **`room: "*"` alongside a `rooms:` list was silently accepted as a
   rule.** `_is_wildcard_room_entry()` checked `room == "*"` first, without
   checking whether `rooms:` was ALSO present — `_parse_one_watcher_rule()`
   has no `rooms:` concept at all and would silently ignore it, turning a
   likely typo (leftover `rooms:` from before adding `room: "*"`, or vice
   versa) into a full wildcard rule instead of the existing, clear "set
   either 'room' or 'rooms', not both" error `_parse_one_watcher_entry()`
   already raises for this shape. Fixed: the dispatcher now returns `False`
   (not a rule) whenever both keys are present, letting that existing
   validation fire as it would have without the wildcard feature.

### PR #79 review, third round (2026-08-05/06) — two more findings, both fixed

Another follow-up pass, again finding real gaps left by the round-two
fixes rather than restating them:

8. **A paused dynamic watcher was permanently stuck after a restart.**
   Finding #6's fix correctly *refuses* to auto-resume a paused persisted
   watcher — but its log message told the operator to run
   `agent-chat-gateway resume <name>`, and that command was itself broken
   for exactly this watcher: `resume_watcher()` (and `reset_watcher()`/
   `pause_watcher()`) looked the name up via `_find_watcher_config()`
   against `_watcher_configs` only, which — same root cause as finding #6
   — never has the entry after a restart. The one supported way to bring
   the watcher back didn't work on it. Fixed: `_find_watcher_config()`
   replaced with `_find_or_reconstruct_watcher_config()`, which falls back
   to a new `_reconstruct_dynamic_watcher_config()` — re-resolves the room
   from the persisted `room_id`, re-matches it against the connector's
   active rule, rebuilds the `WatcherConfig` (shared with
   `try_lazy_create()` via a new `_build_watcher_config_from_rule()`
   helper), and — critically — seeds `self._states[name]` from the
   persisted state too, so the subsequent start correctly resumes the old
   session instead of silently creating a fresh one. Returns "genuinely
   not found" only if the persisted entry isn't `dynamically_created`, or
   the room/rule can no longer be resolved/matched (rule removed, room now
   excluded) — a real orphan, nothing to reconstruct. Applied uniformly to
   `pause_watcher()`/`resume_watcher()`/`reset_watcher()`, not just
   `resume`, for consistency.
9. **The `room`+`rooms`-both-present fix (finding #7) used truthiness, not
   key presence.** `rooms: []` and `rooms: null` are both falsy, so
   `if raw_room and raw_rooms` still let `room: "*"` with either alongside
   it through as a wildcard rule — `rooms:`'s own "must be a non-empty
   list" validation never got a chance to fire, same silent-typo risk
   finding #7 was meant to close, just for the two falsy-value cases it
   didn't cover. Fixed: `_is_wildcard_room_entry()` now checks
   `"room" in wc_raw and "rooms" in wc_raw` (key membership) instead of
   truthiness on the fetched values.

### PR #79 review, fourth round (2026-08-06) — four more findings, all fixed

The deepest pass yet — two P1s this time, both real cross-cutting gaps the
first three rounds' narrower fixes hadn't reached:

10. **Collision/pause checks looked up by generated NAME, not by ROOM.**
    `try_lazy_create()`'s existing-watcher check was
    `get_watcher_config(watcher_name)` — the auto-generated name only. A
    *static* watcher for this exact room with an explicit custom `name:`
    (e.g. `name: incident-agent` for `room: general`) would never be found
    by that lookup, so pausing/stopping it did nothing to prevent lazy
    creation from starting a SECOND watcher for the same room under the
    auto-generated name — a silently-created duplicate, and the pause
    fully defeated. Fixed: search `_watcher_configs` by `.room ==
    room.name` FIRST (any name), and only fall back to the by-name lookup
    (which now only ever needs to catch a genuinely different room
    colliding on the same generated name) once that comes up empty.
11. **Persisted state was trusted by name alone, not by room identity.**
    `state = self._state_store.load().get(watcher_name)` assumes the
    persisted entry under this name belongs to the room currently being
    resolved — but two DIFFERENT rooms can sanitize to the same
    auto-generated name across DIFFERENT runs too (not just the
    same-run race finding #10-adjacent logic already guards): a room
    whose watcher was created, then the room went quiet and its state sat
    dormant, and later ANOTHER room that happens to share the same
    sanitized name posts first. Reusing that state's `session_id` would
    bind the wrong room's conversation context onto this room — a real
    cross-room information leak, and a direct violation of the
    1-session-per-room invariant this whole feature exists to preserve.
    Fixed: compare `state.room_id` against the just-resolved `room.id`;
    a mismatch refuses the creation entirely (logged as a likely name
    collision) rather than silently reusing or silently overwriting
    whichever room's entry was there first.
12. **The finding-#8 CLI fix was itself unreachable through the CLI.**
    `resume_watcher()`/etc. becoming reconstruction-aware only helps if
    something actually calls them — but `ControlServer._find_entry_for_watcher()`
    (`gateway/control.py`) resolves which connector owns a watcher name via
    a plain, synchronous `get_watcher_config()` check across all entries
    *before* routing the command at all. A dynamically-created watcher's
    config is gone from that list after a restart, so this pre-check
    returned "unknown watcher" and `resume_watcher()` was never reached —
    finding #8's fix worked when called directly (as the unit tests do)
    but not through the one real interface operators use. Fixed: rather
    than making the widely-used `_find_entry_for_watcher()` itself async
    (it has two other call sites and several tests mocking it
    synchronously — a much larger, riskier change for this one caller),
    added a narrow fallback at the `pause`/`resume`/`reset` dispatch site
    only: on a miss, try every entry's new async
    `can_find_or_reconstruct_watcher()` probe before giving up.
13. **Lazy-created names were never checked against OTHER connectors.**
    Static watcher names are enforced globally unique across every
    connector at config-load time (`gateway/config.py`'s
    `seen_watcher_names`, shared across the whole file, not per-connector)
    — `ControlServer`'s routing and the scheduler both depend on that
    invariant staying true. `try_lazy_create()`'s checks only ever see
    `self._watcher_rules`/`_watcher_configs` for ITS OWN connector, so a
    lazily-created name could collide with a different connector's
    watcher without either connector ever noticing. Fixed: a new
    `check_global_name_available` callback, threaded from
    `GatewayService` (the only layer with cross-connector visibility) down
    through `SessionManager` to `WatcherLifecycle` — a closure over
    `GatewayService._entries`, read at CALL time so it works correctly
    even though it's handed to each `WatcherLifecycle` before
    `_entries` is fully populated (construction is one connector at a
    time, in the same loop that appends to it). Defaults to "always
    available" for any caller/test with no `GatewayService` above it.

### PR #79 review, fifth round (2026-08-06) — two more findings, both fixed

Round four's fixes were correct as far as they went; round five found the
next layer down — an identifier assumed stable that isn't, and a flag that
didn't survive the very operations meant to preserve a watcher:

14. **Room-based matching (finding #10) used the room's NAME, which isn't
    stable.** `existing_for_room` matched `wc.room == room.name` — but a
    Mattermost channel can be renamed while its watcher is paused. The
    just-resolved `room.name` then no longer matches the config's stale
    stored name, even though `room.id` (the platform's actual stable
    identifier) is unchanged — so a renamed room's paused watcher became
    invisible to the very check finding #10 added, and lazy creation
    would create a duplicate under the new name, right back to the
    original bug finding #10 was fixing. Fixed: when the by-name check
    misses, fall back to matching by `room.id` via each watcher's
    `WatcherState.room_id` (populated from the real resolved room every
    time `_start_watcher()` runs, and — thanks to finding #15 below — now
    survives resume/reset too). A state with an empty `room_id` (a watcher
    paused via the CLI's own not-found fallback, before ever actually
    starting) is excluded from this check — nothing to confirm a match
    against.
15. **The `dynamically_created` marker didn't survive a resume/reset.**
    `_start_watcher()` always builds a brand-new `WatcherState` — finding
    #4's fix set the flag once, right after the INITIAL lazy creation, but
    `_start_watcher()` itself never copied it forward from the incoming
    `state` on any LATER call. The moment a dynamically-created watcher
    was legitimately resumed or reset even once, its state's marker
    silently reset to `False` (the dataclass default) — so
    `sync_watchers()` would then drop its state on the *next* restart
    after all, defeating finding #4's fix for any dynamic watcher that's
    ever been resumed/reset. Fixed at the source: `_start_watcher()` now
    copies `state.dynamically_created` forward (defaulting to `False` only
    when there's no incoming `state` at all) — every caller (lazy
    creation, resume, reset, sync_watchers) benefits automatically,
    instead of needing to remember to re-apply the flag itself.

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

- **`gateway/config_validate.py`'s `_check_state_orphans()` doesn't know
  about lazily-created watchers (found 2026-08-05, not fixed).** It builds
  its "is this state.json entry still configured" set from `config.watchers`
  only — a lazily-created watcher's `WatcherConfig` lives in
  `WatcherLifecycle._watcher_configs` at runtime, never written back to
  `config.yaml`, so its persisted state will always look like an orphan to
  this check. The warning it prints is actively misleading for this case
  ("its session/pause state will be dropped on next start" — it isn't; the
  next message for that room re-triggers `try_lazy_create()`, which resumes
  the persisted session exactly as it would for any dormant static
  watcher). Same "TUI/tooling doesn't fully understand rules yet" scope
  boundary as the `expanded_watchers()` item just below — not fixed in the
  PR that added lazy creation, since it's config-tool-surface work, not
  core lifecycle work; needs its own pass once rules have more general TUI
  support.
- **`gateway/configtool/model.py`'s `EditableConfig.expanded_watchers()`
  (drives the config TUI's Watchers table) silently drops a `room: "*"`
  rule's row.** It calls `_parse_one_watcher_entry()` directly, per raw
  entry, bypassing the `from_file()`/`collect_config()` dispatch that
  routes a wildcard entry to `_parse_one_watcher_rule()` instead — a rule
  entry falls through to `_parse_one_watcher_entry()`'s defensive
  "must be parsed by `_parse_one_watcher_rule()`" `ValueError`, which
  `expanded_watchers()`'s per-entry `except ValueError: continue` then
  silently swallows. Fails safely (no crash, no data corruption — the rule
  still parses and works correctly for the actual gateway, only the TUI's
  table is missing a row for it), but a rule is currently invisible in the
  config TUI entirely. Not fixed here — needs a real "how does the TUI
  represent/edit a rule" design, out of scope for the lazy-creation PR.
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
- **Config-tool: no way to write an explicit `null` override for an
  inherited scalar field (found during PR #77 review, 2026-08-04).**
  `session_idle_days`/`session_expire_days` are inheritable via
  `agent_templates:`, and the parser already correctly honors an explicit
  `session_idle_days: null` in `config.yaml` as "disable the inherited
  value for this one agent" (`_deep_merge()`'s existing explicit-null-
  suppresses-base contract). But the config TUI's form has no way to
  produce that state: blanking the input (or the generic `ctrl+r` reset)
  both mean "revert to inherited," never "explicitly disable it." This is
  a **pre-existing, general gap** — `online_notification`/
  `offline_notification`/`session_id` have the identical limitation today
  — not something introduced by these two fields, and not fixed as part of
  this PR. A proper fix needs a new, distinct UI action ("set to null",
  separate from "revert to inherited") applied consistently to every
  nullable inheritable field, which is real product-design work, not a
  quick patch — worth a dedicated follow-up rather than a one-off hack
  scoped to just these two TTL fields. Until then, the workaround is
  hand-editing `config.yaml` directly. Tracked as
  [issue #78](https://github.com/HammerMei/agent-chat-gateway/issues/78).

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
