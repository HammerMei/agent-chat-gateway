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
3. Create symlinks at `~/.local/bin/agent-chat-gateway` and `~/.local/bin/acg-provision`
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

### 3. Create the symlinks

```bash
mkdir -p ~/.local/bin
repo=~/.agent-chat-gateway/repo

# Same policy as install.sh, one command at a time:
#   * a real file or directory is MOVED ASIDE, because you made it and it cannot
#     be reconstructed;
#   * a symlink is just removed — the file it pointed at is untouched, so there
#     is nothing to preserve, and backing links up would litter a .bak on every
#     re-run.
# Why not `ln -sf`, and why not `rm -f` alone: `ln -sf` onto a symlink that points
# at a DIRECTORY follows it and creates the link *inside* that directory, and
# `rm -f` refuses a directory outright and then `ln -s` does the same thing.
for cmd in agent-chat-gateway acg-provision; do
  link=~/.local/bin/"$cmd"
  if [ ! -L "$link" ] && [ -e "$link" ]; then
    # `|| continue` is the important part: if the backup cannot be made, skip this
    # command entirely. Without it the `rm -f` below still runs and deletes the very
    # file the move was meant to preserve.
    mv "$link" "$link.$(date +%Y%m%d%H%M%S).bak" || {
      echo "skipped $cmd: could not back up $link"; continue
    }
  fi
  rm -f "$link" && ln -s "$repo/.venv/bin/$cmd" "$link"
done
```

`acg-provision` creates Rocket.Chat / Mattermost users and channels; the loop links
it alongside the gateway.

> **Both commands are part of the installation.** `acg-provision` is not an
> optional extra: `install.sh` links it alongside the gateway, and
> `agent-chat-gateway upgrade` keeps both links current. Leaving it out here does
> not stick — the next upgrade creates it — so link both, or link neither and use
> `<repo>/.venv/bin/<command>` directly.
>
> **Note:** a real file or directory that was at either path is now
> `<name>.<timestamp>.bak` beside it — nothing is deleted, but nothing cleans
> those up for you either. `ls -l ~/.local/bin/*.bak` to see them.
>
> Re-running the block is safe: the second run finds a symlink it created itself,
> removes it, and links again, so it never backs up its own work.

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

The `onboard` wizard creates two files in `~/.agent-chat-gateway/`:

| File | Purpose |
|------|---------|
| `config.yaml` | Connector, agent, and watcher definitions — including credentials, stored directly as plain values |
| `install_meta.json` | Install method and version (used by `upgrade`) |

`config.yaml` is chmod'd `0600` automatically (by the wizard, by `agent-chat-gateway start`, and by the config TUI on every save), so putting credentials directly in it is safe as long as you don't commit your filled-in copy to version control. `$VAR`/`${VAR}` references are not expanded — if you're upgrading from an older setup that used a `.env` file, the next `agent-chat-gateway start` (or opening `agent-chat-gateway config`) migrates it into `config.yaml` automatically, one-time.

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

This stops the daemon, runs `git pull` + `uv sync`, runs the pulled release's
post-upgrade steps, and restarts the daemon automatically.

> **One-time note for installs that predate `acg-provision`:** the post-upgrade
> step that puts new commands on your PATH is itself delivered by an upgrade, so
> the first upgrade that lands it cannot run it. If `acg-provision` is not found
> after upgrading, link it once:
>
> ```bash
> # Locate the managed virtualenv. Two things this deliberately avoids:
> #   * assuming ~/.agent-chat-gateway/repo — running `./install.sh` from a local
> #     checkout uses that checkout as the repo and records it in install_meta.json;
> #   * needing a system `python3` — install.sh may have installed Python with
> #     `uv python install`, which provides `python3.12` and not `python3`.
> # Both cases describe installs that predate acg-provision, i.e. this note's readers.
> bin=$(dirname "$(readlink ~/.local/bin/agent-chat-gateway 2>/dev/null)" 2>/dev/null)
> if [ ! -x "$bin/acg-provision" ]; then
>   # The entrypoint is not a managed symlink (a wrapper of your own, say), so use
>   # the path the installer recorded.
>   repo=$(sed -n 's/.*"repo_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' ~/.agent-chat-gateway/install_meta.json)
>   bin=$repo/.venv/bin
> fi
> echo "$bin"          # sanity-check this before continuing
>
> link=~/.local/bin/acg-provision
> backed_up=yes
> # Same policy as install.sh: move a real file/directory aside, but just remove a
> # symlink (nothing to preserve, and this way re-running never backs up its own
> # link into the same timestamped name).
> if [ ! -L "$link" ] && [ -e "$link" ]; then
>   mv "$link" "$link.$(date +%Y%m%d%H%M%S).bak" || backed_up=no
> fi
>
> # Only replace what was successfully preserved — otherwise the `rm -f` would
> # delete the very file the move was meant to save.
> if [ "$backed_up" = yes ]; then
>   rm -f "$link"
>   ln -s "$bin/acg-provision" "$link"
> else
>   echo "could not back up $link — left it alone, nothing changed"
> fi
> ```
>
> Or run it without linking anything at all — same `$bin` as above:
> `"$bin/python" -m gateway.admin --help`.
> Later upgrades handle new commands on their own.

---

## Uninstall

```bash
# Stop the daemon
agent-chat-gateway stop

# Remove the symlinks this install created. The -L test leaves a hand-written
# wrapper of your own at either path alone — uninstalling should remove what was
# installed, not something you wrote.
if [ -L ~/.local/bin/agent-chat-gateway ]; then rm -f ~/.local/bin/agent-chat-gateway; fi
if [ -L ~/.local/bin/acg-provision ];     then rm -f ~/.local/bin/acg-provision;     fi

# If the installer ever moved something of yours aside, it is still here. Check
# before deleting — this is the only copy, and nothing else cleans it up.
ls -l ~/.local/bin/*.bak 2>/dev/null

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
