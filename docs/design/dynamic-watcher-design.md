# Dynamic watcher design

Status: **design, not implemented.** Both connector assumptions have been
verified against live Rocket.Chat and Mattermost servers — see §6 for the
observed behaviour and what it settles.

---

## 1. Motivation

### 1.1 The current model

A watcher is a 1:1 binding between one room and one agent session, declared
in `config.yaml` and started at gateway boot:

```yaml
watchers:
  - name: rc-nest
    connector: rc-home
    room: nest
    agent: claude-main
```

`WatcherConfig.room` is a single concrete room name. `sync_watchers()`
resolves each one and starts a processor for it. There is no runtime
add-watcher path.

### 1.2 Why it needs to change

- **Rooms are created faster than config is edited.** A team spins up an
  incident channel and adds the bot to it. Nothing happens until someone
  edits `config.yaml` and restarts the gateway.
- **Every room costs startup time whether or not it is used.** Boot starts
  every configured watcher unconditionally, including rooms nobody has
  messaged in months.
- **Nothing is ever reclaimed.** A watcher's session lives until an
  operator removes it from config. There is no idle or expiry notion, so
  resource cost grows monotonically with the number of rooms ever
  configured.

### 1.3 Requirements

- **R1** — An operator configures *how* to build a watcher, not *which
  rooms* to watch. Room names are never enumerated in config.
- **R2** — A watcher and its session are created on first activity in a
  matching room, with no restart and no config edit.
- **R3** — Idle watchers release their runtime cost but keep conversational
  continuity; genuinely dead ones release everything.
- **R4** — One session serves exactly one room, always. This is a
  confidentiality boundary, not a tidiness preference — see §4.1.
- **R5** — Different rooms can be served by different agents with different
  settings, chosen by rule rather than by enumeration.
- **R6** — One code path. A watcher's configuration must be derivable
  identically from the message path, the control path and the scheduler.

---

## 2. Design

### 2.1 Watcher rules

A `watchers:` entry stops naming a room and starts describing how to build a
watcher for whatever rooms match it:

```yaml
watchers:
  - name: eng                    # rule identity, operator-supplied, unique
    connector: mm-home
    agent: claude-eng
    rooms:
      include: ["eng-*", "incident-*"]
      exclude: ["eng-archive"]
    session_idle_days: 7
    session_expire_days: 30
    # plus every existing watcher parameter: context_inject_files,
    # history_handoff, notifications, inherits, …
```

**Multiple rules per connector are allowed. The first match in config order
wins.** Precedence is explicit and positional; there is no scoring or
specificity heuristic.

Consequences that must be implemented rather than assumed:

- Patterns are compiled and validated **at config load**. An invalid pattern
  must never surface on the message-delivery path, where no operator is
  present to fix it.
- **Config order is load-bearing.** Any tool that rewrites `config.yaml`
  must preserve watcher entry order; reordering silently re-routes rooms.
- A rule that can never fire must be reported at load, in both forms: a
  pattern matching nothing, and a rule **fully shadowed** by an earlier
  rule whose pattern subsumes it. Both are invisible at runtime, because
  first-match means a shadowed rule never participates in any decision.
  Where shadowing cannot be decided statically, expose a per-rule match
  count so `list` can show a rule that has matched zero rooms.
- `session_id` is **rejected on a rule** (§2.4).

### 2.2 Routing

Each connector subscribes to **all** messages for its platform user and
never unsubscribes for lifecycle reasons. Per inbound message:

```
message → connector (no lifecycle knowledge)
        → router: first matching rule, in config order
            no match  → drop
            match     → manager.get_or_create(connector, room_id)
                          → deliver
```

An unmatched message is dropped deliberately. The connector knows nothing
about watcher lifecycle, which is both the correct separation and the reason
idle becomes cheap: **no unsubscribe means the connector's per-room state —
the dedup watermark and the recent-message-id window — survives an idle drop
untouched**, and recreation needs no re-subscribe on either connector.

Two costs this accepts:

- Per-room structures now scale with *rooms the bot is in* rather than
  *rooms with watchers*. Mattermost spawns one task and one bounded queue
  per channel id on first sight and never reaps them; a reaping hook is
  required (§5.2).
- Work already done before routing can be decided — dedup registration,
  watermark advance, attachment download, capacity preflight — is paid for
  messages that will be dropped. That path needs auditing so dropping is
  cheap rather than merely silent.

### 2.3 Identity and keys

Three distinct keys, deliberately:

| Purpose | Key |
|---|---|
| Watcher instance, sticky binding, per-room lock | `(connector, room_id)` |
| Persisted state record | `(connector, room_id)` |
| Display and CLI | `<connector>-<room_name>` |

`room_name` is carried on the state record for display and refreshed from
inbound messages. **Boot and recreation resolve by `room_id`, never by the
persisted name** — a name freed by a rename can be reused by a different
room, and resolving by name would bind an existing session to the wrong one.

Derived names need no disambiguating flag: connector names are validated
unique at config load, so `<connector>-<room>` is unique by construction —
**provided a connector never spans two namespaces of room names.** On
Mattermost a channel name is unique only within a team (§6.3), so this holds
exactly because one connector serves one team; the room *name* is really
`(team, channel)` even though only the channel part is used. The stable
`room_id` is the actual identity and is globally unique regardless, so a
name collision could only ever produce a confusing display, not a
mis-binding — but the display name is also a filesystem path component
(below), where a collision does real damage. Hence §4.5.

**Name derivation changes to percent-encoding.** The current sanitizer
collapses anything outside `[A-Za-z0-9._-]` to `-` and raises when the
result is empty. Replace with: percent-encode anything outside that set;
never raise; if the result exceeds a length cap, truncate and append a short
`room_id` hash. The cap is required because the derived name is a filesystem
path component in two places and percent-encoded CJK runs nine bytes per
character.

