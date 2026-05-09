## Multi-Agent Deployment Model

The canonical multi-agent setup in ACG is: **each agent has its own RC account.**
When discussing multi-agent communication, collaboration, or message routing,
assume this model unless stated otherwise.

Two watchers sharing the same RC username in the same room is a degenerate case —
agents cannot see each other's responses (own-message filter). This setup has no
practical use for collaboration; it only makes sense for framework-level testing.
