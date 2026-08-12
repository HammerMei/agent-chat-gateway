# Dynamic watcher design

Status: **design, not implemented.** Two assumptions require verification
against live servers before implementation begins (§6).

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
unique at config load, so `<connector>-<room>` is unique by construction.

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

**Mattermost** already receives every channel the bot belongs to on one
socket; the connector currently discards events for unknown channels. It
needs room resolution *by id*, which it lacks.

**RocketChat** can receive messages for rooms it has not per-room-subscribed
to, using `stream-room-messages` with the reserved room id
`__my_messages__`. The server emits every message to such subscribers,
filters per message via a room-access check, and returns a `roomParticipant`
flag distinguishing membership from mere readability — a signal Mattermost
does not have and must approximate. Reaching that requires real work in the
RC connector (§5.2) and is gated on §6.

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
2. **Membership gate**: `roomParticipant` on RocketChat; on Mattermost,
   team scope plus the allow-list precheck, treating
   delivery-implies-membership as an assumption to verify (§6) rather than
   a guarantee.
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

- **Mattermost**: add `resolve_room_by_id`; reconcile the two room-identity
  paths (name-based resolution stores the configured string and cannot
  report DM type; id-based resolution returns the server's name) so one
  canonical form reaches the prompt prefix, session title and history;
  replace the unknown-channel drop with the routing hook; hoist the
  system-message and own-message checks above the state lookup; add channel
  reaping.
- **RocketChat**: make `rid` authoritative rather than the DDP stream event
  name, which today collapses every room onto one queue key; split the
  single room-id key space into a subscription registry keyed by stream and
  a dispatch registry keyed by room; thread the emit's access object through
  instead of discarding it, since it carries `roomParticipant`; make
  subscribe local-only and never send a stream-level unsubscribe from a
  per-room call; add a system-message filter, which the REST path has and
  the live path does not; attempt subscribe-all with a clean per-room
  fallback when the server refuses it.
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
- `session_idle_days` / `session_expire_days` move from the agent to the
  rule, so two rules sharing an agent can differ.
- `session_id` rejected on a rule.
- Literal-only `rooms.include` enforced for connectors declaring no
  unsolicited inbound.
- Pattern compilation, never-firing and shadowed-rule detection at load.
- Warning when a rule's `session_expire_days` exceeds the session retention
  its agent's backend declares (§3).

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

## 6. Unverified assumptions

Both gate a connector's viability. Neither can be settled from this
repository.

**A1 — RocketChat's subscribe-all frame shape.** Capture one raw `changed`
frame from a live `__my_messages__` subscription. It answers: whether the
stream event name carries the literal reserved id or the originating room id
(which decides whether the existing fan-out collapses every room onto one
key); whether the message payload is still the first argument, so current
parsing survives; and **exactly what the access object carries — if it
includes room type and name alongside `roomParticipant`, no new REST call is
needed on the routing path; if only `roomParticipant`, a by-id room resolver
is mandatory.** Also worth capturing in the same session: whether own
messages and system messages are included, and whether `roomParticipant`
appears on every emit.

**A2 — Mattermost's socket scope.** Whether the socket delivers `posted`
for exactly the channels the bot is a *member* of, across teams. This is
currently a docstring claim with no fixture or test behind it, and the whole
Mattermost approach rests on it. If delivery tracks readability rather than
membership, Mattermost needs a membership gate for which it has no signal.

Secondary, not gating: whether the decoded event's channel name, type and
team id are reliably populated (if so, routing can skip a REST call, which
matters for keeping work off the handler path — treat as an optimisation
with the REST resolver as fallback); which RocketChat versions support the
reserved subscription and whether an admin can disable it (a refusal is
detectable at subscribe time, so a per-room fallback is safe either way);
and which single RocketChat REST endpoint returns both room type and a
per-user display name by id.