This is forward-looking rather than a present fix. Both connectors derive
room names from platform *slugs*, and both slug character sets already sit
inside the safe set — Mattermost's are lowercase alphanumeric plus `-`/`_`,
RocketChat's default validation is `[0-9a-zA-Z-_.]+` — so the sanitizer is
currently the identity function and neither collisions nor the raise are
reachable. They become reachable with the first connector whose platform
permits unicode channel names. The consequence is severe enough to design
for now: the derived name keys three namespaces (the state record, the
agent's system-prompt file, the attachment workspace symlink), and a
collision means one session serving two rooms, a system prompt naming the
wrong room, and an attachment path resolving into another room's files.

### 2.4 Sticky binding and materialization

Once a watcher exists for `(connector, room_id)` it stays bound to that key
until it expires. **Editing or deleting the rule that created it does not
rebind or destroy it.** Recreation after an idle drop uses the watcher's own
persisted config, not the current rule.

**That persisted config is a materialized per-watcher config, not a copy of
the rule.** At creation the rule is copied and two fields are overwritten
before anything is persisted:

- `name` → the derived watcher name
- `room` → the **concrete room name**, never the pattern

This matters because `WatcherConfig.room` is consumed as a concrete room in
at least five places, and the most damaging is the durable identity header
(`- **Room:** {wc.room}`), which is delivered as an appended system prompt
specifically so it survives compaction and is re-supplied on every turn. A
rule-shaped config would permanently tell an agent its room is `eng-*`. The
others: room resolution on the creation path and in `fetch-history`, the
backend session title, the reported room in `list`, and the scheduler's
label fallback. Worth an assertion and a round-trip test that a persisted
config's `room` never contains pattern metacharacters.

**`session_id` must not be settable on a rule.** Session provisioning gives
a config-pinned session id absolute priority and returns it unconditionally,
so a rule carrying one would hand *every room it matches the same session* —
violating R4 at config level rather than through a race. Reject at config
load; strip at materialization as a second line of defence.

What sticky binding buys:

- A rule edit cannot take a room from a running or paused watcher, so no
  second watcher can appear in a room whose pause an operator set.
- Agent drift becomes detectable. Because the watcher's own config records
  its agent, editing a rule's `agent:` cannot silently re-point a dormant
  session at a different backend — which would otherwise hand a session id
  created by one backend to another, since provisioning returns the
  persisted id and agent resolution only warns on an unknown name.

#### Two records, not one

A watcher persists **both** forms, because they answer different questions:

| Field | Content | Used for |
|---|---|---|
| `config` | materialized: concrete `room`, derived `name` | recreating the watcher |
| `rule_name` | the rule's operator-supplied identity | finding "my rule" in a later config |
| `rule` | the rule **as resolved at creation**, unmaterialized | detecting that the rule has since changed |

The materialized config cannot serve as the drift baseline: its `name` and
`room` are overwritten by construction, so diffing it against a rule would
report those two fields as changed on every comparison.

**Store the rule in its resolved form, after `inherits:` has been applied.**
Template inheritance is flattened at parse time — a parsed rule carries no
`inherits` field — so the resolved form is what a parser naturally produces,
and storing it means **an edit to a watcher template is caught as drift for
free**. Storing the raw YAML entry instead would let template changes escape
detection entirely.

A content hash of the resolved rule is sufficient for "has anything
changed?" and is worth deriving for cheap equality checks, but the full
content is what allows showing an operator *what* changed, or applying a
change field-by-field.

This is groundwork for automatic rebinding on config change, which is out of
scope here (§3). Two things that design will need, recorded now because they
shape what is worth storing:

- **Content drift and ownership drift are different checks.** Under
  first-match precedence a rule inserted *above* mine begins winning for my
  room without any rule's content changing. Detecting that requires
  re-running the match against the current ordered rule list, not a diff.
  A rule-content diff alone would miss it.
- **The agent's own definition is outside the rule.** A rule names
  `agent: X`, but X's `working_directory`, backend and permission settings
  can all change without the rule changing. Complete drift detection has to
  consider the resolved agent config too.

At scale the same rule content is duplicated across every room it matched.
For the expected range — tens to low hundreds of rooms — that is negligible
and simpler than the alternative. If it ever matters, normalize into a
rules table keyed by `(rule_name, content_hash)` with watchers referencing
it rather than embedding it.

### 2.5 Lifecycle

| State | In memory | On disk | Entered by |
|---|---|---|---|
| **active** | processor + session | record | first message in a matching room; or recreation from an idle record |
| **idle** | nothing | record, incl. session id, watermark, `dropped_at` | no activity for `session_idle_days`; or the bot being added to a matching room (§2.7) |
| **expired** | nothing | nothing (logged) | idle for `session_expire_days`; or the bot being removed from the room |
| **paused** | nothing | record, `paused=True` | operator only |

A watcher can therefore exist as a record before it has ever run — which is
what makes a newly-joined room listable and pausable before its first
message.

**Paused is a real state and is never auto-reclaimed** — not idled, not
expired. Once config no longer names rooms, pause is the only durable way to
mute one, so expiring a paused record would erase an explicit instruction.
For the same reason, `reset` must not silently clear `paused`.

**Resume returns a paused watcher to active and restarts its clock.**
`last_activity_at` is set at the moment of resume, so a watcher paused for
longer than `session_expire_days` does not expire the instant it comes
back — it gets a full idle period like any other active watcher. Stating it
because the alternative reading (pause accrues idle time invisibly, and
resume hands back something already due for reclamation) is a plausible
misimplementation of the same table.

**Idleness needs its own clock.** The existing `last_processed_ts` is a
message-dedup watermark with different semantics and cannot serve. A
gateway-owned `last_activity_at` is written by both the inbound path **and**
scheduled injection, or scheduled-only rooms idle out between fires. It must
also account for in-flight agent turns and outstanding permission requests,
so an idle drop cannot cancel an approval an operator is still reading.

**Boot recreates active records only**, which is what `dropped_at`
distinguishes — without it, boot cannot tell "was active, recreate" from
"was idle, leave dormant" and would recreate every room ever matched,
discarding the idle savings on every restart. `room_id` cannot substitute:
the subscribe-failure rollback deliberately keeps a record with `room_id`
populated, so a start failure is indistinguishable from a healthy record by
that field.

**Boot is serial, deliberately.** The cost asymmetry that makes this
acceptable: history handoff only runs for a newly created session, so
recreating a watcher that resumes an existing session skips both the history
fetch and the full model turn that delivers it.

| | new room (new session) | recreate (resume) |
|---|---|---|
| resolve room | 1 REST call | same |
| create session | 1–5s | resume, cheap |
| fetch history | 1 REST call | skipped |
| deliver history | **full model turn, 5–30s** | skipped |
| context, workspace, subscribe | ~1s | same |
| **total** | **~10–40s** | **~1–3s** |

Boot's common case is therefore the cheap one. Parallelising is a small
change when wanted — each start is independent and takes its own per-room
lock — which is the argument for deferring it rather than pre-building it.
What must be decided before boot recreation ships: whether one watcher
failing aborts boot, skips that watcher, or can leave partial state.

**Expiry reclaims everything**, or it leaves bookkeeping behind: the state
record, the backend session, injector retry state, session maps, the
system-prompt file, the attachment symlink and the attachment cache
directory. A failed backend session delete logs once and accepts the leak
rather than refusing to expire.

### 2.6 Connector-declared capability

Each connector declares whether its transport delivers **unsolicited
inbound** messages. Idle eligibility, eager-versus-lazy creation and
black-hole behaviour all derive from that one property rather than from
per-connector branching.

| Connector | Unsolicited inbound | Creation | Idle / expiry | Membership events |
|---|---|---|---|---|
| Mattermost | yes | lazy, on first message | eligible | yes (§2.7) |
| RocketChat | yes, via subscribe-all | lazy, on first message | eligible | yes (§2.7) |
| Script | **no** | eager | **never** | n/a |
| Voice | **no** | eager | **never** | n/a |

Membership events are a separate, optional capability: they register a record
early but never start a watcher, so a connector without them behaves
identically once the first message arrives.

**Mattermost** already receives, on one socket, every channel the bot is a
member of — and *only* those, verified in §6.2. The connector currently
discards events for channels it has no state for; that discard becomes the
routing hook. Because delivery follows membership, no membership gate is
needed, and the event itself carries channel name, type and team id, so
routing needs no REST lookup for ordinary channels.

**RocketChat** can receive messages for rooms it has not per-room-subscribed
to, using `stream-room-messages` with the reserved room id
`__my_messages__`. Verified working in §6.1. The server emits every message
to such subscribers, filters per message via an access check, and attaches a
`roomParticipant` flag distinguishing membership from mere readability — here
the gate *is* required, precisely because subscribe-all also delivers public
channels the account can only read. The access object also carries room type
and name, so ordinary channels need no REST lookup either; direct messages
are the exception, since they have no name to carry. Reaching this requires
real work in the RC connector (§5.2) — most of all splitting a key space that
subscribe-all otherwise collapses onto a single entry.

**Script and Voice have no inbound stream to discover from.** Script's
messages arrive through direct injection that bypasses the connector
entirely; Voice's rooms arrive as HTTP path segments. Both therefore require
**literal** `rooms.include` entries — no patterns — enforced at config load
for any connector declaring no unsolicited inbound. This keeps eager
creation possible (a concrete room is known) and turns an otherwise silent
misconfiguration into a load error.

Voice's path segment is also its only agent selector, which is why multiple
rules per connector is load-bearing rather than a convenience: two rules
with literal room patterns on one voice connector serve two agents on one
port. Voice additionally needs default-deny on unknown rooms — a typo'd path
must not spawn a fresh context-less session — and its unmatched-room reply
must say no route is configured rather than reporting backpressure.

### 2.7 Creation path

Ordering, which is where the cost and the correctness both live:

1. **Cheap synchronous rejects, above the room-state lookup**: system
   messages, own-message echoes, and the sender/allow-list/mention gate.
   These filters are synchronous; run them with no turn store so the
   precheck does not consume the agent-chain budget.
2. **Membership and scope gate**: on RocketChat, the `roomParticipant` flag
   the server computes per message — required, since subscribe-all also
   delivers public channels the account can merely read. On Mattermost no
   membership gate is needed: delivery *is* the membership signal, verified
   rather than assumed (§6.2). Mattermost does need a **team** gate, since one
   connector serves one team while the socket spans every team the account
   belongs to — and that gate must pass rooms with **no** team (DMs) through
   rather than rejecting them, or enabling it silently disables DM support
   (§6.3).
3. **Creation runs off the connector's handler path**, with the triggering
   message held in a **bounded** per-room buffer and replayed into the new
   processor. Creation is expensive — room resolution, session creation, a
   history fetch, a full model turn, context injection, filesystem setup —
   and Mattermost holds a connector-wide permit for the whole handler call,
   so synchronous creation stalls delivery for every channel once enough
   rooms start at once.
4. **A per-room single-flight guard is required at this step**, because
   step 3 removes the serialisation that was implicitly providing one:
   Mattermost's per-channel worker drains sequentially today, so two
   back-to-back messages for one new room cannot both trigger creation.
   Once creation is not awaited, they can — and the failure is silent,
   because the dispatcher registers processors in a list and fans out to
   all of them, so two processors in one room means two agents answering
   every message. The lock is keyed like the watcher, taken by the manager,
   and covers the existence check and the creation together.
5. **Register the processor before any capacity preflight is evaluated.**
   The preflight reports "no capacity" when no processor exists, so
   evaluating it first would reject the first message from every new room —
   and post a "server busy" notice into the room from a completely idle
   gateway. The preflight should also distinguish *empty* from *full*, so a
   routing miss is never reported as backpressure.
6. **Defer the trigger's dedup bookkeeping until it is accepted.** Message
   ids are currently registered before the handler and rolled back only on
   a negative return, never on an exception. Since a brand-new room has an
   empty watermark, reconnect replay skips it too — so a creation that
   throws loses the message with nothing visible in the room.
7. **Cap concurrent creations.** Queue depth bounds messages per room;
   nothing bounds rooms being created at once. Over-cap triggers get an
   honest "starting up" response.

Because the trigger is held, its timestamp is known — so **history handoff
fetches messages strictly older than it**. Without that the newest-history
block contains the very message that triggered creation, which is then
delivered twice: once inside a history turn whose response is discarded, and
again as the live prompt. Buffering alone does not fix this.

**Notifications are suppressed on idle and reactive paths**, reserved for
operator-initiated pause and resume. Otherwise every idle room announces the
agent offline each idle period and online on each burst.

**DMs are opt-in per rule**, with 1:1 and group DMs distinguished. Group DMs
have no stable display identity on either platform, so deriving a name from
the sender is wrong — it would change with whoever speaks.

Two things make DMs structurally unlike channels, both verified in §6.3:

- **A DM has no team**, so the Mattermost team gate cannot classify it.
- **A DM reaches every socket the bot account has open.** There is nothing to
  withhold at the transport: Mattermost has no per-room subscribe on the wire
  at all — `subscribe_room` is pure in-memory bookkeeping with zero I/O — and
  the server pushes account-level events to every session. So two connectors
  sharing an account *both* receive every DM, unavoidably, and the
  de-duplication has to happen in routing.

**DMs cannot be expressed as a name pattern.** Mattermost's DM
`channel_name` is the opaque `<userid>__<userid>` form and Rocket.Chat omits
the room name for DMs entirely, so no `include:` pattern can match one on
either platform. They need a distinct key:

```yaml
connectors:
  - name: mm-eng                     # same bot account, different teams
    type: mattermost
    server_url: https://mm.example.com
    team: eng
    username: acg-bot
  - name: mm-sales
    type: mattermost
    server_url: https://mm.example.com
    team: sales
    username: acg-bot

watchers:
  - name: eng-channels
    connector: mm-eng
    agent: claude-eng
    rooms:
      include: ["eng-*", "incident-*"]
      exclude: ["eng-archive"]

  - name: direct-messages            # exactly one rule per bot account
    connector: mm-eng                # may opt into DMs
    agent: claude-assistant
    rooms:
      direct: true                   # 1:1 DMs. Default false.
      # group_direct: true           # separate opt-in — no stable identity

  - name: sales-channels
    connector: mm-sales
    agent: claude-sales
    rooms:
      include: ["sales-*"]
      # no `direct:` key, so mm-sales never routes a DM
```

**The opt-in is the gate.** Routing classifies the room first, then applies
the gate that fits it:

```
inbound message
├─ DM?  (channel type D/G, or Rocket.Chat room type d)
│    └─ any rule on THIS connector with rooms.direct / group_direct?
│         yes → first such rule
│         no  → drop
└─ team channel?
     └─ event's team == this connector's team?
          no  → drop
          yes → first rule whose include/exclude matches the channel name
```

Classifying before gating avoids a "an empty team id means pass" special
case, which is subtle and easy to regress.

So in the example above `mm-sales` receives every DM on its socket and drops
each one, because none of its rules opts in — no special-case code, just an
absence of a matching rule. The config-load check in §4.5 is therefore not
what makes the normal path correct; it exists to catch the *misconfiguration*
where an operator sets `direct: true` under both connectors of one account,
which routing alone cannot detect since each connector sees only its own
rules.

A DM-only connector is not expressible as an alternative: `team` is a
required field on a Mattermost connector, so DM ownership has to attach to one
of the team connectors.

#### Membership events: register on join, do not start

Both discovering connectors can tell when the bot is added to a room —
Mattermost via a `user_added` event targeted at the new member, RocketChat
via a subscriptions-changed notification with an inserted action. Without
using it, a bot added to fifty rooms shows nothing in `list` until somebody
talks in each one, and an operator cannot pause a room before its first
message.

Using it to *start* a watcher is the wrong end of the trade, though: it pays
a session and a full history-handoff model turn for every room the bot is
added to, including ones never used. That is the eager cost this design
exists to avoid.

**So a membership-add event creates the record in `idle` state.** The rule is
matched, the watcher is materialized and persisted, and nothing is started.
The room becomes listable and addressable immediately; the first message
wakes it through the normal path. This reuses the existing idle state rather
than inventing a fourth one.

Three properties this must have:

- **It is a supplement, never a replacement.** Mattermost's websocket has no
  gap detection or replay on reconnect, so an add event that arrives during a
  disconnect is simply gone. Message-triggered creation stays the safety net,
  which means it must remain correct for a room with no record at all.
- **The rule is snapshotted at join time**, so a rule edited between join and
  first message does not apply to that room (§2.4, sticky binding). This is
  consistent but worth knowing.
- **Removal is symmetric.** A membership-remove event should expire the
  record: there is no reason to keep a session for a room the bot can no
  longer read, and RocketChat's server tears down its own subscription
  anyway. Expiry rather than idle, since the room is gone rather than quiet —
  subject to the paused-record rule (§4.4).

Connectors that declare no unsolicited inbound have no membership stream and
are unaffected.

### 2.8 The watcher manager

One class owns the lifecycle. Callers ask whether a watcher exists and get
one; they never drive creation, idling or expiry.

```python
WatcherKey = tuple[str, str]          # (connector, room_id)

class WatcherManager:
    # resolution — the only place a display reference becomes a key
    def resolve(self, ref: str, connector: str | None = None) -> WatcherKey
        """Accepts a derived display name, or a room name/id plus a
        connector. Raises on unknown or ambiguous input."""

    # the two ways to obtain a watcher
    async def get(self, key: WatcherKey) -> Watcher | None
        """A READY watcher. Recreates from the persisted record if the
        watcher is idle — callers never observe idleness. Returns None only
        when there is no record and no matching rule."""
    async def get_or_create(self, key: WatcherKey, room_name: str) -> Watcher | None
        """As get(), and additionally creates a first-ever watcher from a
        matching rule. The message path. None when no rule matches."""

    # views and verbs
    def list(self, state: StateFilter = StateFilter.OPERABLE) -> list[WatcherView]
    async def pause(self, key: WatcherKey) -> None
    async def resume(self, key: WatcherKey) -> None
    async def reset(self, key: WatcherKey) -> None
    async def expire(self, key: WatcherKey) -> None
```

**Idleness is invisible to callers.** `get` returns a watcher that is ready
to use; if the record is idle it is recreated first. There is deliberately
no "is it resident?" variant on the routing path — a caller that had to
branch on state would be reimplementing the lifecycle, which is the thing
this class exists to own. Two consequences to accept:

- **`get` is async and can take seconds.** Recreating a watcher resumes a
  session and re-subscribes (~1–3s, §2.5). Every caller must tolerate that,
  including the control server and the scheduler.
- **`get` refuses to override an explicit pause** (§4.4). A paused watcher is
  not "idle with extra steps"; `get` on a paused record returns nothing
  usable rather than silently unpausing. Only `resume` clears it.

Recreation is not optional, because an inbound message cannot be the only
route back: scheduled work does not arrive through the inbound path at all.
Injection enqueues straight onto a processor, so an idled room has none,
injection fails, and the scheduler then advances `next_run` to avoid a retry
flood — the job is skipped silently, and skipped again every period,
forever. For Script this is the *only* path.

`list` enumerates **persisted records**, not live processors, or idle and
paused rooms become invisible to commands that can already act on them. It
takes a composable state filter, and the default is deliberately not "all":

```python
class StateFilter(Flag):
    ACTIVE = auto()
    IDLE   = auto()
    PAUSED = auto()
    OPERABLE = ACTIVE | PAUSED        # the default
    ALL      = ACTIVE | IDLE | PAUSED
```

**Default is active + paused** — the watchers an operator is realistically
about to act on. A paused watcher belongs in the default view precisely
because it is waiting on a human decision. Idle is informational: the bot
knows about the room, but nothing is running and nothing is being withheld.

This matters more once membership events register joined rooms as idle
(§2.7): a bot in two hundred channels would otherwise have a `list` dominated
by rooms nobody has ever spoken in, burying the handful being worked on.
Idle is one flag away when the question is "what does the bot know about"
rather than "what is it doing".

**Rooms with pending scheduled jobs are exempt from expiry, not from
idling.** Idling such a room is harmless: the job fires, `get` recreates the
watcher, the injection proceeds. Exempting it from *idling* would be actively
wrong — a job scheduled a year out would hold a session resident for a year
for nothing. Expiry is the destructive step, because it deletes the record
the recreation reads from, leaving the job pointing at nothing. So expiry
skips any room with a pending job.

One interaction worth stating: a room idle for a long time will have had its
session deleted by the agent backend regardless of what ACG persisted
(§3, backend retention). The recreation path handles that through the typed
session-not-found error — a new session is minted and handoff re-runs — which
is why that error, and not TTL arithmetic, is the load-bearing mechanism.

Configuration resolution is a **pure function of (rule, room)** reachable
identically from the message path, the control path and the scheduler (R6).
Today five divergent resolution predicates exist across the control server,
lifecycle, scheduler and session manager; the manager replaces all of them.

The shortcut to avoid: registering materialized configs into a mutable
process-wide config list. In-memory registrations vanish on restart while
on-disk records persist, and boot then eagerly starts every room ever seen.

---

## 3. Trade-offs

**Accepted:**

- **Rule edits do not affect existing watchers — for now.** A watcher keeps
  its materialized config until it expires. This is the price of sticky
  binding, and it is what prevents a rule edit from stealing a room or
  overriding a pause. **Automatic rebinding on config change is planned and
  deliberately out of scope here**; storing the originating rule at creation
  (§2.4) is groundwork for it, so the follow-up is unblocked rather than
  merely postponed. Until then, operators force a rebind per room with
  `expire`, at the cost of that room's conversational continuity.
- **`list` output becomes dynamic.** There is no longer a static set of
  watchers derivable from `config.yaml`; the answer to "what is being
  watched" is runtime state, so tooling must query the daemon. `acg list`
  defaults to **active + paused** — what an operator is about to act on —
  with `--all`, `--active`, `--idle` and `--paused` for the rest. Idle is
  excluded by default because with membership-event registration (§2.7) it is
  the largest and least actionable group.
- **Idle and expiry are RocketChat and Mattermost only.** Script and Voice
  never reclaim, because neither has an inbound stream to wake from and
  neither supports history, so a fresh session would lose continuity with
  nothing to soften it.
- **First-match precedence is positional.** Reordering rules changes
  routing. This is simple and predictable, and it is the reason config
  order must be preserved by tooling.
- **Percent-encoded names are ugly** for rooms outside the ASCII-safe set.
  Correctness over aesthetics, and unreachable on current platforms.
- **A room's first message pays creation latency**, roughly 10–40s, most of
  it one model turn for history handoff. Acceptable in context: an agent
  turn on a large model can take comparable time anyway, so the first reply
  in a brand-new room being slower is not a distinct class of experience.
  Waking an idle room costs ~1–3s because the session resumes.
- **The connector processes messages for rooms no rule matches.** With
  subscribe-all, every message the bot can see reaches the gateway and is
  filtered, including rooms that will never have a watcher. The cost is
  per-message filter work plus one bounded queue and worker per room the bot
  is in (§2.2). It is accepted because the alternative — the connector
  consulting rules to decide what to subscribe to — puts lifecycle knowledge
  in the transport layer and reintroduces per-room subscribe management,
  which is the coupling this design removes. The pre-routing work audit in
  §2.2 exists to keep the dropped case cheap.
- **Migration is manual and documented.** The config change is breaking:
  `room:`/`rooms:` becomes `rooms.include`/`rooms.exclude`, and TTLs move
  from the agent to the rule. There is **no auto-migration and no
  backward-compatibility shim** — a leftover old field is a hard load error
  naming the replacement, and the release ships a migration guide. Both
  alternatives were considered and rejected: silently accepting old fields
  means two schemas live in the loader indefinitely, and rewriting the
  operator's `config.yaml` in place is a surprising thing for a gateway to
  do to a file a human owns. A one-time documented edit is cheaper than
  permanent dual-path parsing.

**Rejected:**

- **Lazy-only, with no eager start.** Only Mattermost and RocketChat can
  discover rooms from inbound traffic. Removing eager start would mean
  Script and Voice watchers never start at all.
- **One rule per connector.** It cannot express Voice's one-port,
  multiple-agent form, and it forces either a second mechanism or a
  precedence policy anyway.
- **Deriving TTL from each agent backend's own session retention.** Clamping
  ACG's TTL to the backend's declared retention is one-sided and cannot help
  in the direction that matters: a backend configured to delete transcripts
  sooner than ACG expects will do so at *any* TTL value, while
  over-estimating merely wastes a recreate. The **typed session-not-found
  error** is the load-bearing mechanism instead — when a resume reports the
  session is gone, mint a new one and re-run handoff.

  Declared retention is still used, in two non-binding ways: as a hint for
  choosing defaults, and as a **config-load warning when a rule's
  `session_expire_days` exceeds the retention its agent's backend declares**.
  That turns a silently-degrading configuration into something the operator
  sees at the moment they write it, without making the loader enforce a limit
  it cannot actually guarantee. The warning must be worded as a likelihood
  rather than a certainty: a declared value is an assumption about the
  backend's own settings, not a contract. (Claude's default cleanup period is
  30 days but is user-adjustable and skipped in several documented paths;
  OpenCode has no automatic expiry at all, so no warning is possible there.)

