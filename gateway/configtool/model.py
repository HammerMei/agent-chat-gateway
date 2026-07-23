"""EditableConfig — the pre-merge raw document the config TUI reads (and, in
later phases, writes).

This is the keystone decision recorded in docs/design/config-tool.md: the
editor operates on the raw, as-authored YAML structure, never on the
post-merge ``GatewayConfig``. That's the only place two things the TUI needs
are still visible:

  1. Provenance — whether a field on an entry is explicit, inherited from a
     ``*_defaults`` block, or an explicit ``null`` suppressing a default.
     ``GatewayConfig.from_file`` already applied the merge by the time it
     returns; the distinction is gone.
  2. Raw ``rooms:`` groupings — by the time ``GatewayConfig.from_file``
     returns, one raw watcher entry with ``rooms: [a, b, c]`` has already
     been expanded into three independent ``WatcherConfig`` objects; the
     group itself no longer exists as data.

Critically, ``EditableConfig`` loads via plain ``yaml.safe_load`` — never via
``GatewayConfig.from_file``, which expands ``$VAR``/``${VAR}`` environment
references (gateway/config.py's ``_expand_env_vars``). If the editor ever
loaded (or saved) through that path, a save would write resolved secrets —
passwords, tokens — into config.yaml in plain text. ``save()`` writes
``document`` back out with plain ``yaml.dump`` for the same reason: whatever
``$VAR`` string was loaded is the same ``$VAR`` string written back.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml

from ..config import GatewayConfig, WatcherConfig, _deep_merge, _extract_defaults_block
from ..config_validate import Finding, validate_config

# Mirrors the forbidden-key sets GatewayConfig.from_file enforces for each
# *_defaults block (gateway/config.py's calls to _extract_defaults_block) —
# kept in sync by the unit tests importing both from the same source, not by
# hand.
_DEFAULTS_FORBIDDEN_KEYS: dict[str, frozenset[str]] = {
    "connector_defaults": frozenset({"name"}),
    "agent_defaults": frozenset(),
    "watcher_defaults": frozenset({"name", "room", "rooms", "session_id"}),
}


class Provenance(Enum):
    """Where a top-level field's value on an entry actually comes from.

    Computed at whole-field granularity (not per-nested-sub-key) — matches
    how ``_deep_merge`` treats nested dicts as a single mergeable unit and
    lists/scalars as replaced wholesale. A field that is itself a dict (e.g.
    ``permissions``) is EXPLICIT or INHERITED as a whole; this does not
    (yet) distinguish "this one sub-key of permissions is overridden while
    the rest is inherited" — that finer grain isn't needed until a phase
    that edits nested fields individually exists.
    """

    EXPLICIT = "explicit"
    INHERITED = "inherited"
    EXPLICIT_SUPPRESSING = "explicit_suppressing"


@dataclass
class EditableConfig:
    """The raw config.yaml document, kept in its pre-merge, as-authored form.

    ``document`` is the literal top-level mapping from ``yaml.safe_load`` —
    keys like ``connectors``, ``agents``, ``watchers``, ``connector_defaults``,
    ``tool_presets``, etc. Phase 1 only reads it; later phases mutate it and
    call ``save()``.
    """

    document: dict
    path: Path
    # Code review item 8: defaults_block() (and, transitively, merged_entry()/
    # field_provenance()) re-ran _extract_defaults_block from scratch on
    # every single call — repaint_from_memory() alone calls it once per
    # connector/agent/watcher row PLUS once per defaults-table row, all for
    # the same 3 blocks. Cached here, keyed by kind, invalidated by
    # load()/reload()/mark_dirty() (the only ways `document` changes). Not
    # part of equality/repr — it's a memoization detail, not observable state.
    _defaults_cache: dict[str, dict] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # Code review item 7: whether `document` has unsaved changes since the
    # last load()/reload()/save(). There is deliberately no per-field mutation
    # API here (e.g. `set_entry_field()`) — Phase 2's edit screens mutate
    # `document` (and the raw dicts reachable from it) directly, in whatever
    # shape each form needs, and then call `mark_dirty()`. That is the ONE
    # sanctioned seam: it is where cache invalidation and dirty-tracking both
    # live, so every future mutation path — a single field, a whole entry
    # replace, a list append/remove — stays correct by calling it, without
    # this class needing to anticipate each mutation shape up front.
    dirty: bool = field(default=False, init=False, compare=False)

    @classmethod
    def load(cls, path: str | Path) -> "EditableConfig":
        """Load config.yaml as a plain dict — no env-var expansion, no merge.

        Raises FileNotFoundError / ValueError the same way GatewayConfig.from_file
        does for a missing file or a non-mapping top level, so callers can
        handle both the same way they already handle from_file's errors.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path) as f:
            document = yaml.safe_load(f) or {}
        if not isinstance(document, dict):
            raise ValueError(
                f"Config file '{path}' must contain a YAML mapping at the "
                f"top level, got {type(document).__name__}."
            )
        return cls(document=document, path=path)

    def reload(self) -> None:
        """Re-read `document` from disk in place (e.g. after the $EDITOR
        round-trip, or a manual 'refresh' action)."""
        fresh = EditableConfig.load(self.path)
        self.document = fresh.document
        self._defaults_cache.clear()
        self.dirty = False

    def mark_dirty(self) -> None:
        """Call this after mutating `document` (or any raw dict reachable
        from it) directly. See the `dirty`/`_defaults_cache` field comments
        above — this is the one required step after any in-place edit."""
        self._defaults_cache.clear()
        self.dirty = True

    def save(self) -> None:
        """Validate-before-write via a same-directory temp file
        (docs/design/config-tool.md decision 5):

        1. Serialize `document` to `<path>.tmp`, BESIDE the real file (never
           /tmp — `working_directory`/`context_inject_files` in the config
           resolve relative to the real file's directory, and a temp file
           elsewhere would validate paths that don't mean the same thing
           once moved).
        2. Run the real `validate_config()` against that temp file. If it
           doesn't validate, delete the temp file and raise ValueError with
           the errors — the real config on disk is never touched.
        3. Only on success: copy the real file to a timestamped backup under
           `<config_dir>/.config-backups/` (`config.yaml.bak.<unix-ts>`,
           matching gateway/onboard.py's own backup step, which writes to
           the same directory) and atomically replace it with the temp file
           (`os.replace` — atomic on POSIX, so a reader/the daemon never
           observes a partially-written config.yaml).

        `config.yaml` and every backup snapshot can hold a plaintext secret
        — secrets are stored directly in config.yaml (docs/design/
        config-tool.md decision 6 revisited; a not-yet-migrated `.env`
        reference gets folded in as a literal value by
        `gateway/config_migrate.py`, never the other direction). Both
        `config.yaml` itself and each backup file are chmod'd 0600 here
        (matching the treatment `.env` used to get) — `config.yaml`
        specifically needs this on EVERY save because writing `tmp_path` via
        plain `open(..., "w")` takes the process umask, not whatever
        permissions the real file had before; without this line, a manual
        `chmod 600 config.yaml` would silently revert to the umask default
        the very next time this method runs. The backup directory itself is
        chmod'd 0700 for the same reason a bare `.gitignore` entry on
        `config.yaml.bak.*` isn't enough by itself: it also keeps the whole
        deployment's history of past secrets out of a directory readers
        might casually `ls`/glob/back up without expecting a pile of
        `config.yaml.bak.*` files that will keep growing on every future
        save either.

        Raises FileNotFoundError if `path` doesn't exist yet (nothing to
        back up) — Phase 2/3 forms only ever call save() on an already-loaded
        EditableConfig, so this should not happen in practice; surfaced
        rather than silently skipping the backup step.
        """
        if not self.path.exists():
            raise FileNotFoundError(
                f"Cannot save: {self.path} no longer exists (nothing to back up)."
            )

        tmp_path = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(tmp_path, "w") as f:
                yaml.dump(self.document, f, sort_keys=False, allow_unicode=True)

            result = validate_config(str(tmp_path))
            if not result.ok:
                raise ValueError(
                    "Refusing to save — the result would no longer be a "
                    "valid config:\n" + "\n".join(result.errors)
                )

            backup_dir = self.path.parent / ".config-backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_dir.chmod(0o700)
            backup_path = backup_dir / f"{self.path.name}.bak.{int(time.time())}"
            shutil.copy2(self.path, backup_path)
            backup_path.chmod(0o600)
            os.replace(tmp_path, self.path)
            self.path.chmod(0o600)
        finally:
            # Only ever removes OUR OWN temp file, not the real config: if
            # os.replace() above succeeded, tmp_path no longer exists at this
            # path (it WAS renamed to self.path) and unlink is a no-op.
            tmp_path.unlink(missing_ok=True)

        self.dirty = False

    # ── Raw entry accessors (pre-merge, as-authored) ─────────────────────────

    @property
    def connectors_raw(self) -> list[dict]:
        return [c for c in (self.document.get("connectors") or []) if isinstance(c, dict)]

    @property
    def agents_raw(self) -> dict[str, dict]:
        agents = self.document.get("agents") or {}
        return {k: v for k, v in agents.items() if isinstance(v, dict)}

    @property
    def watchers_raw(self) -> list[dict]:
        return [w for w in (self.document.get("watchers") or []) if isinstance(w, dict)]

    @property
    def tool_presets_raw(self) -> dict[str, list]:
        return dict(self.document.get("tool_presets") or {})

    def defaults_block(self, kind: str) -> dict:
        """Return the named `*_defaults:` block (description stripped, same
        as the real loader) — 'connector_defaults' | 'agent_defaults' |
        'watcher_defaults'. Cached per kind (see `_defaults_cache`); the
        underlying `document` never changes except via load()/reload(),
        both of which invalidate the cache."""
        if kind not in self._defaults_cache:
            forbidden = _DEFAULTS_FORBIDDEN_KEYS[kind]
            self._defaults_cache[kind] = _extract_defaults_block(self.document, kind, forbidden)
        return self._defaults_cache[kind]

    # ── Provenance / effective value (reuses the real merge, never reimplemented) ──

    def merged_entry(self, kind: str, entry_raw: dict) -> dict:
        """`entry_raw` deep-merged against its matching *_defaults block —
        the exact value GatewayConfig.from_file would compute for this one
        entry before its own further per-entry processing (path resolution,
        tool-preset resolution, etc). Uses the real _deep_merge.

        Deliberately NOT cached (unlike defaults_block() above): _deep_merge
        already deep-copies on every call, so it's cheap; caching it would
        need a key derived from entry_raw's identity, which stops being safe
        the moment a later phase starts mutating entries in place for
        editing. defaults_block() is document-scoped and only invalidated by
        load()/reload(), which is a much simpler invariant to keep correct."""
        return _deep_merge(self.defaults_block(kind), entry_raw)

    def field_provenance(self, kind: str, entry_raw: dict, field: str) -> Provenance:
        """Where `entry_raw[field]` (or its absence) actually comes from.

        kind: 'connector_defaults' | 'agent_defaults' | 'watcher_defaults'
        (selects which defaults block this entry inherits from).
        """
        if field in entry_raw:
            if entry_raw[field] is None and field in self.defaults_block(kind):
                return Provenance.EXPLICIT_SUPPRESSING
            return Provenance.EXPLICIT
        return Provenance.INHERITED

    # ── Read-only validated view ─────────────────────────────────────────────

    def validated_view(self) -> GatewayConfig:
        """The fully-parsed, env-expanded, merged GatewayConfig — for display
        and cross-reference only (e.g. "this watcher's agent is X"). Loads via
        the real gateway loader; never mutate anything based on what this
        returns — only `document` is ever written back to disk."""
        return GatewayConfig.from_file(self.path)

    def expanded_watchers(self) -> list["ExpandedWatcher"]:
        """Pair each of validated_view()'s expanded WatcherConfig objects with
        the raw `watchers:` entry (and sibling-room count) it came from.

        Per docs/design/config-tool.md, the Watchers table shows EXPANDED
        rows (what `agent-chat-gateway list/pause/resume/reset` operate on),
        but a watcher's detail screen still needs to know whether it's part
        of a shared `rooms:` group. This is computed by replaying the same
        room/rooms-counting order gateway/config.py's loader uses (each raw
        entry contributes exactly `len(rooms or [room])` consecutive expanded
        watchers, in order) — without reimplementing name generation or
        defaults-merging, both of which stay owned by the real loader via
        validated_view().

        Only call this when validated_view() would not raise (i.e. after
        confirming the config loads) — a raw watcher entry's `room`/`rooms`
        keys are never touched by any `*_defaults` merge (both are forbidden
        there), so reading them directly off the raw entry is safe once the
        document is known to be schema-valid.

        `validated_view()` re-reads config.yaml from disk on every call;
        `self.watchers_raw` reads the in-memory `document` (only refreshed by
        `load()`/`reload()`). If the file changes on disk without an
        intervening `reload()` on this instance, the two can disagree on how
        many watchers exist — raises ValueError in that case (never a raw
        IndexError) so callers' existing `except (ValueError, FileNotFoundError)`
        guards catch it like any other "can't compute this right now" case.
        """
        expanded = self.validated_view().watchers
        result: list[ExpandedWatcher] = []
        idx = 0
        for entry in self.watchers_raw:
            rooms = entry.get("rooms")
            if rooms is None:
                rooms = [entry.get("room")]
            count = len(rooms)
            for _ in range(count):
                if idx >= len(expanded):
                    raise ValueError(
                        "expanded_watchers(): the in-memory document and the "
                        "freshly-loaded config disagree on watcher count — "
                        "config.yaml likely changed on disk since this was "
                        "last loaded; call reload() first."
                    )
                result.append(
                    ExpandedWatcher(watcher=expanded[idx], raw_entry=entry, group_size=count)
                )
                idx += 1
        if idx != len(expanded):
            raise ValueError(
                "expanded_watchers(): the in-memory document and the "
                "freshly-loaded config disagree on watcher count — "
                "config.yaml likely changed on disk since this was last "
                "loaded; call reload() first."
            )
        return result


