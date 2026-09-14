# Finding triage — log

Step 6 of the `finding-triage` skill: the five quantities and the verdict for every
finding decided, the one least sure of as a range, and every control result,
convergence and escalation. Entries are marked **settled** once their outcome is
observed — the drop that bit or did not, the fix that saved what it promised or did not.
Settled entries are the only legitimate source of a control finding for a blind second
rater; until one exists, every round runs without a control and says so.

Constants and anchors: `finding-triage.md` in this directory.

---

## 2026-09-14 — PR #166 round 1

First live run of the skill on this repository.

**Chain detector:** 1 finding over 1 round, no chain.
**Control finding:** none — the log was empty. Agreement in this round is *not*
evidence of independence; recorded as **uncorroborated**.

### F1 — Coalesce concurrent failed recovery attempts

Codex P2, `gateway/service.py` `_recover_agent`. Two `resume`/`reset` verbs racing for
the same still-broken agent: the first attempt fails and leaves the agent unavailable,
so the second waiter on `_recover_lock` runs `start_some` again — one more ~40 s
attempt per additional concurrent verb, and a drain waits the sum.

| | rater 1 (author) | rater 2 (blind) |
|---|---|---|
| `cheap` | no — an in-flight future per agent: new state + tests | no — an attempt-token counter, 6–8 lines, but a new attribute, an invariant and a test |
| `cannot-occur` | no — two shells needed; the CLI glob batch is sequential | no — traced `control.py` one task per connection; batch sequential; nothing else sends lifecycle verbs |
| `silent` | no — each attempt is logged and the verb is refused with the remedy | no |
| `hours_per_hit` | 0.05–0.2 | 0.04 (0.02–0.1) |
| `hits_per_year` | **0.1–1** — correlated path: a second shell or an agent-issued verb | **0.7 (0.06–7.5)** — correlated path: the impatient Ctrl-C re-run |
| `discount` | 1.0 — no clause covers operator verbs | 1.0 |
| `fix_hours` | 1.5–2 | 1.5 |
| `tax_hours_per_year` | 0.2 | 0.25 |
| `net_annual_saving` | ≤ 0 | −0.22 (−0.25 … +0.5) |
| verdict | **DROP** | **DROP** |

Verdicts agree and the `hits_per_year` ranges overlap → settled per the Step 4 table,
**uncorroborated** (no control).

Notes:
- Rater 2 named a correlated path rater 1 had not considered (an operator who thinks
  the verb hung, Ctrl-C's the client and re-runs; the daemon-side attempt runs on and
  the re-run queues on the lock).
- Layer (rater 2): if ever revisited, a per-agent start backoff belongs in
  `AgentRuntimeManager` or the adapter's restart circuit-breaker, shared by a reload's
  start pass and a verb's — not in the verb callback, whose lock exists to prevent two
  brokers, not to rate-limit failing starts.
- Least-sure quantity: `hits_per_year`, both raters' widest range.
- Thread: replied with the scored reason; resolved by id.
- Adoption this round: 0 of 1 acted on.

**Status: open.** Becomes settled when a queued recovery wait is observed in the field,
or after a year without one (which would confirm the rate's upper bound was generous).