---

## 4. Invariants the implementation must preserve

### 4.1 One session, one room

A session is bound to a room in three artifacts: the durable identity header
naming the room, re-supplied every turn; the transcript, which contains that
room's fetched history and every prior turn's server-injected
`[connector #room | from: user | role: …]` prefix; and the session-to-room
map, which is single-valued, so permission prompts for one room's tool calls
would surface in another. **A reused session is a cross-room data leak.**

The enforcement point does not exist today: processor registration appends
to a per-room list and dispatch fans out to every entry, so a duplicate
degrades silently into two agents answering every message. Registration must
become reject-or-replace, with a test asserting it.

### 4.2 State persistence must merge

State is currently written wholesale from the in-memory map, and boot seeds
that map only from configured watchers before saving unconditionally. With
rooms unenumerated, that combination erases every persisted record — session
ids, watermarks and paused flags — on the first boot, before any message
arrives. Six further save sites re-truncate on any later command.

Either every persisted record stays resident in the in-memory map, or the
save becomes a merge. The state file is the room registry under this design;
it cannot be a projection of config.

### 4.3 Watermark capture precedes unsubscribe

Processor teardown unsubscribes before reading the connector's watermark,
and unsubscribing at zero refcount discards the per-room state that read
depends on, so the persisted watermark silently stays stale. §2.2 removes
this from the idle path by never unsubscribing there, but it remains live on
pause, reset and shutdown. Fix with a reproducing test.

