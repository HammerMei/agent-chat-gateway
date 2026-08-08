# On-the-fly watchers: rule-based room matching + lazy watcher lifecycle

Status (updated 2026-08-07, round 11): **config schema landed (PR #77); rule-based
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

### PR #79 review, sixth round (2026-08-06) — two more findings, both fixed

Round six moved past the lazy path entirely and found two gaps in code
this feature's earlier rounds hadn't touched at all — `sync_watchers()`'s
own static-watcher startup loop, and a genuine `asyncio` concurrency
hazard between it and the lazy path running alongside it:

16. **`sync_watchers()`'s startup loop could double-start a concurrently
    lazily-created watcher.** `for wc in self._watcher_configs:` iterates
    the list directly — but the Mattermost websocket listen loop is
    already running as a background task by the time `sync_watchers()` is
    called (`SessionManager.run_once()` calls `connector.connect()`,
    which starts it, *before* calling `sync_watchers()`). If a message
    completes `try_lazy_create()` for some other room WHILE this loop is
    still `await`-ing an earlier static watcher's own `_start_watcher()`
    call, that lazy path's `self._watcher_configs.append(wc)` mutates the
    exact list object this `for` loop is iterating — Python's list
    iterator picks up items appended during iteration, so the loop would
    eventually reach and revisit that already-fully-started lazy watcher,
    calling `_start_watcher()` on it a SECOND time. `_processors[name]`
    gets silently overwritten with a new `MessageProcessor`, but
    `MessageDispatcher.add_processor()` *appends* rather than replaces, so
    the first processor stays registered too — two live processors for
    the same room (duplicate agent responses), and the first one becomes
    an orphan, no longer reachable via `_processors`, never stoppable
    again. Fixed: `for wc in list(self._watcher_configs):` — a plain
    snapshot. Nothing appended to the real list during iteration is ever
    visited by this loop, which is exactly what's wanted — the lazy path
    already fully started that watcher itself.
17. **`sync_watchers()`'s static-watcher path had no equivalent of the
    lazy path's room_id mismatch check.** `persisted.get(wc.name)` looks
    up retained state by NAME alone, with no room check — if that name
    later gets assigned to a static config entry for a *different* room
    (a config edit, the wildcard rule being removed, or a sanitized-name
    collision), `_provision_session()` would reuse the old room's
    `session_id` before `_start_watcher()` ever overwrites `room_id`,
    leaking that room's conversation context into the new one — the exact
    same class of bug as finding #11, just in the one code path that
    fix's location (inside `try_lazy_create()` only) never covered. Fixed
    at a more central point this time: right after `_start_watcher()`
    resolves `room` (its very first step), a `state.room_id != room.id`
    mismatch discards `state` entirely (sets it to `None`) rather than
    refusing to start — unlike the lazy path, a *configured* watcher must
    still end up running one way or another, so the safe response is a
    fresh session, not a hard failure. Placed inside `_start_watcher()`
    itself (not duplicated per-caller) so every caller (`sync_watchers()`,
    `resume_watcher()`, `reset_watcher()`, `try_lazy_create()`) is covered
    by the same one check — `try_lazy_create()`'s own earlier, stricter
    check (refuse outright) still fires first for that path regardless,
    so this new one is simply never reached with a mismatched state from
    there.

### PR #79 review, seventh round (2026-08-07) — two more findings, both fixed

Round seven went back to the fourth round's cross-connector name check
(`GatewayService._is_watcher_name_globally_available()`) and found it was
still incomplete in two independent ways — one a coverage gap, one a
genuine concurrency race:

18. **The global name check missed dormant dynamic watchers.** A
    dynamically-created watcher's `WatcherConfig` is never persisted, only
    its `WatcherState` (via `dynamically_created`) — after a restart, a
    lazy watcher that hasn't yet received a message to reconstruct itself
    has no entry in any connector's `_watcher_configs`, so the old
    `get_watcher_config()`-only check considered its name available. A
    newly added connector, or a rule generating the same composite name,
    could claim it before the dormant watcher ever woke up — later lazy
    creation attempts and unqualified lifecycle commands (`pause`/
    `resume`/`reset <name>`) would then resolve to the wrong owner, or the
    original watcher would become unreachable under its own name. Fixed by
    switching the check from `get_watcher_config()` (plain, non-
    reconstructing) to `can_find_or_reconstruct_watcher()` — the same
    reconstruction-aware probe `ControlServer` already relies on for
    routing pause/resume/reset — which resolves the room and rebuilds a
    dormant dynamic watcher's config (and seeds its state) from persisted
    state if one matches, then reports it as found.
19. **The global name check was a plain, unsynchronized read — a genuine
    cross-connector TOCTOU race.** Each `WatcherLifecycle` only ever
    serializes lazy creation for a given name against ITSELF, via its own
    per-connector `_get_watcher_lock()`; nothing serialized the check
    ACROSS different `WatcherLifecycle` instances. Two connectors
    receiving their first message for room/connector pairs that sanitize
    to the same composite name could both call the check concurrently,
    both observe "available," and both proceed to `_start_watcher()`
    before either had registered anything — leaving two watchers live
    under the one name every other part of the system assumes is unique.
    Fixed by replacing the plain read with an atomic reserve/release pair
    owned by `GatewayService`: `_reserve_watcher_name()` holds a single
    lock shared by every connector (`self._watcher_name_lock`, deliberately
    NOT per-connector — the race is *between* connectors) across the
    whole check-then-reserve sequence, and tracks in-flight reservations
    in `self._reserved_watcher_names` so the window between "check passed"
    and "watcher durably registered in `_watcher_configs`" is covered too,
    not just the initial read. `try_lazy_create()` now calls
    `reserve_global_name()` in place of the old
    `check_global_name_available()` and wraps everything after it in a
    `finally: release_global_name()` — released on every exit path
    (success, refusal, or a `_start_watcher()` failure), since holding a
    reservation past its need would blackhole that name for every future
    attempt, on every connector, after a single transient failure.

### PR #79 review, eighth round (2026-08-07) — two more findings, both fixed

Round eight found two ways the seventh round's own fixes over-corrected —
both are cases where an un-paused dynamic watcher legitimately reclaiming
its OWN identity got treated as a collision with itself (or lost its
identity entirely), rather than a genuinely different owner:

20. **A dormant (un-paused) dynamic watcher couldn't reclaim its own
    name.** The seventh round's cross-connector reservation
    (`reserve_global_name()`) fans out to
    `can_find_or_reconstruct_watcher()` on EVERY connector, including the
    very one making the call — for the ordinary "next message after a
    restart" case (same room, same auto-generated name, not paused), that
    probe would truthfully reconstruct and report the watcher as "already
    occupied" — by itself — and refuse forever, since nothing else in
    `try_lazy_create()` would ever call it again for that name.
    Previously-dormant-but-legitimate watchers would get permanently stuck
    needing a manual `resume`, exactly the failure mode the seventh round's
    fix was supposed to prevent for OTHER connectors, now inflicted on the
    watcher's own connector instead. Fixed by loading this connector's own
    persisted state under `watcher_name` *before* deciding whether to
    reserve at all: if that state's `room_id` already matches the room
    just resolved, this is unambiguously a self-reclaim, not a collision —
    skip the reservation entirely and go straight to the existing
    resume-a-dormant-session logic. **(Superseded by the ninth round below —
    skipping the reservation entirely turned out to have its own bug; the
    fix moved to excluding just the requesting connector from the check,
    not skipping the check.)** A genuinely different room's stale
    state colliding on the same name is unaffected (its `room_id` won't
    match, so it still goes through the full reservation and is caught by
    the existing room_id-mismatch refusal). The `finally: release_global_name()`
    from the seventh round now only fires when a reservation was actually
    taken (tracked via a `reserved` flag) — unconditionally releasing would
    risk clearing a *different*, unrelated caller's legitimate in-flight
    reservation for that same name.
21. **The fifth round's stable-room-ID fallback silently dropped a renamed
    watcher's identity instead of reconstructing it.** When a dynamic
    watcher's channel is renamed, `existing_for_room`'s primary check
    (`wc.room == room.name`) naturally misses it (name changed), so it
    falls back to matching by `state.room_id` — but that fallback then
    calls the plain, non-reconstructing `get_watcher_config()`, which
    returns `None` for a dynamic watcher whose config was never
    reconstructed (only its `WatcherState` survived, same premise as
    finding #18). The code read that `None` as "no existing watcher here,"
    fell through, and would have gone on to build a *brand-new* config
    under the freshly-generated name — abandoning the old session and
    silently bypassing a persisted pause. Fixed by reconstructing (via
    `_reconstruct_dynamic_watcher_config()`) whenever the stable-ID match's
    `get_watcher_config()` lookup misses AND the matched state's own
    `watcher_name` differs from the one just computed for the current room
    name (i.e., a rename actually happened) — the reconstructed watcher
    then correctly falls into the existing "known but not running, needs
    explicit `resume`" branch. Deliberately scoped to the renamed case
    only: when the name is unchanged, this is finding #20's ordinary
    self-reclaim case, and reconstructing-and-blocking here too would
    reintroduce that exact bug via a different code path.

Both findings share a root cause worth naming explicitly: the seventh
round added machinery (`can_find_or_reconstruct_watcher`,
`_reconstruct_dynamic_watcher_config`) that treats "this name is
reconstructable" as equivalent to "this name is occupied by someone else,"
which is correct for cross-connector collisions but wrong whenever the
someone else turns out to be the watcher's own past self. The fix in both
cases is the same shape: detect the self-reclaim case *before* invoking
that machinery, using a property that can't lie about identity —
`state.room_id` — rather than trying to teach the reconstruction machinery
itself to tell "self" from "other."

### PR #79 review, ninth round (2026-08-07) — two more findings, both fixed

Round nine went back to the eighth round's own fix for finding #20 (skip
the cross-connector reservation entirely on a self-reclaim) and found it
over-corrected in two ways — skipping the check ENTIRELY, rather than
just excluding the requesting connector from it, threw out real
protection along with the false positive:

22. **Skipping the reservation for a self-reclaim also skipped checking
    every OTHER connector.** If a dynamic watcher survives a restart
    (dormant, un-paused) and, in the meantime, a *new* static watcher gets
    configured on a *different* connector using the exact same composite
    name — which passes that connector's own config-load-time uniqueness
    check, since dormant dynamic state living only in a `WatcherState` is
    invisible to `config.watchers` — the eighth round's fix would let the
    next message in the dynamic watcher's own room skip the reservation
    entirely (because `reclaiming_own_state` was true) and start it
    directly, without ever asking whether some OTHER connector had since
    claimed the same name. The result: two connectors both durably own a
    watcher under the one name every other part of the system
    (`ControlServer._find_entry_for_watcher()`, the scheduler) assumes is
    globally unique — unqualified lifecycle commands and scheduled jobs
    can silently resolve to the wrong connector. Fixed by removing the
    skip-the-reservation-entirely branch and instead making the
    reservation call itself connector-aware: `GatewayService` now binds a
    *separate* `reserve_global_name` callback per connector via
    `functools.partial(self._reserve_watcher_name, requesting_connector=cc.name)`,
    and `_reserve_watcher_name()`'s scan **excludes only the requesting
    connector's own entry** — not the check as a whole. `try_lazy_create()`
    now calls `reserve_global_name()` unconditionally again, same as the
    seventh round, but the false-positive self-block is gone because the
    self-check that caused it is gone, not because the whole check is
    skipped. Every OTHER connector is still fully checked, closing the gap
    this round found.
23. **Merely probing a name during that scan could permanently disable its
    legitimate owner.** The seventh round's reservation scan used
    `can_find_or_reconstruct_watcher()` to see persisted dynamic state on
    other connectors — but that method's reconstruction is a deliberate
    *mutating* side effect (documented and correct for `ControlServer`,
    which immediately acts on a positive result). Reusing it here meant a
    connector B merely asking "does connector A already have this name?"
    would eagerly reconstruct A's dormant watcher's `WatcherConfig` into
    A's own `_watcher_configs` — unstarted, purely as a side effect of B's
    probe, whether or not A's watcher was paused. The next genuine message
    for A's own room would then find that eagerly-registered-but-never-
    started config via `existing_by_name`/`existing_for_room` and treat it
    as an explicitly stopped watcher needing manual `resume` — even though
    its persisted state had `paused=False` the whole time. A cross-
    connector availability check silently disabling the very watcher it
    was checking about, on a connector that never asked for anything, is
    the same "reconstruction side effect leaks past its intended caller"
    class of bug as finding #22, just one layer deeper. Fixed by adding
    `WatcherLifecycle.is_watcher_name_known()` / `SessionManager
    .is_watcher_name_known()` — a **non-mutating** peek (checks
    `get_watcher_config()`, then a raw `dynamically_created` flag on the
    persisted state, with no room resolution or registration) — and
    switching `_reserve_watcher_name()`'s scan to call that instead of
    `can_find_or_reconstruct_watcher()`. `ControlServer`'s own use of
    `can_find_or_reconstruct_watcher()` for pause/resume/reset routing is
    unchanged — that caller *does* intend to act on a positive result
    immediately, so the mutation there is correct and desired.

With this round, `try_lazy_create()`'s reservation call is unconditional
again (matching the seventh round's shape), and the self-vs-other
distinction that findings #20/#22/#23 all turn on lives entirely on the
`GatewayService` side, expressed as "which connector is asking," not as
special-cased logic inside `WatcherLifecycle` about what it's asking
about.

### PR #79 review, tenth round (2026-08-07) — three more findings, all fixed

Round ten found two remaining gaps in the lazy-creation feature's own
logic (not this time a regression from a prior round's fix) plus one gap
in config-time validation:

24. **The startup window itself could still lose a renamed watcher's
    identity.** `SessionManager.run_once()` calls `connector.connect()` —
    which starts the Mattermost websocket listener as a background task —
    *before* calling `sync_watchers()`, and `sync_watchers()` only copies
    a dormant dynamic watcher's persisted state into `self._states` as its
    very LAST step (after every static watcher has already started). A
    message for a renamed dynamic watcher's room arriving anywhere in that
    window would find `self._states` empty or incomplete, miss the stable-
    room-ID fallback (finding #21) entirely, and then also miss the
    "resume a dormant session" step (which looks up disk state by the NEW,
    post-rename name — the persisted entry is still filed under the OLD
    one) — silently creating a brand-new watcher/session under the new
    name, abandoning the old one and bypassing a persisted pause. Fixed by
    falling back to a direct disk read (`self._state_store.load()`) when
    the in-memory `self._states` search comes up empty — disk is always
    complete/authoritative this early in the process's life, and the
    "resume a dormant session" step a few lines below already reads disk
    directly for the identical reason.
25. **A dynamic watcher's state was preserved forever, even after its
    wildcard rule was removed entirely.** `sync_watchers()`'s startup
    preservation loop (the fourth/sixth round's fix) copies any persisted
    `dynamically_created` entry into `self._states` unconditionally,
    regardless of whether this connector still has an active `room: "*"`
    rule to match it against. If the operator removes the rule,
    `_reconstruct_dynamic_watcher_config()` explicitly refuses to bring
    that watcher back (nothing left to match) — so preserving its state
    kept it, and the disk write that follows, alive forever with zero path
    back. Worse, `is_watcher_name_known()` (ninth round) would keep
    reporting that name as globally taken forever too, permanently
    blocking any OTHER connector from ever claiming a generated name that
    can never actually run again. Fixed by gating the preservation on
    `self._watcher_rules` being non-empty — an orphaned entry with no rule
    left to reconstruct against is now pruned on the next save, same
    treatment any other genuinely-removed watcher already got.
26. **A `room: "*"` rule was accepted for connector types that can never
    trigger it.** `try_lazy_create()` is only ever called via a connector's
    own `register_lazy_creation_hook()` override — the base
    `Connector.register_lazy_creation_hook()` is a no-op, and currently
    only Mattermost overrides it (RocketChat's own lazy-creation support,
    via a membership-event-hook-triggered subscription, is a separate,
    not-yet-built follow-up). Config loading accepted a `room: "*"` rule
    for ANY connector type, including RocketChat — the gateway would boot
    successfully, log nothing wrong, and silently drop every message on
    that connector forever, with no error anywhere pointing at why the
    documented "listen to all rooms" behavior never happens. Fixed by
    validating the resolved connector's `type` against a new
    `_LAZY_CREATION_SUPPORTED_CONNECTOR_TYPES = {"mattermost"}` set in
    `_parse_one_watcher_rule()`, raising a clear `ValueError` at
    config-load time (or a collected `ConfigIssue` via `collect_config()`)
    instead.

### PR #79 review, eleventh round (2026-08-07) — two more findings, both fixed

Round eleven found one more instance of the tenth round's "startup window"
class of bug (a different piece of state left uninitialized during the
same `connect()`-before-`sync_watchers()` gap), plus a narrower variant of
the tenth round's own finding #25:

27. **The fail-closed blocked-agents guard was itself empty during the
    startup window.** Same root cause as finding #24, a different victim:
    `self._blocked_agents` (the set `try_lazy_create()` and
    `sync_watchers()` both fail closed against, per the original review
    round's fix) was previously populated ONLY inside `sync_watchers()` —
    which, like the dynamic-state preservation in finding #24, only runs
    *after* `connector.connect()` starts the Mattermost websocket
    listener. A message arriving in that window would pass the
    fail-closed check trivially (the set was still empty, straight from
    `__init__`), lazily creating a watcher for an agent whose permission
    broker genuinely failed to start — running with zero tool-call
    enforcement, the exact hole the original fail-closed check was built
    to close. Fixed by adding `WatcherLifecycle.seed_blocked_agents()` and
    calling it from `SessionManager.run_once()` *before*
    `connector.connect()` — `sync_watchers()`'s own identical assignment
    is left unchanged (harmlessly redundant on the normal path; still
    correct for a hypothetical hot-reload re-call with no
    `unavailable_agents` passed).
28. **A dynamic watcher's state was preserved even after its OWN room was
    added to `exclude_room:`.** The tenth round's finding #25 fix gated
    preservation on "does this connector have SOME active wildcard rule,"
    which isn't the same question as "would THIS watcher's specific room
    still match that rule." If the operator excludes this exact room
    without removing the rule entirely, every future message for it is
    rejected by `try_lazy_create()`'s own exclusion check, and
    `_reconstruct_dynamic_watcher_config()` refuses it too — just as
    unreachable as the fully-removed-rule case, just a narrower trigger,
    and with the same consequence (an orphaned session, plus a generated
    name `is_watcher_name_known()` reports as taken forever). Fixed by
    adding `_dynamic_state_still_matches_rule()`, which resolves the
    room by its stable ID and checks the CURRENT name against
    `exclude_rooms` — with a fast path that skips the network round-trip
    entirely when the rule has no exclusions at all (the common case),
    and fails toward preserving (not pruning) on a resolution error,
    matching `_reconstruct_dynamic_watcher_config()`'s own best-effort
    posture — a transient network blip during startup must not
    permanently discard a legitimate session.

## Startup ordering: root-cause design review (2026-08-07, IMPLEMENTED on `feature/mm-startup-ordering`, branched off `feature/lazy-watcher-mm-creation`)

Eleven review rounds in, a pattern was worth stepping back for instead of
patching the next instance: **rounds ten and eleven found four findings
that look superficially identical** (#24, #25, #27, #28), but they are
actually two different bug classes, and only one of them is a startup-
ordering problem at all.

- **#24 (`_states` not seeded) and #27 (`_blocked_agents` empty) are the
  real ordering bugs.** Both are symptoms of the exact same root cause:
  `SessionManager.run_once()` calls `connector.connect()` — which, for
  Mattermost, opens the websocket and starts its listen loop as a
  background task — *before* `sync_watchers()` finishes populating
  `self._states` / `self._blocked_agents`. Any piece of `WatcherLifecycle`
  state that `sync_watchers()` is responsible for initializing is exposed
  to this same window; #24 and #27 are just the first two pieces of state
  the review happened to find first, not an exhaustive list. A third,
  currently-undiscovered piece of shared mutable state populated inside
  `sync_watchers()` would reproduce this exact bug shape again.
- **#25 (rule removed) and #28 (room excluded) are NOT ordering bugs.**
  They're a wrong *predicate* in `sync_watchers()`'s dynamic-state
  preservation logic — "preserve if some rule exists" instead of "preserve
  if THIS watcher's room still matches the active rule." They would exist
  identically even with perfect startup ordering, because the bug is in
  what gets checked, not when. Their fixes (gate on `self._watcher_rules`
  non-empty; then further gate on `exclude_rooms`) are independently
  correct and **out of scope for this redesign** — nothing below touches
  them.

So the actual question is: how to structurally close the #24/#27-shaped
bug class, rather than continuing to find and patch its next instance one
piece of state at a time.

### Options considered

**A — Split every connector's `connect()` into an auth phase and a
"start receiving events" phase, uniformly.** Rejected: RocketChat's DDP
transport requires an actual `sub` message sent over the *already-open*
connection to receive events for a room at all (see "Room discovery +
membership-event hooks" above) — its `subscribe_room()` cannot be
meaningfully separated from having a live connection the way Mattermost's
can. Forcing a uniform two-phase contract onto a connector whose protocol
doesn't have that shape would be solving a Mattermost-specific problem by
distorting the shared `Connector` ABC for everyone.

**B — A single readiness gate (`asyncio.Event`) inside `WatcherLifecycle`,
awaited at the top of `try_lazy_create()` and set at the end of
`sync_watchers()`.** Closes the bug class (any future state gets the same
protection for free) without touching the `Connector` ABC at all. But it
serializes *against* the race window instead of removing it, which brings
its own problems:
- The websocket is still live and dispatching during the gap — this
  option holds the triggering message's processing coroutine hostage
  (via the per-channel worker queue) rather than preventing the event
  from arriving in the first place. A slow `sync_watchers()` (many static
  watchers, slow REST calls) delays the first lazy-creation attempt in a
  new room by however long the whole static startup loop takes — likely
  fine, but a real, user-visible latency characteristic that option C
  doesn't have.
- The gate must be released even when `sync_watchers()` raises partway
  through, or when `run_once()` never reaches it at all (an earlier
  `connect()` failure) — a bare `self._ready.set()` at the end of
  `sync_watchers()` is not enough; it needs a `finally` at minimum, plus a
  deliberate decision about what "gate released after a failure" means
  (proceed with incomplete state? refuse and drop the message?) —
  currently unresolved, and either answer is a real design choice, not a
  detail.
- Every existing `try_lazy_create()` unit test (~50 in
  `test_watcher_lifecycle_lazy_create.py`) constructs a `WatcherLifecycle`
  without ever calling `sync_watchers()` first — `_make_lifecycle()` would
  need to default-set the event, or all of them hang forever. Mechanical,
  but it means the test harness stops exercising the real gate at all,
  and the one new test that WOULD exercise it (does the gate actually
  block/release correctly) has to be written from scratch with no
  existing pattern to extend.

**C — RECOMMENDED. Give `Connector` a new `start_realtime()` lifecycle
method (default no-op, same pattern already used for
`register_capacity_check()`/`register_lazy_creation_hook()`), and move
Mattermost's websocket startup into it.** `MattermostConnector.connect()`
currently ends with:
```python
await self._ws.connect()
await self._ws.start()
```
These two lines move into `start_realtime()`; everything before them
(REST `authenticate()`/`get_me()`/`resolve_team()`) stays in `connect()`.
`SessionManager.run_once()` reorders to `connect()` → `sync_watchers()` →
`start_realtime()`. This removes the window entirely rather than
serializing against it: no `posted` event can arrive before
`self._states`/`self._blocked_agents` (or any future state
`sync_watchers()` owns) are fully populated, because the socket that
would deliver it isn't open yet. Verified safe by reading
`gateway/connectors/mattermost/websocket.py` end to end (not assumed):
- `subscribe_room()` (called by every `_start_watcher()` during
  `sync_watchers()`) makes **no wire-protocol call at all** — its own
  docstring says so, confirmed by reading it: "No wire-protocol call: the
  WebSocket already streams every channel the bot is a member of. This
  just registers local dispatch state." It only touches
  `MattermostConnector._channels` (a local dict) and calls
  `self._ws.register_channel(room.id)`.
- `MattermostWebSocketClient.register_channel()` is `self._registered_channels.add(channel_id)`
  — a bare set insert. `_registered_channels` is write-only in this file
  (grepped: only ever added-to/discarded-from, never read) — it isn't
  even the mechanism that filters incoming events (that's
  `MattermostConnector._channels`, checked in `_on_posted_event`). Neither
  touches `self._ws` (the actual socket object) in any way.
- Therefore `sync_watchers()`'s entire static-watcher-starting path
  (resolve room via REST, register local channel state, start the
  processor) has zero dependency on the websocket being open. No
  buffer-and-flush shim is needed — this is a clean two-line move, not a
  redesign of `subscribe_room()`.
- RocketChat, Script, and Voice inherit the default no-op `start_realtime()`
  — their `connect()` is completely unchanged, so they're provably
  unaffected by this change. RC's own DDP subscribe-needs-a-live-
  connection constraint (which sank option A) never comes into play here,
  because RC doesn't support `room: "*"` rules at all (finding #26) — this
  fix only needs to matter for a connector that actually calls
  `register_lazy_creation_hook()`.

**Decision: C, approved by 老哥 and implemented.** Round ten and eleven's
fixes for #24 (disk fallback in the stable-room-ID search) and #27
(`seed_blocked_agents()`) are **kept as defense-in-depth**, not reverted —
correct as a second line of defense even once the ordering itself is
fixed (e.g. if a future connector or code path reintroduces a similar
gap). #25/#28 are untouched, per the framing above.

### Process notes

- **This lives in a separate branch/PR, not folded into #79.** #79 is
  already eleven review rounds and 28 findings deep; a change to
  `Connector`'s ABC and `SessionManager.run_once()`'s call order is a
  distinct, separable change from "lazy creation for Mattermost," and
  keeping it separate makes both easier to review and easier to revert
  independently if either turns out to have a problem in production.
  Branched off `feature/lazy-watcher-mm-creation` (not `main`) because the
  bug being fixed only exists once that branch's code
  (`try_lazy_create()`, `register_lazy_creation_hook()`,
  `seed_blocked_agents()`) is present — `main` has no lazy-creation hook
  wired up yet, so this race is inert there. This second branch is
  intended to merge INTO `feature/lazy-watcher-mm-creation` before (or as
  part of) that branch's own merge to `main` as #79 — not merge to `main`
  independently first.
- **The existing rollout constraint applies here too, arguably more
  strictly.** Per the "Rollout" section below, nothing in this feature has
  touched macbook-server yet and a low-traffic test window must be
  coordinated with the repo owner first. A change to *startup sequencing*
  specifically is a class of change unit tests alone cannot fully
  validate (ordering bugs are, by definition, about real-world timing) —
  this should get at least one live low-traffic startup cycle watched
  directly before being trusted, independent of the general rollout gate.

### Implementation

- `gateway/core/connector.py`: added `Connector.start_realtime()` —
  non-abstract, default no-op, same pattern as
  `register_capacity_check()`/`register_lazy_creation_hook()` above it.
- `gateway/connectors/mattermost/connector.py`: `connect()` now does REST
  auth only (`authenticate()`/`get_me()`/`resolve_team()`) plus
  registering the websocket handler/reconnect callback (pure local
  bookkeeping, confirmed by reading `MattermostWebSocketClient
  .register_handler()`/`set_reconnect_callback()` — neither touches the
  socket). `await self._ws.connect()` / `await self._ws.start()` moved
  into a new `start_realtime()` override.
- `gateway/core/session_manager.py`: `run_once()` now calls
  `connector.connect()` → `lifecycle.sync_watchers()` →
  `connector.start_realtime()`, in that order (previously `connect()`
  did everything, `sync_watchers()` ran after).
- **One deliberate, minor behavior change worth naming**: previously, a
  WebSocket-open failure happened INSIDE `connect()`, before
  `sync_watchers()` ever ran — no watcher work was attempted at all if
  the socket couldn't open. Now, `sync_watchers()` runs (and can fully
  succeed — provisioning sessions, registering local channel state) even
  if the LATER `start_realtime()` call then fails to open the socket. The
  failure still surfaces identically to before (`start_realtime()`'s
  exception propagates out of `run_once()` exactly like `connect()`'s
  used to, caught the same way by `GatewayService.run()`'s
  `asyncio.gather(..., return_exceptions=True)`), and cleanup is
  unaffected (`GatewayService.run()`'s `finally: await self.shutdown()`
  unconditionally tears down every entry regardless of how far startup
  got, and `SessionManager.shutdown()` → `WatcherLifecycle.stop_all()` /
  `connector.disconnect()` are both already safe to call on partially- or
  never-started state). Net effect: on a websocket-open failure, more
  work happens before the failure surfaces than before, but the
  end state (fully torn down, error reported) is identical.
- Tests: `tests/unit/test_connector.py` (`start_realtime()` default is an
  awaitable no-op), `tests/unit/test_mattermost_connector.py`
  (`TestConnectStartRealtimeSplit` — `connect()` does REST auth and
  registers callbacks but never touches the socket; `start_realtime()`
  opens it and doesn't repeat REST auth), `tests/unit/test_session_manager_commands.py`
  (`run_once()` calls `start_realtime()`, and specifically calls it AFTER
  `sync_watchers()`, verified via real call-order tracking, not just
  individual `assert_called` checks).

### PR #80 review round (2026-08-07) — Finding A is a genuine regression in the design above, corrected

GPT/Codex review on PR #80 itself (the branch implementing option C above)
found two issues. Both were verified against actual code before being
accepted, per the standing rule for every round in this document — and this
one is worth being unusually explicit about, because the finding is against
a design that had already been through advisor consultation and a three-
option comparison, not against a quick patch.

- **Finding A (P1) — confirmed real, and it falsifies the "removes the
  window entirely" framing above.** Moving `_ws.connect()`/`_ws.start()`
  into `start_realtime()` means the socket simply isn't open for the
  entire `sync_watchers()` duration. Any message posted during that window
  isn't merely deferred — it's **gone**: `MattermostWebSocketClient`'s
  reconnect-triggered history replay (`_on_reconnect_cb`, driving
  `_on_ws_reconnect()`'s REST catch-up) is wired to fire only from
  `_reconnect()`, itself reachable only from `_listen_loop`'s
  post-`ConnectionClosed` exception path — confirmed by grep to never run
  on an initial `start()`. So option C did not remove the "drop vs. delay"
  tradeoff that option B was criticized for above (see "The websocket is
  still live and dispatching during the gap..." under option B) — it
  silently resolved that tradeoff as "drop," and with a **wider** blast
  radius than the narrower, previously-accepted case this feature already
  lived with (a static watcher whose `subscribe_room()` hadn't run yet):
  now *every* channel, for the *entire* `sync_watchers()` duration, loses
  messages with zero recovery path. This is worse than option B, which at
  least made an explicit, bounded "delay" choice. **The "Options
  considered" section above is left unedited on purpose** (history, not
  corrected in place) — this section is the correction.
- **Finding B (P2) — confirmed real, secondary.** `MattermostConnector`'s
  class docstring usage example, and the base `Connector.connect()` ABC
  docstring's "Must be called once before the Connector can receive or
  send messages," both stopped being true the moment `connect()` no longer
  opened the socket. `SessionManager.run_once()` is the only current call
  site, so this wasn't a live bug, but it's a real contract inconsistency.

**Corrected design: gate delivery, not the connection.** The fix is not a
fourth option — it's a repair to option C that keeps its shape (same
`start_realtime()` method, same call position in `run_once()`, same
ordering test) while removing the data loss:

- `MattermostConnector.connect()` goes back to opening the websocket
  (`await self._ws.connect()` / `await self._ws.start()`), same as before
  option C ever existed. Nothing is lost at the transport layer from this
  point on.
- `MattermostWebSocketClient` already buffers: `_dispatch()` puts every
  decoded event on a per-channel `asyncio.Queue` (bounded, depth 50,
  overflow logged rather than silently dropped) the instant it arrives,
  regardless of anything downstream. The only thing that needed delaying
  was *draining* those queues, not receiving into them. Added a single
  `asyncio.Event` (`_dispatch_gate`, closed by default) that each
  `_channel_worker` now awaits once, at entry, before its drain loop —
  events queue up normally while it's closed, and once
  `open_dispatch_gate()` is called, every worker (already-running or
  spun up later) drains and delivers in the original arrival order.
- `MattermostConnector.start_realtime()` now just calls
  `self._ws.open_dispatch_gate()` — no socket I/O. `SessionManager
  .run_once()`'s call order (`connect()` → `sync_watchers()` →
  `start_realtime()`) is **unchanged**, so `try_lazy_create()` still never
  sees an event before local state is ready (Finding A doesn't reopen
  #24/#27) — it's just that "not yet delivered" now means "queued," not
  "never received."
- Finding B fixed by updating both docstrings (base `Connector.connect()`
  and `MattermostConnector`'s class example) to describe the corrected
  two-phase contract accurately: `connect()` opens the transport and
  begins queuing; `start_realtime()` is what makes queued/incoming events
  actually reach the handler. The class usage example now calls
  `start_realtime()` explicitly for direct (non-`run_once()`) callers.
- One trade-off worth naming honestly: the dispatch gate is
  **closed-by-default with no matching close operation** (opens once,
  stays open). A direct caller that calls `connect()` but never calls
  `start_realtime()` now receives nothing, ever — same failure mode
  Finding B originally flagged, just moved from "socket never opens" to
  "gate never opens." This was a deliberate choice (fail-safe over
  fail-open, since `run_once()` — the only production caller — is a
  security-relevant gate for `_blocked_agents`), not an oversight; the
  docstring fix above is what makes the requirement discoverable instead
  of silent.
- Verified, not assumed: `MattermostWebSocketClient.stop()` already
  cancels every task in `_channel_workers` unconditionally, so a worker
  parked on a still-closed gate (e.g. shutdown ordered before
  `start_realtime()` ever runs) is cancelled cleanly, not left hanging —
  covered by a new regression test.
- Tests: `tests/unit/test_mattermost_ws.py`'s new `TestDispatchGate` class
  (events queue but don't deliver before the gate opens; buffered events
  deliver in order once it does; the gate has no "close" once opened; a
  worker parked on a closed gate is cancelled cleanly by `stop()`);
  `TestConnectStartRealtimeSplit` in `test_mattermost_connector.py`
  rewritten for the corrected split (`connect()` opens the socket;
  `start_realtime()` only opens the gate).

**Rollout note updated accordingly**: the live low-traffic startup-cycle
observation called for in "Process notes" above should now also confirm
that a message posted right at startup (during `sync_watchers()`) is
delivered once ready, not merely that no error is logged — the previous
design would not have failed loudly, it would have failed silently.

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
