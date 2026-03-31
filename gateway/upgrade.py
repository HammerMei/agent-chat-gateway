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


def do_git_upgrade(repo_path: Path) -> None:
    """git pull + uv sync in the given repo directory."""
    console.print(f"  Running [bold]git pull[/bold] in {repo_path} ...")
    result = subprocess.run(
        ["git", "-C", str(repo_path), "pull"],
        check=False,
    )
    if result.returncode != 0:
        console.print("[red]Error:[/red] git pull failed.")
        sys.exit(1)

    console.print("  Running [bold]uv sync[/bold] ...")
    result = subprocess.run(
        ["uv", "sync"],
        cwd=str(repo_path),
        check=False,
    )
    if result.returncode != 0:
        console.print("[red]Error:[/red] uv sync failed.")
        sys.exit(1)


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
