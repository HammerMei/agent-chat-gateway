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
