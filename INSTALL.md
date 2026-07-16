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

**Setup:**

1. **Copy the example directory** to your deployment location:
   ```bash
   cp -r docker/docker-compose.example my-acg
   cd my-acg
   ```
   If you don't have the repo, download it:
   ```bash
   curl -fsSL https://github.com/HammerMei/agent-chat-gateway/archive/refs/heads/main.tar.gz \
     | tar -xz --strip-components=2 agent-chat-gateway-main/docker/docker-compose.example
   cd docker-compose.example
   ```

2. **Fill in `.env`** — Claude Code OAuth token (see the file for instructions on how to obtain it)

3. **Fill in `config/.env`** — chat platform credentials. Rocket.Chat:
   ```
   RC_URL=https://your-rocketchat.example.com
   RC_USERNAME=bot
   RC_PASSWORD=yourpassword
   ```
   Mattermost (no `.env` convention is generated for you — the Docker example ships
   with Rocket.Chat only; add your own vars here and reference them from
   `config/config.yaml`'s `server:` block, e.g. `MM_URL`, `MM_TEAM`, `MM_BOT_TOKEN`):
   ```
   MM_URL=https://your-mattermost.example.com
   MM_TEAM=yourteam
   MM_BOT_TOKEN=yourbotaccesstoken
   ```

4. **Edit `config/config.yaml`** — set your owners, watcher rooms, and agent config.
   A commented example is included in the file.

5. *(Optional)* Customize agent personas:
   - `agents/claude_agent/CLAUDE.md` — Claude Code persona & instructions
   - `agents/opencode_agent/AGENTS.md` — OpenCode persona & instructions

6. **Start:**
   ```bash
   docker compose up -d
   docker compose logs -f
   ```

**Volume layout:**

| Host path | Container path | Purpose |
|-----------|---------------|---------|
| `./config/` | `~/.agent-chat-gateway/config/` | `config.yaml` + `.env` (chat platform credentials) |
| `./agents/` | `~/.agent-chat-gateway/work/` | Agent working directories |
| `./contexts/` | `~/.agent-chat-gateway/contexts/` | Context files injected into agent sessions |

**Image:** `ghcr.io/hammermei/agent-chat-gateway:latest`

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

**Mattermost:** the `onboard` wizard only walks through Rocket.Chat setup today — it does not
yet generate a Mattermost `connectors:` block. To add a Mattermost connector, run the wizard
for your first (Rocket.Chat) connector as usual, then hand-edit `config.yaml` to add a second
connector with `type: mattermost` — see the [Connectors](user-guide.md#connectors) section of
the user guide for the full field reference and a worked example (including the
`server.team`/`server.token` fields Mattermost needs that Rocket.Chat doesn't).

### Watcher room formats

- `@username` — direct message room with that user (both platforms)
- `roomname` — a Rocket.Chat channel or private group
- `channelname` — a Mattermost channel within the connector's configured `server.team`

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
- Invalid config YAML — run `agent-chat-gateway config validate` to check syntax, cross-references,
  and per-connector credentials without starting the daemon (add `--lint` to also flag redundant
  defaults)
- Wrong Rocket.Chat credentials — verify RC_URL, RC_USERNAME, RC_PASSWORD in `~/.agent-chat-gateway/.env`
- Wrong Mattermost credentials — verify `server.url`/`server.team`/`server.token` (or `username`/`password`) in `config.yaml`
- Bot account not added to the watched room in Rocket.Chat, or not a member of the configured `server.team` in Mattermost

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
