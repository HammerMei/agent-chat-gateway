"""Profile-based config for the standalone RC/MM admin CLI.

Deliberately a plain YAML file, separate from ACG's own ``config.yaml`` /
``ConnectorConfig`` (see gateway/admin/__init__.py for why). Profile-based
rather than a single server/credential pair because one ACG deployment can
have agents talking to multiple RC and/or MM servers — the CLI operates on
one named profile per invocation.

File shape::

    profiles:
      mm-lab:
        type: mattermost
        server_url: https://mm.labpig.com
        team: labteam          # required for mattermost, ignored for rocketchat
        token: xxx             # preferred for mattermost (PAT) — see MattermostAdmin
        # username: admin      # fallback auth mode if no token
        # password: xxx
      rc-lab:
        type: rocketchat
        server_url: https://rc.labpig.com
        username: admin
        password: xxx
"""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("admin-profiles.yaml")
CONFIG_PATH_ENV_VAR = "ACG_ADMIN_CONFIG"

SUPPORTED_TYPES = ("rocketchat", "mattermost")


class AdminConfigError(Exception):
    """Raised for missing/malformed config files or unknown/invalid profiles."""


@dataclass
class AdminProfile:
    """One named RC or MM server + admin credentials.

    Auth precedence (mirrors MattermostREST's own dual-mode support):
    ``token`` wins if set; otherwise ``username``/``password`` are used.
    Rocket.Chat has no equivalent token-only constructor path today (see
    RocketChatAdmin), so ``token`` is effectively mattermost-only for now.
    """

    name: str
    type: str
    server_url: str
    team: str | None = None
    username: str | None = None
    password: str | None = None
    token: str | None = None

    def __post_init__(self) -> None:
        if self.type not in SUPPORTED_TYPES:
            raise AdminConfigError(
                f"Profile '{self.name}': unknown type {self.type!r}, "
                f"must be one of {SUPPORTED_TYPES}"
            )
        if not self.server_url:
            raise AdminConfigError(f"Profile '{self.name}': server_url is required")
        if not self.token and not (self.username and self.password):
            raise AdminConfigError(
                f"Profile '{self.name}': must set either 'token', or both "
                "'username' and 'password'"
            )
        if self.type == "mattermost" and not self.team:
            raise AdminConfigError(
                f"Profile '{self.name}': 'team' is required for type=mattermost "
                "(Mattermost channels are scoped to a team; see MattermostAdmin.connect)"
            )


def _resolve_config_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path)
    env_path = os.environ.get(CONFIG_PATH_ENV_VAR)
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG_PATH


def load_profiles(path: str | Path | None = None) -> dict[str, AdminProfile]:
    """Load all profiles from a YAML file.

    Resolution order for the file path: explicit ``path`` argument, then
    the ``ACG_ADMIN_CONFIG`` env var, then ``./admin-profiles.yaml``.
    """
    config_path = _resolve_config_path(path)
    if not config_path.exists():
        raise AdminConfigError(
            f"Admin config file not found: {config_path} "
            f"(pass --config, set {CONFIG_PATH_ENV_VAR}, or create ./admin-profiles.yaml)"
        )
    with open(config_path) as f:
        try:
            raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise AdminConfigError(f"{config_path}: invalid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise AdminConfigError(
            f"{config_path}: expected a mapping at the top level, got {type(raw).__name__}"
        )

    raw_profiles = raw.get("profiles")
    if not raw_profiles:
        raise AdminConfigError(f"{config_path}: no 'profiles' section found")
    if not isinstance(raw_profiles, dict):
        raise AdminConfigError(
            f"{config_path}: 'profiles' must be a mapping, got {type(raw_profiles).__name__}"
        )

    profiles: dict[str, AdminProfile] = {}
    for name, fields in raw_profiles.items():
        if not isinstance(fields, dict):
            raise AdminConfigError(f"{config_path}: profile '{name}' must be a mapping")
        profiles[name] = AdminProfile(name=name, **fields)
    return profiles


def get_profile(profiles: dict[str, AdminProfile], name: str) -> AdminProfile:
    """Look up a profile by name, raising AdminConfigError with the
    available names if it's not found (rather than a bare KeyError)."""
    try:
        return profiles[name]
    except KeyError:
        available = ", ".join(sorted(profiles)) or "(none defined)"
        raise AdminConfigError(
            f"Unknown profile '{name}'. Available profiles: {available}"
        ) from None
