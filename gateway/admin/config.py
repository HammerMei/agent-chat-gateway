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
        # Type gate, before any of the semantic checks below. Every field is
        # declared `str` / `str | None` and everything downstream relies on
        # that — but nothing enforced it, and YAML hands over plenty of
        # non-strings for what looks like a string: `123`/`1.5` (numeric
        # scalar), `true`/`yes` (the classic "Norway problem", same footgun
        # load_profiles() already documents for profile *names*),
        # `2026-08-11` (unquoted date/timestamp), a nested list/mapping from
        # a bad indent, even bytes via `!!binary`.
        #
        # The truthiness checks below cannot catch these: a non-empty int,
        # float, list, dict, set, date or bytes is truthy, so it sails
        # straight through `if not self.server_url`. That mattered most for
        # server_url, whose only consumers are
        # RocketChatREST/MattermostREST.__init__ doing
        # `server_url.rstrip("/")` — reached from admin_factory(), which
        # cli._run() guards with `except AdminConfigError` ONLY, so the
        # resulting AttributeError (TypeError for bytes, which *has*
        # .rstrip) escaped as a raw traceback, violating the CLI's
        # "ordinary config mistakes print Error: <message>" contract.
        # The remaining fields didn't traceback (they're consumed after that
        # block, where a broad `except Exception` catches everything) but
        # reached the server as nonsense and failed with a message that
        # named neither the field nor the real cause — e.g. a dict `team`
        # produced "Team '{'a': 'b'}' not found among the caller's own
        # teams", and a date `username` produced "Object of type date is not
        # JSON serializable". Rejecting all of them here, by shape, at the
        # one chokepoint both the CLI and direct library construction pass
        # through, is what makes those a single fix rather than seven.
        #
        # `None` is deliberately allowed through: it's the legitimate
        # default for team/username/password/token, and for the required
        # fields the checks below already reject it with a more specific
        # message ("server_url is required", "unknown type None"). The
        # offending value is described by type only, never echoed —
        # `password`/`token` would otherwise be printed verbatim to stderr
        # (an unquoted all-digit password parses as an int and would land in
        # this very message), and RocketChatREST.__repr__ already sets the
        # opposite convention with `password=***`.
        for field_name in ("name", "type", "server_url", "team", "username", "password", "token"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise AdminConfigError(
                    f"Profile '{self.name}': '{field_name}' must be a string, got "
                    f"{type(value).__name__} (quote it in YAML if it looks like a "
                    "number, boolean, or date)"
                )
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

    The file is opened in *binary* mode and handed to PyYAML undecoded, so
    PyYAML applies its own YAML-spec encoding detection (UTF-8/16/32, BOM
    sniffing). Two reasons, both about the CLI's "no raw tracebacks"
    contract: a file in any non-UTF-8 encoding (a latin-1 accented
    password, a UTF-16 file from a Windows editor) would otherwise raise
    ``UnicodeDecodeError`` out of the text-mode ``read()`` — a ``ValueError``,
    so neither the ``OSError`` nor the ``yaml.YAMLError`` arm below caught
    it — and in binary mode the same file instead yields a
    ``yaml.reader.ReaderError`` (a YAMLError, with the byte offset). As a
    bonus, genuinely UTF-16/32-encoded config files now load rather than
    merely failing cleanly.
    """
    config_path = _resolve_config_path(path)
    # No os.path.exists() pre-check: it bought a nicer "not found" message
    # at the cost of a whole second class of escaping errors. Path.exists()
    # only swallows ENOENT/ENOTDIR/EBADF/ELOOP (pathlib._IGNORED_ERRNOS) and
    # re-raises every other OSError — so an unsearchable parent directory
    # (EACCES) or an over-long pasted path (ENAMETOOLONG) raised *before*
    # the try block below existed to convert it. open() reports all of those
    # as OSError subclasses anyway, with FileNotFoundError preserving the
    # original message, so the check was pure liability.
    try:
        with open(config_path, "rb") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError as e:
        raise AdminConfigError(
            f"Admin config file not found: {config_path} "
            f"(pass --config, set {CONFIG_PATH_ENV_VAR}, or create ./admin-profiles.yaml)"
        ) from e
    except OSError as e:
        # e.g. --config pointing at a directory (IsADirectoryError), an
        # unreadable file (PermissionError), an unsearchable parent dir, a
        # symlink loop, or a too-long path — ordinary configuration mistakes
        # that open() surfaces before YAML parsing even starts.
        raise AdminConfigError(f"{config_path}: could not read config file: {e}") from e
    except yaml.YAMLError as e:
        raise AdminConfigError(f"{config_path}: invalid YAML: {e}") from e
    except Exception as e:
        # Backstop, because yaml.safe_load's exception surface is not
        # enumerable: PyYAML's SafeConstructor leaks several raw exceptions
        # that are NOT YAMLError subclasses for explicitly-tagged scalars —
        # ValueError ("!!int abc", "!!float abc"), AttributeError
        # ("!!timestamp nonsense"), KeyError ("!!bool maybe") — and its
        # recursive composer raises RecursionError past ~500 levels of
        # nesting. Enumerating today's list would silently rot with the next
        # PyYAML release, and every one of them is a malformed *config file*,
        # which the CLI contract says must print "Error: ..." rather than a
        # traceback. Deliberately narrow in scope: the try body is only
        # open() + safe_load(), so this cannot mask a logic bug in the
        # validation code below. KeyboardInterrupt/SystemExit are
        # BaseException and still propagate.
        raise AdminConfigError(
            f"{config_path}: could not parse config file: {type(e).__name__}: {e}"
        ) from e

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
        if not isinstance(name, str):
            # An unquoted YAML scalar that looks numeric/boolean (123, true,
            # yes/no — the classic "Norway problem") parses as that type,
            # not a string. argparse always hands the CLI's profile argument
            # back as a str, so the lookup in get_profile() would silently
            # miss even for what looks like the same name, and — worse —
            # ", ".join(sorted(profiles)) in that function's error message
            # raises TypeError on a non-str key, escaping AdminConfigError
            # handling as a traceback. Reject it here instead.
            raise AdminConfigError(
                f"{config_path}: profile name {name!r} must be a string "
                f"(quote it in YAML if it looks like a number or boolean)"
            )
        if not isinstance(fields, dict):
            raise AdminConfigError(f"{config_path}: profile '{name}' must be a mapping")
        try:
            profiles[name] = AdminProfile(name=name, **fields)
        except TypeError as e:
            # A misspelled/unsupported key, or a redundant 'name' key inside
            # the profile body (colliding with the name=name passed above),
            # makes this raise TypeError — not caught by _run()'s
            # `except AdminConfigError`, so left alone this was a raw
            # traceback for a common config typo instead of a clean error.
            raise AdminConfigError(f"{config_path}: profile '{name}' has invalid fields: {e}") from e
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
