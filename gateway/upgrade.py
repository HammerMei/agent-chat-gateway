"""Upgrade logic for agent-chat-gateway."""

import json
import subprocess
import sys
from pathlib import Path

from rich.console import Console

from .daemon import is_running, start_daemon, stop_daemon  # noqa: F401 (re-exported for patching)

RUNTIME_DIR = Path.home() / ".agent-chat-gateway"
META_FILE = RUNTIME_DIR / "install_meta.json"

console = Console()


def load_install_meta(meta_file: Path | None = None) -> dict:
    """Load ~/.agent-chat-gateway/install_meta.json. Returns {} if missing."""
    path = meta_file or META_FILE
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _file_hash(path: Path) -> str | None:
    """Return SHA256 hex digest of a file, or None if the file does not exist."""
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _snapshot_context_hashes(repo_path: Path) -> dict[str, str]:
    """Return {filename: sha256} for all files in repo/contexts/ before git pull.

    Called before git pull so we can later tell whether the repo file changed
    and whether the user's runtime copy still matches the old repo version.
    """
    ctx_dir = repo_path / "contexts"
    if not ctx_dir.is_dir():
        return {}
    return {
        f.name: h
        for f in ctx_dir.iterdir()
        if f.is_file() and (h := _file_hash(f)) is not None
    }


def _sync_context_files(
    repo_path: Path,
    runtime_dir: Path,
    pre_pull_hashes: dict[str, str],
) -> None:
    """Sync user-facing context files from repo to runtime dir after git pull.

    Built-in system context files (rc-gateway-context.md, scheduling-context.md)
    are bundled inside the gateway Python package (gateway/contexts/) and
    auto-injected at runtime — they are NOT synced here.

    Only user-editable example files in repo/contexts/ (e.g. rc-room-profiles.example.md)
    are synced to the runtime dir.

    Decision table for each file in repo/contexts/:
      - Repo file is brand-new (wasn't in pre-pull snapshot) → copy unconditionally.
      - User is missing the file (first upgrade from old install) → copy unconditionally.
      - Repo file unchanged after pull → skip (nothing new to deliver).
      - Repo file changed + user copy unmodified (hash matches old repo) → overwrite.
      - Repo file changed + user copy modified (hash differs from old repo) → save new
        version as <name>.default and warn the user to merge manually.
    """
    import shutil

    ctx_src = repo_path / "contexts"
    ctx_dst = runtime_dir / "contexts"

    if not ctx_src.is_dir():
        return

    ctx_dst.mkdir(parents=True, exist_ok=True)

    for src_file in sorted(ctx_src.iterdir()):
        if not src_file.is_file():
            continue

        name = src_file.name
        dst_file = ctx_dst / name
        new_hash = _file_hash(src_file)
        old_repo_hash = pre_pull_hashes.get(name)
        user_hash = _file_hash(dst_file)

        # Brand-new file added to repo, or user is missing the file entirely
        # (e.g. upgrading from an old install that predates context file copying).
        # Copy unconditionally in both cases.
        if old_repo_hash is None or user_hash is None:
            shutil.copy2(src_file, dst_file)
            console.print(f"  New context file: contexts/{name}")
            continue

        # Repo file unchanged after pull — nothing to do.
        if new_hash == old_repo_hash:
            continue

        # Repo file changed. User copy is "unmodified" when it still matches
        # the old repo version.
        user_modified = user_hash != old_repo_hash

        if not user_modified:
            shutil.copy2(src_file, dst_file)
            console.print(f"  Updated context file: contexts/{name}")
        else:
            default_file = ctx_dst / f"{name}.default"
            shutil.copy2(src_file, default_file)
            console.print(
                f"  [yellow]Warning:[/yellow] contexts/{name} has local changes.\n"
                f"    New version saved as contexts/{name}.default\n"
                f"    Review and merge manually — check release notes for details."
            )


def _find_uv() -> str:
    """Return the path to the uv executable.

    SSH sessions often have a minimal PATH that omits ~/.local/bin, so
    shutil.which() may fail even when uv is installed.  Fall back to the
    standard installation locations used by the official uv installer.
    """
    import shutil

    if path := shutil.which("uv"):
        return path
    for candidate in [
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".cargo" / "bin" / "uv",
    ]:
        if candidate.exists():
            return str(candidate)
    console.print("[red]Error:[/red] uv not found. Install from https://docs.astral.sh/uv/")
    sys.exit(1)


def do_git_upgrade(repo_path: Path) -> None:
    """git pull + uv sync + context file sync in the given repo directory."""
    # Snapshot context file hashes BEFORE git pull so we can compare afterwards
    # to determine which files changed and whether users have local modifications.
    pre_pull_hashes = _snapshot_context_hashes(repo_path)

    console.print(f"  Running [bold]git pull[/bold] in {repo_path} ...")
    result = subprocess.run(
        ["git", "-C", str(repo_path), "pull"],
        check=False,
    )
    if result.returncode != 0:
        console.print("[red]Error:[/red] git pull failed.")
        sys.exit(1)

    uv = _find_uv()
    console.print("  Running [bold]uv sync[/bold] ...")
    result = subprocess.run(
        [uv, "sync"],
        cwd=str(repo_path),
        check=False,
    )
    if result.returncode != 0:
        console.print("[red]Error:[/red] uv sync failed.")
        sys.exit(1)

    _sync_context_files(repo_path, RUNTIME_DIR, pre_pull_hashes)


