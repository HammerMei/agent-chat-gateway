# agent-chat-gateway

[![CI](https://github.com/HammerMei/agent-chat-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/HammerMei/agent-chat-gateway/actions/workflows/ci.yml)
[![Docker](https://ghcr-badge.egpl.dev/hammermei/agent-chat-gateway/latest_tag?trim=major&label=docker&color=blue)](https://github.com/HammerMei/agent-chat-gateway/pkgs/container/agent-chat-gateway)
![Python](https://img.shields.io/badge/python-%3E%3D3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**Turn your AI agent into a team-shared chatbot — in minutes.**

Already running Claude Code or OpenCode? `agent-chat-gateway` connects it to your team's chat system (Rocket.Chat, and more) so everyone can talk to it directly from chat — no terminal required, no code changes to your agent.

Inspired by [OpenClaw](https://github.com/openclaw/openclaw)'s vision of making AI agents accessible from any messaging app — built for the team layer.

> **How it compares to Claude Code Channels:** Claude Code's native [Channels](https://code.claude.com/docs/en/channels) feature connects a single session to Telegram, Discord, or iMessage — great for personal use. `agent-chat-gateway` is built for teams: multiple agents, multiple chat systems, per-user roles, human oversight for sensitive operations, and shared sessions across your whole workspace.

---

## Features

- 💬 **[Works with Rocket.Chat](docs/supported-features.md#chat-platform-connectors)** — and extensible to Slack, Discord, or any other chat system you use
- 🤖 **[Bring your own agent](docs/supported-features.md#agent-backends)** — Claude Code and OpenCode work out of the box; plug in any other agent too
- 👥 **[User-aware in chat](docs/user-guide.md#user-aware-responses)** — the agent knows who sent each message and can personalize tone, language, and style per person using room profiles
- 🔒 **[Owner & Guest roles](docs/permission-reference.md#roles)** — control who can do what, with different permissions per role
- 🛡️ **[Human oversight for sensitive actions](docs/permission-reference.md#approval-workflow)** — the agent pauses and asks for your approval before executing risky operations like file writes or shell commands
- 🔗 **[Continue your session remotely](docs/user-guide.md#use-case-3--continue-an-existing-agent-session-remotely)** — pin a chat room to an existing agent session and pick up right where you left off, from anywhere
- 📎 **[File attachments](docs/user-guide.md#attachment-handling)** — send files in chat and the agent can read and work with them
- 🧠 **[Context injection](docs/user-guide.md#context-files)** — pre-load domain knowledge, system prompts, or project context into the agent at startup
- ⚡ **[Multiple chat systems at once](docs/user-guide.md#multi-connector-setup)** — connect to several chat platforms simultaneously
- ⏰ **[Built-in task scheduler](docs/scheduling.md)** — let the agent schedule recurring or one-shot tasks directly from chat ("remind me in 5 minutes", "run daily standup at 09:00") without any infrastructure setup
- 🤝 **[Agent-to-agent collaboration](docs/agent-chain.md)** — let multiple AI agents collaborate in a shared room; built-in loop protection keeps conversations bounded and human-observable

---

## What's Supported

| | Supported today | Can be extended |
|--|--|--|
| **Chat platforms** | Rocket.Chat | Slack, Discord, and others |
| **Agent backends** | Claude Code, OpenCode | Any agent with a CLI interface |

---

## Quick Start

### Option A — AI-guided install (recommended)

The easiest way to install is to ask your AI agent to do it for you — it handles dependencies, configuration, and any troubleshooting automatically.

In Claude Code or OpenCode, run this prompt:

```
Please install agent-chat-gateway by following the instructions at https://raw.githubusercontent.com/HammerMei/agent-chat-gateway/main/docs/install-agent.md
```

### Option B — Docker (no local dependencies)

If you'd rather skip installing Python, Node.js, or Claude Code locally, run ACG in a container:

```bash
# 1. Copy the example directory to your deployment location
cp -r docker/docker-compose.example my-acg
cd my-acg

# 2. Fill in your credentials and settings
#    .env          — Claude Code OAuth token (see file for instructions)
#    config/.env   — Rocket.Chat URL, username, password
#    config/config.yaml — owners, rooms, agents

# 3. Start
docker compose up -d

# Logs
docker compose logs -f
```

See [`docker/docker-compose.example/`](docker/docker-compose.example/) for the full annotated setup — all files are pre-structured and ready to fill in.

> Prefer a native install? See [INSTALL.md](INSTALL.md) for step-by-step instructions.

---

## Running the Gateway

```bash
# Start the gateway
agent-chat-gateway start

# Check status
agent-chat-gateway status

# Stop the gateway
agent-chat-gateway stop
```

See [docs/user-guide.md](docs/user-guide.md) for the full CLI reference, configuration options, and usage examples.

---

## Documentation

| Document | Description |
|---|---|
| [INSTALL.md](INSTALL.md) | Manual installation guide |
| [docs/user-guide.md](docs/user-guide.md) | Configuration reference, CLI usage, and operational guide |
| [docs/architecture.md](docs/architecture.md) | System architecture and module breakdown |
| [docs/permission-reference.md](docs/permission-reference.md) | Roles, permissions, and human oversight deep dive |
| [docs/supported-features.md](docs/supported-features.md) | Supported features, known limitations, and roadmap |
| [docs/requirements.md](docs/requirements.md) | Functional specification and behavioral requirements |
| [docs/scheduling.md](docs/scheduling.md) | Built-in task scheduler — recurring and one-shot jobs from chat |
| [docs/agent-chain.md](docs/agent-chain.md) | Agent-to-agent collaboration — enabling multiple heterogeneous AI agents to coordinate via chat |