### 4.4 An explicit pause is never overridden

Not by creation, not by wake, not by rule edits. Corollary: expiry must skip
paused records.

### 4.5 One bot account, one connector — and one owner for direct messages

Under subscribe-all, a connector receives everything its bot account can see.
Two connectors sharing an account therefore receive **identical** streams, and
every room matching rules on both gets two watchers — two agents in one room,
which is §4.1 again. The `(connector, room_id)` key cannot detect it: the
records differ in their connector component, and each connector writes a
separate state file.

Config load must reject two connectors that share a `(server_url, bot
identity)` pair, with one exception and one condition:

- **Exception — Mattermost connectors scoped to different teams.** A channel
  name is unique only within a team (§6.3), and one connector serves one team,
  so team-scoped connectors both disambiguate their channels and keep their
  derived names unique. The team gate on the routing path is what enforces
  this, which is why it is an invariant and not an optimisation.
- **Condition — at most one of them may handle direct messages.** A DM has no
  team, so the team gate cannot separate it, and it is delivered to every
  socket the account has open (§6.3). DM handling is opt-in per rule (§2.7), so
  config load must additionally reject more than one DM-enabled rule per bot
  account.

The general rule matters beyond Mattermost: Rocket.Chat has no team scoping to
fall back on, so two Rocket.Chat connectors sharing an account duplicate
*everything*, not just DMs.