def run_migrations(from_version: str) -> None:
    """No-op skeleton for future config migrations."""
    # Future: add per-version migration logic here.
    # e.g. if from_version == "0.1.0": migrate_0_1_0_to_0_2_0()
    pass


def _read_current_version(repo_path: Path) -> str:
    """Read version from pyproject.toml after upgrade."""
    try:
        toml = (repo_path / "pyproject.toml").read_text()
        for line in toml.splitlines():
            stripped = line.strip()
            if stripped.startswith("version"):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"


def _is_pip_installed() -> bool:
    """Return True if the package is installed as a regular pip/PyPI package."""
    try:
        import importlib.metadata

        importlib.metadata.version("agent-chat-gateway")
        # Confirm it's not a local editable install (editable installs have a direct_url.json
        # with "editable": true, or a .pth file pointing to a local path)
        dist = importlib.metadata.distribution("agent-chat-gateway")
        direct_url_text = None
        for f in dist.files or []:
            if f.name == "direct_url.json":
                try:
                    direct_url_text = f.read_text()
                except Exception:
                    pass
                break
        if direct_url_text:
            import json as _json

            info = _json.loads(direct_url_text)
            # Editable or local directory installs are not "pip from PyPI"
            if info.get("dir_info", {}).get("editable") or "url" not in info:
                return False
            if info["url"].startswith("file://"):
                return False
        return True
    except Exception:
        return False


def _do_pip_upgrade() -> None:
    """Upgrade via pip install --upgrade."""
    console.print("  Detected install method: [bold]pip (PyPI)[/bold]")
    console.print("  Running [bold]pip install --upgrade agent-chat-gateway[/bold] ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "agent-chat-gateway"],
        check=False,
    )
    if result.returncode != 0:
        console.print("[red]Error:[/red] pip upgrade failed.")
        sys.exit(1)
    console.print("[green]Upgrade complete![/green]")


def run_upgrade() -> None:
    """Entry point called by CLI."""
    console.print("[bold cyan]agent-chat-gateway upgrade[/bold cyan]")

    meta = load_install_meta()
    if not meta:
        # No install_meta.json — check if installed via pip before giving up
        if _is_pip_installed():
            _do_pip_upgrade()
            return
        console.print(
            "[yellow]Warning:[/yellow] install_meta.json not found.\n"
            "Cannot determine install method. Please upgrade manually:\n"
            "  cd <repo>  &&  git pull  &&  uv sync"
        )
        sys.exit(1)

    method = meta.get("method", "unknown")
    old_version = meta.get("version", "unknown")

    if method == "brew":
        console.print("  Detected install method: [bold]Homebrew[/bold]")
        console.print("  Running [bold]brew upgrade agent-chat-gateway[/bold] ...")
        result = subprocess.run(
            ["brew", "upgrade", "agent-chat-gateway"],
            check=False,
        )
        if result.returncode != 0:
            console.print("[red]Error:[/red] brew upgrade failed.")
            sys.exit(1)
        console.print("[green]Upgrade complete![/green]")
        return

    if method == "git":
        repo_path_str = meta.get("repo_path")
        if not repo_path_str:
            console.print("[red]Error:[/red] repo_path not set in install_meta.json.")
            sys.exit(1)
        repo_path = Path(repo_path_str)
        if not repo_path.exists():
            console.print(
                f"[red]Error:[/red] Repo path not found: {repo_path}\n"
                "Update install_meta.json with the correct repo path or upgrade manually."
            )
            sys.exit(1)

        # Check if daemon is running; stop it if so
        running, _pid = is_running()
        if running:
            console.print("  Daemon is running — stopping it before upgrade...")
            stop_daemon()

        do_git_upgrade(repo_path)
        run_migrations(old_version)

        # Update version in install_meta
        new_version = _read_current_version(repo_path)
        meta["version"] = new_version
        META_FILE.write_text(json.dumps(meta, indent=2))
        console.print(f"  Updated install_meta.json: version={new_version}")

        if running:
            console.print("  Restarting daemon...")
            from .cli import DEFAULT_CONFIG as _default_config
            start_daemon(_default_config)

        console.print(
            f"\n[green]Upgrade complete![/green] {old_version} → {new_version}\n"
            "Changelog: https://github.com/HammerMei/agent-chat-gateway/releases"
        )
        return

    # Unknown method
    console.print(
        f"[red]Error:[/red] Unknown install method: [bold]{method}[/bold]\n\n"
        "Please upgrade manually:\n"
        "  cd <repo>  &&  git pull  &&  uv sync"
    )
    sys.exit(1)