@dataclass
class ExpandedWatcher:
    """One expanded WatcherConfig plus the raw `watchers:` entry it came
    from. `group_size > 1` means this watcher shares a `rooms:` list with
    `group_size - 1` sibling watchers."""

    watcher: WatcherConfig
    raw_entry: dict
    group_size: int

    @property
    def sibling_rooms(self) -> list[str]:
        if self.group_size <= 1:
            return []
        rooms = list(self.raw_entry.get("rooms") or [])
        return [r for r in rooms if r != self.watcher.room]


class StatusIndex:
    """Groups a ValidationResult's structured `findings` by (entity_kind,
    entity_name) for cheap per-row lookup in the TUI's tables.

    Known gap (documented, not silently papered over): `_lint_config`'s
    per-watcher findings are attributed to the RAW entry's own `name` (or a
    `watchers[i]` placeholder when unnamed) — for a multi-room `rooms:`
    group with no explicit name, that placeholder matches none of the
    group's expanded watcher names, so those specific lint findings won't
    surface on any single row. They are never dropped from
    `result.lint_findings`/`result.findings` overall — only from this
    per-row index — so a global lint count elsewhere always accounts for
    them.
    """

    _SEVERITY_RANK = {"error": 3, "warning": 2, "lint": 1}

    def __init__(self, findings: list[Finding]):
        self._by_entity: dict[tuple[str, str], list[Finding]] = {}
        for f in findings:
            if f.entity_name is not None:
                self._by_entity.setdefault((f.entity_kind, f.entity_name), []).append(f)

    def findings_for(self, kind: str, name: str) -> list[Finding]:
        return self._by_entity.get((kind, name), [])

    def status_for(self, kind: str, name: str) -> str:
        """'error' | 'warning' | 'lint' | 'ok', highest severity present."""
        items = self.findings_for(kind, name)
        if not items:
            return "ok"
        return max(items, key=lambda f: self._SEVERITY_RANK.get(f.severity, 0)).severity
