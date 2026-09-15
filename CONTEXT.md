# AgentCoop

The product that puts AI agents into a team's chat rooms as teammates — of humans and of each other — and manages their sessions, permissions and lifecycle there. Formerly agent-chat-gateway (ACG); the name changed when the project outgrew "a gateway from chat to an agent".

## Language

### Naming

**AgentCoop**:
The product, as written in documentation, release notes and the README. One word, two capitals.
_Avoid_: agent-chat-gateway, ACG, Agent Chat Gateway, AgentCoOp, Agent Coop, Agentcoop

**Coop**:
The short, spoken form of AgentCoop. Also the CLI command (`coop`) and the environment-variable prefix (`COOP_`).
_Avoid_: acg, coop-ai

**gateway**:
The AgentCoop daemon process — the thing `coop start` starts, that holds watcher records and talks to connectors. A component of AgentCoop, not a synonym for it. Also the Python package name.
_Avoid_: the server, the service (in prose), "the coop" (for the process)

**Coop Session Identity**:
The header block injected at the top of every agent session that tells the agent it is running under Coop and names its watcher and room. The name is deliberate: it lets an agent tell this environment apart from any other session or context it may hold.
_Avoid_: ACG Session Identity, Session Identity

### Permissions

**permission broker**:
The gateway component that decides every tool call an agent makes — allow, deny, or ask a human. One per agent, always running; the backend's own permission engine is bypassed so the broker is the only gate (ADR-0002).
_Avoid_: the hook, the plugin (those are its transports), "permissions" as a synonym for the broker

**allow-list**:
`owner_allowed_tools` / `guest_allowed_tools`: the tool rules a role may run without being asked. The gateway's built-in rules for its own `coop …` commands are part of the owner allow-list. This is the whole policy; everything else is the remainder.
_Avoid_: whitelist, auto-approve list

**human approval**:
What `permissions.enabled` switches: whether an owner's tool call outside the allow-list is put to a person in chat (`true`) or denied at once (`false`). Not a switch for the permission broker or the allow-lists.
_Avoid_: "permissions on/off", "permission system disabled"