---

## 5. What changes

### 5.1 Prerequisites

These block the design and are independently shippable:

1. **State save becomes a merge** (§4.2), with a test asserting an idle
   room's record survives a save driven by an unrelated command.
2. **Watermark capture before unsubscribe** (§4.3), with a reproducing test.
3. **Config-tool room merging must exclude roomless rules.** The merge
   target search matches on a hardcoded field allowlist that is blind to
   rule-ness, so adding a room would rewrite a rule in place into a
   single-room entry. The result is valid YAML, so validation passes and
   the write commits — silently dropping every other room's watcher at the
   next start. This must land before a roomless rule is expressible.
4. **Ten hardcoded watcher-field lists collapse into one declarative
   table** — template forbidden keys, two shared-field sets, known fields,
   template fields, template defaults, shared field keys, required field
   keys, the split loop, and the JSON schema — plus the user guide. None
   are auto-derived, and the design adds fields to all of them.

### 5.2 Connector interface

```python
class Connector(ABC):
    # new
    @property
    def delivers_unsolicited_inbound(self) -> bool: ...
    async def resolve_room_by_id(self, room_id: str) -> Room: ...
    def reap_room(self, room_id: str) -> None: ...

    # new, optional — implemented only where a membership stream exists
    def register_membership_hook(self, hook: MembershipHook) -> None: ...

    # existing, semantics changed
    async def subscribe_room(self, room: Room, ...) -> None:
        """Local bookkeeping only; no longer gates delivery."""
```

