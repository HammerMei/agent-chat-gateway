# Finding triage — AgentCoop's constants

The *method* is not here. It lives in the `finding-triage` skill, which is the same
for every project. This file holds only what is specific to AgentCoop: the discount
ladder, the anchors to score against, and which reviewer to read.

## Reviewer

`chatgpt-codex-connector` — Codex, requested explicitly with a `@codex review`
comment. See `CLAUDE.md` § *Code Review with Codex* for how to confirm a review
actually arrived.

## E — the expectation discount

E is how much the operator has already priced in. It is capped at 0.5, i.e. it can
divide the harm by at most ~3; it is a nudge at the boundary, never a verdict.

| operation | E | divides harm by | grounded in |
|---|---|---|---|
| message handling, `status`, `list`, `config show` | 0 | 1 | the promised path |
| `config reload`, `config migrate-env`, TUI save | 0.25 | ~1.8 | §14.2 — a write carries its own risk; operators run it off-hours |
| `coop upgrade`, data migration, a forked or unsupported setup | 0.5 | ~3.2 | §14.1 — upgrades are best effort, back up first |

Section numbers are `docs/requirements.md` § *Operational Commitments*. **Cite the
clause when scoring E.** If no clause covers the case, E is 0 — absence of a promise
is not a discount.

## P — the user base

AgentCoop is self-hosted and administered by a small number of operators
(`requirements.md` §14.5). P is occurrences per year **across all of them**, so the
same defect is worth far less here than in software with thousands of installs. The
bar for fixing is correspondingly higher, and that is documented rather than lazy.

For a security finding, P is set by **reachability**, not by likelihood. See
`docs/adr/0001-the-agent-is-the-trust-boundary.md` for what counts: a message reaching
the wrong session of the *same* agent is a correctness defect; one reaching a
*different* agent is a security defect.

## Anchors — score by comparison, not in the abstract

Three cases, each reaching its verdict by a different route. Ask "is this worse or
lighter than MEDIUM?" rather than "what is R?".

### LARGE → FIX — driven by a high R and by silence

*A state file that cannot be read, with the daemon booting anyway* (the behaviour
before #143). A removed connector's state file is never opened again, so the daemon
starts successfully while silently abandoning every session in it.

`R 1` (~10h: notice it, work out which rooms lost their sessions, reset each)
`P 0.5` (~3/yr, on connector rename or removal with state present — and silent, so it
accumulates) · `E 0` (boot path) · `C 1` (~10h: version marker plus refuse-to-boot)
→ ~30 h/yr against 10 h to fix → **pays back in ~4 months.**

### MEDIUM → FILE — driven by a low P, and deliberately a frightening one

*Two agents answering the same DM* (PR #149). A rule-only reload skips the identity
barrier, so two connectors sharing a bot account both acquire the DM claim.

`R 0` (~1h: notice, fix the rules, reload) · `P 0` (~1/yr: needs a shared account *and*
a rule-only reload that changes DM claims) · `E 0.25` · `C 0.5` (~3h, a 28-line method)
→ ~0.6 h/yr against 3 h to fix → **pays back in ~6 years.**

It sounds severe — two agents inside one private conversation — and still does not pay
back. Frequency decides, not the category. Per ADR 0001 this is not a confidentiality
event, because both connectors resolve to the same agent; it would be one if they
resolved to different agents.

### SMALL → DROP — killed by a gate, never scored

*A canonical digest collision between the integer key `1` and the string key `"int:1"`*
(PR #149). No configuration that exists does this, so it fails gate B and is dropped
with a reason. It was the last link of a chain that ran for six review rounds.

## What the anchors teach

- **LARGE** — silence lets P accumulate; with a high R it genuinely pays back.
- **MEDIUM** — frightening is not the same as worth fixing.
- **SMALL** — most corner cases should die at a gate, before any arithmetic.
