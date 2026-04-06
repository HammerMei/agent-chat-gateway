# Installing agent-chat-gateway

## Prerequisites

- **Python 3.12+** — https://python.org
- **git** — https://git-scm.com (required by the installer and `upgrade` command)
- **uv** — https://docs.astral.sh/uv/getting-started/installation/
- **Agent backend** (at least one):
  - **Claude Code** — https://claude.ai/download
  - **opencode** — https://opencode.ai

---

## Quick Install

### Option A: One-line shell installer (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/HammerMei/agent-chat-gateway/main/install.sh | bash
```

This will:
1. Clone the repo to `~/agent-chat-gateway`
2. Install dependencies with `uv sync`
3. Create a symlink at `~/.local/bin/agent-chat-gateway`
4. Launch the interactive setup wizard

### Option B: AI-guided install with Claude Code

Ask Claude Code to install agent-chat-gateway:

```
claude "Please install agent-chat-gateway by following the instructions at https://raw.githubusercontent.com/HammerMei/agent-chat-gateway/main/docs/install-agent.md"
```

Claude will read the install guide and walk you through the setup interactively.

### Option C: AI-guided install with opencode

```
opencode "Please install agent-chat-gateway by following the instructions at https://raw.githubusercontent.com/HammerMei/agent-chat-gateway/main/docs/install-agent.md"
```

### Option D: Manual install

See the [Manual Steps](#manual-steps) section below.

### Option E: Docker (no local dependencies)

Run ACG as a container — no Python, Node.js, or Claude Code required on the host.

**Prerequisites:** Docker with Compose plugin installed.

```bash
# 1. Copy the example compose setup
curl -fsSL https://raw.githubusercontent.com/HammerMei/agent-chat-gateway/main/docker/docker-compose.example/docker-compose.yml -o docker-compose.yml

# 2. Claude Code auth — set your OAuth token in .env
echo "CLAUDE_CODE_OAUTH_TOKEN=your_token_here" > .env

# 3. Rocket.Chat credentials — create config/.env
mkdir -p config
cat > config/.env <<EOF
RC_URL=https://your-rocketchat.example.com
RC_USERNAME=bot
RC_PASSWORD=yourpassword
EOF

# 4. Gateway config — create config/config.yaml
#    See docker/docker-compose.example/ for a full annotated example
#    At minimum, set: owners, connectors[].watcher_rooms, agents

# 5. Start
docker compose up -d

# Logs
docker compose logs -f
```

**Volume layout:**

| Host path | Container path | Purpose |
|-----------|---------------|---------|
| `./config/` | `~/.agent-chat-gateway/config/` | `config.yaml` + `.env` (RC credentials) |
| `./agents/` | `~/.agent-chat-gateway/work/` | Agent working directories |
| `./contexts/` | `~/.agent-chat-gateway/contexts/` | Context files injected into agent sessions |

**Image:** `ghcr.io/hammermei/agent-chat-gateway:latest`

See [`docker/docker-compose.example/`](docker/docker-compose.example/) for the full annotated example including optional agent persona files (`CLAUDE.md`, `AGENTS.md`).

---

## Manual Steps

### 1. Clone the repository

```bash
mkdir -p ~/.agent-chat-gateway
git clone https://github.com/HammerMei/agent-chat-gateway.git ~/.agent-chat-gateway/repo
```

### 2. Install dependencies

```bash
uv sync --project ~/.agent-chat-gateway/repo
```

### 3. Create the symlink

```bash
mkdir -p ~/.local/bin
ln -sf ~/.agent-chat-gateway/repo/.venv/bin/agent-chat-gateway ~/.local/bin/agent-chat-gateway
```

Add `~/.local/bin` to your PATH if needed (add to `~/.zshrc` or `~/.bashrc`):
```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 4. Run the setup wizard

```bash
agent-chat-gateway onboard --repo-path ~/.agent-chat-gateway/repo
```

---

## Configuration

The `onboard` wizard creates three files in `~/.agent-chat-gateway/`:

| File | Purpose |
|------|---------|
| `config.yaml` | Connector, agent, and watcher definitions |
| `.env` | Secrets: RC_URL, RC_USERNAME, RC_PASSWORD |
| `install_meta.json` | Install method and version (used by `upgrade`) |

**Never put passwords directly in `config.yaml`.** Use the `$RC_PASSWORD` env var reference — it is expanded automatically from `.env` at startup.

### Watcher room formats

- `@username` — direct message room with that user
- `roomname` — a Rocket.Chat channel or private group

---

## Upgrade

```bash
agent-chat-gateway upgrade
```

This stops the daemon, runs `git pull` + `uv sync`, and restarts the daemon automatically.

---

## Uninstall

```bash
# Stop the daemon
agent-chat-gateway stop

# Remove the symlink
rm -f ~/.local/bin/agent-chat-gateway

# Remove all data — repo, config, logs (this deletes everything!)
rm -rf ~/.agent-chat-gateway
```

---

## Troubleshooting

### `agent-chat-gateway: command not found`

`~/.local/bin` is not in your PATH. Add it:
```bash
export PATH="$HOME/.local/bin:$PATH"
```
Then add the same line to your `~/.zshrc` or `~/.bashrc` so it persists.

### Gateway won't start

Check the log file:
```bash
tail -50 ~/.agent-chat-gateway/gateway.log
```

Common causes:
- Invalid config YAML — run `python3 -c "import yaml; yaml.safe_load(open('$HOME/.agent-chat-gateway/config.yaml'))"` to validate
- Wrong Rocket.Chat credentials — verify RC_URL, RC_USERNAME, RC_PASSWORD in `~/.agent-chat-gateway/.env`
- Bot account not added to the watched room in Rocket.Chat

### Permission denied errors

The `.env` file should be readable only by you:
```bash
chmod 600 ~/.agent-chat-gateway/.env
```

### Running onboard again

Re-running `onboard` when a config already exists offers three options:
1. Update existing (keeps old values, you can change them)
2. Start fresh (backs up old files with a timestamp)
3. Cancel

```bash
agent-chat-gateway onboard
```