`MembershipHook` receives added/removed events for the bot's own membership
(§2.7). Mattermost and RocketChat implement it; the base is a no-op, so a
connector without a membership stream needs no carve-out.

- **Mattermost**: replace the unknown-channel discard with the routing hook;
  hoist the system-message and own-message checks above the state lookup
  (both kinds are delivered, §6.2); take channel name, type and team id from
  the event rather than a REST call, treating an empty `data.team_id` as
  "not a team channel" (DMs) rather than a failure; use
  `channel_display_name` for a DM's identity, since `channel_name` is the
  opaque `<id>__<id>` form; add channel reaping. `resolve_room_by_id` is
  still needed, but as a fallback rather than on the hot path — and adding it
  means reconciling the two room-identity paths, since name-based resolution
  stores the configured string and cannot report DM type while id-based
  resolution returns the server's name, and only one form may reach the
  prompt prefix, session title and history.
- **RocketChat**: make `rid` authoritative rather than the DDP stream event
  name — confirmed necessary, since the event name is the literal reserved id
  and would otherwise collapse every room onto one queue and worker (§6.1);
  split the single room-id key space into a subscription registry keyed by
  stream and a dispatch registry keyed by room; thread the access object
  through instead of discarding it — it carries `roomParticipant` for the
  membership gate plus room type and name, which is what spares the routing
  path a REST lookup; map the raw `c`/`p`/`d` type letters onto the internal
  channel/group/dm vocabulary that history fetching depends on; resolve a DM's
  display identity separately, since the access object omits `roomName` for
  type `d`; **add a system-message filter to the live path** — the REST path
  has one and the live path does not, and under subscribe-all every
  join/leave/rename in every readable room now arrives (`t: "au"` observed);
  make subscribe local-only and never send a stream-level unsubscribe from a
  per-room call; attempt subscribe-all with a clean per-room fallback on
  `nosub`.
