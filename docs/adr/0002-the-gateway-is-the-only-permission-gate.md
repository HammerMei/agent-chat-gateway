# 2. The gateway is the only permission gate, for every agent

- **Status:** Accepted
- **Date:** 2026-09-14

## Context

`permissions.enabled: false` used to mean *no permission broker*. `_build_agent_backend`
passed no `GatewayBrokerConfig`, both backends' `create_gateway_broker()` returned `None`,
and what happened to a tool call then depended on the backend:

- **Claude** ran without `--dangerously-skip-permissions` and without the hook settings
  file, so Claude Code's own headless permission engine decided — the same way for an
  owner and a guest, and without ever consulting `owner_allowed_tools`,
  `guest_allowed_tools` or the built-in `coop …` rules.
- **OpenCode** still ran the role-enforcement plugin and the injected `bash["*"] = "ask"`,
  so every write and every bash command raised a `permission.asked` that nobody answered.
  The call sat until the agent timeout (#165).

`docs/requirements.md` §6.2 promised a third thing — "all tools are auto-approved for
owners" — which neither backend did.

Verifying the fix live on opencode 1.18.13 surfaced a second, older gap. The
role-enforcement plugin was believed to gate owner `write`/`edit`/`multiedit` by setting
`output.status = "ask"` in its `tool.execute.before` hook. opencode's trigger discards
that output and runs the tool: a `write` completed with **no** `permission.asked` at all.
Only bash was ever gated, and only because the adapter injects `bash["*"] = "ask"` into
opencode's own permission ruleset. Since the sidecar runs as `COOP_ROLE=owner` for every
chatter, an owner *or a guest* could write files through an OpenCode agent, with
`permissions.enabled` in either state.

The first fix for #165 kept "no broker" and taught the OpenCode sidecar a deny mode:
`COOP_APPROVAL_MODE=deny`, a `"*": "deny"` bash catch-all, the plugin throwing for
write tools, and a list of opencode's own ask-by-default rules (`external_directory`,
`doom_loop`) forced to `deny`. Review found the list already incomplete on the version it
was verified against — opencode 1.18.13 also asks before reading `*.env` / `*.env.*` — so
a `.env` read still hung. Any approach that enumerates "what asks by default" per backend
has to be re-verified on every backend release, and misses fail exactly the way #165 did.

## Decision

**Every agent has a permission broker. `permissions.enabled` decides only what happens to
an owner's tool call that no allow-list matches: `true` asks a human in chat, `false`
denies it at once. Allow-lists apply in both modes. The gateway is the sole permission
gate on every backend.**

Concretely:

- `_build_agent_backend` always builds a `GatewayBrokerConfig`; `human_approval` carries
  `permissions.enabled`. `None` from `create_gateway_broker()` now means only "constructed
  outside the gateway" (the AgentSession/TUI path, which attaches a callable broker).
- In both brokers the deny check sits after the `skip_owner_approval` and owner allow-list
  checks and before the ask. Claude gets a `block` with a reason the model reads; OpenCode
  gets `reject` through the reply API, which carries no reason text.
- `owner_allowed_tools` and `guest_allowed_tools` — including the built-in owner rules for
  `coop send`, `coop schedule`, `coop fetch-history`, `coop instructions` and `date` — are
  the whole policy. They reach the broker through `effective_owner_allowed_tools()` /
  `effective_guest_allowed_tools()` in every mode.
- `permissions.enabled: false` together with `skip_owner_approval: true` is a config load
  error: with nobody asked, there is no approval to skip. The dataclass still admits the
  pair; if it is built anyway, skip is checked first and approves.
- The OpenCode sidecar is told only `COOP_ROLE=owner`. The adapter injects `"ask"` into
  opencode's permission ruleset for everything the gateway gates — `bash["*"]`, `edit`
  (the key `write`, `edit` and `multiedit` ask under), `webfetch` and `websearch` — unless
  the user set a value. opencode's own ask-by-default rules (`external_directory`,
  `doom_loop`, `.env` reads) are left alone. Every ask reaches the broker and is answered.
  The plugin's owner path is kept but documented as inert on 1.18.13; the ruleset is the
  gate.

## Rationale

