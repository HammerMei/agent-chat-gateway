## Connector Prompt Prefix Formats

Each connector injects a trusted server-controlled header into the agent prompt
via `format_prompt_prefix()`.  New connectors that use RBAC **must** document
their format here.

| Connector     | Prefix format                                          |
|---------------|--------------------------------------------------------|
| RocketChat    | `[Rocket.Chat #<room> \| from: <user> \| role: <role>]` |
| Voice Gateway | `[Voice \| from: <user> \| role: <role>]`              |
| Mattermost    | `[Mattermost #<channel> \| from: <user> \| role: <role>]` |

These headers are server-injected and must never be sourced from user-controlled
content (per OpenClaw security principle).

Note: RocketChat's and Mattermost's actual `format_prompt_prefix()` implementations
append optional `day:`/`ts:`/`to:` fields beyond the base form shown above (e.g.
`... | role: owner | day: Tue | ts: 2026-07-07T21:53:45-07:00 | to: me]`) — see
`gateway/contexts/rc-gateway-context.md` / `mm-gateway-context.md` for the full
documented format each connector's agent-facing context actually describes.

## Multi-Agent Deployment Model

The canonical multi-agent setup in ACG is: **each agent has its own RC account.**
When discussing multi-agent communication, collaboration, or message routing,
assume this model unless stated otherwise.

Two watchers sharing the same RC username in the same room is a degenerate case —
agents cannot see each other's responses (own-message filter). This setup has no
practical use for collaboration; it only makes sense for framework-level testing.

## Code Review with Codex

Every PR goes through Codex review and is not merged until it comes back clean.
The bar is a 👍 — see *Checking for a review* below, because a clean review does
not look like a review.

Request it explicitly with a `@codex review` comment rather than relying on the
automatic trigger, and then confirm a response actually arrived. A draft PR does
not get reviewed automatically at all.

### Assess severity yourself; do not adopt Codex's labels

Codex attaches `P1`/`P2` badges. **Re-rank every finding by consequence before
acting on it**, and say where your ranking differs and why. Its labels have been
wrong in both directions — inflating a finding, and flattening a whole batch to
one level when the batch contained both a silent misconfiguration and a
practically-unreachable Unicode edge case.

Useful separators when ranking:

- **Does it fail silently or loudly?** Silent is worse. A value that quietly
  binds a watcher to the wrong account outranks one that raises at load.
- **Is it reachable now?** A defect in code nothing calls yet is real but not
  urgent; the same defect on a released path is.
- **Is the defect in behaviour, or in a documented rationale?** A wrong
  explanation in a design doc or comment can outrank a small bug, because it
  teaches the next reader the wrong model.

### Not every comment has to be fixed

Fixing all of them is not the goal; deciding about all of them is. Weigh
severity against likelihood and the cost of the fix, then either fix it or reply
saying explicitly why not. Legitimate reasons not to fix:

- The finding is real but the case cannot occur in this system (justify it — e.g.
  both platforms build room names as ASCII slugs).
- The fix costs more than the defect (disproportionate complexity, or it would
  couple two things that should stay separate).
- It is already tracked as deliberately deferred work, with the reason recorded.

Reply on the thread either way, so the decision is visible rather than implied by
silence. Resolve threads you have addressed or consciously declined; leave open
the ones genuinely waiting on future work.

### Look for the pattern, not just the findings

If consecutive review rounds keep surfacing the same *kind* of defect, the
response is a systematic sweep plus a test that enumerates the surface — not
another individual patch. Three rounds on one PR each found another field read
without a type check; the fix that ended it was a test walking every declared
field, so the next unvalidated one fails locally instead of in a fourth review.

### Checking for a review

**An empty `pulls/<n>/reviews` does not mean Codex has not looked.** When it finds
nothing it leaves a 👍 reaction plus a one-line comment and posts no review and no
inline threads. Check all four places, and check *which commit* was reviewed:

```bash
gh api repos/<owner>/<repo>/issues/<n>/reactions   # 👀 = running, 👍 = found nothing
gh api repos/<owner>/<repo>/issues/<n>/comments    # "Didn't find any major issues" + Reviewed commit
gh api repos/<owner>/<repo>/pulls/<n>/reviews      # present only when it has findings
gh api repos/<owner>/<repo>/pulls/<n>/comments     # inline threads
git rev-parse <branch>                             # must match the reviewed commit
```

The commit check matters: a PR can carry two reviews that both predate the push
which fixed their findings. Commits written *in response to* a review are the ones
most in need of another pass, so "a review exists" is the weakest possible
evidence there.
