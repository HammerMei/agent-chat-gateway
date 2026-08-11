# On-the-fly watchers: rule-based room matching + lazy watcher lifecycle

Status (updated 2026-08-11, round 15): **config schema landed (PR #77); rule-based
room matching + lazy creation landed for Mattermost.** RocketChat support,
`session_id`/`online_notification`/`offline_notification` retirement, the
`expire` CLI, **and all TTL idle/expire enforcement** (`session_idle_days`/
`session_expire_days` parse and validate but have zero runtime consumers —
verified 2026-08-11) are still design-only. Coordinating a low-traffic
production test window with the repo owner is still required before any of
this touches macbook-server (see "Rollout" below) — nothing in this feature
has been deployed to production yet, regardless of what has merged.
**Round 15 opened an architecture review** (see "Architecture review
(2026-08-11): retiring the static/dynamic watcher split" below) on
finishing the single-shot migration this doc already committed to in
"Rollout" — PR #79 shipped `watchers:`/`watcher_rules:` as two permanently
separate paths instead, which the round-15 root-cause tally traces most of
PR #79's review findings to. A subsequent full code-level pass (see "Full
code-level analysis of the unification (2026-08-11)") confirmed the
one-config-shape/one-code-path half is worth doing, but found that
**"lazy-init only" is not achievable** — only Mattermost implements the
lazy path, so removing eager start would stop RocketChat/Script/Voice
watchers from ever starting. Decision pending.

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

### PR #79 review, twelfth round (2026-08-09) — six more findings, all fixed; correcting the "28/28 resolved" claim above

**Correction first**: the eleventh-round entry above says all 28 threads
were resolved. That was wrong — four more findings had already landed on
2026-08-07 (the same day, hours after round eleven) and went unprocessed
while attention moved to the startup-ordering design review and PR #80.
Only two of this round's six findings are genuinely new (filed
2026-08-09, after PR #80's revert). Listing all six together since they
surfaced and were fixed together, but the timeline matters for anyone
reconstructing what "round eleven complete" actually meant at the time.

29. **Connector names were never validated for path-safety.** Static
    watcher names reject `/` at config-load time, but that check only
    fires when a name is actually generated at load time — a
    wildcard-rule-only connector never does that (its names are generated
    later, per room, at runtime via `auto_watcher_name()`, which
    concatenates the raw connector name with a separately-sanitized room
    name). A connector named e.g. `mm/team` or `../team` sailed through
    undetected. Worse than the finding itself described: `gateway/core/
    state.py`'s `_state_file()` (`RUNTIME_DIR / f"state.{connector_name}.json"`)
    was already exploitable by a bad connector name with no lazy watchers
    involved at all. Fixed by rejecting `/` in connector names at
    `_parse_one_connector()` — the one place any connector name is
    guaranteed to be validated regardless of whether it ever uses
    `room: "*"`.
30. **A persisted session's agent wasn't checked against the CURRENT
    resolved agent.** When a wildcard rule's `agent:` changes between
    restarts, `WatcherState` had no record of which agent owned
    `session_id` — `_start_watcher()` would select the new rule's agent
    while `_provision_session()` blindly reused the old session ID.
    Session IDs are backend-specific: this could fail to resume, or
    attach to an unrelated session that happens to share the ID format.
    Fixed by adding an `agent` field to `WatcherState` (defaults to `""`
    for state persisted before this field existed — read as "unknown,
    assume compatible" so this doesn't force-reset every already-running
    watcher's session on the first restart after it ships).
    `_start_watcher()` discards the WHOLE retained state on a mismatch —
    same treatment as the existing room-id mismatch check right above it,
    not just the session_id — so `context_injected` also resets correctly
    instead of skipping re-injection on the new backend.
31. **`_find_entry_for_watcher()`'s plain scan could silently route to the
    wrong connector.** Config-load-time watcher-name uniqueness only
    covers names sourced from config.yaml — a dynamically-created
    watcher's name is never in config.yaml (only its `WatcherState` is
    persisted), so it's invisible to that check. A static watcher later
    configured on a DIFFERENT connector with the same name (accidental, or
    because the auto-generated format is predictable) could collide with
    nothing catching it at load time, and the plain scan would pick the
    live match without ever checking whether another connector also had a
    dormant claim. Fixed by scanning every other connector with the
    non-mutating `is_watcher_name_known()` probe before committing to a
    match in `dispatch_command()`'s pause/resume/reset routing — an
    ambiguous name now returns an error asking for `--connector` instead
    of guessing. Added `--connector` to the `pause`/`resume`/`reset` CLI
    subcommands, which had no way to disambiguate at all before this.
32. **A dynamic watcher's cached config went stale after a rename that
    happened AFTER it was already reconstructed once.** Different from
    the fifth-round rename fix (which covers a config that had never been
    reconstructed before): if this watcher's `WatcherConfig` was already
    cached in `_watcher_configs` from an earlier reconstruction in this
    same process (e.g. a prior pause/resume call), and the room was
    renamed after that, the cached `.room` goes stale — a later
    resume/reset would call `resolve_room()` with the old name, which no
    longer exists, failing until a full restart rebuilt
    `_watcher_configs` from scratch. Fixed by refreshing the cached
    config's `.room` in place when `try_lazy_create()`'s room-ID fallback
    detects this, gated on `dynamically_created` so it can never touch an
    operator's static config.
33. **Lazy creation ran before the sender/mention filter, not after.** For
    an unwatched channel, the lazy-creation hook ran before
    `resolve_username()`/`filter_mm_message()`, so a sender excluded by
    `filter_sender` — or any ordinary post that never mentions the bot —
    still fully provisioned a session, subscribed, started a processor,
    and could post an `online_notification` before the real filter ever
    got a chance to reject the message. Fixed by resolving the sender and
    running a sender/mention pre-check (via `filter_mm_message()` with
    `turn_store=None` and `last_processed_ts=None`, deliberately skipping
    the dedup/turn-budget steps rather than double-running them later)
    before ever calling the hook. Safe to move `resolve_username()`
    earlier here specifically: no `state`/`seen_ids_set` exists yet for an
    unwatched channel, so the live-vs-replay dedup race the existing
    ordering comment protects against doesn't apply — reinforced by
    replay never reaching this branch at all, and per-channel dispatch
    serializing same-channel events through one worker.
34. **A scheduled job for a dormant dynamic watcher was silently skipped
    forever.** A lazily created watcher intentionally goes dormant between
    messages — normal, not a failure — but `inject_message()` returned
    `False` the instant no processor existed, and `_fire_once()` advances
    `next_run` on any injection failure (anti-flood design). Together,
    every due/catch-up run for an otherwise-healthy, unpaused dynamic
    watcher was silently skipped and pushed to the next scheduled time,
    repeating indefinitely, until unrelated live channel traffic happened
    to wake it via `try_lazy_create()`. Fixed by adding
    `WatcherLifecycle.wake_dormant_watcher()`, used by `inject_message()`
    before giving up — deliberately narrow, mirroring `resume_watcher()`'s
    own guards exactly (only wakes a watcher flagged `dynamically_created`,
    never wakes a paused watcher, respects the fail-closed blocked-agents
    guard) rather than a looser "just start it." Verified the scheduler's
    existing fan-out fallback (calling `inject_message()` on every session
    manager when a job's connector isn't specified) stays safe: the new
    method gates on `self._states`, which is per-`WatcherLifecycle`-
    instance, so only the owning connector ever finds a match.

All six confirmed real before being accepted, per the standing rule for
every round in this document. Landed as five commits (findings #30 and
#32 share `watcher_lifecycle.py`'s dynamic-watcher reconstruction path
closely enough to land together; the other four are independent) rather
than one, specifically because the startup-ordering saga just above this
section showed what a single large commit in this area costs when a
review finds a problem with only part of it. Full unit (2169) + integration
(219) suites green after all six.

### PR #79 review, thirteenth round (2026-08-10) — two findings against the twelfth round's own fixes

Two more findings landed almost immediately, both against fixes #30 and
#34 from the round directly above — the exact pattern the startup-ordering
saga trained everyone to expect by now, caught fast because each fix was
verified against actual code rather than assumed correct on submission.

35. **The agent-mismatch discard (finding #30) lost `dynamically_created`.**
    Building the replacement state as `state = None` reset EVERYTHING,
    including the identity marker that isn't actually agent-specific.
    Unlike the pre-existing room-id mismatch (genuinely a different room's
    leftover data, where full discard is correct), an agent mismatch is
    still the same watcher's own identity — only session_id/
    context_injected/last_processed_ts should reset.
    `try_lazy_create()` masked this by re-setting the flag explicitly
    right after calling `_start_watcher()` (an existing, unrelated line
    from the fifth round), but `resume_watcher()`/`reset_watcher()`/
    `wake_dormant_watcher()` (finding #34, THIS round) have no such
    fixup — they rely entirely on `_start_watcher()`'s carry-forward from
    the incoming `state`. Losing the marker there means `sync_watchers()`
    prunes the watcher as removed on the next restart, abandoning it a
    second time. Fixed by replacing the full `state = None` with a new
    `WatcherState` that preserves `dynamically_created`/`room_id`/
    `room_type` and resets only the session-specific fields. Regression
    test exercises this through `resume_watcher()` specifically (not
    `try_lazy_create()`, which would have masked the bug) — it would have
    failed before the fix.
36. **`wake_dormant_watcher()` (finding #34) started a watcher with no
    cross-connector reservation.** `try_lazy_create()` has always reserved
    the name via `_reserve_global_name()` before starting, for exactly
    this reason: a dormant dynamic watcher's name is invisible to
    config-load-time uniqueness checks (never in config.yaml), so a
    static watcher configured on a DIFFERENT connector after this one
    went dormant could already be live under the same name by the time a
    scheduled job wakes it — leaving two processors sharing a supposedly
    globally-unique name. Finding #31 (also this round) closed the
    equivalent gap for CLI pause/resume/reset routing, but that fix never
    touched this scheduler-driven path, since the scheduler calls
    `SessionManager.inject_message()` directly and never goes through
    `ControlServer.dispatch_command()`'s ambiguity check at all. Fixed by
    reserving before reconstructing/starting, with the same try/finally
    release discipline `try_lazy_create()` already uses.

Both confirmed real before being accepted. Landed as one commit (both are
small, same-file corrections to the same round's own fixes — no case here
for splitting further). Full unit (2172) + integration (219) suites green.
All 36 PR #79 review threads now resolved.

### PR #79 review, fourteenth round (2026-08-10) — one fixed, one deferred as an Open Item

Two more findings, again against code from the round directly above —
third consecutive round where every finding lands on the immediately
preceding round's own fixes (12 → 13 → 14). Noted explicitly to the repo
owner as a signal that `watcher_lifecycle.py`'s dynamic-watcher path has
accumulated more interacting invariants (pause-respect, cross-connector
reservation, dynamic provenance, room-identity, agent-identity,
blocked-agents) than local patches can reliably hold correct — not a
reason to stop, but a reason to ask whether this file should keep taking
new rounds before PR #79 merges, or whether the remaining scope should
wait.

37. **(Fixed) `wake_dormant_watcher()` didn't recheck `paused` after
    acquiring the lock.** It snapshots `state` before acquiring
    `self._get_watcher_lock(name)`. If a concurrent `pause_watcher()` call
    acquired that same lock first and completed — mutating `state.paused`
    in place, or (if no state existed yet) installing a fresh paused
    `WatcherState` — the pre-lock snapshot was stale by the time
    `wake_dormant_watcher()` got the lock. Its in-lock recheck covered
    only `_processors`, not `paused`, so `_start_watcher()` ran anyway and
    silently undid the completed pause, letting a scheduled message fire
    against a watcher the operator had just paused. Fixed by re-fetching
    `state = self._states.get(name)` immediately after acquiring the lock
    and rechecking `paused`/`dynamically_created` there too — mirroring
    the existing `_processors` recheck already at that spot. Regression
    test drives the actual interleaving directly (holds the per-name
    lock, starts `wake_dormant_watcher()` in a task so it blocks
    acquiring it, mutates `state.paused = True` to simulate the
    concurrent pause completing, releases the lock, asserts the wake is
    refused) rather than mocking around it; confirmed it fails on the
    pre-fix code.
38. **(Deferred) `_dynamic_state_still_matches_rule()`'s fast path never
    resolves the room when the active rule has no `exclude_rooms`.** A
    deleted/moved room's dynamic state is therefore preserved on every
    startup forever, and its generated name stays reserved via
    `is_watcher_name_known()` forever too — real, but checked the blast
    radius before deciding whether to patch it here: `auto_watcher_name()`
    prefixes the *owning* connector's own name into every generated
    dynamic name, and `_reserve_watcher_name()` already excludes the
    requesting connector's own entry from its scan (ninth round, finding
    #23). So the zombie can only ever be observed by a **different**
    connector, and only if that connector happens to have a *static*
    watcher explicitly named to collide with `<this-connector>-<room>` —
    narrow and mostly coincidental, not the common path. A proper fix
    needs a connector-agnostic "room genuinely gone" signal, distinct
    from a transient resolution error (which must still preserve
    conservatively, per this method's existing philosophy) — and that
    signal doesn't exist at this layer today: `RoomNotFoundError` is
    defined independently by `mattermost/rest.py` and `rocketchat/rest.py`
    as two unrelated classes, with no shared contract on `Connector`
    (`gateway/core/connector.py`). Building that properly is a 4-file
    change (a core exception + both `rest.py` implementations + this call
    site) — importing Mattermost's connector-specific exception into core
    just to close this one P2 would be the wrong direction for this
    file's layering, for a fix this narrow in practice. Logged below as
    an Open Item instead; natural to pick up alongside RC's own
    lazy-creation path, since that's when a second connector actually
    implementing `resolve_room_by_id()` makes the connector-agnostic
    contract worth building.

Finding #37 landed alone (finding #38 has no code change this round).
Full unit (2173) + integration (219) suites green. All 38 PR #79 review
threads now resolved — 0 unresolved as of this round, confirmed via a
follow-up GraphQL query rather than assumed (see the "28/28" correction
two rounds above for why that check is now standard practice here).

### PR #79 review, fifteenth round (2026-08-11) — three root-caused, plus the round that triggered the architecture review below

A fresh GraphQL sweep after round 14 turned up four more findings, all
against `_start_watcher()`'s `state` parameter and the resume/reset/
schedule paths around it — the fourth consecutive round landing on the
immediately preceding rounds' own territory (11 → 12 → 13 → 14 → 15). The
round count itself was rejected as a signal to pause or rush a merge
decision; the direction instead was to find the shared root cause and fix
that, not the four symptoms one at a time. Two `/advisor` passes (one
after an initial mis-framing — see
[[feedback_many_rounds_signal_simplify]]) converged on: `_start_watcher()`
took `state` as a caller-supplied parameter, so every caller had to
independently get its own snapshot right — three of the four fresh
findings were exactly that pattern wearing different clothes.

**(a) `_start_watcher()` stopped taking `state` as a parameter.** It now
reads `state = self._states.get(wc.name)` itself, at the top, under an
`assert self._get_watcher_lock(wc.name).locked()`. Every caller
(`sync_watchers()`, `try_lazy_create()`, `resume_watcher()`,
`reset_watcher()`, `wake_dormant_watcher()`) now seeds `self._states`
before calling it instead of passing a value that could already be stale
by the time it's read inside. Closes the whole "caller forgot to
re-snapshot" bug class at the source instead of patching each call site.

**(b) Cross-connector name reservation added to `resume_watcher()` and
`reset_watcher()`.** Both already existed for `try_lazy_create()`/
`wake_dormant_watcher()` (ninth round, finding #23) but not for
resume/reset — a dormant dynamic watcher could be resumed/reset even
after a *different* connector had since claimed its generated name,
letting two connectors run watchers under the same name simultaneously.
Same `try/except/finally` + `reserved` flag shape as the existing two
call sites.

**(c) `schedule create` and `fetch-history` became reconstruction-aware.**
Prompted by a broader sweep of the scheduler and every other code path
that assumes a watcher's runtime object exists: both control
paths looked a watcher up via the plain, non-reconstructing
`_find_entry_for_watcher()`, so a dormant dynamic watcher — invisible
until something calls `can_find_or_reconstruct_watcher()` — would report
"not found" even though pausing/resuming it by name worked fine. New
`ControlServer._resolve_watcher_entry()` shares the ambiguity-scan +
reconstruction-aware lookup logic across pause/resume/reset routing,
`_handle_schedule_create()`, and `_handle_fetch_history()`.
`_find_connector_for_watcher()`, confirmed dead (zero callers via grep)
once this landed, was deleted.

**(884, fixed) `reset_watcher()`'s reservation came after its destructive
steps.** Reservation moved to happen first, before `_stop_processor()`/
session reset, so a refused reservation leaves the watcher untouched
instead of already-torn-down.

**(1142, fixed) `_reconstruct_dynamic_watcher_config()` ran outside its
caller's lock in one path.** `can_find_or_reconstruct_watcher()` (the only
caller with no access to `WatcherLifecycle`'s internal lock) now acquires
`self._get_watcher_lock(name)` itself around the call.

**(config.py:259, logged as an Open Item, not fixed)** —
`find_mergeable_watcher_entry()` doesn't exclude wildcard rule entries
from config-tool room merging, the third instance of the same "TUI
doesn't understand rules yet" boundary as `_check_state_orphans()` and
`expanded_watchers()` above.

**(P1, deferred, not fixed)** — the history-handoff double-delivery
finding was re-raised this round; deferring it was confirmed, citing the
existing 2026-08-05 "known, accepted limitation" decision above (no
message-ID field to match on; fixing it would touch a shared
platform-agnostic interface for a cosmetic failure mode).

Four more findings surfaced by the fresh review pass after these landed —
`control.py:387` (a reconstruction-lookup for `schedule-create`/
`fetch-history` registers a dormant dynamic watcher's config as a side
effect of merely looking it up, without starting a processor),
`control.py:445` (no `--connector` disambiguation option when
`schedule create`'s ambiguity check correctly refuses two same-named
watchers on different connectors), `watcher_lifecycle.py:1386` (clearing
`last_processed_ts` on an agent-mismatch discard conflates the room-level
delivery watermark with session-specific state), and
`watcher_lifecycle.py:1071` (`wake_dormant_watcher()`'s success path
updates `self._states` but never calls `self._state_store.save()`,
unlike every other start path). Not yet fixed — see the architecture
review immediately below for why they were paused rather than patched.

At this point the actual pattern was named directly: four rounds in a
row landing on the immediately preceding round's own fixes is itself the
signal that this file's static/dynamic dual-path architecture — not any
individual missing check — is the thing generating new findings faster
than they can be patched. A review of unifying the two paths was
requested before writing a fifth round of patches. That review is below.

## Architecture review (2026-08-11): retiring the static/dynamic watcher split

**The question under review:** keep only `watcher_rules`, delete
`sync_watchers()` and the exact-room `watchers:` list entirely, and treat
every watcher — including today's "static, exact-room" ones — as a rule.
Would this actually simplify the code, or just move the complexity
around?

**This is not a new idea — it's the design this document already
committed to, and PR #79 never finished executing it.** The "Rollout"
section above, written before implementation started, says explicitly:

> **Decision: single-shot migration.** Static `room: roomX` watchers
> migrate to the new rule engine (a single-room rule is a strict
> degenerate case of the general mechanism, so this is mechanically
> safe) in the same release as the rest of this design — no
> additive-first staging, no separate follow-up release for the
> migration.

What actually shipped kept `watchers:` and `watcher_rules:` as two
permanently separate `GatewayConfig` fields with two separate lifecycle
paths (`sync_watchers()` eager-starts one; `try_lazy_create()` +
reconstruction lazily materializes the other). This was a deliberate
choice, not an oversight — the "Scope decisions made while starting
implementation (2026-08-05)" section above (dated *after* the Rollout
decision) makes it explicitly, and that matches the recollection of
proposing exactly this — keeping the static room config and rule-based
config separate to keep the scope of the original PR limited. So this is
two dated decisions in real tension, not one that silently reversed the
other. The 2026-08-05 section's own stated
reason for the split is narrow, though: it's about `resolve_room(wc.room)`
never seeing a literal `"*"` string at boot — a config-schema/parsing
concern. That reason doesn't cover, and was never argued to cover, the
*lifecycle* divergence that followed it (separate eager-start path,
`dynamically_created`, non-persisted dynamic configs, the whole
reconstruction layer). Keeping the schema split (two config-parsing
shapes, for the parsing-safety reason already given) and unifying the
*lifecycle* (one start path, one reconstructible-config model) are
independent axes — the finding tally below traces to the second axis,
not the first.

**Root-cause tally.** Went back through every PR #79 finding and checked
whether it traces to the same asymmetry: an exact-room watcher's
`WatcherConfig` is always present (loaded from `config.yaml` at
construction, lives in `self._watcher_configs` for the process's
lifetime), while a dynamically-created watcher's config is **never**
persisted — only its `WatcherState` is, forcing every operation that
touches a dynamic watcher after a restart to reconstruct a config it
shouldn't need to reconstruct in the first place. Findings #21, #22, #23,
#25, #28, #30, #31, #34, #35, #36, #37, #38, plus this round's #884 and
#1142 — every one of them is either (a) a check that exists on the
reconstruction path but was missing on the static path or vice versa, or
(b) a race/staleness bug in the reconstruction machinery itself. None of
them would exist if there were only one path.

**What would actually disappear, not just move:**
`_reconstruct_dynamic_watcher_config()`, `_find_or_reconstruct_watcher_config()`,
`can_find_or_reconstruct_watcher()`, `is_watcher_name_known()`,
`WatcherState.dynamically_created`, `_dynamic_state_still_matches_rule()`,
`sync_watchers()`'s entire tail preservation/pruning loop (lines
243–306 above), the `_reserve_watcher_name()`/`_release_watcher_name()` +
`_reserved_watcher_names` cross-connector reservation machinery (still
needed in some form, but currently duplicated across four call sites
specifically *because* dynamic names aren't known until runtime — a
single unified start path needs it in exactly one place), and
`ControlServer._resolve_watcher_entry()`'s ambiguity-scan-then-reconstruct
dance. `gateway/config.py`'s two parse dispatch blocks
(`_is_wildcard_room_entry()` → `_parse_one_watcher_rule()` vs.
`_parse_one_watcher_entry()`) collapse toward one shape, since every
entry becomes the same kind of thing (a rule; an exact room is just a
rule matching exactly one room).

**Two things initially assumed to be regressions, checked against actual
code, and found not to be:**
- **Startup pre-warming.** The fear: unifying loses "static watchers
  always start eagerly regardless of activity" (the `#nest`-latency
  concern in "Lazy watcher creation" above). Checked: today's eager start
  for static watchers isn't actually activity-based — it's *possible*
  because the room name is already known from `config.yaml`, so
  `resolve_room(wc.room)` works at boot with no discovery step. An
  exact-room *rule* has exactly the same room name available at boot, so
  it can be resolved and started eagerly the same way — discovery is
  only needed for *wildcard*-matched rooms nobody's named yet, which stay
  exactly as activity-gated as they are today. No regression: only
  wildcard rules are lazy either way, before or after.
- **`agent-chat-gateway list`.** Same reasoning — an exact-room rule
  still materializes a concrete `WatcherConfig` at boot (the name is
  known), so it still has a row in the table without needing any new
  machinery.

**What's genuinely open, not a blocker:**
- RC still can't do message-triggered lazy creation for *wildcard* rules
  (DDP requires an explicit `sub` before any event fires) — unchanged by
  unification either way. RC's exact-room rules are unaffected and work
  today exactly as they would post-unification.
- `WatcherConfig.session_id` (sticky pinning) is dropped when
  `_build_watcher_config_from_rule()` derives a config from a rule
  (`session_id=None`, "neither concept applies once a real room is
  known" — correct for a *wildcard* rule matching many rooms, but an
  exact-room rule matching exactly one room could theoretically want it).
  Not moot, but a sequencing precondition: `session_id` retirement is
  still design-only per the status line above, not shipped. This
  migration must land after (or together with) that retirement — landing
  it first would silently drop sticky-session pinning for every
  exact-room watcher the moment it becomes a rule.
- Finding #38's Open Item (a connector-agnostic "room genuinely gone"
  signal, vs. `RoomNotFoundError` currently being two unrelated
  per-connector classes) stops being a narrow, deferrable P2 and becomes
  a real prerequisite: every watcher's config becomes reconstructible
  from (rule + persisted room_id) rather than just dynamic ones, so
  "the room this rule used to match is actually gone, not just
  transiently unreachable" needs a real answer for every watcher, not
  just the rare cross-connector collision case that motivated deferring
  it in round 14.

**Effect on this round's four still-open findings** — checked each
against the unified design rather than assuming unification makes them
moot:
- `control.py:387`, `control.py:445` — **survive unchanged.** Both are
  about `ControlServer`'s lookup/disambiguation behavior, orthogonal to
  whether the underlying config is static or rule-derived. Still need
  their own fix either way.
- `watcher_lifecycle.py:1386` — **survives unchanged.** Agent-reassignment
  discard logic applies to any watcher whose agent changed out from under
  it; not specific to the static/dynamic split.
- `watcher_lifecycle.py:1071` — **the specific function disappears, but
  the bug pattern doesn't.** `wake_dormant_watcher()` only exists because
  it's the dynamic-specific "bring a `dynamically_created` watcher back"
  path; under unification, waking any dormant rule-derived watcher
  becomes one universal path, and whatever that path ends up looking
  like needs to be checked for the same missing-`save()`-after-success
  mistake, since nothing about unification makes that mistake structurally
  impossible on its own.

**Recommendation:** proceed with the migration — it's not a new
direction, it's finishing the one this document already committed to,
and the round-15 tally shows it would have prevented essentially every
finding of the last five rounds rather than just the four that prompted
this review. Scope is real, though: this changes the `GatewayConfig`
schema (`watchers:`/`watcher_rules:` merge into one shape) and touches
`gateway/config.py` (parsing + the duplicated `from_dict`/`from_file`
blocks), `watcher_lifecycle.py` (most of the file), `control.py`
(`_resolve_watcher_entry()` simplifies once there's one lookup path),
`config_validate.py`'s `_check_state_orphans()`, and the config TUI's
`expanded_watchers()` — bigger than PR #79 itself, and needs its own
migration-guide treatment (same precedent as `agent_defaults` →
`agent_templates`, PR #71). Given the project's stated preference for
infra work (a larger, correct PR over patching an unstable design), the
question isn't whether to do this but whether to do it as a follow-up PR
after landing #79's current fixes, or as more commits on this same
branch before merging anything.

### Full code-level analysis of the unification (2026-08-11) — and three corrections to the section above

The review above was written from a reading of this document plus targeted
code checks. It was then followed by an exhaustive pass: seven independent
full-file reads (`watcher_lifecycle.py` all 1768 lines, `config.py` all
1839, `control.py`, `config_validate.py`, `configtool/model.py`,
`core/connector.py`, `mattermost/connector.py`, `core/scheduler.py`, plus
a test-suite blast-radius inventory), each mapping every function that
touches the static/dynamic split. That pass **disproved three claims in
the section above** and surfaced one finding that changes the shape of
the answer entirely. Corrections first, since the section above is
already committed:

**Correction 1 — "idle/expire semantics" is not an existing guarantee,
because it isn't implemented.** The section above lists it among the
lifecycle guarantees unification must preserve. Verified: `session_idle_days`
and `session_expire_days` are parsed, cross-validated (`config.py:804-847`),
modeled on `AgentConfig` (`core/config.py:136-139`) and exposed in the
config TUI — but have **zero runtime consumers**. Nothing in
`watcher_lifecycle.py`, `session_manager.py`, `service.py`, or
`scheduler.py` ever reads either field; there is no background tick and
no expiry hook. `AgentBackend.typical_session_retention_days()` is
implemented by both adapters and consumed by nothing. `WatcherState` has
no "expired" flag. The `agent-chat-gateway expire <watcher>` CLI
described under "CLI" above **does not exist** in `cli.py`,
`control.py`, or `session_manager.py` — the "resolved 2026-08-02" note
there records a design decision, not a shipped feature (the status line
at the top of this document does say "the `expire` CLI [is] still
design-only", so this is the section above over-reading its own
document). Consequence for unification: there is no TTL behavior to
preserve or regress. This *removes* a constraint rather than adding one.

**Correction 2 — the round-15 tally claim was overstated.** The section
above says unification "would have prevented essentially every finding
of the last five rounds." Its own following paragraph then concludes 3 of
the 4 newest findings survive unification unchanged. Both can't be true.
The accurate statement: unification eliminates the
**reconstruction/dual-path finding class** — the bulk of #21, #22, #23,
#25, #28, #30, #31, #34, #35, #36, #37, #38, #884, #1142 — and does
nothing for control-plane findings (`control.py:387`, `control.py:445`)
or agent-reassignment findings (`watcher_lifecycle.py:1386`). "Most of
the findings, and specifically the ones that kept recurring" is the
defensible version.

**Correction 3 — `_build_watcher_config_from_rule()` needs a new branch,
not just to survive.** The section above treats the `session_id`
sequencing issue as the only wrinkle. It's sharper than that: that
function (`watcher_lifecycle.py:1106-1125`) *unconditionally* zeroes both
`session_id` (line 1119) and `exclude_rooms` (line 1120) on the stated
grounds that "neither concept applies once a real room is known" — true
for a wildcard-derived room, false for an exact-room rule, which can
legitimately pin a session. `_provision_session()` (1603-1641) already
prioritizes a pinned `wc.session_id` correctly and needs no change, and
`reset_watcher()`'s pinned-session branch (line 904) already reads it —
so if this function isn't given an exact-room branch, both silently stop
working for every migrated watcher. Still moot **if** `session_id`
retirement lands first, which remains the sequencing precondition.

#### The finding that reframes the question: "lazy init only" is not achievable

The request under review was "rule-based only **and lazy-init only**,
without the static one." The second half is not implementable without
first building infrastructure that does not exist, and attempting it
would silently stop three of the four connector types from working at
all. Evidence, all verified directly:

- Four connector types are registered (`connectors/__init__.py:16-27`):
  `rocketchat`, `script`, `voice`, `mattermost`.
- **Only Mattermost implements the lazy path.**
  `register_lazy_creation_hook()` is overridden solely in
  `mattermost/connector.py:253`; `resolve_room_by_id()` solely in
  `mattermost/connector.py:338`. RC, Script, and Voice inherit the base
  ABC's inert no-op / `NotImplementedError` defaults
  (`core/connector.py:224`, `:339`).
- This is enforced at config-load time:
  `_LAZY_CREATION_SUPPORTED_CONNECTOR_TYPES = {"mattermost"}`
  (`config.py:1044`), raising for any other connector type inside
  `_parse_one_watcher_rule()` (`config.py:1129-1136`).
- **No room discovery exists anywhere.** There is no
  `subscriptions.get` / `GET /api/v4/users/me/channels` enumeration in
  either connector's `rest.py` — only single-room `resolve_room()` /
  `get_channel_by_id()` calls. The "Room discovery + membership-event
  hooks" section near the top of this document describes a design, not
  shipped code.
- RC's constraint is structural, not incidental: DDP requires an explicit
  per-room `sub` frame (`rocketchat/websocket.py:207-290`, frame at
  234-242) before the client receives anything for a room, and
  `RocketChatConnector.subscribe_room()` (`connector.py:379-433`) is the
  only sender — called only for rooms an existing watcher already names.

Therefore, for RC/Script/Voice, a watcher can start **only** because its
room name is known from config at boot and `sync_watchers()` resolves it
eagerly. Remove eager start and those watchers never start, ever — not a
config-format break but a total functional loss.

**The one escape hatch, checked rather than assumed:** `subscribe_room()`
is step 7 of `_start_watcher()` (line 1548), not step 1 — so one could
ask whether it can be *hoisted* to boot (eagerly DDP-`sub` every named
room) while deferring the expensive rest of init (session provisioning,
history handoff, context injection, processor construction) to first
message. RC's plumbing does not rule this out: `subscribe_room()`
(`rocketchat/connector.py:379-433`) is refcounted and registers a
per-room callback, and RC's `_handle_room_message` gates precisely on
`self._callbacks.get(room_id)` (`websocket.py:528-530`) — so an eager
boot-time subscribe would in fact make messages start arriving. But
making that useful requires two further changes: (a) a "message arrived,
no processor yet → materialize" hook on RC's delivery path, which *is*
the RC lazy-creation support already scoped as a separate follow-up, and
(b) splitting subscription lifetime from processor lifetime, which
rewrites the subscribe-failure rollback (1555-1573) and `_stop_processor()`'s
unsubscribe (1698) around a new invariant: a subscription that outlives
its processor. That new seam is the same shape as the bug class this
whole refactor exists to remove. So the accurate claim is not "lazy-init
only is impossible" but: **not achievable without first building RC
lazy-creation support and splitting subscribe from init — which adds a
seam rather than removing one, and so argues against doing it regardless.**
The recommendation is unchanged either way. So:

- **"Rule-based only" (one config shape, one lifecycle code path):
  achievable and worth doing.**
- **"Lazy-init only" (no eager start): not achievable today.** What can
  actually be eliminated is the *separate* `sync_watchers()`-vs-
  `try_lazy_create()` **code paths** — not the eager *trigger*. Eager
  start for exact-room rules is mandatory, not an optional pre-warming
  optimization. (This also means `seed_blocked_agents()` and the whole
  startup-ordering hazard class stay relevant, since two start entry
  points still coexist.)

A useful corollary: this reframes the "startup pre-warming" discussion
above. `last_processed_ts` is verified to be a message-dedup watermark
only (`watcher_lifecycle.py:1590-1593` restore, `1722-1726` capture) and
is **never** read to decide whether to start anything. Static watchers
start unconditionally at every boot — not because they're active, but
because their room name is resolvable without discovery. Wildcard-matched
rooms are never pre-started at all today
(`watcher_lifecycle.py:260-268` says so explicitly). So "activity-gated
pre-warming" is not a behavior anything can regress from; it has never
existed.

#### What genuinely simplifies

- **The reconstruction quartet collapses to one resolver.**
  `_reconstruct_dynamic_watcher_config()` (1127-1172),
  `_find_or_reconstruct_watcher_config()` (1211-1227),
  `can_find_or_reconstruct_watcher()` (1174-1209), and
  `is_watcher_name_known()` (1077-1104) exist as four functions only
  because a dynamic watcher's config doesn't survive a restart while a
  static one's does. One uniform "materialize a config for this name"
  resolver replaces all four (a side-effect-free probe variant is still
  worth keeping, but for reasons unrelated to the split).
- **`sync_watchers()`'s two-phase shape collapses to one.** Its phase-2
  tail (249-305) exists purely to reconcile the blind spot that phase 1
  only ever walks `_watcher_configs`. The three-way
  preserve/prune/debug-log branch — branched entirely on
  `dynamically_created` — becomes one question asked uniformly of every
  persisted state: does some rule still cover this room?
  `_dynamic_state_still_matches_rule()` (310-343) becomes that uniform
  helper.
- **`WatcherState.dynamically_created` disappears** — 18 references,
  including the carry-forward at `_start_watcher()` lines 1387 and 1413
  plus its dedicated comment block (1405-1412), which is the single
  cleanest deletion attributable to unification.
- **`try_lazy_create()`'s two separate paused-checks become one** — the
  pre-restart check via `_processors`/`_states` (551-567) and the
  post-restart disk lookup for the deterministic name (680-687) are two
  checks only because a static config's pause status is visible
  immediately while a dynamic one's isn't until that path loads it.
- **`control.py`'s `_resolve_watcher_entry()` loses its two-tier
  structure** — two lookup passes, two claimant tiers, and two
  differently-worded ambiguity messages exist to bridge the asymmetry.
- **Config parsing loses one of its two dispatch shapes.** Note the
  `from_file()` (fail-fast, 135-310) vs `collect_config()`
  (fault-tolerant, 1443-1839) duplication is **orthogonal** and does not
  go away — those two already share every per-entity parser deliberately
  (module docstring, 596-607); what's duplicated is the
  `_is_wildcard_room_entry()` dispatch shape and two post-loop
  cross-entry checks, each spelled twice. Unification removes the
  dispatch, not the two-loader structure.

#### What gets harder — the counterweight the section above omitted

1. **`self._watcher_rules[0]` is hardcoded in three places** (lines 331,
   396, 1166) on the documented "at most one rule per connector"
   invariant, enforced at config-load time (`config.py:283-294` /
   `1804-1818`). **Unification breaks that invariant by construction** —
   an exact-room rule and a wildcard rule coexisting on one connector
   becomes the normal case. All three `[0]`s must become precedence-aware
   matching, and the load-time check must be *replaced by a precedence
   policy*, not deleted. This is a correctness change, not a
   simplification, and it should be settled before any code moves.
2. **Cross-connector name reservation becomes the only uniqueness
   guard.** Today static names are validated unique at config-load time,
   so `_reserve_global_name`/`_release_global_name` (call sites 639/736,
   820/836, 875/926, 1035/1070) only ever guard runtime-generated names —
   `sync_watchers()` never calls them. Post-unification every name can
   materialize at runtime, making this reservation path load-bearing for
   everything. Concretely this is a **fail-fast → fail-late regression**:
   a config typo colliding two exact-room watcher names currently fails
   at startup; under a fully-runtime model it may only surface on first
   message. Mitigation: keep load-time uniqueness validation for rules
   whose room set is statically known.
3. **`list_watchers()` requires new code** (930-953). It iterates
   `_watcher_configs` only; with no authoritative in-memory list of
   "every watcher that exists," it needs new enumeration over persisted
   states and/or rules. Related pre-existing gap worth fixing
   independently: dormant dynamic watchers are already invisible to
   `list` and to `schedule-create`'s "Available watchers:" hint
   (`control.py:612-621`, `448-450`) even though pause/resume/reset/
   fetch-history/schedule-create can all resolve them.
4. **`wake_dormant_watcher()` loses its only semantic signal.** Its
   `dynamically_created` gate (1016, 1031) distinguishes "dormant because
   this watcher idles between messages" (safe for the scheduler to
   auto-wake) from "dormant because it failed to start at boot — blocked
   agent, subscribe failure" (must not be silently retried), per its own
   docstring at 984-987. Unification deletes the flag and leaves nothing
   in its place. A replacement signal (e.g. "has this ever started
   successfully?") must be designed, not assumed away.
5. **First-ever start of an exact-room rule has no persisted `room_id`.**
   The unified premise is "reconstructible from rule + persisted
   room_id," but a rule that has never run has no state to reconstruct
   from — its first start must resolve by the rule's `room:` *name*
   (as `_start_watcher()` does at line 1305), which reintroduces exactly
   the name-vs-id staleness that `try_lazy_create()`'s 479-514 refresh
   branch handles today for the dynamic case only. That handling would
   need to become universal.

#### Functional-breakage risks (config-format breakage excluded by request)

**Method caveat, stated because this document holds itself to it
elsewhere:** the risks in this subsection were derived in a single
reasoning pass over the seven discovery maps. Unlike the numbered
findings in the review-round sections above, they were **not**
independently multi-lens hunted or adversarially verified — the fan-out
that was supposed to do that (six breakage lenses, three refuters per
finding) was cut short by an org spend limit before any of it ran. Every
*code fact* cited below was verified directly against the source; the
severity labels and the completeness of the list were not. Treat this as
a first pass that still wants an adversarial round, not as a verified
finding set.

- **(Resolved by Decision 2 in the design below; recorded because it is
  the failure mode a naive implementation walks straight into) the
  `_LAZY_CREATION_SUPPORTED_CONNECTOR_TYPES` gate would reject every
  RC/Script/Voice watcher.** Today that gate
  (`config.py:1129`) lives inside `_parse_one_watcher_rule()`, so it
  fires only for rule-shaped entries — correctly, because only *deferred*
  room resolution needs a connector that can lazily create. If
  unification routes every entry through the rule parser, every existing
  RocketChat exact-room watcher fails config load. Mitigation: the gate
  must key on "is this rule's room set unbounded/deferred?" (i.e. only
  wildcards), never on "is this entry rule-shaped."
- **P1 — paused-watcher precedence would silently change.** Verified
  behavior today: for a room covered by both an exact-room watcher and a
  wildcard rule, when that watcher is *running*, MM's
  `_on_posted_event` never even invokes the lazy hook
  (`mattermost/connector.py:677-753` gates on
  `self._channels.get(channel_id) is None`), so no collision arises. When
  it is *paused*, the hook does fire, `try_lazy_create()` finds the entry
  via its by-room scan (426-428) and **deliberately returns False**
  (551-567) — refusing to resurrect it, dropping the message. That's the
  correct behavior (an explicit pause must not be overridden), and a
  naive "any matching rule may create a watcher" unification breaks it:
  the wildcard rule would spin up a second, differently-named watcher for
  a room whose operator-set pause is thereby silently overridden. Note
  also that no config-load check cross-references exact entries against a
  same-connector wildcard rule's coverage today — verified absent from
  both loaders — so this overlap is currently permitted and invisible.
  Any unified precedence policy must preserve "a more specific rule
  claims the room even when its watcher is paused."
- **P2 — scheduler auto-wake could start a boot-failed watcher** (risk 4
  above), turning a legitimate startup failure into a silent retry loop
  against a watcher whose agent is unavailable.
- **P2 — an unrelated pre-existing gap found during this pass, worth
  filing separately:** `JobScheduler._get_sm_for_watcher()`
  (`scheduler.py:438-450`) uses a plain, non-reconstructing
  `get_watcher_config()` lookup on its failure-notification path — the
  same defect class as the `control.py` paths fixed in round 15, in a
  file round 15 didn't sweep. Not caused by unification; would be
  inherited by it.
- **P2 — `pause_watcher()`'s fabricated-state case has no unified
  reconstruction story.** Pausing a name with no prior `WatcherState`
  (770-777) fabricates one with `room_id=""`. Under "derive every config
  from rule + persisted room_id," that shape is unreconstructible. Needs
  an explicit decision (reject pausing unknown names, or give the shape a
  real path).

#### Blast radius

Test inventory across the whole `tests/` tree (no `tests/e2e` or
`helpers.py` references): **~100 tests by literal symbol grep, ~135
counting same-mechanism tests that reach the code via
`pause_watcher`/`resume_watcher`/connector hooks instead of naming the
symbol.** Dominated by `test_watcher_lifecycle_lazy_create.py` (~85,
the feature's dedicated file). Rewrite-not-touch-up categories:
`TestLazyCreationHook` (4), `TestReserveWatcherName` (6),
`TestInjectMessageWakesDormantWatcher` (3), ~6 of `test_control_server.py`,
1 in `test_schedule_cmd.py`. Roughly 20 more (`test_config_collect.py`'s
4 plus `test_config_loading.py`'s wildcard cluster of ~16) move in or out
of scope depending on whether the *config representation* is unified or
only the *runtime path*. Also of note: `dynamically_created` — an
on-disk format field — has **no dedicated serialization/round-trip
test**, so the persistence surface being changed has no direct
regression net today.

Two known config-tool bugs were re-verified during this pass and are
worse than the Open Items below record.
`find_mergeable_watcher_entry()` (`configtool/model.py:532-551`) doesn't
just produce an invalid config: traced end-to-end, it mutates the
in-memory document in place (`add_watcher_rooms()`, 587-598) turning
`room: "*"` into `rooms: ["*", "<newroom>"]`, which `_is_wildcard_room_entry()`
disqualifies as a rule, so it falls through to `_parse_one_watcher_entry()`
→ `_auto_watcher_name(connector, "*")` → `sanitize_room_for_name("*")`
returns empty → `ValueError`. `save()`'s validate-before-write gate does
protect the on-disk file, but the in-memory document stays corrupted and
dirty, and the only recovery is `reload()`, which discards every other
unsaved edit in that TUI session. And `_check_state_orphans()`
(`config_validate.py:229-254`) indexes only `config.watchers` (line 233),
so it emits a false "will be dropped" warning for exactly the states
`sync_watchers()` deliberately preserves.

#### Proposed design

The three decisions the analysis above says must be settled first are
decidable from the code, so they're answered here rather than deferred.

**Decision 1 — rule precedence: most-specific-wins, and a paused rule
still holds its claim.** Replace the "at most one `room: "*"` rule per
connector" load-time check with a two-tier claim model per connector:
an *exact* rule (`room: <name>`) claims exactly that room; a *wildcard*
rule (`room: "*"`, plus `exclude_rooms`) claims everything else it
matches. Load-time validation becomes: exact rules must be unique per
`(connector, room)`; at most one wildcard rule per connector (unchanged);
an exact rule overlapping a wildcard rule is **explicitly legal** and
means the exact rule wins — which is what makes the currently-unchecked,
load-time-invisible overlap described above a defined case instead of an
accident. Crucially, a room claimed by an exact rule stays claimed **even
while that watcher is paused**, so the wildcard rule never picks up its
traffic — exactly reproducing today's verified drop behavior
(`try_lazy_create()` 551-567) rather than silently overriding an
operator's pause with a second, differently-named watcher. All three
`self._watcher_rules[0]` sites (331, 396, 1166) become one
`_match_rule_for_room(room) -> WatcherConfig | None` helper implementing
this precedence, which is also the natural home for the `exclude_rooms`
check currently inlined in each.

**Decision 2 — the lazy-capable-connector gate keys on the room set, not
the entry shape.** Move the `_LAZY_CREATION_SUPPORTED_CONNECTOR_TYPES`
check (`config.py:1129`) out of "this entry parsed as a rule" and onto
"this rule's room set is unbounded/deferred," i.e. `rule.room == "*"`.
An exact rule needs no lazy-creation capability from its connector — its
room resolves by name at boot — so RC/Script/Voice exact rules keep
loading and keep working. This is the single change that makes unification
safe for three of the four connector types.

**Decision 3 — `wake_dormant_watcher()` needs a new persisted field; the
obvious candidate does not work.** `state.room_id` looks like a
ready-made "has this ever started successfully?" signal (it's written
only inside `_start_watcher()`, at line 1400), but it is not: on a
subscribe failure the rollback path **deliberately keeps** the state in
`_states` with `room_id` already populated (line 1571, with an explicit
comment — it preserves `context_injected`/`session_id` for the next
attempt). So a watcher that failed at boot is indistinguishable by
`room_id` from one that ran fine and went dormant — precisely the
distinction the `dynamically_created` gate (1016, 1031) is standing in
for today. Proposal: add `WatcherState.last_started_ok: str = ""` (ISO
timestamp), written only after `_start_watcher()` completes its final
step, and gate the scheduler's auto-wake on it being non-empty. Additive,
with a fail-safe default (`""` = never confirmed started = do not
auto-wake), and it expresses the real predicate rather than proxying it
through provenance.

**Data model.** `GatewayConfig.watchers` and `GatewayConfig.watcher_rules`
collapse into one `rules: list[WatcherConfig]`, where `room == "*"` marks
a wildcard rule and anything else is an exact rule. `WatcherConfig` is
unchanged as a dataclass except that `exclude_rooms` becomes
load-time-rejected on an exact rule (meaningless there — today it's just
silently ignored). `WatcherState` loses `dynamically_created` and gains
`last_started_ok`. `WatcherLifecycle.__init__` takes one `rules` list
instead of two; `_watcher_configs` survives only as an in-process cache
of *materialized* configs (rename it to say so), not as a second source
of truth.

**Function-by-function transformation.** Deleted outright:
`_reconstruct_dynamic_watcher_config`, `_find_or_reconstruct_watcher_config`,
`can_find_or_reconstruct_watcher`, `is_watcher_name_known` — replaced by
one `_materialize_watcher_config(name)` resolver plus a side-effect-free
`_knows_watcher(name)` probe (the probe/act split is worth keeping, but
for reasons unrelated to the static/dynamic asymmetry that created these
four). Also deleted: `_dynamic_state_still_matches_rule` (subsumed by
`_match_rule_for_room`), `sync_watchers()`'s phase-2 preserve/prune/log
branch (249-305), and `_start_watcher()`'s `dynamically_created`
carry-forward (1387, 1413) with its comment block (1405-1412). Rewritten:
`sync_watchers()` becomes one pass that, for every rule with a statically
known room plus every persisted state, resolves the owning rule via
`_match_rule_for_room` and either starts it (exact rules — eager, as
today) or preserves/prunes it (wildcard-derived — dormant until a
message); `try_lazy_create()` keeps its structure but takes its rule from
`_match_rule_for_room` and loses one of its two paused-checks (551-567
and 680-687 merge); `_build_watcher_config_from_rule()` gains the
exact-rule branch that carries `session_id` forward (moot if `session_id`
retirement lands first, which it should); `list_watchers()` gains
enumeration over persisted states so dormant watchers become visible —
a behavior gain, and it closes the existing `control.py:612-621` /
`448-450` gap for free; `_stop_processor()`'s linear `_watcher_configs`
scan (1690) becomes a dict lookup. Unchanged: `_get_watcher_lock`,
`_start_watcher`'s entire body apart from the two deleted lines,
`_provision_session`, `_cleanup_startup_session_best_effort`,
`_ensure_agent_available`, `_resolve_agent_name`, `stop_all`,
`save_state`, `get_watcher_state`, `get_processor`, `pause_watcher`/
`resume_watcher`/`reset_watcher` bodies (their simplification lives
entirely inside the resolver they call). In `control.py`,
`_resolve_watcher_entry()` loses its two-tier claimant structure and its
two ambiguity messages become one. In `config.py`, the
`_is_wildcard_room_entry()` *dispatch* disappears (one parser, with a
wildcard branch) while the deliberate `from_file()`/`collect_config()`
two-loader split stays; the two post-loop cross-entry checks become the
precedence validation from Decision 1.

**Sequencing.** (1) `session_id` retirement — still a hard precondition,
per "Also removing" above. (2) The `last_started_ok` state field, landable
on its own as a small additive change with its own persistence test (note
`dynamically_created` shipped without one, which is why this whole
on-disk surface has no regression net today). (3) `_match_rule_for_room`
+ the precedence validation, additive while both lists still exist.
(4) The list collapse and the lifecycle rewrite. (5) Independently of all
of the above and worth doing now regardless: the `list_watchers()`/
`get_all_watcher_names()` dormant-visibility fix, `_check_state_orphans()`,
`find_mergeable_watcher_entry()`, and the `scheduler.py:438-450` gap —
none of these depend on unification, and three of them are live bugs.

#### Recommendation

Do the unification, with the scope corrected: **one config shape and one
lifecycle code path, but keeping eager start.** "Lazy-init only" as
originally framed should be dropped, not deferred — it requires building
RC lazy-creation support *and* splitting subscribe from init, and that
split introduces exactly the kind of dual-path seam this refactor exists
to remove. The three previously-open decisions are answered above and
none of them blocks starting.

Given the ~135-test blast radius, that this is strictly larger than PR
#79 itself, and that step 5 of the sequencing contains three live bugs
worth shipping sooner, this belongs in its own PR after #79's remaining
findings land — not as more commits on an already-fifteen-round branch.
The breakage list above should get the adversarial round it didn't get
(see its method caveat) before implementation starts, since a missed
functional break in the lifecycle path is exactly what the last five
review rounds were made of.

## Startup ordering: root-cause design review (2026-08-07, REVERTED 2026-08-09 — see "Reverted" at the end of this section)

**Read this first if you're new to this section**: everything below was
built on the premise that #24/#27 (below) described a live, unclosed race
that needed an architectural fix. That premise was wrong — both had
already been closed by targeted fixes that shipped as part of PR #79
itself, before this design review even started. The `start_realtime()` /
dispatch-gate / two-queue mechanism this section designs, implements, and
then repeatedly patches was prophylaxis against a *hypothetical* future
instance of the same bug shape, not a fix for an open one — and that
prophylaxis cost four real, confirmed data-loss bugs (Findings A/C/D/E)
before the premise itself was checked. The mechanism has been fully
reverted; nothing described in "Options considered" or "Implementation"
below is live in the current code. Kept in full, not rewritten, as the
record of how that conclusion was reached — see "Reverted" at the end for
what actually ships and why. If you're deciding whether a *new* piece of
`sync_watchers()`-owned state needs similar protection, read "Reverted"
first, then verify against actual code whether it's already closed before
reaching for a gate.

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

**Decision: C, approved and implemented.** Round ten and eleven's
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

### PR #80 review, second round (2026-08-07/08) — Finding C: the same bug class a third time, now fixed at the actual root

GPT/Codex review found one more issue on the corrected design directly
above, confirmed real before accepting: **the per-channel queue used to
buffer events while the gate is closed was still constructed with
`asyncio.Queue(maxsize=_CHANNEL_QUEUE_DEPTH)` (depth 50).** With no worker
draining it while the gate is closed, the 51st event posted to one channel
during `sync_watchers()` hits `QueueFull`, and `_dispatch()`'s existing
`except asyncio.QueueFull: logger.warning(...)` handler drops it — silently,
and with the socket staying connected the whole time, so no reconnect-replay
ever runs to recover it either. This is the *same* data-loss bug class as
Finding A, just re-introduced by the fix for Finding A, with a higher
threshold (50 events, not 0) instead of being closed. Per the reviewer's
own framing: "the new gate reuses the existing 50-item bounded queue without
draining it."

The reviewer again suggested "replay from the restored watermark when
opening the gate" as an alternative. **Rejected again, for the identical
reason given for Finding A above**: a brand-new lazy-creatable room has no
channel/watermark state to replay from, so this suggestion cannot cover the
one case this entire feature exists to handle, no matter how many times it
recurs as a suggestion.

**Fix: make capacity manual and gate-aware, instead of relying on
`asyncio.Queue`'s built-in `maxsize`.** The queue is now always constructed
unbounded (`asyncio.Queue()`); `_dispatch()` checks
`self._dispatch_gate.is_set() and queue.qsize() >= _CHANNEL_QUEUE_DEPTH`
before enqueuing, and only drops-with-a-warning under that condition. While
the gate is closed, capacity is never checked — nothing can be dropped for
being "too much," matching the requirement that a closed gate must buffer
losslessly. Once the gate is open, a worker is guaranteed to be actively
draining, so the check is behaviorally identical to the pre-#80 semantics:
both `asyncio.Queue(maxsize=N).put_nowait()` and
`qsize() >= N` evaluated at call time reject exactly the same events.

**Accepted tradeoff, written down rather than left implicit (the exact
category of omission this section exists to close):** for the duration
between the gate closing and opening, one channel's queue can grow to
`(inbound rate) × (sync_watchers() duration)` with no cap at all. This
window is bounded in *time* — it ends the instant `sync_watchers()`
returns — and by realistic chat pace, unlike the steady-state case
`_CHANNEL_QUEUE_DEPTH` protects against (a handler stuck indefinitely).
This is the same memory-growth caveat already named against option B
above, accepted here for the same reason: a bounded delay beats an
unbounded, silent loss.

**Process note, said plainly rather than glossed over:** this is the third
data-loss finding in this same area, and the second one against a fix that
had already been reviewed once. Neither of the last two findings (A, C)
came from a design re-read — both came from an independent reviewer
reading the actual shipped diff and asking "what happens to the events in
between?" The lesson isn't "get one more review pass" in the abstract; it's
that **for this specific change, the failure mode is silent** (nothing
logs, nothing raises, no existing test fails) — so the bar for calling it
done has to be a test that positively proves delivery under load, not the
absence of an error. The regression tests added below are deliberately
built around that: they assert what *does* arrive, not just that nothing
crashes.

- `gateway/connectors/mattermost/websocket.py`: `_CHANNEL_QUEUE_DEPTH`'s
  comment corrected — it no longer describes an `asyncio.Queue`-enforced
  bound; the queue is unbounded, the constant is a manually-checked
  threshold applied only once the dispatch gate is open. `_dispatch()`'s
  docstring documents the accepted unbounded-while-closed tradeoff inline.
- Tests: `tests/unit/test_mattermost_ws.py` —
  `test_more_than_queue_depth_events_survive_a_closed_gate` (posts
  `_CHANNEL_QUEUE_DEPTH + 20` events to one channel with the gate closed,
  then opens it, and asserts every single one is delivered in order — the
  direct regression test for Finding C); `test_queue_still_drops_on_overflow_once_gate_is_open`
  (gate already open, worker parked mid-handler, fills the queue to exactly
  `_CHANNEL_QUEUE_DEPTH`, asserts the next event is dropped and never
  delivered — pins the pre-#80 drop behavior at the `_dispatch()` level,
  which no test did before this round, so a future change back to
  unconditional unbounded queuing would be caught here).

### PR #80 review, third round (2026-08-08) — Finding D: fixing Finding C created a new false-positive drop, fixed by splitting into two queues

GPT/Codex found a fourth issue in this same area, confirmed real: **Finding
C's fix (a single unbounded queue with a manual `qsize() >= _CHANNEL_QUEUE_DEPTH`
check applied once the gate opens) moved the loss window rather than
closing it.** If a channel's startup backlog exceeds `_CHANNEL_QUEUE_DEPTH`
(the exact scenario Finding C's own fix was written to allow), then the
moment the gate opens, `qsize()` starts at whatever the backlog grew to —
above depth — and stays above depth until the worker drains it back down.
Any *live* event arriving during that drain-down, even with a perfectly
healthy WebSocket and handler, hits the same `qsize() >= _CHANNEL_QUEUE_DEPTH`
check and gets dropped as if it were steady-state overflow, when it isn't:
nothing is stuck, the queue is just working through a legitimate backlog.
In the reviewer's own words: "making the startup queue unbounded merely
moves the loss window to immediately after the gate opens."

**Root cause of both C and D, named plainly rather than patched around
again: a single queue conflates two different things — "how much startup
backlog is left to drain" and "is the handler currently keeping up with
live traffic" — that need independent capacity semantics.** Comparing one
queue's size against one threshold cannot answer both questions correctly
at once, no matter where the threshold is set or when it's checked. Every
fix so far (unbounded-while-closed for C, gate-aware check for the same
fix) was a correct answer to the wrong question.

**Fix: two queues per channel, not one, matching the two distinct
concerns exactly:**
- `_channel_backlogs[channel_id]` — unbounded, written to only while the
  gate is closed. This is the startup buffer; nothing here is ever dropped
  for volume, by construction (there's no capacity check on this path at
  all).
- `_channel_queues[channel_id]` — bounded at `_CHANNEL_QUEUE_DEPTH` via
  `asyncio.Queue`'s own `maxsize` again (back to the original, pre-#80
  mechanism), written to only once the gate is open. A live event's fate
  now depends *only* on live-queue depth, never on how large the startup
  backlog was or how far the worker has gotten through it.
- `_channel_worker` waits for the gate, then fully drains `backlog_queue`
  (unbounded, in arrival order) before touching `live_queue` at all. This
  is safe because nothing can write to `backlog_queue` after the gate
  opens (`_dispatch()`'s branch on `self._dispatch_gate.is_set()` has no
  `await` before the write, and `_listen_loop` dispatches events strictly
  sequentially — confirmed by reading both, not assumed) — so once the
  worker starts the backlog-drain loop, that queue can only shrink.
  Draining backlog fully before live traffic also preserves overall
  arrival order: nothing buffered before the gate opened can be delivered
  after something that arrived post-gate.
- The handler-invocation body (semaphore + try/except around
  `self._handler(decoded)`) was pulled into one shared `_invoke_handler()`
  helper, used by both the backlog-drain and live-drain loops — three
  near-identical copies of that block across two fix rounds was exactly
  how this kind of thing keeps recurring.

**One overflow path is now real and is NOT a repeat of Finding D — said
explicitly so it doesn't get filed as a fifth finding:** if live traffic
itself exceeds `_CHANNEL_QUEUE_DEPTH` while the backlog is still draining,
live events do get dropped. That's genuine backpressure (the handler
can't keep up with live traffic on top of catching up on backlog) — the
distinguishing fact about Finding D was that a *healthy* handler with *no*
live-traffic overload still got events dropped purely because of backlog
volume. With two independent queues, that specific failure mode is gone;
true overload is a different, legitimate case with the same accepted
drop-with-a-warning behavior it always had.

**Scope note, said directly rather than glossed over:** this is the fourth
finding in this area, the third against a fix to the *same* mechanism
(`_dispatch()`/`_channel_worker`), and each individual fix was locally
correct for the case it addressed. That pattern is a signal about the
layer, not about care taken: transport-level buffering keeps growing new
edge cases because the actual problem — "don't let `WatcherLifecycle` see
an event before its state is ready" — lives one layer up, in
`WatcherLifecycle`/`try_lazy_create()`. Option B from the original design
review (a readiness gate inside `WatcherLifecycle` itself, awaited at the
top of `try_lazy_create()`) was passed over partly on the cost of updating
~50 existing tests in `test_watcher_lifecycle_lazy_create.py` — three
rounds of transport-layer surgery have now cost more than that estimate,
and touched code (`_dispatch()`, queue capacity, worker draining) with a
much larger blast radius than `try_lazy_create()` alone. **Not being
revisited in this PR** — landing this fix and getting #80 green is the
immediate goal — but if a fifth finding lands inside `_dispatch()` or
`_channel_worker` specifically, the answer should be to move the gate up
to `WatcherLifecycle`, not to patch the transport layer a fourth time.
This is a scope question for the repo owner, not a decision made unilaterally here.

- Tests: `tests/unit/test_mattermost_ws.py` —
  `test_live_event_delivered_while_backlog_still_draining` (backlog of
  `_CHANNEL_QUEUE_DEPTH + 20`, gate opens, handler is parked mid-drain via
  an `asyncio.Event` so a live event can be injected while backlog draining
  is still in progress, asserts the live event is delivered and delivered
  strictly after every backlog event — the direct Finding D regression and
  an ordering check in one); `test_stop_clears_backlog_queues` (the new
  `_channel_backlogs` dict is cleared by `stop()`, same as the pre-existing
  dicts — an omission the reviewer didn't catch this round but was worth
  closing proactively). Both pre-existing Finding C tests re-verified
  against the two-queue split: `test_more_than_queue_depth_events_survive_a_closed_gate`
  still passes (all backlog delivered, gate closed the whole time, never
  touches the live queue); `test_queue_still_drops_on_overflow_once_gate_is_open`
  still passes and now specifically exercises the live queue's `maxsize`
  path (gate was already open before any dispatch in that test, so backlog
  is empty and irrelevant — confirmed by re-reading the test, not assumed).

### PR #80 review, fourth round (2026-08-09) — Finding E, and the question that ended the mechanism instead of extending it again

A fifth issue landed (Finding E, P1, on `_channel_worker`'s strict
backlog-then-live draining order): with a large enough backlog, more than
`_CHANNEL_QUEUE_DEPTH` live events can arrive while the worker is still
occupied draining backlog, overflowing the live queue and dropping
messages even when the handler is fast enough to keep up with live
traffic — it's just sequentially blocked behind backlog by construction,
not actually overloaded. Real, per the reviewer's framing: "the new
separate live queue is still capped at 50 while these lines refuse to
drain it until the entire backlog is gone."

At this point two explicit instructions changed the shape of the
response: (1) fix it in the *right layer*, not with a fifth patch to this
one; (2) never again let "this would require updating many existing
tests" be a reason to avoid the architecturally correct fix — and two
independent advisor passes were requested specifically so this wouldn't
turn into another round of patching.

**First advisor pass: moving the gate into `WatcherLifecycle.try_lazy_create()`
was also wrong, and for a sharper reason than test-migration cost.**
Any gate placed on the handler call path — transport-level or inside
`try_lazy_create()` — inherits the same drop-vs-delay problem in different
clothes, plus a new one specific to `try_lazy_create()`: `_channel_worker`
holds `_callback_sem` (`_CALLBACK_CONCURRENCY = 20`, connector-wide) for
the full duration of `await self._handler(...)`. Twenty channels parked
awaiting a `WatcherLifecycle`-level gate during `sync_watchers()` would
hold all twenty permits connector-wide, stalling delivery to *every*
channel — including ones for static watchers that had already started
successfully. Worse than anything Findings A/C/D/E produced. The
generalizable test: **any fix that puts an `await` on the handler call
path inherits drop-vs-delay and (if it shares the semaphore) can cascade
connector-wide — regardless of which file the `asyncio.Event` lives in.**

**Second advisor pass, prompted by asking "does this need a gate at all?"
instead of "which layer should the gate live in": no gate is needed
anywhere, because #24 and #27 were never actually open by the time this
whole redesign began.** Verified directly against code, not assumed:

- **#24** (`_states` cold during the startup window): `try_lazy_create()`
  already has a disk-read fallback (added in PR #79's tenth round,
  untouched by any of this) that reads `self._state_store.load()` directly
  when in-memory `self._states` doesn't have an entry — explicitly
  documented as authoritative "this early in the process's life," because
  nothing has been mutated in memory that isn't already on disk. This
  closes the dormant/renamed-watcher lookup case independent of whether
  `sync_watchers()` has run yet.
- More fundamentally: `self._watcher_configs` (which the *first*,
  by-room collision check scans) is populated once at `WatcherLifecycle`
  construction, directly from loaded config — **not** built up
  progressively by `sync_watchers()`. A statically-configured room is
  findable via `existing_for_room` at every point in the connector's
  lifetime, including before `sync_watchers()` has reached it. If that
  room's watcher hasn't started yet, the existing check
  (`existing_for_room.name not in self._processors → return False`,
  PR #79's fourth round) correctly refuses and the triggering message
  drops — the same narrow, already-accepted "static watcher not yet
  subscribed" case this whole feature always lived with, not a new
  regression. Confirmed fail-safe, not corrupting: it refuses to act, it
  doesn't act on wrong state.
- **#27** (`_blocked_agents` empty during the startup window):
  `seed_blocked_agents()` (PR #79's eleventh round, also untouched by any
  of this) is the *first line* of `run_once()`, called before
  `connector.connect()` — `_blocked_agents` is correctly populated before
  the websocket can possibly open, independent of anything in this
  section.

**Conclusion: the entire `start_realtime()` / dispatch-gate / two-queue
mechanism has been reverted.** `gateway/core/connector.py`,
`gateway/connectors/mattermost/connector.py`,
`gateway/connectors/mattermost/websocket.py`, `gateway/core/session_manager.py`,
and their test files are back to exactly their state on
`feature/lazy-watcher-mm-creation` (verified via
`git diff origin/feature/lazy-watcher-mm-creation -- . ':!docs/*'` being
empty before this revert commit). `gateway/core/watcher_lifecycle.py` and
its tests were never touched by any of this — #24/#27's actual fixes were
never part of this branch, confirmed by the same diff being empty against
`watcher_lifecycle.py` for the whole lifetime of `feature/mm-startup-ordering`.
PR #80 is reduced to a docs-only PR: this section, in full, as the record
of how the conclusion was reached.

**The lesson, stated precisely because a nearby-sounding one is already in
memory and this is a different point:** the failure here was not
"rejecting Option B over test-migration cost" (real, and worth its own
correction, but secondary). It was that **the redesign was never
necessary in the first place** — #24 and #27 were reframed from "these
shipped fixes closed a real bug" into "a hypothetical future instance of
this bug shape might exist," and four rounds were spent paying for that
speculation before anyone checked whether the originally-motivating bug
was still open. Next time a design review starts from "here's a bug class
worth closing structurally," the first step is verifying the motivating
instance is still open against current code — not just true when the
finding was originally filed.

- Finding E's thread: closed as "mechanism deleted, not patched" —
  the reply states the WHY (solving a problem already solved one layer
  up, not "we found a simpler approach") so a future reviewer doesn't
  re-propose the same gate.
- Tests: full unit + integration suite re-run after the revert to confirm
  it's byte-for-byte equivalent to `feature/lazy-watcher-mm-creation`'s
  own (already-green) test state, not just "still green" by coincidence.

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

- **`_dynamic_state_still_matches_rule()`'s fast path never resolves the
  room when the active rule has no `exclude_rooms` (found during PR #79
  review, fourteenth round, finding #38).** A deleted/moved room's dynamic
  state is preserved on every startup forever, and its generated name
  stays reserved via `is_watcher_name_known()` forever too. Checked blast
  radius before deferring: `auto_watcher_name()` prefixes the *owning*
  connector's own name into every generated dynamic name, and
  `_reserve_watcher_name()` already excludes the requesting connector's
  own entry from its scan — so this can only ever be observed by a
  *different* connector with a *static* watcher explicitly named to
  collide with `<this-connector>-<room>`, a narrow, mostly-coincidental
  trigger. Proper fix needs a connector-agnostic "room genuinely gone"
  signal distinct from a transient resolution error (which must keep
  preserving conservatively) — `RoomNotFoundError` is currently defined
  independently by `mattermost/rest.py` and `rocketchat/rest.py` as two
  unrelated classes, with no shared contract on the `Connector` ABC
  (`gateway/core/connector.py`). Real fix is a 4-file change: a core
  exception + both `rest.py` implementations + this call site. Natural
  to build alongside RC's own lazy-creation path, since that's the point
  a second connector actually implements `resolve_room_by_id()` and the
  shared contract becomes worth having rather than speculative.
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
- **`gateway/config.py`'s `find_mergeable_watcher_entry()` doesn't exclude
  wildcard rules from config-tool room merging (found during PR #79
  review, fifteenth round).** Once `room: "*"` is a valid entry, the config
  tool still passes every raw watcher entry — rules included — to this
  merge-target search. Adding an ordinary named room with the same
  connector/agent/shared fields as an existing wildcard rule selects the
  rule as the merge target and rewrites it to `rooms: ["*", "new-room"]`;
  validation then correctly rejects that as malformed, so an operator with
  the common "one minimal wildcard rule" setup can't add a normal watcher
  through the TUI at all. Third instance of the exact same scope boundary
  as the two items above (`_check_state_orphans()`, `expanded_watchers()`)
  — config-tool surface work, not core lifecycle work, deliberately not
  fixed in this PR. Needs the same "how does the TUI represent/edit a
  rule" design those two are waiting on; the merge search should skip
  rule entries entirely once that design exists.
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