- **Voice**: default-deny unknown rooms; replace the busy reply for a
  routing miss; evict room state on expiry.
- **Script**: make the reply queue per-room before a rule may match more
  than one room, and surface the injection handler's result so an unmatched
  message fails fast instead of blocking.

### 5.3 State schema

Records are keyed on `(connector, room_id)`. Added to each record:

| Field | Purpose |
|---|---|
| `room_name` | display; refreshed from inbound messages |
| `connector`, `agent` | so a rule edit cannot silently re-point a dormant session |
| `created_at` | audit |
| `last_activity_at` | the idle clock (§2.5) |
| `dropped_at` | distinguishes was-active from was-idle at boot |
| `config` | the materialized watcher config used to recreate |
| `rule_name` | which rule created this watcher |
| `rule` | that rule as resolved at creation — the drift baseline (§2.4) |

Each field lands in three places — dataclass, current-format branch, legacy
branch — and **must ship with a round-trip serialization test**. This
on-disk surface has no serialization test today, which is why every
addition here carries one.

`config` and `rule` are both nested structures rather than scalars, so the
serialization test must cover nesting and the empty/absent cases, not just
presence.

### 5.4 Config schema

- `watchers[].room` / `.rooms` → `watchers[].rooms.include` /
  `.rooms.exclude`, patterns, order-significant.
- New `watchers[].rooms.direct` and `.group_direct` booleans, both defaulting
  to false — DMs cannot be matched by name pattern on either platform (§2.7).
- `session_idle_days` / `session_expire_days` move from the agent to the
  rule, so two rules sharing an agent can differ.
- `session_id` rejected on a rule.
- Literal-only `rooms.include` enforced for connectors declaring no
  unsolicited inbound.
- Pattern compilation, never-firing and shadowed-rule detection at load.
- Warning when a rule's `session_expire_days` exceeds the session retention
  its agent's backend declares (§3).
- **Rejection of two connectors sharing a `(server_url, bot identity)` pair**,
  except Mattermost connectors scoped to different teams — and among those, at
  most one DM-enabled rule per bot account (§4.5). Only connector *names* are
  checked for uniqueness today.

**Migration is manual.** Every removed or moved field is a hard load error
naming its replacement — no silent acceptance, no auto-rewrite of the
operator's file, no dual-path parsing. The release ships a migration guide
with the mechanical edits, following the precedent already set for earlier
breaking config changes. Rationale is in §3.

### 5.5 Config tooling

Split the single Watchers view into a config-backed **Rules** tab and a
runtime-backed **Sessions** tab. Editing a rule and acting on a session are
different operations on different sources of truth; sharing one screen is
what makes §5.1's corruption reachable.

- Rules rows keyed by **list index**, which is also what preserves the
  ordering §2.1 depends on.
- Sessions rows keyed by `(connector, room_id)`, each labelled with its
  originating rule and **marked when that rule no longer exists or has
  changed since materialization**.
- Deleting a rule warns with the live and on-disk session counts it strands
  and the scheduled jobs it orphans.
- Four display states specified rather than discovered: daemon down → Rules
  only with an explicit note, never an empty table implying zero rooms;
  daemon up → both; config unparseable → the existing error banner; and the
  case with no precedent — the daemon's config is frozen at startup while
  the tool edits the file, so a live session can reference a rule already
  deleted on disk.
- The daemon query must not block the paint; refresh asynchronously with a
  spinner and degrade to "daemon down" on timeout.

### 5.6 Order

1. §5.1 prerequisites.
2. Rule parsing: patterns, include/exclude, order preservation,
   literal-only enforcement, shadowing detection.
3. State schema and its serialization tests.
4. Processor registration becomes reject-or-replace; capacity preflight
   distinguishes empty from full.
5. The watcher manager: resolution as a pure function of (rule, room),
   materialization, the per-room lock, transparent recreation in `get`, the
   four-state lifecycle.
6. Routing: connector subscribes to everything, router walks rules,
   unmatched dropped — with the pre-routing cost audit. Mattermost first,
   then RocketChat.
7. The creation path in §2.7's ordering.
8. `list` with its state filter, in the control server and the CLI — before
   the idle tick, so there is a way to observe what idling does.