- **Simplicity.** One flag with one meaning. The reader of `permissions:` no longer has to
  know which backend the agent is to predict what a denied call looks like.
- **One story for every agent type.** The allow-list is the policy; the broker enforces
  it; the backend's own permission engine is out of the loop (Claude always runs with
  `--dangerously-skip-permissions` and the hook, as it already did whenever the broker
  ran). Adding a backend means wiring its ask signal to the broker, not designing a deny
  mode for it.
- **No enumeration of "asks by default".** With a broker that always answers, an ask the
  gateway did not anticipate is denied or put to a human — it does not hang. The `.env`
  miss is the concrete case this closes, and the class of case it closes for good.
- **`enabled: false` stays useful.** The agent can still run everything the operator
  allow-listed, and the gateway's own commands keep working — which is what makes the
  broker worth keeping rather than removing.

## Consequences

- **Claude with `enabled: false` is stricter than before.** The operator's own
  `~/.claude/settings.json` permission rules and Claude Code's read-only classifier no
  longer apply under the gateway; every tool passes the `.*` hook, so `Read`, `Glob`,
  `Grep` and `WebFetch` are denied unless allow-listed. Documentation examples for
  approval-off agents carry an `owner_allowed_tools` preset for that reason. Whether the
  built-in owner rules should grow read-only entries — or `_CLAUDE_META_TOOLS` should
  cover `TodoWrite`, `Task`, `Skill`, `AskUserQuestion` — is a follow-up, not part of this
  decision.
- **OpenCode writes and web access are gated for the first time.** `edit` (write, edit,
  multiedit), `webfetch` and `websearch` now ask, so they reach the broker: allow-listed
  → run; otherwise a human or a denial. Before, they ran for anyone, in either mode.
- **The two backends are still not symmetric, and say so.** OpenCode raises an ask for
  bash, `edit`, `webfetch`, `websearch` and its own `external_directory` / `doom_loop` /
  `.env` rules; its `read`, `glob`, `grep` and `list` inside the working directory never
  reach the broker. Claude sends every tool. The same `enabled: false` config therefore
  leaves an OpenCode agent its in-directory read tools and a Claude agent only the
  allow-list. Gating those read tools on OpenCode too is a later decision.
- **OpenCode with `enabled: false` regains `coop send`.** The interim deny mode ran no
  bash at all; bash now asks, the broker matches the built-in rule, and the command runs.
- **A denial on OpenCode ends the turn with no reply.** opencode's reply API is
  `once`/`reject` with no reason field, and after a `reject` the model produces no text
  (live: the adapter returned `(empty response)` for every denied call). The chat sees an
  empty answer. Surfacing opencode's own rejection text in the response is a follow-up.
  On Claude the model sees why, and what the operator can change.
- **Allow-list rules on OpenCode use opencode's permission names.** The broker matches
  the `permission` field of the ask: `edit` (for write, edit and multiedit), `bash`,
  `webfetch`, `websearch`, `external_directory`, … A Claude-style `tool: Write` rule never
  matches on OpenCode. Documentation says so wherever allow-lists are explained.
- **`permissions.enabled: true` OpenCode agents will see new 🔐 prompts** for edits and web
  access outside the allow-list — they were silently allowed before. Operators add
  `tool: edit` / `webfetch` / `websearch` rules or `skip_owner_approval` to keep them
  unprompted.
- **The AgentSession/TUI path without a permission handler widens an existing gap.** There
  the sidecar asks and nothing answers; bash already did that before this change, and
  `edit`/`webfetch`/`websearch` now join it. Pre-existing class, not addressed here;
  `--no-permissions` on an OpenCode agent is not a working configuration for those tools.
- **A broker that fails to start marks any agent unavailable**, not only one with
  approval on. The fail-closed rule in `docs/architecture.md` is unconditional.
- **`docs/requirements.md` §6.2 promises denial, not approval**, for the not-allow-listed
  remainder when approval is off, and must not reacquire "auto-approved for owners".
- **Open, for a later phase:** whether a common agent protocol (ACP or similar) would let
  the broker attach at one seam instead of a Claude HTTP hook plus an OpenCode
  plugin-and-SSE pair. Not decided here; the decision above does not depend on it.
