#!/usr/bin/env bash
# agent-chat-gateway installer
# Usage:  bash install.sh
# Or:     curl -fsSL https://raw.githubusercontent.com/HammerMei/agent-chat-gateway/main/install.sh | bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()    { printf '\033[0;36m[ACG]\033[0m %s\n' "$*"; }
success() { printf '\033[0;32m[ACG]\033[0m %s\n' "$*"; }
warn()    { printf '\033[0;33m[ACG]\033[0m %s\n' "$*" >&2; }
error()   { printf '\033[0;31m[ACG] Error:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# OS / architecture detection
# ---------------------------------------------------------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
  Darwin) OS_NAME="macos" ;;
  Linux)  OS_NAME="linux" ;;
  *)      error "Unsupported OS: $OS. This installer supports macOS and Linux." ;;
esac

info "Detected OS: $OS_NAME ($ARCH)"

# ---------------------------------------------------------------------------
# Python 3.12+ check
# ---------------------------------------------------------------------------
if ! command -v python3 &>/dev/null; then
  error "python3 not found. Install Python 3.12+ from https://python.org"
fi

PYTHON_VERSION="$(python3 --version 2>&1 | awk '{print $2}')"
PYTHON_MAJOR="$(echo "$PYTHON_VERSION" | cut -d. -f1)"
PYTHON_MINOR="$(echo "$PYTHON_VERSION" | cut -d. -f2)"

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 12 ]; }; then
  error "Python 3.12+ required (found $PYTHON_VERSION). Install from https://python.org"
fi

info "Python $PYTHON_VERSION — OK"

# ---------------------------------------------------------------------------
# uv check / install
# ---------------------------------------------------------------------------
if ! command -v uv &>/dev/null; then
  warn "uv not found. Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # Add to PATH for this session
  export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
  if ! command -v uv &>/dev/null; then
    error "uv installation failed. Install manually: https://docs.astral.sh/uv/getting-started/installation/"
  fi
  success "uv installed."
else
  info "uv found: $(uv --version)"
fi

# ---------------------------------------------------------------------------
# Determine repo directory
# Detect curl|bash mode: BASH_SOURCE[0] is empty or is /dev/stdin or "bash"
# ---------------------------------------------------------------------------
SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
CURL_PIPE=false

if [ -z "$SCRIPT_SOURCE" ] || [ "$SCRIPT_SOURCE" = "/dev/stdin" ] || [ "$SCRIPT_SOURCE" = "bash" ] || [ "$SCRIPT_SOURCE" = "-bash" ]; then
  CURL_PIPE=true
fi

if [ "$CURL_PIPE" = true ]; then
  REPO_DIR="$HOME/agent-chat-gateway"
  info "Running via curl|bash — will clone to $REPO_DIR"
  if [ -d "$REPO_DIR/.git" ]; then
    info "Repo already exists at $REPO_DIR — pulling latest..."
    git -C "$REPO_DIR" pull
  else
    git clone https://github.com/HammerMei/agent-chat-gateway.git "$REPO_DIR"
  fi
else
  # Running as a local script — use the script's own directory
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO_DIR="$SCRIPT_DIR"
  info "Running locally — using repo at $REPO_DIR"
fi

# ---------------------------------------------------------------------------
# uv sync
# ---------------------------------------------------------------------------
info "Installing Python dependencies (uv sync)..."
uv sync --project "$REPO_DIR"

# ---------------------------------------------------------------------------
# Symlink into ~/.local/bin
# ---------------------------------------------------------------------------
VENV_BIN="$REPO_DIR/.venv/bin/agent-chat-gateway"
if [ ! -f "$VENV_BIN" ]; then
  error "Expected binary not found: $VENV_BIN"
fi

mkdir -p "$HOME/.local/bin"
ln -sf "$VENV_BIN" "$HOME/.local/bin/agent-chat-gateway"
success "Symlink created: ~/.local/bin/agent-chat-gateway → $VENV_BIN"

# ---------------------------------------------------------------------------
# PATH setup — add ~/.local/bin if missing
# ---------------------------------------------------------------------------
case ":$PATH:" in
  *":$HOME/.local/bin:"*)
    info "~/.local/bin is already in PATH."
    ;;
  *)
    warn "~/.local/bin is not in PATH. Adding to shell rc files..."
    PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
    for RC in "$HOME/.bashrc" "$HOME/.zshrc"; do
      if [ -f "$RC" ]; then
        # Only add if not already present
        if ! grep -qF '.local/bin' "$RC" 2>/dev/null; then
          printf '\n# Added by agent-chat-gateway installer\n%s\n' "$PATH_LINE" >> "$RC"
          info "  Added to $RC"
        fi
      fi
    done
    export PATH="$HOME/.local/bin:$PATH"
    ;;
esac

# ---------------------------------------------------------------------------
# Run onboard wizard
# ---------------------------------------------------------------------------
info "Launching setup wizard..."
"$VENV_BIN" onboard --repo-path "$REPO_DIR"

# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------
printf '\n'
success "Installation complete!"
printf '\n'
printf '  Start the gateway:   agent-chat-gateway start\n'
printf '  Check status:        agent-chat-gateway status\n'
printf '  View logs:           tail -f ~/.agent-chat-gateway/gateway.log\n'
printf '\n'
printf '  If agent-chat-gateway is not found, restart your shell or run:\n'
printf '    export PATH="$HOME/.local/bin:$PATH"\n'
printf '\n'