9. The idle tick, for connectors declaring unsolicited inbound.
10. Expiry, with full reclamation.
11. Membership-event registration (join → idle record, leave → expire). Last
    of the runtime work because it is an optimisation over the
    message-triggered path, which must be correct on its own first.
12. Config tooling.
13. The migration guide, shipped with the release that lands the schema
    change.

---

## 6. Verified platform behaviour

Both connector assumptions were resolved against a live Rocket.Chat and
Mattermost instance. Reproducible with `scripts/probe_a1_rc.py`,
`scripts/probe_a1_rc_followup.py` and `scripts/probe_a2_mm.py`.

### 6.1 Rocket.Chat: `__my_messages__` works, and carries what routing needs

The subscription is accepted (`ready`, never `nosub`) and delivers messages
for rooms the account never per-room-subscribed to. Frame shape:

```json
{"msg":"changed","collection":"stream-room-messages","id":"id",
 "fields":{"eventName":"__my_messages__",
           "args":[ {...message...},
                    {"roomParticipant":true,"roomType":"p","roomName":"sandbox"} ]}}
```

| Observed | Design consequence |
|---|---|
| `fields.eventName` is the **literal** `"__my_messages__"`, not the originating room id | **The key-space split is required.** Current fan-out reads `eventName or rid` with eventName winning, so every room would collapse onto one queue and worker. |
| `args` has length 2 — message first, access object second | `args[0]` parsing survives unchanged; `args[1]` must be threaded through rather than discarded. |
| The access object carries `roomParticipant`, `roomType` **and** `roomName` | **No by-id REST resolver is needed on the routing path** for channels and groups — type and name arrive with the message. |
| For a DM the access object is `{"roomParticipant":true,"roomType":"d"}` — **`roomName` is absent** | A DM has no name to carry, so a display identity must be resolved another way (REST lookup, or derived from the sender). This is the one case that still needs a resolver. |
| `roomParticipant` is `true` for a room the account belongs to and `false` for a public channel it can merely read | This is the membership gate, server-computed per message, exactly as needed. |
| Own messages **are** delivered | Own-message filtering is required (already present). |
| System messages **are** delivered — observed `t: "au"` for a member-added event | **A `t`-field filter is required on the live path.** Only the REST history path filters system messages today; under subscribe-all every join/leave/rename in every readable room arrives. |
| `roomType` uses Rocket.Chat's raw letters: `c` public channel, `p` private group, `d` direct | Needs mapping to the internal `channel`/`group`/`dm` vocabulary that history fetching depends on. |
| Second `sub` parameter `false` (what ACG sends) vs `true` (what Rocket.Chat's own SDK sends) | No observable difference in the emitted frames. No change needed. |

### 6.2 Mattermost: delivery tracks membership, not readability

The docstring claim holds. With the probe account a **non-member** of a public
channel it could nonetheless read — membership lookup returning 404 while the
channel appeared in its own visible channel list — a post in that channel
produced **no `posted` event at all**, while posts in a channel it belonged to
arrived normally.

| Observed | Design consequence |
|---|---|
| No event for a readable-but-not-joined public channel | Delivery **is** the membership signal. No additional membership gate is needed, and the creation path does not need a REST membership check. |
| `data.channel_name`, `data.channel_type` and `data.team_id` are all populated for channel posts | Routing can resolve name, type and team **from the event**, skipping a REST call — which matters for keeping work off the semaphore-held handler path. |
| `data.team_id` is empty for DMs; `broadcast.team_id` is empty always | Use `data.team_id`, and treat empty as "not a team channel" rather than a lookup failure. |
| DM `channel_name` is the opaque `<userid>__<userid>` form, but `channel_display_name` is the counterpart handle | Mattermost supplies a usable DM display name where Rocket.Chat does not. |
| Own messages and system messages (`system_join_channel`, `system_leave_channel`) are delivered | Both filters are required; the existing non-empty-`type` check covers the system case. |

### 6.3 Mattermost: channel names are per-team, DMs are per-account

Two further observations, both with design consequences:

**A channel name is unique only within a team.** Creating `sandbox` in a
second team succeeded while `sandbox` already existed in the first, yielding
two distinct room ids. So `channel name` is not a global identifier on
Mattermost, and a derived display name of the form `<connector>-<channel>` is
unique only because a connector is scoped to exactly one team. That scoping
stops being an organisational preference and becomes load-bearing — see §4.5.

**A direct message belongs to no team and reaches every socket the account
has open.** Observed by opening two independent sessions for one bot account:
a single DM was delivered to **both**, with `team_id` empty on each. A
team-channel post was likewise delivered to both sockets, because the account
belongs to that team.

The channel case is handled by the team gate — a connector scoped to team B
discards a team-A channel event. **The DM case is not**, and it cannot be,
because there is no team to gate on. Two connectors serving two teams with the
same bot account would therefore both create a watcher for the same DM. Since
each connector keys its state by `(connector, room_id)` and writes its own
state file, the two records differ in their connector component and the usual
one-watcher-per-room dedup never sees the collision. The result is two agents
answering one DM — an R4 violation invisible to every existing check. Closed
by §4.5.

### 6.4 Still open

None of these gate the design.

- **Group DMs** were not exercised on either platform. Rocket.Chat reports
  them as type `d` like a 1:1, and Mattermost as type `G`; neither has a
  stable display identity, which is why they are a separate opt-in (§2.7).
- **Which Rocket.Chat versions support the reserved subscription**, and
  whether an administrator can disable it. A refusal arrives as `nosub` at
  subscribe time, so attempting it with a per-room fallback is safe
  regardless; this only decides how long the fallback must be kept.
- **Server-side cost of subscribe-all on a large workspace**, given the
  access check runs per message per subscriber.
- **Cross-team delivery on Mattermost** — the probe ran within a single team,
  so "across every team the bot belongs to" is still only a docstring claim.
  Team scope is available on the event either way (§6.2).
